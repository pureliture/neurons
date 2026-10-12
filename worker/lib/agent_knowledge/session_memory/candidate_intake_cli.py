"""Proposal-only candidate intake for the LBrain work-memory product.

This module connects the existing PostgreSQL session-memory artifact surface to
the existing envelope miner and the existing Brain Steward proposal writer. It
creates no new approval path:

- candidates are written through ``BrainStewardService.candidate_create`` and
  therefore land in the review queue only (``needs_review`` / ``candidate``);
- accepted/current authority is never written here;
- the legacy RetiredIndexBridge mining path and the auto-accept autopilot loop
  are not used.

Cost safety is the default. The runner performs no model call and no write
unless a source of session chunks is actually available and the caller has
opted in with ``--enable``. ``--dry-run`` stops before any model call.

Repeat state is a durable checkpoint file that retains every successfully
processed source revision per project; it is not a rolling cache. It never mutates the existing
source projection cursor and never marks a failed candidate as a completed
projection.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .batch_miner import DEFAULT_MAX_CALL_CHARS

DEFAULT_BATCH_CANDIDATES = 5
DEFAULT_MAX_CANDIDATES_PER_CHUNK = 5
DEFAULT_MAX_CHUNKS_PER_RUN = 100
DEFAULT_REVISION_LIMIT = 64
DEFAULT_CHECKPOINT_VERSION = 1
_PROPOSAL_ONLY_NOTE = (
    "candidate intake writes review-queue proposals only; accepted authority is unchanged"
)


class CandidateIntakeConfigError(ValueError):
    """Raised when the intake is asked to run without the required inputs."""


@dataclass(frozen=True)
class CandidateIntakeSettings:
    """Bounded, explicit settings for one intake run."""

    project: str
    batch_candidates: int = DEFAULT_BATCH_CANDIDATES
    max_chunks_per_run: int = DEFAULT_MAX_CHUNKS_PER_RUN
    max_candidates_per_chunk: int = DEFAULT_MAX_CANDIDATES_PER_CHUNK
    max_call_chars: int = DEFAULT_MAX_CALL_CHARS
    enable_proposals: bool = False
    checkpoint_path: str = ""
    refresh_watermark: str = ""

    def __post_init__(self) -> None:
        if not str(self.project or "").strip():
            raise CandidateIntakeConfigError("project is required")
        if not 1 <= int(self.batch_candidates) <= 100:
            raise CandidateIntakeConfigError("batch_candidates must be within 1..100")
        if not 1 <= int(self.max_candidates_per_chunk) <= 100:
            raise CandidateIntakeConfigError("max_candidates_per_chunk must be within 1..100")
        if not 1 <= int(self.max_chunks_per_run) <= 1000:
            raise CandidateIntakeConfigError("max_chunks_per_run must be within 1..1000")
        if int(self.max_call_chars) < 1:
            raise CandidateIntakeConfigError("max_call_chars must be positive")


@dataclass(frozen=True)
class CandidateIntakeReport:
    """Deterministic, redaction-safe report of one intake run."""

    project: str
    status: str
    dry_run: bool
    enabled: bool
    examined_chunks: int = 0
    proposed_count: int = 0
    duplicate_count: int = 0
    skipped_count: int = 0
    error_code: str = ""
    remaining_chunks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "llm_brain_candidate_intake.v1",
            "project": self.project,
            "status": self.status,
            "dry_run": self.dry_run,
            "proposals_enabled": self.enabled,
            "examined_chunks": self.examined_chunks,
            "proposed_count": self.proposed_count,
            "duplicate_count": self.duplicate_count,
            "skipped_count": self.skipped_count,
            "error_code": self.error_code,
            "remaining_chunks": self.remaining_chunks,
            "proposal_only": True,
            "authoritative_memory_changed": False,
            "note": _PROPOSAL_ONLY_NOTE,
        }


def _hash_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def load_checkpoint(path: str) -> dict[str, Any]:
    """Read the durable checkpoint; an absent file is an empty checkpoint."""
    if not path:
        return {"version": DEFAULT_CHECKPOINT_VERSION, "projects": {}}
    checkpoint_file = Path(path).expanduser().resolve()
    if not checkpoint_file.exists():
        return {"version": DEFAULT_CHECKPOINT_VERSION, "projects": {}}
    try:
        raw = json.loads(checkpoint_file.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
        if not isinstance(raw, Mapping) or not isinstance(raw.get("projects"), Mapping):
            raise ValueError("invalid checkpoint structure")
        version = raw.get("version")
        if type(version) is not int or version != DEFAULT_CHECKPOINT_VERSION:
            raise ValueError("unsupported checkpoint version")
        for state in raw["projects"].values():
            if not isinstance(state, Mapping):
                raise ValueError("invalid checkpoint project")
            _revision_history(state)
            _source_history(state)
    except (OSError, TypeError, ValueError) as exc:
        # Resetting unreadable history can silently re-run paid model work.
        raise CandidateIntakeConfigError("checkpoint unreadable or invalid") from exc
    return {"version": version, "projects": copy.deepcopy(dict(raw["projects"]))}


@contextmanager
def _checkpoint_lock(path: str, suffix: str):
    """Hold a stable sidecar inode, never the atomically replaced data inode."""
    import fcntl

    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + suffix)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield target
    finally:
        # Close releases the advisory lock even on failures. Do not unlink the
        # sidecar: another process may already be waiting on this same inode.
        os.close(fd)


def _revision_history(state: Mapping[str, Any]) -> list[str]:
    raw = state.get("revisions", [])
    if not isinstance(raw, list) or any(not isinstance(v, str) or not v for v in raw):
        raise ValueError("invalid checkpoint revisions")
    legacy = state.get("source_hash")
    if legacy is not None and (not isinstance(legacy, str) or not legacy):
        raise ValueError("invalid checkpoint legacy revision")
    return list(dict.fromkeys(([legacy] if legacy else []) + raw))


def _source_history(state: Mapping[str, Any]) -> dict[str, str]:
    from .memory_card import validate_content_hash
    raw = state.get("source_revisions", {})
    if not isinstance(raw, Mapping):
        raise ValueError("invalid checkpoint source history")
    result = {}
    for key, revision in raw.items():
        validate_content_hash(key, "source_key")
        validate_content_hash(revision)
        result[key] = revision
    return result


def _merge_checkpoints(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(current))
    projects = merged.setdefault("projects", {})
    for project, update in (incoming.get("projects") or {}).items():
        if not isinstance(update, Mapping):
            raise CandidateIntakeConfigError("invalid checkpoint project")
        old = projects.get(project, {})
        combined = {**old, **copy.deepcopy(dict(update))}
        combined["revisions"] = list(dict.fromkeys(_revision_history(old) + _revision_history(update)))
        sources = _source_history(old)
        for key, revision in _source_history(update).items():
            if key in sources and sources[key] != revision:
                raise CandidateIntakeConfigError("conflicting checkpoint source identity")
            sources[key] = revision
        if sources or "source_revisions" in old or "source_revisions" in update:
            combined["source_revisions"] = sources
        combined.pop("source_hash", None)
        combined.pop("revision_limit", None)
        projects[str(project)] = combined
    merged["version"] = DEFAULT_CHECKPOINT_VERSION
    return merged


def save_checkpoint(path: str, checkpoint: Mapping[str, Any]) -> None:
    """Merge concurrent history, fsync, then replace using a private temp file."""
    if not path:
        return
    with _checkpoint_lock(path, ".write.lock") as target:
        merged = _merge_checkpoints(load_checkpoint(str(target)), checkpoint)
        payload = json.dumps(merged, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def checkpoint_source_hash(project: str) -> str:
    """Stable, non-reversible key for one processed source revision."""
    return "sha256:" + _hash_text(project)


def already_processed(checkpoint: Mapping[str, Any], *, project: str, source_hash: str, source_key: str | None = None) -> bool:
    """True only when this exact project+revision was recorded as processed."""
    state = (checkpoint.get("projects") or {}).get(project)
    if not isinstance(state, Mapping):
        return False
    if source_key is not None:
        return _source_history(state).get(source_key) == source_hash
    # Legacy inspection remains supported; execution uses exact source identity.
    return bool(source_hash) and str(source_hash) in _revision_history(state)


def record_processed(
    checkpoint: dict[str, Any], *, project: str, source_hash: str, watermark: str,
    source_key: str | None = None,
) -> dict[str, Any]:
    """Retain completed revisions as durable history, never evict for a run cap."""
    if not source_hash:
        raise CandidateIntakeConfigError("source revision is required")
    projects = copy.deepcopy(dict(checkpoint.get("projects") or {}))
    state = dict(projects.get(project) or {})
    revisions = _revision_history(state)
    if source_hash not in revisions:
        revisions.append(source_hash)
    state.pop("source_hash", None)
    state.pop("revision_limit", None)
    if source_key is not None:
        sources = _source_history(state)
        sources[source_key] = source_hash
        state["source_revisions"] = sources
    state.update(revisions=revisions, last_source_hash=source_hash, refresh_watermark=watermark)
    projects[project] = state
    checkpoint["projects"] = projects
    return checkpoint


def _chunk_field(chunk: Mapping[str, Any], key: str) -> str:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), Mapping) else {}
    value = chunk.get(key)
    if value is None:
        value = metadata.get(key)
    return "" if value is None else str(value)


def iter_source_hashes(chunks: Iterable[Mapping[str, Any]]) -> list[str]:
    """Deterministic, deduplicated revision list from the chunk source."""
    hashes: list[str] = []
    for chunk in chunks:
        content_hash = str((chunk or {}).get("content_hash") or "")
        if content_hash and content_hash not in hashes:
            hashes.append(content_hash)
    return hashes


def run_candidate_intake(
    *,
    settings: CandidateIntakeSettings,
    chunks: Sequence[Mapping[str, Any]],
    completion_fn: Callable[[list[dict]], Any],
    steward: Any,
    dry_run: bool = False,
    checkpoint: Mapping[str, Any] | None = None,
) -> tuple[CandidateIntakeReport, dict[str, Any]]:
    """Mine candidates from bounded session chunks and store proposals only.

    Returns the report and the updated checkpoint. Writes happen only when
    proposals are enabled and this is not a dry run, so a caller can prove the
    exact behaviour without a model or database.
    """
    if settings.checkpoint_path and settings.enable_proposals and not dry_run:
        with _checkpoint_lock(settings.checkpoint_path, ".project-" + _hash_text(settings.project) + ".lock") as path:
            # A stale caller snapshot is never authority over the durable file.
            current = load_checkpoint(str(path))
            report, state = _run_candidate_intake(
                settings=settings, chunks=chunks, completion_fn=completion_fn,
                steward=steward, dry_run=False, checkpoint=current,
            )
            if report.status == "completed" and state != current:
                save_checkpoint(str(path), state)
            return report, state
    return _run_candidate_intake(
        settings=settings, chunks=chunks, completion_fn=completion_fn,
        steward=steward, dry_run=dry_run, checkpoint=checkpoint,
    )


def _run_candidate_intake(
    *, settings: CandidateIntakeSettings, chunks: Sequence[Mapping[str, Any]],
    completion_fn: Callable[[list[dict]], Any], steward: Any,
    dry_run: bool, checkpoint: Mapping[str, Any] | None,
) -> tuple[CandidateIntakeReport, dict[str, Any]]:
    state = copy.deepcopy(dict(checkpoint if checkpoint is not None else load_checkpoint(settings.checkpoint_path)))
    # ``batch_candidates`` is the per-call batch width, not a cap on how many
    # chunks one run may examine. The chunk cap is its own bound so a run of
    # 20 chunks still mines all 20 across four batches.
    source = list(chunks)
    cap = int(settings.max_chunks_per_run)
    watermark = settings.refresh_watermark or settings.project
    if not source:
        return CandidateIntakeReport(project=settings.project, status="no_source",
            dry_run=dry_run, enabled=settings.enable_proposals), state
    if dry_run or not settings.enable_proposals:
        return CandidateIntakeReport(project=settings.project,
            status="dry_run" if dry_run else "disabled", dry_run=dry_run,
            enabled=settings.enable_proposals, examined_chunks=min(len(source), cap)), state

    from .batch_miner import BatchedEnvelopeMiner, _batch_source_key
    from .llm_brain_miner import _validate_chunk_source

    unique: dict[str, Mapping[str, Any]] = {}
    try:
        for chunk in source:
            provenance = _validate_chunk_source(chunk, scope="project")
            if provenance["project"] != settings.project:
                raise ValueError("source project mismatch")
            unique.setdefault(_batch_source_key(chunk, scope="project"), chunk)
    except (ValueError, KeyError, TypeError):
        return CandidateIntakeReport(project=settings.project, status="invalid_source",
            dry_run=False, enabled=True, error_code="invalid_source_scope_or_revision"), state

    previous = (state.get("projects") or {}).get(settings.project, {})
    unattributed_legacy = set(_revision_history(previous)) - set(_source_history(previous).values())
    if any(_chunk_field(chunk, "content_hash") in unattributed_legacy for chunk in unique.values()):
        # A legacy hash alone cannot identify which of two equal-content sources
        # was processed. Do not guess a source association or silently re-mine.
        return CandidateIntakeReport(project=settings.project, status="checkpoint_upgrade_required",
            dry_run=False, enabled=True, error_code="checkpoint_source_identity_required"), state

    fresh_all = [chunk for key, chunk in unique.items() if not already_processed(
        state, project=settings.project, source_hash=_chunk_field(chunk, "content_hash"), source_key=key)]
    already_seen = len(source) - len(fresh_all)
    # Apply the work cap after removing processed inputs, so repeated once runs
    # can reach the tail of a source file rather than forever inspecting its head.
    fresh = fresh_all[:cap]
    bounded = fresh
    if not fresh:
        return CandidateIntakeReport(project=settings.project, status="completed",
            dry_run=False, enabled=True, duplicate_count=already_seen), state

    batch_size = max(1, min(int(settings.batch_candidates), 100))
    miner = BatchedEnvelopeMiner(
        completion_fn=completion_fn,
        scope="project",
        max_candidates=settings.max_candidates_per_chunk,
        batch_size=batch_size,
        max_call_chars=max(1, int(settings.max_call_chars or DEFAULT_MAX_CALL_CHARS)),
    )
    try:
        candidates, mining_report = miner.mine_batches(fresh, refresh_watermark=watermark)
    except Exception:
        # A model-side failure is reported, never counted as completed work.
        return (
            CandidateIntakeReport(
                project=settings.project,
                status="mining_failed",
                dry_run=False,
                enabled=True,
                error_code="model_call_failed",
                examined_chunks=len(bounded),
            ),
            state,
        )

    proposed = 0
    duplicates = 0
    seen_ids: set[str] = set()
    processed_sources: set[str] = set()

    for candidate in candidates:
        memory_id = str(candidate.get("memory_id") or "")
        if not memory_id or memory_id in seen_ids:
            continue
        seen_ids.add(memory_id)
        try:
            outcome = steward.candidate_create(
                source_span=candidate,
                refresh_watermark=watermark,
                mark_needs_review=True,
                review_reason="session-memory candidate intake",
                proposer="candidate-intake",
            )
        except Exception:
            # Earlier successful proposals may exist, but never claim source
            # completion when a proposal write is failed or uncertain.
            return CandidateIntakeReport(
                project=settings.project, status="proposal_failed", dry_run=False,
                enabled=True, examined_chunks=len(bounded), proposed_count=proposed,
                error_code="proposal_write_failed",
            ), state
        if isinstance(outcome, Mapping) and outcome.get("proposal_write_performed") is False:
            duplicates += 1
        else:
            proposed += 1

    # Repeat state is keyed on the chunk's own content hash, so one project has
    # many revisions and no chunk is ever skipped by a project-level key.
    for chunk in fresh:
        revision = _chunk_field(chunk, "content_hash")
        if not revision or already_processed(state, project=settings.project, source_hash=revision,
                source_key=_batch_source_key(chunk, scope="project")):
            continue
        processed_sources.add(_batch_source_key(chunk, scope="project"))
        record_processed(
            state, project=settings.project, source_hash=revision, watermark=watermark,
            source_key=_batch_source_key(chunk, scope="project"),
        )
    duplicates += already_seen + (len(fresh) - len(processed_sources))
    skipped = mining_report.chunk_count - len(candidates)

    report = CandidateIntakeReport(
        project=settings.project,
        status="completed",
        dry_run=False,
        enabled=True,
        examined_chunks=len(bounded),
        proposed_count=proposed,
        duplicate_count=duplicates,
        skipped_count=skipped,
        remaining_chunks=len(fresh_all) - len(fresh),
    )
    return report, state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-memory-propose",
        description="Proposal-only session-memory candidate intake for LBrain.",
    )
    parser.add_argument("--project", required=True, help="single project scope")
    parser.add_argument(
        "--mode",
        choices=("dry-run", "once", "continuous"),
        default="dry-run",
        help="dry-run inspects without model calls; once runs one batch",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="allow proposal writes; without it nothing is written",
    )
    parser.add_argument(
        "--source-json",
        default="",
        help="JSON array of redacted session chunks; required for once/continuous",
    )
    parser.add_argument(
        "--source-mode",
        choices=("json", "pg"),
        default="json",
        help="json reads --source-json; pg reads the project's newest PostgreSQL session chunks",
    )
    parser.add_argument(
        "--source-limit",
        type=int,
        default=20,
        help="pg source: newest chunks to load per run (1..200)",
    )
    parser.add_argument(
        "--batch-candidates",
        type=int,
        default=DEFAULT_BATCH_CANDIDATES,
        help="chunks per model call (1..100); cost scales with batches, not chunks",
    )
    parser.add_argument(
        "--max-chunks-per-run",
        type=int,
        default=DEFAULT_MAX_CHUNKS_PER_RUN,
        help="unprocessed unique chunks per run (1..1000); repeats advance to remaining input",
    )
    parser.add_argument(
        "--max-call-chars",
        type=int,
        default=DEFAULT_MAX_CALL_CHARS,
        help="positive source-body character budget for batch splitting; not a hard prompt cap",
    )
    parser.add_argument(
        "--checkpoint",
        default="",
        help="durable checkpoint path for repeat state",
    )
    parser.add_argument(
        "--refresh-watermark",
        default="",
        help="mining watermark label recorded on created candidates",
    )
    return parser


def _load_chunks(path: str) -> list[Mapping[str, Any]]:
    if not path:
        raise CandidateIntakeConfigError("--source-json is required for this mode")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise CandidateIntakeConfigError("--source-json must contain a JSON array")
    if any(not isinstance(item, Mapping) for item in raw):
        raise CandidateIntakeConfigError("--source-json entries must be objects")
    return raw


def load_chunks_from_pg(dsn: str, *, project: str, limit: int) -> list[Mapping[str, Any]]:
    """Load the newest redacted session chunks for one project from PostgreSQL."""
    import psycopg

    bounded = max(1, min(int(limit), 200))
    with psycopg.connect(dsn, autocommit=True, connect_timeout=8) as conn:
        rows = conn.execute(
            "SELECT chunk_id, content_markdown, content_hash, provider "
            "FROM session_memory_chunks WHERE project = %s "
            "ORDER BY updated_at DESC LIMIT %s",
            (project, bounded),
        ).fetchall()
    chunks: list[Mapping[str, Any]] = []
    for chunk_id, content, content_hash, provider in rows:
        body = str(content or "").strip()
        revision = str(content_hash or "").strip()
        if not body or not revision:
            continue
        chunks.append(
            {
                "content": body,
                "content_hash": revision,
                "metadata": {
                    "knowledge_id": str(chunk_id or ""),
                    "project": project,
                    "provider": str(provider or ""),
                },
            }
        )
    return chunks


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = CandidateIntakeSettings(
            project=args.project,
            batch_candidates=args.batch_candidates,
            max_chunks_per_run=args.max_chunks_per_run,
            max_call_chars=args.max_call_chars,
            enable_proposals=bool(args.enable),
            checkpoint_path=args.checkpoint,
            refresh_watermark=args.refresh_watermark,
        )
    except CandidateIntakeConfigError as exc:
        print(json.dumps({"error_code": "invalid_settings", "message": str(exc)}, sort_keys=True))
        return 2

    dry_run = args.mode == "dry-run"
    if args.mode == "continuous":
        # A continuous scheduler is a future gate; it is not enabled here.
        print(
            json.dumps(
                {
                    "error_code": "continuous_mode_not_enabled",
                    "message": "continuous mode requires an explicit scheduler gate",
                    "project": args.project,
                },
                sort_keys=True,
            )
        )
        return 2

    try:
        if dry_run:
            chunks = []
        elif args.source_mode == "pg":
            dsn = (
                os.environ.get("NEURON_LBRAIN_PGVECTOR_DSN")
                or os.environ.get("LLM_BRAIN_PGVECTOR_DSN")
                or ""
            ).strip()
            if not dsn:
                raise CandidateIntakeConfigError(
                    "NEURON_LBRAIN_PGVECTOR_DSN is required for --source-mode pg"
                )
            chunks = load_chunks_from_pg(dsn, project=settings.project, limit=args.source_limit)
        else:
            chunks = _load_chunks(args.source_json)
    except (CandidateIntakeConfigError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {"error_code": "invalid_source", "error_type": type(exc).__name__,
                 "message": "--source-json is missing, unreadable, or invalid"}, sort_keys=True
            )
        )
        return 2

    if dry_run:
        report = CandidateIntakeReport(
            project=settings.project,
            status="dry_run",
            dry_run=True,
            enabled=settings.enable_proposals,
        )
        print(json.dumps(report.to_dict(), sort_keys=True))
        return 0

    # Writing requires an injected steward and model function. They are built by
    # the registered entry point, never guessed here, and never defaulted to a
    # live model without explicit cost approval.
    from .extraction_llm import build_intake_completion_from_env

    if not settings.enable_proposals:
        report = CandidateIntakeReport(
            project=settings.project,
            status="disabled",
            dry_run=False,
            enabled=False,
            examined_chunks=len(chunks),
        )
        print(json.dumps(report.to_dict(), sort_keys=True))
        return 0

    if not settings.checkpoint_path:
        print(json.dumps({"error_code": "checkpoint_required", "message": "enabled once requires --checkpoint"}, sort_keys=True))
        return 2
    try:
        load_checkpoint(settings.checkpoint_path)
    except CandidateIntakeConfigError:
        print(json.dumps({"error_code": "checkpoint_unavailable"}, sort_keys=True))
        return 2

    try:
        completion_fn = build_intake_completion_from_env()
    except Exception as exc:
        print(
            json.dumps(
                {"error_code": "model_unavailable", "message": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 3

    from .brain_steward import BrainStewardService

    ledger = None
    pgvector_store = _build_store_from_env()
    if pgvector_store is None:
        print(json.dumps({"error_code": "postgresql_required"}, sort_keys=True))
        return 3
    steward = BrainStewardService(ledger, pgvector_store=pgvector_store)

    try:
        report, state = run_candidate_intake(
            settings=settings,
            chunks=chunks,
            completion_fn=completion_fn,
            steward=steward,
            dry_run=False,
        )
    except (CandidateIntakeConfigError, OSError):
        print(json.dumps({"error_code": "checkpoint_unavailable", "status": "failed"}, sort_keys=True))
        return 3
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.status == "completed" else 1


def _build_store_from_env() -> Any:
    """Build the PostgreSQL store from the existing canonical DSN env names."""
    dsn = (
        os.environ.get("NEURON_LBRAIN_PGVECTOR_DSN")
        or os.environ.get("LLM_BRAIN_PGVECTOR_DSN")
        or ""
    ).strip()
    if not dsn:
        return None
    from ..postgres_store.pgvector_store import PgVectorStore

    return PgVectorStore(dsn=dsn)


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())

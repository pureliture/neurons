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

Repeat state is a small durable checkpoint file that records the last
successfully processed source hash per project. It never mutates the existing
source projection cursor and never marks a failed candidate as a completed
projection.
"""

from __future__ import annotations

import argparse
import json
import os
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
            "proposal_only": True,
            "authoritative_memory_changed": False,
            "note": _PROPOSAL_ONLY_NOTE,
        }


def _hash_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def load_checkpoint(path: str) -> dict[str, Any]:
    """Read the durable checkpoint; an absent file is an empty checkpoint."""
    if not path:
        return {"version": DEFAULT_CHECKPOINT_VERSION, "projects": {}}
    checkpoint_file = Path(path)
    if not checkpoint_file.exists():
        return {"version": DEFAULT_CHECKPOINT_VERSION, "projects": {}}
    try:
        raw = json.loads(checkpoint_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": DEFAULT_CHECKPOINT_VERSION, "projects": {}}
    if not isinstance(raw, Mapping):
        return {"version": DEFAULT_CHECKPOINT_VERSION, "projects": {}}
    projects = raw.get("projects")
    return {
        "version": int(raw.get("version") or DEFAULT_CHECKPOINT_VERSION),
        "projects": dict(projects) if isinstance(projects, Mapping) else {},
    }


def save_checkpoint(path: str, checkpoint: Mapping[str, Any]) -> None:
    """Atomically persist the checkpoint; never a partial write."""
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "version": int(checkpoint.get("version") or DEFAULT_CHECKPOINT_VERSION),
            "projects": {
                str(project): dict(state)
                for project, state in (checkpoint.get("projects") or {}).items()
                if isinstance(state, Mapping)
            },
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)


def checkpoint_source_hash(project: str) -> str:
    """Stable, non-reversible key for one processed source revision."""
    return "sha256:" + _hash_text(project)


def already_processed(checkpoint: Mapping[str, Any], *, project: str, source_hash: str) -> bool:
    """True only when this exact project+revision was recorded as processed."""
    state = (checkpoint.get("projects") or {}).get(project)
    if not isinstance(state, Mapping):
        return False
    revisions = state.get("revisions")
    if isinstance(revisions, list):
        return str(source_hash or "") in {str(value) for value in revisions}
    # Legacy single-hash checkpoints stay readable.
    return str(state.get("source_hash") or "") == str(source_hash or "")


def record_processed(
    checkpoint: dict[str, Any], *, project: str, source_hash: str, watermark: str
) -> dict[str, Any]:
    """Record one processed revision for a project.

    A project has many revisions, so the checkpoint keeps a bounded set rather
    than a single hash: overwriting one slot would forget the earlier
    revision and re-mine it on the next run.
    """
    projects = dict(checkpoint.get("projects") or {})
    state = dict(projects.get(project) or {})
    revisions = [str(value) for value in state.get("revisions") or [] if str(value)]
    if source_hash not in revisions:
        revisions.append(source_hash)
    limit = max(1, int(state.get("revision_limit") or DEFAULT_REVISION_LIMIT))
    bounded = revisions[-limit:]
    projects[project] = {
        "revisions": bounded,
        "revision_limit": limit,
        "last_source_hash": bounded[-1],
        "refresh_watermark": watermark,
    }
    checkpoint["projects"] = projects
    return checkpoint


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
    state = dict(checkpoint or load_checkpoint(settings.checkpoint_path))
    # ``batch_candidates`` is the per-call batch width, not a cap on how many
    # chunks one run may examine. The chunk cap is its own bound so a run of
    # 20 chunks still mines all 20 across four batches.
    bounded = list(chunks)[: max(1, int(settings.max_chunks_per_run))]
    watermark = settings.refresh_watermark or settings.project

    # A revision already processed on an earlier run is never re-sent to the
    # model. This is the cost guard: repeat runs stay free instead of paying
    # again for the same session.
    fresh = [
        chunk for chunk in bounded
        if not already_processed(
            state,
            project=settings.project,
            source_hash=str(chunk.get("content_hash") or ""),
        )
    ]
    already_seen = len(bounded) - len(fresh)

    if not bounded:
        # An empty source is reported, never treated as completed production.
        return (
            CandidateIntakeReport(
                project=settings.project,
                status="no_source" if not already_seen else "already_processed",
                dry_run=dry_run,
                enabled=settings.enable_proposals,
                examined_chunks=len(bounded),
                duplicate_count=already_seen,
            ),
            state,
        )

    if dry_run or not settings.enable_proposals:
        # No model call and no write on this path.
        return (
            CandidateIntakeReport(
                project=settings.project,
                status="dry_run" if dry_run else "disabled",
                dry_run=dry_run,
                enabled=settings.enable_proposals,
                examined_chunks=len(bounded),
            ),
            state,
        )

    # Batched mining: cost scales with batches, not with individual chunks.
    from .batch_miner import BatchedEnvelopeMiner

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
    processed_hashes: set[str] = set()

    for candidate in candidates:
        memory_id = str(candidate.get("memory_id") or "")
        if not memory_id or memory_id in seen_ids:
            continue
        seen_ids.add(memory_id)
        steward.candidate_create(
            source_span=candidate,
            refresh_watermark=watermark,
            mark_needs_review=True,
            review_reason="session-memory candidate intake",
            proposer="candidate-intake",
        )
        proposed += 1

    # Repeat state is keyed on the chunk's own content hash, so one project has
    # many revisions and no chunk is ever skipped by a project-level key.
    for chunk in fresh:
        revision = str(chunk.get("content_hash") or "")
        if not revision or already_processed(state, project=settings.project, source_hash=revision):
            continue
        processed_hashes.add(revision)
        record_processed(
            state, project=settings.project, source_hash=revision, watermark=watermark
        )
    duplicates = already_seen + (len(fresh) - len(processed_hashes))
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
        help="bounded chunks examined per run (1..1000)",
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
    return [item for item in raw if isinstance(item, Mapping)]


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
                {"error_code": "invalid_source", "message": str(exc)}, sort_keys=True
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

    report, state = run_candidate_intake(
        settings=settings,
        chunks=chunks,
        completion_fn=completion_fn,
        steward=steward,
        dry_run=False,
        checkpoint=load_checkpoint(settings.checkpoint_path),
    )
    save_checkpoint(settings.checkpoint_path, state)
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

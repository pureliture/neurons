from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_knowledge.couchdb_source.document_model import SourceDocType
import threading
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionCurrentnessLimitError,
    SourceRevisionDeferredBudget,
)
from agent_knowledge.couchdb_source.source_store import CouchDBSourceStore
from agent_knowledge.ledger import Ledger
from .bulk_full_input import (
    FullInputBuilder,
    iter_bounded_chunks,
    FullInputState,
    assert_source_current,
    enforce_free_only_extractor,
    make_full_chunk_session_input,
    privacy_gate,
    shadow_group_ids,
    validate_source_documents,
)
from .bulk_streaming import safe_reason
from .bulk_full_input_neo4j_writer import Neo4jShadowWriter
from .bulk_full_input_pg_state import PgChunkState
from .runtime import _conversation_chunk_order_key

from .bulk_semantic import (
    BulkSemanticSessionInput,
    DEFAULT_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS,
    DEFAULT_BULK_SEMANTIC_MAX_SESSION_CHARS,
    DEFAULT_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL,
    DeterministicGraphitiSemanticWriter,
    OpenAICompatibleBulkSemanticExtractor,
    make_bulk_session_input,
)
from .couchdb_projection_cli import (
    _acquire_runtime_lock,
    _build_source_store,
    _project_ref,
    _release_runtime_lock,
    _select_sessions_bounded,
    _write_dead_letter,
    _write_jsonl,
)
from .ledger_adapter import (
    EXTRACTION_LEVEL_ENTITY,
    LedgerGraphProjectionStateStore,
    LedgerSessionMemoryArtifactStore,
)
from .models import PROJECTION_SCHEMA_VERSION
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from .bulk_authority import authorized_sources_by_hash
from .bulk_full_input_reader import PgAuthorityCardStore
from .runtime import (
    session_episode_from_couchdb_source,
    session_source_revision_from_couchdb_source,
)


def _source_authority_sources(manifest: Any, chunk_hash: str) -> list[dict[str, str]]:
    """Return source-declared card provenance matching this exact chunk only."""
    if not isinstance(manifest, Mapping):
        return []
    project_authority = manifest.get("project_authority")
    if not isinstance(project_authority, Mapping):
        return []
    declared = project_authority.get("authority_sources")
    if not isinstance(declared, (list, tuple)):
        return []
    sources: list[dict[str, str]] = []
    for item in declared:
        if not isinstance(item, Mapping):
            continue
        memory_id = str(item.get("authority_memory_id") or "")
        content_hash = str(item.get("content_hash") or "")
        if memory_id and content_hash == chunk_hash:
            sources.append({"authority_memory_id": memory_id, "content_hash": content_hash})
    return sources


def _pg_authority_by_hash(dsn: str, project: str, hashes: list[str], *, as_of=None):
    return authorized_sources_by_hash(dsn, project, hashes, as_of=as_of)

BULK_SEMANTIC_SCHEMA_VERSION = "llm_brain_couchdb_bulk_semantic.v1"
# A session that cannot be read inside one cycle's budget is deferred, never
# truncated: the next cycle retries it first with a doubled budget, and only an
# explicit dead-letter entry ends the escalation.
DEFAULT_MAX_SESSION_MEMBER_DOCS = 20000
DEFAULT_SESSION_DEADLINE_SECONDS = 180.0
DEFERRED_MAX_ATTEMPTS = 4
DEFERRED_BUDGET_ESCALATION = 2
DEFERRED_MAX_MEMBER_DOCS = 200000
DEFERRED_MAX_DEADLINE_SECONDS = 1800.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge couchdb-graph-bulk-semantic")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--project", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--couchdb-url", default=os.environ.get("COUCHDB_URL", ""))
    parser.add_argument("--couchdb-db", default=os.environ.get("COUCHDB_DB", "transcript_source"))
    parser.add_argument("--couchdb-user", default=os.environ.get("COUCHDB_USER", ""))
    parser.add_argument("--couchdb-password-env", default="COUCHDB_PASSWORD")
    parser.add_argument("--dead-letter-jsonl", default="")
    parser.add_argument("--progress-jsonl", default="")
    parser.add_argument("--report-every", type=int, default=25)
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--max-projects", type=int, default=0)
    parser.add_argument(
        "--max-sessions-per-call",
        type=int,
        default=_positive_int_env(
            os.environ.get("LLM_BRAIN_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL", ""),
            DEFAULT_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL,
        ),
    )
    parser.add_argument(
        "--max-session-chars",
        type=int,
        default=_positive_int_env(
            os.environ.get("LLM_BRAIN_BULK_SEMANTIC_MAX_SESSION_CHARS", ""),
            DEFAULT_BULK_SEMANTIC_MAX_SESSION_CHARS,
        ),
    )
    parser.add_argument("--allow-empty-sessions", action="store_true")
    parser.add_argument(
        "--max-session-member-docs",
        type=int,
        default=_positive_int_env(
            os.environ.get("LLM_BRAIN_BULK_SEMANTIC_MAX_MEMBER_DOCS", ""),
            DEFAULT_MAX_SESSION_MEMBER_DOCS,
        ),
    )
    parser.add_argument(
        "--session-deadline-seconds",
        type=float,
        default=_positive_float_env(
            os.environ.get("LLM_BRAIN_BULK_SEMANTIC_SESSION_DEADLINE_SECONDS", ""),
            DEFAULT_SESSION_DEADLINE_SECONDS,
        ),
    )
    parser.add_argument("--deferred-jsonl", default="")
    parser.add_argument(
        "--full-input-policy-version",
        default=os.environ.get("LLM_BRAIN_BULK_FULL_INPUT_POLICY_VERSION", ""),
    )
    parser.add_argument("--trial-neo4j-writer", action="store_true")
    parser.add_argument("--privacy-forbidden-marker", action="append", default=[])
    parser.add_argument("--stream-mode", action="store_true")
    parser.add_argument(
        "--spool-body-budget-bytes", type=int,
        default=int(os.environ.get("NEURON_BULK_SPOOL_BODY_BUDGET_BYTES") or 32 * 1024 * 1024),
    )
    parser.add_argument(
        "--spool-response-budget-bytes", type=int,
        default=int(os.environ.get("NEURON_BULK_SPOOL_RESPONSE_BUDGET_BYTES") or 48 * 1024 * 1024),
    )
    parser.add_argument(
        "--currentness-page-bytes", type=int,
        default=int(os.environ.get("NEURON_SOURCE_CURRENTNESS_MAX_PAGE_BYTES") or 512 * 1024),
    )
    parser.add_argument(
        "--currentness-compact-bytes", type=int,
        default=int(os.environ.get("NEURON_SOURCE_CURRENTNESS_MAX_COMPACT_BYTES") or 8 * 1024 * 1024),
    )
    args = parser.parse_args(argv)
    os.environ["NEURON_BULK_SPOOL_BODY_BUDGET_BYTES"] = str(args.spool_body_budget_bytes)
    os.environ["NEURON_BULK_SPOOL_RESPONSE_BUDGET_BYTES"] = str(args.spool_response_budget_bytes)
    os.environ["NEURON_SOURCE_CURRENTNESS_MAX_COMPACT_BYTES"] = str(args.currentness_compact_bytes)
    os.environ["NEURON_SOURCE_CURRENTNESS_MAX_PAGE_BYTES"] = str(args.currentness_page_bytes)

    try:
        source_store = _build_source_store(
            couchdb_url=args.couchdb_url,
            couchdb_db=args.couchdb_db,
            couchdb_user=args.couchdb_user,
            couchdb_password_env=args.couchdb_password_env,
        )
        if args.stream_mode:
            if os.environ.get("NEURON_TRIAL_MODE") != "1":
                raise RuntimeError("stream mode requires NEURON_TRIAL_MODE=1")
            from .bulk_full_input import (
                FullInputState,
                enforce_opencode_go_hy4_flash_free,
            )
            from .bulk_full_input_pg_state import PgChunkState
            from .bulk_full_input_neo4j_writer import Neo4jShadowWriter
            from .bulk_stream_runner import run_streamed_full_input

            extractor = OpenAICompatibleBulkSemanticExtractor.from_env()
            enforce_opencode_go_hy4_flash_free(
                extractor,
                allowlist=frozenset(filter(None,
                    os.environ.get("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "").split(","))),
            )
            if not args.trial_neo4j_writer:
                raise RuntimeError("stream mode requires the isolated shadow writer")
            writer = Neo4jShadowWriter(
                os.environ["TRIAL_NEO4J_URI"], "neo4j", os.environ["TRIAL_NEO4J_PASSWORD"]
            )
            state = (
                PgChunkState(os.environ["TRIAL_PG_DSN"])
                if os.environ.get("TRIAL_PG_DSN")
                else FullInputState(Path(args.runtime_dir) if args.runtime_dir else Path("."))
            )
            report = run_streamed_full_input(
                ledger_path=Path(args.ledger), source_store=source_store,
                extractor=extractor, writer=writer, full_input_state=state,
                policy_version=str(args.full_input_policy_version or "stream-default"),
                project=str(args.project or ""), limit=int(args.limit),
                forbidden_markers=tuple(
                    marker.encode("utf-8") for marker in args.privacy_forbidden_marker
                ),
                batch_size=max(1, int(args.max_sessions_per_call)),
            )
            print(json.dumps(report, sort_keys=True))
            return 0 if report.get("status") in {"ok", "already_running"} else 1
        report = run_couchdb_bulk_semantic_projection(
            ledger_path=Path(args.ledger),
            source_store=source_store,
            limit=int(args.limit),
            project=str(args.project or ""),
            provider=str(args.provider or ""),
            dead_letter_jsonl=Path(args.dead_letter_jsonl) if args.dead_letter_jsonl else None,
            progress_jsonl=Path(args.progress_jsonl) if args.progress_jsonl else None,
            report_every=int(args.report_every),
            runtime_dir=Path(args.runtime_dir) if args.runtime_dir else None,
            max_projects=int(args.max_projects),
            max_sessions_per_call=int(args.max_sessions_per_call),
            max_session_chars=int(args.max_session_chars),
            max_session_member_docs=int(args.max_session_member_docs),
            session_deadline_seconds=float(args.session_deadline_seconds),
            deferred_jsonl=Path(args.deferred_jsonl) if args.deferred_jsonl else None,
            allow_empty_sessions=(
                bool(args.allow_empty_sessions)
                or _truthy(os.environ.get("LLM_BRAIN_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS", ""))
                or DEFAULT_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS
            ),
            full_input_policy_version=str(args.full_input_policy_version or ""),
            use_trial_neo4j_writer=bool(args.trial_neo4j_writer),
            privacy_forbidden_markers=tuple(args.privacy_forbidden_marker),
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": BULK_SEMANTIC_SCHEMA_VERSION,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "error_name": getattr(exc, "name", ""),
                    "error_reason": safe_reason(exc),
                    "message": "couchdb bulk semantic projection failed",
                    "failure_reasons": {safe_reason(exc): 1},
                    "raw_paths_printed": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, sort_keys=True))
    # failed/partial은 non-zero로 종료해 상위 오케스트레이션(trigger wrapper 등)이
    # 실패를 성공으로 오판하지 않게 한다. already_running은 no-op이므로 0.
    return 0 if report.get("status") in {"ok", "already_running"} else 1


def run_couchdb_bulk_semantic_projection(
    *,
    ledger_path: Path,
    source_store: CouchDBSourceStore,
    limit: int,
    project: str = "",
    provider: str = "",
    dead_letter_jsonl: Path | None = None,
    progress_jsonl: Path | None = None,
    report_every: int = 25,
    runtime_dir: Path | None = None,
    max_projects: int = 0,
    max_sessions_per_call: int = DEFAULT_BULK_SEMANTIC_MAX_SESSIONS_PER_CALL,
    max_session_chars: int = DEFAULT_BULK_SEMANTIC_MAX_SESSION_CHARS,
    allow_empty_sessions: bool = DEFAULT_BULK_SEMANTIC_ALLOW_EMPTY_SESSIONS,
    extractor: Any | None = None,
    writer: Any | None = None,
    max_session_member_docs: int = 0,
    session_deadline_seconds: float = 0.0,
    deferred_jsonl: Path | None = None,
    full_input_policy_version: str = "",
    full_input_state: Any | None = None,
    use_trial_neo4j_writer: bool = False,
    privacy_forbidden_markers: tuple[str, ...] = (),
) -> dict[str, Any]:
    lock_handle, locked_report = _acquire_runtime_lock(runtime_dir)
    if locked_report is not None:
        return {
            "schema_version": BULK_SEMANTIC_SCHEMA_VERSION,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
            "status": "already_running",
            "runtime_lock": locked_report.get("runtime_lock", {}),
            "mutation_performed": False,
            "raw_paths_printed": False,
        }

    started = time.monotonic()
    ledger = Ledger(ledger_path)
    artifact_store = LedgerSessionMemoryArtifactStore(ledger)
    projection_state_store = LedgerGraphProjectionStateStore(ledger)
    extractor = extractor or OpenAICompatibleBulkSemanticExtractor.from_env()
    writer = writer or DeterministicGraphitiSemanticWriter.from_env()

    if full_input_policy_version and os.environ.get("NEURON_TRIAL_MODE") != "1":
        raise RuntimeError("full-input mode requires NEURON_TRIAL_MODE=1")
    if use_trial_neo4j_writer and (
        not full_input_policy_version or os.environ.get("NEURON_TRIAL_MODE") != "1"
    ):
        raise RuntimeError("trial Neo4j writer requires trial full-input mode")
    if use_trial_neo4j_writer:
        writer = Neo4jShadowWriter(
            os.environ["TRIAL_NEO4J_URI"],
            "neo4j",
            os.environ["TRIAL_NEO4J_PASSWORD"],
        )
    full_input_state = full_input_state or (
        PgChunkState(os.environ["TRIAL_PG_DSN"])
        if full_input_policy_version and os.environ.get("TRIAL_PG_DSN")
        else FullInputState(runtime_dir)
        if full_input_policy_version and runtime_dir is not None
        else None
    )
    if full_input_policy_version:
        free_allowlist = frozenset(
            item.strip()
            for item in os.environ.get("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "").split(",")
            if item.strip()
        )
        enforce_free_only_extractor(extractor, free_allowlist)
    selection = _select_sessions_bounded(
        source_store,
        project=project,
        provider=provider,
        limit=limit,
        projection_state_store=None if full_input_policy_version else projection_state_store,
        extraction_level=EXTRACTION_LEVEL_ENTITY,
    )
    selected = selection.sessions
    total_available = selection.source_count
    max_sessions_per_call = max(1, int(max_sessions_per_call))
    max_session_chars = max(200, int(max_session_chars))
    report_every = max(1, int(report_every))
    max_projects = max(0, int(max_projects))
    budget_docs = (
        int(max_session_member_docs)
        if int(max_session_member_docs) > 0
        else DEFAULT_MAX_SESSION_MEMBER_DOCS
    )
    budget_seconds = (
        float(session_deadline_seconds)
        if float(session_deadline_seconds) > 0
        else DEFAULT_SESSION_DEADLINE_SECONDS
    )
    deferred_path = deferred_jsonl
    if deferred_path is None:
        # Deferral state must survive even for callers that pass no runtime dir;
        # the ledger directory is always explicit and per-brain.
        deferred_path = Path(ledger_path).parent / "bulk-semantic-deferred.jsonl"
    deferred_records = _load_deferred_records(deferred_path)
    deferred_updates: dict[str, dict[str, Any]] = {}
    retried_hashes: set[str] = set()
    priority_sessions: list[dict[str, Any]] = []
    project_filter = str(project or "")
    provider_filter = str(provider or "")
    for session_id_hash, record in deferred_records.items():
        record_project = str(record.get("project") or "")
        record_provider = str(record.get("provider") or "")
        if project_filter and record_project != project_filter:
            continue
        if provider_filter and record_provider != provider_filter:
            continue
        priority_sessions.append(
            {
                "session_id_hash": session_id_hash,
                "project": record_project,
                "provider": record_provider,
                "_deferred_attempts": int(record.get("attempts") or 0),
            }
        )
    if priority_sessions:
        priority_sessions.sort(key=lambda item: item["session_id_hash"])
        deferred_hashes = {item["session_id_hash"] for item in priority_sessions}
        selected = priority_sessions + [
            session for session in selected
            if str(session.get("session_id_hash") or "") not in deferred_hashes
        ]
        if int(limit) > 0:
            # Priority retries must not widen the operator-requested window.
            selected = selected[: int(limit)]

    # Only touched sessions from this bounded selection are cached; historical
    # projection state is checked with an indexed exact-identity query above.
    projected_cache: dict[str, dict[str, set[str]]] = {}
    by_provider: Counter[str] = Counter()
    by_project: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    durations: list[int] = []
    batch_items: list[tuple[dict[str, Any], Any]] = []
    projected = skipped_resumed = failed = materialized = llm_batches = 0
    deferred = deferred_exhausted = 0
    entities_written = relations_written = 0
    fallback_single_session_batches = 0
    processed = 0
    stopped_after_max_projects = False
    full_input_source_invalid = 0
    full_input_chunks = 0
    full_input_meta: dict[str, dict[str, Any]] = {}

    _write_jsonl(
        progress_jsonl,
        {
            "event": "start",
            "selected": len(selected),
            "total_available": total_available,
            "limit": int(limit),
            "max_sessions_per_call": max_sessions_per_call,
            "max_session_chars": max_session_chars,
            "max_session_member_docs": budget_docs,
            "session_deadline_seconds": budget_seconds,
            "deferred_retried": len(priority_sessions),
        },
    )

    try:
        for index, session in enumerate(selected, start=1):
            processed = index
            item_started = time.monotonic()
            session_project = str(session.get("project") or "")
            session_provider = str(session.get("provider") or "")
            session_id_hash = str(session.get("session_id_hash") or "")
            by_project[session_project] += 1
            by_provider[session_provider] += 1
            natural_id = session_id_hash.replace(":", "_")
            status = "unknown"
            reason = ""
            if max_projects and materialized >= max_projects:
                stopped_after_max_projects = True
                break
            attempts = int(session.get("_deferred_attempts") or 0)
            if attempts:
                retried_hashes.add(session_id_hash)
            session_budget_docs = min(
                budget_docs * (DEFERRED_BUDGET_ESCALATION ** attempts),
                DEFERRED_MAX_MEMBER_DOCS,
            )
            session_budget_seconds = min(
                budget_seconds * (DEFERRED_BUDGET_ESCALATION ** attempts),
                DEFERRED_MAX_DEADLINE_SECONDS,
            )
            session_deadline = time.monotonic() + session_budget_seconds
            try:
                source_hash = session_source_revision_from_couchdb_source(
                    session_id_hash=session_id_hash,
                    source_store=source_store,
                )
                if full_input_state is not None:
                    docs = source_store.find_by_session(session_id_hash=session_id_hash)
                    manifest = next(
                        (doc for doc in docs if doc.get("doc_type") == SourceDocType.COVERAGE_MANIFEST),
                        None,
                    )
                    chunk_docs = [
                        doc for doc in docs
                        if doc.get("doc_type") == SourceDocType.CONVERSATION_CHUNK
                    ]
                    evidence_docs = [
                        doc for doc in docs
                        if doc.get("doc_type") == SourceDocType.TOOL_EVIDENCE_BUNDLE
                    ]
                    try:
                        validate_source_documents(chunk_docs + evidence_docs, manifest or {})
                        builder = FullInputBuilder(
                            chunk_docs,
                            session_hash=session_id_hash,
                            source_hash=source_hash,
                            policy_version=full_input_policy_version,
                            run_id=f"synthetic-run-{index}",
                            evidence_documents=evidence_docs,
                        )
                        authority_by_hash = _pg_authority_by_hash(
                            os.environ.get("TRIAL_PG_DSN", ""),
                            session_project,
                            [chunk.canonical_hash for chunk in builder.plan.chunks]
                            + [chunk.canonical_hash for chunk in builder.evidence_plan.chunks],
                        )
                        if any(
                            not doc.get("redaction_version") for doc in chunk_docs + evidence_docs
                        ):
                            raise SourceInvalid("missing redaction provenance")
                        privacy_gate(
                            b"".join(
                                str(doc.get("body") or "").encode("utf-8")
                                for doc in evidence_docs
                            ),
                            forbidden_markers=tuple(
                                marker.encode("utf-8") for marker in privacy_forbidden_markers
                            ),
                            all_redacted=True,
                        )
                        privacy_gate(
                            builder.canonical_bytes,
                            forbidden_markers=tuple(
                                marker.encode("utf-8") for marker in privacy_forbidden_markers
                            ),
                            all_redacted=True,
                        )
                        streamed = dict(
                            iter_bounded_chunks(chunk_docs, max_resident_bodies=2)
                        )
                        ordered_docs = sorted(chunk_docs, key=_conversation_chunk_order_key)
                        for chunk in builder.plan.chunks:
                            body = streamed.get(chunk.ordinal)
                            source_doc = ordered_docs[chunk.ordinal]
                            digest = "sha256:" + hashlib.sha256(body or b"").hexdigest()
                            if (
                                body is None
                                or digest != str(source_doc.get("content_hash") or "")
                                or digest != chunk.canonical_hash
                            ):
                                raise SourceInvalid("active source body hash mismatch")
                        conversation_keys = [
                            f"{session_id_hash}:{chunk.key}" for chunk in builder.plan.chunks
                        ]
                        evidence_keys = [
                            f"{session_id_hash}:ev{chunk.ordinal}"
                            for chunk in builder.evidence_plan.chunks
                        ]
                        chunk_keys = conversation_keys + evidence_keys
                        if hasattr(full_input_state, "register_plan"):
                            full_input_state.register_plan(
                                run_id=f"synthetic-run-{index}",
                                project=session_project,
                                session_hash=session_id_hash,
                                policy_version=full_input_policy_version,
                                source_hash=source_hash,
                                map_digest=builder.plan.map_digest,
                                chunk_keys=chunk_keys,
                            )
                        if full_input_state.is_session_complete(
                            project=session_project,
                            policy_version=full_input_policy_version,
                            source_hash=source_hash,
                            map_digest=builder.plan.map_digest,
                            chunk_keys=chunk_keys,
                        ):
                            skipped_resumed += 1
                            status = "skipped_resumed"
                        else:
                            episode = session_episode_from_couchdb_source(
                                session_id_hash=session_id_hash,
                                source_store=source_store,
                                artifact_store=artifact_store,
                                extractor_version="couchdb-bulk-semantic.1",
                                max_member_docs=session_budget_docs,
                                deadline_monotonic=session_deadline,
                            )
                            shadow_groups = shadow_group_ids(
                                f"/project/{session_project}", full_input_policy_version
                            )
                            pending_keys: list[str] = []
                            tokens: dict[str, int] = {}
                            owner = f"trial-{os.getpid()}-{threading.get_ident()}"
                            for chunk in builder.plan.chunks:
                                key = conversation_keys[chunk.ordinal]
                                if full_input_state.is_complete(key):
                                    continue
                                authority_sources = authority_by_hash.get(
                                    chunk.canonical_hash, []
                                )
                                if key not in tokens and hasattr(full_input_state, "claim"):
                                    tokens[key] = full_input_state.claim(key=key, owner=owner)
                                chunk_episode = replace(
                                    episode,
                                    payload={
                                        **episode.payload,
                                        "input_lane": "conversation",
                                        "canonical_chunk_hash": chunk.canonical_hash,
                                        "graph_group_id": (
                                            shadow_groups[0] if shadow_groups else ""
                                        ),
                                        "fence_token": tokens.get(key, 0),
                                        "authority_sources": authority_sources,
                                    },
                                )
                                batch_items.append(
                                    (
                                        session,
                                        BulkSemanticSessionInput(
                                            session_key=f"{session_id_hash}-c{chunk.ordinal}",
                                            episode=chunk_episode,
                                            text=streamed[chunk.ordinal].decode("utf-8"),
                                        ),
                                    )
                                )
                                pending_keys.append(key)
                            for chunk in builder.evidence_plan.chunks:
                                key = evidence_keys[chunk.ordinal]
                                if full_input_state.is_complete(key):
                                    continue
                                authority_sources = authority_by_hash.get(
                                    chunk.canonical_hash, []
                                )
                                if key not in tokens and hasattr(full_input_state, "claim"):
                                    tokens[key] = full_input_state.claim(key=key, owner=owner)
                                evidence_episode = replace(
                                    episode,
                                    payload={
                                        **episode.payload,
                                        "input_lane": "evidence",
                                        "canonical_chunk_hash": chunk.canonical_hash,
                                        "graph_group_id": (
                                            shadow_groups[0] if shadow_groups else ""
                                        ),
                                        "fence_token": tokens.get(key, 0),
                                        "authority_sources": authority_sources,
                                    },
                                )
                                batch_items.append(
                                    (
                                        session,
                                        BulkSemanticSessionInput(
                                            session_key=f"{session_id_hash}-ev{chunk.ordinal}",
                                            episode=evidence_episode,
                                            text=builder.render_evidence(chunk.ordinal),
                                        ),
                                    )
                                )
                                pending_keys.append(key)
                            full_input_chunks += len(pending_keys)
                            full_input_meta[session_id_hash] = {
                                "map_digest": builder.plan.map_digest,
                                "chunk_keys": chunk_keys,
                                "shadow_groups": shadow_groups,
                            }
                            materialized += 1
                            status = "queued"
                            item_natural = {
                                item.session_key: item.episode.natural_id
                                for _session, item in batch_items
                            }
                            write_report = _flush_batch_items(
                                batch_items,
                                extractor=extractor,
                                writer=writer,
                                projection_state_store=projection_state_store,
                                projected_cache=projected_cache,
                                dead_letter_jsonl=dead_letter_jsonl,
                                failure_reasons=failure_reasons,
                                allow_empty_sessions=allow_empty_sessions,
                            )
                            batch_items = []
                            llm_batches += int(write_report["llm_batches"])
                            fallback_single_session_batches += int(
                                write_report["fallback_single_session_batches"]
                            )
                            projected += int(write_report["projected"])
                            failed += int(write_report["failed"])
                            entities_written += int(write_report["entities_written"])
                            relations_written += int(write_report["relations_written"])
                            status = "projected" if not write_report["failed"] else "failed"
                            receipts = dict(write_report.get("chunk_receipts", {}) or {})
                            if not receipts:
                                written_ids = set(
                                    write_report.get("projected_natural_ids", ()) or ()
                                )
                                receipts = {
                                    session_key: "natural-id-receipt:" + session_key
                                    for session_key, natural_id in item_natural.items()
                                    if natural_id in written_ids
                                }
                            pending_map: dict[str, str] = {}
                            for key in pending_keys:
                                if key in conversation_keys:
                                    session_key = (
                                        f"{session_id_hash}-c{conversation_keys.index(key)}"
                                    )
                                else:
                                    session_key = (
                                        f"{session_id_hash}-ev{evidence_keys.index(key)}"
                                    )
                                pending_map[session_key] = key

                            verified = [
                                (pending_map[item_key], receipts[item_key])
                                for item_key in pending_map
                                if item_key in receipts and receipts[item_key]
                            ]
                            if verified:
                                assert_source_current(
                                    session_id_hash=session_id_hash,
                                    source_store=source_store,
                                    expected_hash=source_hash,
                                )
                            for key, receipt in verified:
                                if key in tokens:
                                    full_input_state.complete(
                                        key=key,
                                        fence_token=tokens[key],
                                        owner=owner,
                                        output_digest=builder.plan.map_digest,
                                        write_receipt=receipt,
                                    )
                                else:
                                    full_input_state.record_chunk(
                                        key=key,
                                        project=session_project,
                                        policy_version=full_input_policy_version,
                                        source_hash=source_hash,
                                        map_digest=builder.plan.map_digest,
                                        output_digest=builder.plan.map_digest,
                                        receipt=receipt,
                                    )
                    except Exception as exc:
                        full_input_source_invalid += 1
                        failed += 1
                        status = "failed"
                        failure_reasons[safe_reason(exc)] += 1
                        _write_dead_letter(
                            dead_letter_jsonl,
                            session=session,
                            reason_code=reason,
                            stage="full_input_materialize",
                        )
                elif projection_state_store.has_projected_source_hash(
                    session_project,
                    natural_id,
                    source_hash,
                    extraction_level=EXTRACTION_LEVEL_ENTITY,
                    entity_type="Session",
                ):
                    skipped_resumed += 1
                    status = "skipped_resumed"
                else:
                    episode = session_episode_from_couchdb_source(
                        session_id_hash=session_id_hash,
                        source_store=source_store,
                        artifact_store=artifact_store,
                        extractor_version="couchdb-bulk-semantic.1",
                        max_member_docs=session_budget_docs,
                        deadline_monotonic=session_deadline,
                    )
                    batch_items.append(
                        (
                            session,
                            make_bulk_session_input(
                                session_key=f"s{index}",
                                episode=episode,
                                max_chars=max_session_chars,
                            ),
                        )
                    )
                    materialized += 1
                    status = "queued"
                    if len(batch_items) >= max_sessions_per_call or (
                        max_projects and materialized >= max_projects
                    ):
                        report = _flush_batch_items(
                            batch_items,
                            extractor=extractor,
                            writer=writer,
                            projection_state_store=projection_state_store,
                            projected_cache=projected_cache,
                            dead_letter_jsonl=dead_letter_jsonl,
                            failure_reasons=failure_reasons,
                            allow_empty_sessions=allow_empty_sessions,
                        )
                        batch_items = []
                        llm_batches += int(report["llm_batches"])
                        fallback_single_session_batches += int(report["fallback_single_session_batches"])
                        projected += int(report["projected"])
                        failed += int(report["failed"])
                        entities_written += int(report["entities_written"])
                        relations_written += int(report["relations_written"])
                        if int(report["failed"]):
                            status = "failed"
                            reason = "extract_or_write"
                        else:
                            status = "projected"
            except (
                SourceRevisionDeferredBudget,
                SourceRevisionCurrentnessLimitError,
            ) as exc:
                # Budget/page-cap signals keep the session pending: no partial
                # source is materialized, and other selected sessions continue.
                reason = exc.reason_code or "session_budget_exceeded"
                next_attempt = attempts + 1
                if next_attempt > DEFERRED_MAX_ATTEMPTS:
                    deferred_exhausted += 1
                    failed += 1
                    deferred_updates[session_id_hash] = None
                    _write_dead_letter(
                        dead_letter_jsonl,
                        session=session,
                        reason_code="session_budget_exhausted",
                        stage="materialize_budget",
                        diagnostics={
                            "error_type": type(exc).__name__,
                            "error_param": reason,
                        },
                    )
                    status = "failed"
                    failure_reasons["session_budget_exhausted"] += 1
                else:
                    deferred += 1
                    deferred_updates[session_id_hash] = {
                        "session_id_hash": session_id_hash,
                        "project": session_project,
                        "provider": session_provider,
                        "attempts": next_attempt,
                        "reason_code": reason,
                        "members_total": int(getattr(exc, "members_total", 0) or 0),
                        "members_read": int(getattr(exc, "members_read", 0) or 0),
                    }
                    status = "deferred_budget"
            except Exception as exc:
                failed += 1
                status = "failed"
                reason = safe_reason(exc)
                failure_reasons[reason] += 1
                _write_dead_letter(
                    dead_letter_jsonl,
                    session=session,
                    reason_code=reason,
                    stage="materialize_extract_or_write",
                )

            elapsed_ms = int((time.monotonic() - item_started) * 1000)
            durations.append(elapsed_ms)
            if (
                index == 1
                or index % report_every == 0
                or status in {"failed", "deferred_budget"}
                or index == len(selected)
            ):
                _write_jsonl(
                    progress_jsonl,
                    {
                        "event": "progress",
                        "index": index,
                        "selected": len(selected),
                        "status": status,
                        "reason_code": reason,
                        "elapsed_ms": elapsed_ms,
                        "project_ref": _project_ref(session_project),
                        "provider": session_provider,
                        "projected": projected,
                        "skipped_resumed": skipped_resumed,
                        "failed": failed,
                        "deferred": deferred,
                        "llm_batches": llm_batches,
                    },
                )
            if max_projects and materialized >= max_projects:
                stopped_after_max_projects = True
                break

        if batch_items and (not max_projects or projected < max_projects):
            report = _flush_batch_items(
                batch_items,
                extractor=extractor,
                writer=writer,
                projection_state_store=projection_state_store,
                projected_cache=projected_cache,
                dead_letter_jsonl=dead_letter_jsonl,
                failure_reasons=failure_reasons,
                allow_empty_sessions=allow_empty_sessions,
            )
            llm_batches += int(report["llm_batches"])
            fallback_single_session_batches += int(report["fallback_single_session_batches"])
            projected += int(report["projected"])
            failed += int(report["failed"])
            entities_written += int(report["entities_written"])
            relations_written += int(report["relations_written"])
    finally:
        # Persist inside the runtime lock so a successor cannot read a stale
        # snapshot or race the same temporary file.
        _persist_deferred_records(
            deferred_path,
            deferred_records,
            deferred_updates,
            retried_hashes,
        )
        if lock_handle is not None:
            _release_runtime_lock(lock_handle)

    elapsed_total_ms = int((time.monotonic() - started) * 1000)
    attempted = processed
    status = (
        "ok"
        if failed == 0 and deferred == 0
        else ("partial" if projected or skipped_resumed or deferred else "failed")
    )

    return {
        "schema_version": BULK_SEMANTIC_SCHEMA_VERSION,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "status": status,
        "canonical_counts": {
            "source_sessions": total_available,
            "eligible_sessions": selection.eligible_count,
            "selected_sessions": len(selected),
        },
        "filters": {
            "project_set": bool(project),
            "project_ref": _project_ref(project),
            "provider": provider,
        },
        "limit": int(limit),
        "truncated": bool(
            selection.eligible_count > len(selected)
            or stopped_after_max_projects
        ),
        "target_extraction_level": EXTRACTION_LEVEL_ENTITY,
        "runtime_lock": {
            "enabled": runtime_dir is not None,
            "acquired": runtime_dir is not None,
        },
        "projection": {
            "attempted": attempted,
            "materialized": materialized,
            "projected": projected,
            "skipped_resumed": skipped_resumed,
            "failed": failed,
            "deferred": deferred,
            "deferred_exhausted": deferred_exhausted,
            "deferred_retried": len(priority_sessions),
            "failure_rate": (failed / attempted) if attempted else 0.0,
            "failure_reasons": dict(sorted(failure_reasons.items())),
            "stopped_after_max_projects": stopped_after_max_projects,
        },
        "semantic": {
            "llm_batches": llm_batches,
            "fallback_single_session_batches": fallback_single_session_batches,
            "max_sessions_per_call": max_sessions_per_call,
            "max_session_chars": max_session_chars,
            "entities_written": entities_written,
            "relations_written": relations_written,
            "allow_empty_sessions": bool(allow_empty_sessions),
        },
        "metrics": {
            "avg_ms": int(sum(durations) / len(durations)) if durations else 0,
            "p95_ms": _p95(durations),
            "elapsed_total_ms": elapsed_total_ms,
        },
        "project_count": len(by_project),
        "raw_paths_printed": False,
        "full_input_policy_version": full_input_policy_version,
        "full_input_chunks": full_input_chunks,
        "full_input_source_invalid": full_input_source_invalid,
        "full_input_shadow_groups": sorted(
            {group for meta in full_input_meta.values() for group in meta["shadow_groups"]}
        ),
    }


def _flush_batch_items(
    batch_items: list[tuple[dict[str, Any], Any]],
    *,
    extractor: Any,
    writer: Any,
    projection_state_store: LedgerGraphProjectionStateStore,
    projected_cache: dict[str, dict[str, set[str]]],
    dead_letter_jsonl: Path | None,
    failure_reasons: Counter[str],
    allow_empty_sessions: bool,
) -> dict[str, int]:
    try:
        report = _extract_write_and_mark(
            batch_items,
            extractor=extractor,
            writer=writer,
            projection_state_store=projection_state_store,
            projected_cache=projected_cache,
            allow_empty_sessions=allow_empty_sessions,
        )
    except Exception as exc:
        reason = _reason_code(exc)

        if isinstance(exc, (json.JSONDecodeError, ValueError)) and len(batch_items) > 1:
            return _flush_batch_items_as_singletons(
                batch_items,
                extractor=extractor,
                writer=writer,
                projection_state_store=projection_state_store,
                projected_cache=projected_cache,
                dead_letter_jsonl=dead_letter_jsonl,
                failure_reasons=failure_reasons,
                allow_empty_sessions=allow_empty_sessions,
            )
        failure_reasons[reason] += len(batch_items)
        for session, _item in batch_items:
            _write_dead_letter(
                dead_letter_jsonl,
                session=session,
                reason_code=reason,
                stage="extract_or_write",
            )
        return {
            "projected": 0,
            "failed": len(batch_items),
            "entities_written": 0,
            "relations_written": 0,
            "llm_batches": 1,
            "fallback_single_session_batches": 0,
            "projected_natural_ids": (),
            "chunk_receipts": {},
        }

    return {
        "projected": int(report.projected),
        "failed": 0,
        "entities_written": int(report.entities_written),
        "relations_written": int(report.relations_written),
        "projected_natural_ids": tuple(getattr(report, "projected_natural_ids", ()) or ()),
        "chunk_receipts": dict(getattr(report, "chunk_receipts", {}) or {}),
        "llm_batches": 1,
        "fallback_single_session_batches": 0,
    }


def _flush_batch_items_as_singletons(
    batch_items: list[tuple[dict[str, Any], Any]],
    *,
    extractor: Any,
    writer: Any,
    projection_state_store: LedgerGraphProjectionStateStore,
    projected_cache: dict[str, dict[str, set[str]]],
    dead_letter_jsonl: Path | None,
    failure_reasons: Counter[str],
    allow_empty_sessions: bool,
) -> dict[str, int]:
    projected = failed = entities_written = relations_written = 0
    llm_batches = 1
    receipts: dict[str, str] = {}
    written_ids_acc: set[str] = set()
    for batch_item in batch_items:
        try:
            report = _extract_write_and_mark(
                [batch_item],
                extractor=extractor,
                writer=writer,
                projection_state_store=projection_state_store,
                projected_cache=projected_cache,
                allow_empty_sessions=allow_empty_sessions,
            )
        except Exception as exc:
            reason = _reason_code(exc)

            failure_reasons[reason] += 1
            session, _item = batch_item
            _write_dead_letter(
                dead_letter_jsonl,
                session=session,
                reason_code=reason,
                stage="extract_or_write_singleton_fallback",
            )
            failed += 1
        else:
            projected += int(report.projected)
            entities_written += int(report.entities_written)
            relations_written += int(report.relations_written)
            receipts.update(dict(getattr(report, "chunk_receipts", {}) or {}))
        llm_batches += 1
    return {
        "projected": projected,
        "failed": failed,
        "entities_written": entities_written,
        "relations_written": relations_written,
        "llm_batches": llm_batches,
        "projected_natural_ids": tuple(written_ids_acc),
        "chunk_receipts": receipts,
        "fallback_single_session_batches": len(batch_items),
    }


def _extract_write_and_mark(
    batch_items: list[tuple[dict[str, Any], Any]],
    *,
    extractor: Any,
    writer: Any,
    projection_state_store: LedgerGraphProjectionStateStore,
    projected_cache: dict[str, dict[str, set[str]]],
    allow_empty_sessions: bool,
) -> Any:
    batch_inputs = [item for _session, item in batch_items]
    extraction = extractor.extract(list(batch_inputs))
    report = writer.write_batch(
        list(batch_inputs),
        extraction,
        allow_empty_sessions=allow_empty_sessions,
    )
    written_natural_ids = set(report.projected_natural_ids)
    for item in batch_inputs:
        episode = item.episode
        if episode.natural_id not in written_natural_ids:
            continue
        projection_state_store.mark_projected(
            episode,
            "inserted",
            extraction_level=EXTRACTION_LEVEL_ENTITY,
        )
        project = str(episode.payload.get("project") or "")
        projected_cache.setdefault(project, {}).setdefault(
            episode.natural_id, set()
        ).add(str(episode.payload.get("source_hash") or ""))
    return report


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return int(ordered[index])


def _positive_int_env(value: str, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float_env(value: str, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_deferred_records(path: Path | None) -> dict[str, dict[str, Any]]:
    """Read explicit deferred-session records; unreadable lines stay ignored."""

    if path is None:
        return {}
    records: dict[str, dict[str, Any]] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                session_id_hash = str(record.get("session_id_hash") or "")
                if session_id_hash:
                    records[session_id_hash] = record
    except FileNotFoundError:
        return {}
    return records


def _persist_deferred_records(
    path: Path | None,
    previous: Mapping[str, dict[str, Any]],
    updates: Mapping[str, dict[str, Any] | None],
    retried: set[str],
) -> None:
    """Rewrite the deferred file, dropping entries that resolved or exhausted."""

    if path is None:
        return
    pending: dict[str, dict[str, Any]] = {}
    for session_id_hash, record in previous.items():
        if session_id_hash in retried:
            continue
        pending[session_id_hash] = record
    for session_id_hash, record in updates.items():
        if record is None:
            pending.pop(session_id_hash, None)
            continue
        pending[session_id_hash] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in sorted(pending.values(), key=lambda item: str(item.get("session_id_hash") or "")):
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    temporary.replace(path)


def _reason_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code is None:
        return type(exc).__name__
    try:
        return f"{type(exc).__name__}_{int(code)}"
    except (TypeError, ValueError):
        return type(exc).__name__

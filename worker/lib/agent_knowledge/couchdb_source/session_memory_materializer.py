"""Materialize RetiredIndexBridge session-memory from the CouchDB source plane.

The derived ``session-memory`` document must let normal recall work from RetiredIndexBridge
*alone* (requirement: normal recall does not fetch CouchDB evidence refs). So the
materializer embeds the conversation chunk bodies and the *full* tool evidence
summaries from the bounded bundles into one public-safe session-memory body.

Materialization-loss safety (design): if the store holds fewer conversation
chunks or tool evidence bundles than the coverage manifest expects, the result is
marked not-fully-materialized and projection is refused (fail-closed), keeping
the CouchDB source intact.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..session_memory.chunk_overlap import ChunkView, canonicalize_chunk_views

from .document_model import (
    OwnershipViolation,
    ProjectionStatus,
    RETIRED_INDEX_BRIDGE_RECALL_PROFILE,
    SourceDocType,
    assert_index_target_allowed,
    build_coverage_manifest_document,
    build_projection_state_document,
    build_source_hash,
    build_source_revision_token,
    coverage_manifest_doc_id,
    observed_time_bounds,
    projection_state_doc_id,
    sha256_hash,
)
from .source_store import CouchDBSourceStore, SourceStoreConflict, SourceStoreError


@dataclass(frozen=True)
class MaterializedSessionMemory:
    session_id_hash: str
    provider: str
    project: str
    target_profile: str
    body: str
    content_hash: str
    conversation_chunk_count: int
    tool_evidence_bundle_count: int
    fully_materialized: bool
    source_hash: str = ""
    observed_at_start: str = ""
    observed_at_end: str = ""
    materialized_at: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_projection_document(self) -> dict:
        return {
            "target_profile": self.target_profile,
            "session_id_hash": self.session_id_hash,
            "provider": self.provider,
            "project": self.project,
            "body": self.body,
            "content_hash": self.content_hash,
            "conversation_chunk_count": self.conversation_chunk_count,
            "tool_evidence_bundle_count": self.tool_evidence_bundle_count,
            "source_hash": self.source_hash,
            "observed_at_start": self.observed_at_start,
            "observed_at_end": self.observed_at_end,
            "materialized_at": self.materialized_at,
        }


def upsert_transcript_session_aggregate(
    *,
    store: CouchDBSourceStore,
    incoming: dict,
):
    """Merge one live chunk's session envelope without erasing derived state."""

    return store.merge_transcript_session_aggregate(incoming=incoming)


@runtime_checkable
class SessionMemoryProjector(Protocol):
    def project(self, *, target_profile: str, document: dict) -> str: ...


@runtime_checkable
class QdrantMirrorSink(Protocol):
    """Best-effort forward sink into the Qdrant searchable mirror.

    Called from the projection SUCCESS path AFTER the canonical projection_state is
    committed. A sink failure must NEVER break the canonical projection (it is
    wrapped in try/except by the caller). The same build path the offline backfill
    uses, so a forward-mirrored point and a backfilled point share a deterministic
    point_id (idempotent across both paths).
    """

    def submit(
        self,
        *,
        session_id_hash: str,
        provider: str,
        project: str,
        content_hash: str,
        body: str,
        memory_id: str,
    ) -> None: ...


class RecordingSessionMemoryProjector:
    """Test/dry-run projector. Records calls and rejects non-session-memory targets."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def project(self, *, target_profile: str, document: dict) -> str:
        assert_index_target_allowed(target_profile)  # transcript-memory rejected
        ref = "session-memory-ref-" + str(document.get("content_hash", "")).split(":")[-1][:12]
        self.calls.append({"target_profile": target_profile, "content_hash": document.get("content_hash")})
        return ref


def _session_docs(store: CouchDBSourceStore, session_id_hash: str, doc_type: str) -> list[dict]:
    return store.find_by_session(session_id_hash=session_id_hash, doc_type=doc_type)


def update_coverage_with_tool_evidence(*, session_id_hash: str, store: CouchDBSourceStore) -> dict | None:
    """Rebuild the per-session coverage manifest with current tool evidence counts.

    Preserves the project_authority / ledger_comparison blocks from the existing
    manifest written during historical import.
    """

    for _attempt in range(3):
        snapshot = _coverage_snapshot(session_id_hash=session_id_hash, store=store)
        if snapshot is None:
            return None
        doc, sessions, observed_at_start, observed_at_end = snapshot
        store.put(doc)
        if sessions:
            session_doc = dict(sessions[0])
            session_doc.update(
                {
                    "started_at": observed_at_start
                    or str(session_doc.get("started_at") or ""),
                    "ended_at": observed_at_end or str(session_doc.get("ended_at") or ""),
                    "observed_at_start": observed_at_start,
                    "observed_at_end": observed_at_end,
                    "source_hash": str(doc.get("source_hash") or ""),
                }
            )
            store.merge_transcript_session_aggregate(
                incoming=session_doc,
                source_hash_authoritative=True,
            )

        # CouchDB's generic 409 retry can legally write a stale aggregate whose
        # revision was computed before a concurrent distinct chunk arrived. Read
        # the complete source again and converge only when the persisted manifest
        # and session row cover that latest snapshot.
        current = _coverage_snapshot(session_id_hash=session_id_hash, store=store)
        if current is None:
            continue
        current_doc, current_sessions, _current_start, _current_end = current
        persisted = store.get(coverage_manifest_doc_id(session_id_hash)) or {}
        session_current = current_sessions[0] if current_sessions else {}
        expected_hash = str(current_doc.get("source_hash") or "")
        if (
            str(persisted.get("source_hash") or "") == expected_hash
            and int(persisted.get("conversation_chunk_count") or 0)
            == int(current_doc.get("conversation_chunk_count") or 0)
            and int(persisted.get("tool_evidence_bundle_count") or 0)
            == int(current_doc.get("tool_evidence_bundle_count") or 0)
            and (not current_sessions or str(session_current.get("source_hash") or "") == expected_hash)
        ):
            return current_doc
    raise SourceStoreError("coverage manifest did not converge after concurrent source updates")


def _coverage_snapshot(
    *, session_id_hash: str, store: CouchDBSourceStore
) -> tuple[dict, list[dict], str, str] | None:
    sessions = _session_docs(store, session_id_hash, SourceDocType.TRANSCRIPT_SESSION)
    chunks = _session_docs(store, session_id_hash, SourceDocType.CONVERSATION_CHUNK)
    bundles = _session_docs(store, session_id_hash, SourceDocType.TOOL_EVIDENCE_BUNDLE)
    existing = store.get(coverage_manifest_doc_id(session_id_hash))
    if existing is None and not chunks and not bundles:
        return None

    anchor = existing or (chunks[0] if chunks else (bundles[0] if bundles else {}))
    observed_at_start, observed_at_end = _observed_bounds(
        sessions=sessions,
        chunks=chunks,
        bundles=bundles,
    )
    doc = build_coverage_manifest_document(
        session_id_hash=session_id_hash,
        provider=str(anchor.get("provider") or ""),
        project=str(anchor.get("project") or ""),
        conversation_chunk_count=len(chunks),
        tool_evidence_bundle_count=len(bundles),
        conversation_content_hashes=[c.get("content_hash", "") for c in chunks],
        tool_evidence_coverage_hashes=[b.get("coverage_hash", "") for b in bundles],
        source_locator_hash=str(anchor.get("source_locator_hash") or ""),
        ledger_comparison=(existing or {}).get("ledger_comparison"),
        project_authority=(existing or {}).get("project_authority"),
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        conversation_revision_tokens=[
            build_source_revision_token(chunk, material_hash_field="content_hash")
            for chunk in chunks
        ],
        tool_evidence_revision_tokens=[
            build_source_revision_token(bundle, material_hash_field="content_hash")
            for bundle in bundles
        ],
    )
    return doc, sessions, observed_at_start, observed_at_end


def _observed_bounds(
    *,
    sessions: list[dict],
    chunks: list[dict],
    bundles: list[dict] | None = None,
) -> tuple[str, str]:
    return observed_time_bounds(
        sessions=sessions,
        chunks=[*chunks, *(bundles or [])],
    )


def mark_projection_pending_if_source_changed(
    *,
    session_id_hash: str,
    provider: str,
    project: str,
    source_hash: str,
    store: CouchDBSourceStore,
    source_changed: bool,
) -> dict:
    del source_changed
    state_id = projection_state_doc_id(session_id_hash)
    for _attempt in range(3):
        existing = store.get(state_id)
        current_source_hash = _current_session_source_hash(
            session_id_hash=session_id_hash,
            store=store,
        )
        if current_source_hash and source_hash != current_source_hash:
            return existing or {}
        if (
            existing is not None
            and str(existing.get("projection_status") or "") == ProjectionStatus.PROJECTED
            and source_hash
            and str(existing.get("projected_source_hash") or "") == source_hash
        ):
            return existing
        if (
            existing is not None
            and str(existing.get("projection_status") or "") == ProjectionStatus.PENDING
            and source_hash
            and str(existing.get("source_hash") or "") == source_hash
        ):
            return existing
        projected_source_hash = str((existing or {}).get("projected_source_hash") or "")
        if (
            not projected_source_hash
            and str((existing or {}).get("projection_status") or "")
            == ProjectionStatus.PROJECTED
        ):
            projected_source_hash = str((existing or {}).get("source_hash") or "")
        state = build_projection_state_document(
            session_id_hash=session_id_hash,
            provider=provider,
            project=project,
            projection_status=ProjectionStatus.PENDING,
            session_memory_knowledge_id=str(
                (existing or {}).get("session_memory_knowledge_id") or ""
            ),
            active_content_hash=str((existing or {}).get("active_content_hash") or ""),
            source_hash=source_hash,
            projected_source_hash=projected_source_hash,
            materialized_at=str((existing or {}).get("materialized_at") or ""),
        )
        try:
            store.put_if_revision(
                state,
                expected_rev=str((existing or {}).get("_rev") or ""),
            )
        except SourceStoreConflict:
            continue
        return store.get(state_id) or state
    raise SourceStoreConflict("projection pending conflict retry exhausted")


def _chunk_to_view(chunk: dict) -> ChunkView:
    # `... or <default>` (not get's default) so a present-but-None field coerces to the
    # default rather than the string "None"/an int error.
    return ChunkView(
        content_hash=str(chunk.get("content_hash") or ""),
        turn_start_index=int(chunk.get("turn_start_index") or 0),
        turn_end_index=int(chunk.get("turn_end_index") or 0),
        part_index=int(chunk.get("part_index") or 1),
        part_count=int(chunk.get("part_count") or 1),
        char_start=int(chunk.get("char_start") or 0),
        char_end=int(chunk.get("char_end") or 0),
        redaction_version=str(chunk.get("redaction_version") or ""),
        text=str(chunk.get("body") or ""),
    )


def materialize_session_memory(*, session_id_hash: str, store: CouchDBSourceStore) -> MaterializedSessionMemory:
    sessions = _session_docs(store, session_id_hash, SourceDocType.TRANSCRIPT_SESSION)
    chunks = _session_docs(store, session_id_hash, SourceDocType.CONVERSATION_CHUNK)
    bundles = _session_docs(store, session_id_hash, SourceDocType.TOOL_EVIDENCE_BUNDLE)
    coverage = store.get(coverage_manifest_doc_id(session_id_hash))

    provider = sessions[0].get("provider", "") if sessions else (chunks[0].get("provider", "") if chunks else "")
    project = sessions[0].get("project", "") if sessions else (chunks[0].get("project", "") if chunks else "")

    chunks = sorted(chunks, key=lambda d: (d.get("turn_start_index", 0), d.get("_id", "")))
    bundles = sorted(bundles, key=lambda d: d.get("part_index", 0))

    # De-overlap same-session chunks for the body only: a re-shipped grown session can
    # store a longer chunk that subsumes an earlier shorter one. Counts/coverage below
    # stay on the STORED `chunks`, so the coverage gate is unaffected.
    body_views, overlap_report = canonicalize_chunk_views([_chunk_to_view(chunk) for chunk in chunks])

    lines = [
        f"# session-memory {provider} {project}",
        f"session_id_hash: {session_id_hash}",
        "",
        "## conversation",
    ]
    for view in body_views:
        lines.append("")
        lines.append(view.text.rstrip())
    lines.append("")
    lines.append("## tool_evidence_summary")
    for bundle in bundles:
        # Full materialization: embed the bundle body so recall needs no CouchDB ref.
        lines.append("")
        lines.append(str(bundle.get("body", "")).rstrip())
    body = "\n".join(lines).rstrip() + "\n"

    notes: list[str] = []
    if overlap_report["dropped_count"]:
        # Audit counter: how many stored chunks were de-overlapped out of the body
        # (subsumed/exact-dup). Counts/coverage below stay on the stored chunk list.
        notes.append(f"deoverlapped_{overlap_report['dropped_count']}")
    fully_materialized = True
    if coverage is not None:
        expected_chunks = int(coverage.get("conversation_chunk_count", 0))
        expected_bundles = int(coverage.get("tool_evidence_bundle_count", 0))
        if len(chunks) < expected_chunks or len(bundles) < expected_bundles:
            fully_materialized = False
            notes.append("materialization_loss")
    else:
        notes.append("no_coverage_manifest")
    if not chunks:
        fully_materialized = False
        notes.append("no_conversation_source")

    observed_at_start, observed_at_end = _observed_bounds(
        sessions=sessions,
        chunks=chunks,
        bundles=bundles,
    )
    source_hash = build_source_hash(
        [str(chunk.get("content_hash") or "") for chunk in chunks],
        [str(bundle.get("coverage_hash") or "") for bundle in bundles],
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        conversation_revision_tokens=[
            build_source_revision_token(chunk, material_hash_field="content_hash")
            for chunk in chunks
        ],
        tool_evidence_revision_tokens=[
            build_source_revision_token(bundle, material_hash_field="content_hash")
            for bundle in bundles
        ],
    )
    coverage_source_hash = str((coverage or {}).get("source_hash") or "")
    if coverage_source_hash and coverage_source_hash != source_hash:
        notes.append("coverage_source_hash_mismatch")

    return MaterializedSessionMemory(
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE,
        body=body,
        content_hash=sha256_hash(body),
        conversation_chunk_count=len(chunks),
        tool_evidence_bundle_count=len(bundles),
        fully_materialized=fully_materialized,
        source_hash=source_hash,
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        materialized_at=datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        notes=tuple(notes),
    )


def _current_session_source_hash(
    *, session_id_hash: str, store: CouchDBSourceStore
) -> str:
    snapshot = _coverage_snapshot(session_id_hash=session_id_hash, store=store)
    if snapshot is None:
        return ""
    return str(snapshot[0].get("source_hash") or "")


def _projection_state_for_materialization(
    *,
    materialized: MaterializedSessionMemory,
    existing: dict,
    projection_status: str,
    failure_reason: str = "",
    ref: str = "",
) -> dict:
    projected_source_hash = str(existing.get("projected_source_hash") or "")
    if (
        not projected_source_hash
        and str(existing.get("projection_status") or "") == ProjectionStatus.PROJECTED
    ):
        projected_source_hash = str(existing.get("source_hash") or "")
    if projection_status == ProjectionStatus.PROJECTED:
        return build_projection_state_document(
            session_id_hash=materialized.session_id_hash,
            provider=materialized.provider,
            project=materialized.project,
            projection_status=projection_status,
            session_memory_knowledge_id=ref,
            active_content_hash=materialized.content_hash,
            source_hash=materialized.source_hash,
            projected_source_hash=materialized.source_hash,
            materialized_at=materialized.materialized_at,
        )
    return build_projection_state_document(
        session_id_hash=materialized.session_id_hash,
        provider=materialized.provider,
        project=materialized.project,
        projection_status=projection_status,
        failure_reason=failure_reason,
        source_hash=materialized.source_hash,
        projected_source_hash=projected_source_hash,
    )


def _commit_projection_state_if_source_current(
    *,
    materialized: MaterializedSessionMemory,
    store: CouchDBSourceStore,
    projection_status: str,
    failure_reason: str = "",
    ref: str = "",
) -> tuple[str, dict]:
    state_id = projection_state_doc_id(materialized.session_id_hash)
    for _attempt in range(3):
        if (
            _current_session_source_hash(
                session_id_hash=materialized.session_id_hash,
                store=store,
            )
            != materialized.source_hash
        ):
            return "source_revision_changed", store.get(state_id) or {}
        existing = store.get(state_id) or {}
        if (
            str(existing.get("projection_status") or "") == ProjectionStatus.PROJECTED
            and str(existing.get("projected_source_hash") or "")
            == materialized.source_hash
        ):
            return "already_projected", existing
        state = _projection_state_for_materialization(
            materialized=materialized,
            existing=existing,
            projection_status=projection_status,
            failure_reason=failure_reason,
            ref=ref,
        )
        try:
            store.put_if_revision(
                state,
                expected_rev=str(existing.get("_rev") or ""),
            )
        except SourceStoreConflict:
            continue
        return "written", store.get(state_id) or state
    raise SourceStoreConflict("projection state conflict retry exhausted")


def _latest_materialized_at(existing: object, incoming: object) -> str:
    values = [str(value or "") for value in (existing, incoming) if str(value or "")]
    if len(values) < 2:
        return values[0] if values else ""
    parsed: list[tuple[datetime.datetime, str]] = []
    for value in values:
        try:
            timestamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
        parsed.append((timestamp.astimezone(datetime.timezone.utc), value))
    if parsed:
        return max(parsed)[1]
    return max(values)


def _update_session_materialization_if_source_current(
    *, materialized: MaterializedSessionMemory, store: CouchDBSourceStore
) -> bool:
    for _attempt in range(3):
        if (
            _current_session_source_hash(
                session_id_hash=materialized.session_id_hash,
                store=store,
            )
            != materialized.source_hash
        ):
            return False
        sessions = _session_docs(
            store,
            materialized.session_id_hash,
            SourceDocType.TRANSCRIPT_SESSION,
        )
        if not sessions:
            return False
        current = sessions[0]
        updated = dict(current)
        updated.update(
            {
                "source_hash": materialized.source_hash,
                "observed_at_start": materialized.observed_at_start,
                "observed_at_end": materialized.observed_at_end,
                "materialized_at": _latest_materialized_at(
                    current.get("materialized_at"), materialized.materialized_at
                ),
            }
        )
        try:
            store.put_if_revision(
                updated,
                expected_rev=str(current.get("_rev") or ""),
            )
        except SourceStoreConflict:
            continue
        return True
    raise SourceStoreConflict("session materialization conflict retry exhausted")


def project_session_memory(
    *,
    materialized: MaterializedSessionMemory,
    store: CouchDBSourceStore,
    projector: SessionMemoryProjector,
    mirror_sink: "QdrantMirrorSink | None" = None,
) -> dict:
    """Project a materialized session-memory to RetiredIndexBridge, recording projection_state.

    Fail-closed: a not-fully-materialized session is never projected. A projector
    error leaves the CouchDB source intact and records a failed projection_state.

    ``mirror_sink`` (optional) forwards the just-projected body into the Qdrant
    searchable mirror, best-effort: it is called only on the SUCCESS path, only
    AFTER the canonical projection_state has been committed, and any exception it
    raises is swallowed so a mirror failure can never break the canonical
    projection.
    """

    if not materialized.fully_materialized:
        commit_status, _state = _commit_projection_state_if_source_current(
            materialized=materialized,
            store=store,
            projection_status=ProjectionStatus.FAILED,
            failure_reason="materialization_loss",
        )
        result = {
            "status": ProjectionStatus.FAILED,
            "reason": "materialization_loss",
            "ref": "",
        }
        if commit_status == "source_revision_changed":
            result["state_write_skipped"] = commit_status
        return result

    try:
        ref = projector.project(
            target_profile=materialized.target_profile,
            document=materialized.to_projection_document(),
        )
    except OwnershipViolation:
        raise
    except Exception as exc:  # backend/projection failure -> keep source, mark failed
        commit_status, _state = _commit_projection_state_if_source_current(
            materialized=materialized,
            store=store,
            projection_status=ProjectionStatus.FAILED,
            failure_reason=type(exc).__name__,
        )
        result = {
            "status": ProjectionStatus.FAILED,
            "reason": type(exc).__name__,
            "ref": "",
        }
        if commit_status == "source_revision_changed":
            result["state_write_skipped"] = commit_status
        return result

    commit_status, _state = _commit_projection_state_if_source_current(
        materialized=materialized,
        store=store,
        projection_status=ProjectionStatus.PROJECTED,
        ref=ref,
    )
    if commit_status == "source_revision_changed":
        return {
            "status": ProjectionStatus.FAILED,
            "reason": "source_revision_changed",
            "ref": "",
            "state_write_skipped": commit_status,
        }
    _update_session_materialization_if_source_current(
        materialized=materialized,
        store=store,
    )

    # Best-effort forward mirror: only after the canonical state is committed, and
    # never allowed to break the projection. mirror_failed is reported for
    # observability but does not change the canonical PROJECTED outcome.
    mirror_failed = False
    if mirror_sink is not None:
        try:
            mirror_sink.submit(
                session_id_hash=materialized.session_id_hash,
                provider=materialized.provider,
                project=materialized.project,
                content_hash=materialized.content_hash,
                body=materialized.body,
            )
        except Exception as exc:  # best-effort: canonical projection already committed
            mirror_failed = True
            # Redaction-safe observability: exception type only (no message/payload),
            # so a silently-failing forward mirror is debuggable in operation.
            logging.getLogger(__name__).warning(
                "forward mirror submit failed: %s", type(exc).__name__
            )

    return {
        "status": ProjectionStatus.PROJECTED,
        "reason": "",
        "ref": ref,
        "mirror_failed": mirror_failed,
    }


def materialize_and_project(
    *,
    session_id_hash: str,
    store: CouchDBSourceStore,
    projector: SessionMemoryProjector | None = None,
    mirror_sink: "QdrantMirrorSink | None" = None,
) -> dict:
    """End-to-end M3 step for one session: refresh coverage, materialize, project."""

    update_coverage_with_tool_evidence(session_id_hash=session_id_hash, store=store)
    materialized = materialize_session_memory(session_id_hash=session_id_hash, store=store)
    projection = None
    if projector is not None:
        projection = project_session_memory(
            materialized=materialized, store=store, projector=projector, mirror_sink=mirror_sink
        )
    return {
        "session_id_hash": session_id_hash,
        "fully_materialized": materialized.fully_materialized,
        "conversation_chunk_count": materialized.conversation_chunk_count,
        "tool_evidence_bundle_count": materialized.tool_evidence_bundle_count,
        "content_hash": materialized.content_hash,
        "source_hash": materialized.source_hash,
        "observed_at_start": materialized.observed_at_start,
        "observed_at_end": materialized.observed_at_end,
        "materialized_at": materialized.materialized_at,
        "notes": list(materialized.notes),
        "projection": projection,
    }


__all__ = [
    "MaterializedSessionMemory",
    "SessionMemoryProjector",
    "QdrantMirrorSink",
    "RecordingSessionMemoryProjector",
    "update_coverage_with_tool_evidence",
    "mark_projection_pending_if_source_changed",
    "materialize_session_memory",
    "project_session_memory",
    "materialize_and_project",
]

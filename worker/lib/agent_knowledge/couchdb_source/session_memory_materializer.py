"""Materialize RAGFlow session-memory from the CouchDB source plane.

The derived ``session-memory`` document must let normal recall work from RAGFlow
*alone* (requirement: normal recall does not fetch CouchDB evidence refs). So the
materializer embeds the conversation chunk bodies and the *full* tool evidence
summaries from the bounded bundles into one public-safe session-memory body.

Materialization-loss safety (design): if the store holds fewer conversation
chunks or tool evidence bundles than the coverage manifest expects, the result is
marked not-fully-materialized and projection is refused (fail-closed), keeping
the CouchDB source intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .document_model import (
    OwnershipViolation,
    ProjectionStatus,
    RAGFLOW_RECALL_PROFILE,
    SourceDocType,
    assert_ragflow_target_allowed,
    build_coverage_hash,
    build_coverage_manifest_document,
    build_projection_state_document,
    coverage_manifest_doc_id,
    sha256_hash,
)
from .source_store import CouchDBSourceStore


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
        }


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
        assert_ragflow_target_allowed(target_profile)  # transcript-memory rejected
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

    chunks = _session_docs(store, session_id_hash, SourceDocType.CONVERSATION_CHUNK)
    bundles = _session_docs(store, session_id_hash, SourceDocType.TOOL_EVIDENCE_BUNDLE)
    existing = store.get(coverage_manifest_doc_id(session_id_hash))
    if existing is None and not chunks and not bundles:
        return None

    provider = ""
    project = ""
    source_locator_hash = ""
    if existing is not None:
        provider = existing.get("provider", "")
        project = existing.get("project", "")
        source_locator_hash = existing.get("source_locator_hash", "")
    elif chunks:
        provider = chunks[0].get("provider", "")
        project = chunks[0].get("project", "")
        source_locator_hash = chunks[0].get("source_locator_hash", "")

    doc = build_coverage_manifest_document(
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        conversation_chunk_count=len(chunks),
        tool_evidence_bundle_count=len(bundles),
        conversation_content_hashes=[c.get("content_hash", "") for c in chunks],
        tool_evidence_coverage_hashes=[b.get("coverage_hash", "") for b in bundles],
        source_locator_hash=source_locator_hash,
        ledger_comparison=(existing or {}).get("ledger_comparison"),
        project_authority=(existing or {}).get("project_authority"),
    )
    store.put(doc)
    return doc


def materialize_session_memory(*, session_id_hash: str, store: CouchDBSourceStore) -> MaterializedSessionMemory:
    sessions = _session_docs(store, session_id_hash, SourceDocType.TRANSCRIPT_SESSION)
    chunks = _session_docs(store, session_id_hash, SourceDocType.CONVERSATION_CHUNK)
    bundles = _session_docs(store, session_id_hash, SourceDocType.TOOL_EVIDENCE_BUNDLE)
    coverage = store.get(coverage_manifest_doc_id(session_id_hash))

    provider = sessions[0].get("provider", "") if sessions else (chunks[0].get("provider", "") if chunks else "")
    project = sessions[0].get("project", "") if sessions else (chunks[0].get("project", "") if chunks else "")

    chunks = sorted(chunks, key=lambda d: (d.get("turn_start_index", 0), d.get("_id", "")))
    bundles = sorted(bundles, key=lambda d: d.get("part_index", 0))

    lines = [
        f"# session-memory {provider} {project}",
        f"session_id_hash: {session_id_hash}",
        "",
        "## conversation",
    ]
    for chunk in chunks:
        lines.append("")
        lines.append(str(chunk.get("body", "")).rstrip())
    lines.append("")
    lines.append("## tool_evidence_summary")
    for bundle in bundles:
        # Full materialization: embed the bundle body so recall needs no CouchDB ref.
        lines.append("")
        lines.append(str(bundle.get("body", "")).rstrip())
    body = "\n".join(lines).rstrip() + "\n"

    notes: list[str] = []
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

    return MaterializedSessionMemory(
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        target_profile=RAGFLOW_RECALL_PROFILE,
        body=body,
        content_hash=sha256_hash(body),
        conversation_chunk_count=len(chunks),
        tool_evidence_bundle_count=len(bundles),
        fully_materialized=fully_materialized,
        notes=tuple(notes),
    )


def project_session_memory(
    *,
    materialized: MaterializedSessionMemory,
    store: CouchDBSourceStore,
    projector: SessionMemoryProjector,
    mirror_sink: "QdrantMirrorSink | None" = None,
) -> dict:
    """Project a materialized session-memory to RAGFlow, recording projection_state.

    Fail-closed: a not-fully-materialized session is never projected. A projector
    error leaves the CouchDB source intact and records a failed projection_state.

    ``mirror_sink`` (optional) forwards the just-projected body into the Qdrant
    searchable mirror, best-effort: it is called only on the SUCCESS path, only
    AFTER the canonical projection_state has been committed, and any exception it
    raises is swallowed so a mirror failure can never break the canonical
    projection.
    """

    if not materialized.fully_materialized:
        state = build_projection_state_document(
            session_id_hash=materialized.session_id_hash,
            provider=materialized.provider,
            project=materialized.project,
            projection_status=ProjectionStatus.FAILED,
            failure_reason="materialization_loss",
        )
        store.put(state)
        return {"status": ProjectionStatus.FAILED, "reason": "materialization_loss", "ref": ""}

    try:
        ref = projector.project(
            target_profile=materialized.target_profile,
            document=materialized.to_projection_document(),
        )
    except OwnershipViolation:
        raise
    except Exception as exc:  # backend/projection failure -> keep source, mark failed
        state = build_projection_state_document(
            session_id_hash=materialized.session_id_hash,
            provider=materialized.provider,
            project=materialized.project,
            projection_status=ProjectionStatus.FAILED,
            failure_reason=type(exc).__name__,
        )
        store.put(state)
        return {"status": ProjectionStatus.FAILED, "reason": type(exc).__name__, "ref": ""}

    state = build_projection_state_document(
        session_id_hash=materialized.session_id_hash,
        provider=materialized.provider,
        project=materialized.project,
        projection_status=ProjectionStatus.PROJECTED,
        session_memory_knowledge_id=ref,
        active_content_hash=materialized.content_hash,
    )
    store.put(state)

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
                memory_id=ref,
            )
        except Exception:  # best-effort: canonical projection already committed
            mirror_failed = True

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
        "notes": list(materialized.notes),
        "projection": projection,
    }


__all__ = [
    "MaterializedSessionMemory",
    "SessionMemoryProjector",
    "QdrantMirrorSink",
    "RecordingSessionMemoryProjector",
    "update_coverage_with_tool_evidence",
    "materialize_session_memory",
    "project_session_memory",
    "materialize_and_project",
]

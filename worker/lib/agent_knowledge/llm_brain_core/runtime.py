from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_knowledge.couchdb_source.document_model import SourceDocType
from agent_knowledge.couchdb_source.source_store import CouchDBSourceStore

from ._util import hash_payload, public_safe_text, require_non_empty, require_sha256, short_hash, utc_now_iso
from .artifact_store import SessionMemoryArtifactStore
from .context import BrainReadService
from .document_bridge import DocumentBridge
from .event_replay import BrainEventReplayStore
from .graph import GraphMemoryAdapter, NullGraphMemoryAdapter
from .models import BrainEventEnvelope, OntologyEpisode, SessionMemoryArtifact, SourceRefRecord
from .source_ref import SourceRefCatalog, SourceRefResolver


def materialize_artifact_from_couchdb_source(
    *,
    session_id_hash: str,
    source_store: CouchDBSourceStore,
    artifact_store: SessionMemoryArtifactStore | None = None,
    ontology_version: str = "1.0.0",
    extractor_version: str = "runtime.1",
) -> SessionMemoryArtifact:
    """Build a core artifact from CouchDB source docs without copying source bodies."""

    sessions = source_store.find_by_session(
        session_id_hash=session_id_hash,
        doc_type=SourceDocType.TRANSCRIPT_SESSION,
    )
    chunks = source_store.find_by_session(
        session_id_hash=session_id_hash,
        doc_type=SourceDocType.CONVERSATION_CHUNK,
    )
    evidence = source_store.find_by_session(
        session_id_hash=session_id_hash,
        doc_type=SourceDocType.TOOL_EVIDENCE_BUNDLE,
    )
    if not sessions and not chunks:
        raise ValueError("session source docs are required")
    provider = str((sessions[0] if sessions else chunks[0]).get("provider") or "")
    project = str((sessions[0] if sessions else chunks[0]).get("project") or "")
    _validate_session_doc_scope(sessions + chunks + evidence, provider=provider, project=project)
    chunks = sorted(chunks, key=lambda doc: (doc.get("turn_start_index", 0), doc.get("_id", "")))
    evidence = sorted(evidence, key=lambda doc: (doc.get("part_index", 0), doc.get("_id", "")))
    summary = public_safe_text(
        " ".join(
            [
                f"Session artifact for {provider}/{project}.",
                f"conversation_chunks={len(chunks)}.",
                f"tool_evidence_bundles={len(evidence)}.",
                _latest_chunk_hint(chunks),
                _latest_evidence_hint(evidence),
            ]
        ),
        max_chars=1024,
    )
    artifact = SessionMemoryArtifact.from_summary(
        session_id_hash=session_id_hash,
        project=project,
        provider=provider,
        summary=summary,
        source_event_ids=[_source_event_id(doc) for doc in sessions + chunks + evidence],
        chunk_refs=[str(doc.get("_id") or "") for doc in chunks],
        tool_evidence_refs=[str(doc.get("_id") or "") for doc in evidence],
        ontology_version=ontology_version,
        extractor_version=extractor_version,
        created_at=str((sessions[0] if sessions else chunks[0]).get("started_at") or utc_now_iso()),
    )
    if artifact_store is not None:
        artifact_store.upsert(artifact)
    return artifact


def brain_event_from_ingress_payload(
    payload: Mapping[str, Any],
    *,
    event_id: str = "",
    device_id_hash: str = "",
    occurred_at: str = "",
    observed_at: str = "",
    event_type: str = "ingress_event",
    tombstone: bool = False,
) -> BrainEventEnvelope:
    """Map existing queue/event payload shape into the core replay envelope."""

    normalized = dict(payload)
    payload_hash = str(
        normalized.get("contentHash")
        or normalized.get("content_hash")
        or normalized.get("payload_hash")
        or hash_payload(normalized)
    )
    require_sha256(payload_hash, "payload_hash")
    key = str(normalized.get("idempotencyKey") or normalized.get("idempotency_key") or "")
    if not key:
        key = f"brain-event:{short_hash([payload_hash, event_type])}"
    source_event_id = event_id or str(
        normalized.get("eventId")
        or normalized.get("event_id")
        or normalized.get("source_event_id")
        or f"evt:{short_hash([key, payload_hash])}"
    )
    event_payload = _public_event_payload(normalized, payload_hash=payload_hash)
    return BrainEventEnvelope.from_payload(
        event_id=source_event_id,
        idempotency_key=key,
        device_id_hash=device_id_hash or str(normalized.get("device_id_hash") or ""),
        event_type=event_type,
        occurred_at=occurred_at or str(normalized.get("occurredAt") or normalized.get("occurred_at") or utc_now_iso()),
        observed_at=observed_at or str(normalized.get("observedAt") or normalized.get("observed_at") or utc_now_iso()),
        payload=event_payload,
        tombstone=tombstone or bool(normalized.get("tombstone", False)),
    )


def source_ref_from_catalog_event(event: Mapping[str, Any]) -> SourceRefRecord:
    device_id_hash = str(event.get("device_id_hash") or event.get("deviceIdHash") or "")
    relative_path_hash = str(event.get("relative_path_hash") or event.get("relativePathHash") or "")
    content_hash = str(event.get("content_hash") or event.get("contentHash") or "")
    root_id = str(event.get("root_id") or event.get("rootId") or "project-root")
    source_ref_id = str(event.get("source_ref_id") or event.get("sourceRefId") or "")
    if not source_ref_id:
        source_ref_id = f"src_{short_hash([device_id_hash, root_id, relative_path_hash, content_hash])}"
    return SourceRefRecord(
        source_ref_id=source_ref_id,
        device_id_hash=device_id_hash,
        root_id=root_id,
        relative_path_hash=relative_path_hash,
        content_hash=content_hash,
        mtime=str(event.get("mtime") or event.get("modifiedAt") or ""),
        size=_safe_size(event.get("size")),
        sync_policy=event.get("sync_policy") or event.get("syncPolicy") or "metadata_only",
        permission_scope=str(event.get("permission_scope") or event.get("permissionScope") or "project"),
        last_seen_at=str(event.get("last_seen_at") or event.get("lastSeenAt") or utc_now_iso()),
        deleted_at=str(event.get("deleted_at") or event.get("deletedAt") or ""),
        revoked_at=str(event.get("revoked_at") or event.get("revokedAt") or ""),
        derived_summary=str(event.get("derived_summary") or event.get("derivedSummary") or ""),
        redacted_content=str(event.get("redacted_content") or event.get("redactedContent") or ""),
    )


def episode_from_memory_card(card: Mapping[str, Any]) -> OntologyEpisode:
    payload = {
        "memory_id": str(card.get("memory_id") or ""),
        "brain_id": str(card.get("brain_id") or ""),
        "project": str(card.get("project") or ""),
        "card_type": str(card.get("card_type") or ""),
        "title": public_safe_text(str(card.get("title") or ""), max_chars=240),
        "summary": public_safe_text(str(card.get("summary") or ""), max_chars=512),
        "typed_payload": dict(card.get("typed_payload") or {}),
    }
    memory_id = require_non_empty(payload["memory_id"], "memory_id")
    card_type = require_non_empty(payload["card_type"], "card_type")
    # brain_id is the graph group key. A missing brain_id silently breaks
    # group_ids scoping, so fail fast instead of projecting an ungrouped episode.
    payload["brain_id"] = require_non_empty(payload["brain_id"], "brain_id")
    natural_id = f"{card_type}:{memory_id}"
    entity_type = _entity_type_for_card(payload["card_type"])
    return OntologyEpisode.from_payload(
        event_id=f"evt:{short_hash([natural_id, card.get('content_hash', '')])}",
        entity_type=entity_type,
        natural_id=natural_id,
        payload=payload,
        lifecycle_state=str(card.get("lifecycle_state") or "accepted"),
        currentness=str(card.get("currentness") or "unknown"),
        source_event_ids=tuple(str(ref) for ref in _list_or_empty(card.get("derived_from"))),
        source_ref_ids=tuple(_source_ref_ids(card)),
        observed_at=str(card.get("approved_at") or card.get("accepted_at") or card.get("updated_at") or utc_now_iso()),
        ontology_version=str(card.get("ontology_version") or "1.0.0"),
        extractor_version="memory-card-runtime.1",
    )


def build_runtime_brain_service(
    *,
    project: str,
    artifact_store: SessionMemoryArtifactStore,
    read_model: Any | None = None,
    source_catalog: SourceRefCatalog | Any | None = None,
    graph_adapter: GraphMemoryAdapter | None = None,
    document_bridge: DocumentBridge | None = None,
    card_limit: int = 100,
) -> BrainReadService:
    cards = []
    if read_model is not None:
        cards = read_model.list_accepted_cards(project=project, limit=card_limit)
    resolver = source_catalog.resolver() if source_catalog is not None else SourceRefResolver()
    graph = graph_adapter or NullGraphMemoryAdapter()
    return BrainReadService(
        artifact_store=artifact_store,
        memory_cards=cards,
        graph_adapter=graph,
        source_resolver=resolver,
        document_bridge=document_bridge,
    )


def replay_ingress_events(events: list[Mapping[str, Any]], *, device_id_hash: str) -> dict[str, Any]:
    replay = BrainEventReplayStore()
    envelopes = [
        brain_event_from_ingress_payload(event, device_id_hash=device_id_hash)
        for event in events
    ]
    return replay.apply(envelopes).to_dict()


def _latest_chunk_hint(chunks: list[dict]) -> str:
    if not chunks:
        return "no conversation chunks."
    latest = chunks[-1]
    return f"latest_chunk_ref={latest.get('_id', '')}."


def _latest_evidence_hint(evidence: list[dict]) -> str:
    if not evidence:
        return "no tool evidence bundles."
    latest = evidence[-1]
    return f"latest_tool_evidence_ref={latest.get('_id', '')}."


def _validate_session_doc_scope(docs: list[Mapping[str, Any]], *, provider: str, project: str) -> None:
    require_non_empty(provider, "provider")
    require_non_empty(project, "project")
    for doc in docs:
        doc_provider = str(doc.get("provider") or "")
        doc_project = str(doc.get("project") or "")
        if doc_provider and doc_provider != provider:
            raise ValueError("session source docs have inconsistent provider")
        if doc_project and doc_project != project:
            raise ValueError("session source docs have inconsistent project")


def _safe_size(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _list_or_empty(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _source_event_id(doc: Mapping[str, Any]) -> str:
    doc_id = str(doc.get("_id") or "")
    return f"evt:{short_hash([doc_id, doc.get('content_hash', ''), doc.get('coverage_hash', '')])}"


def _public_event_payload(payload: Mapping[str, Any], *, payload_hash: str) -> dict[str, Any]:
    document = ((payload.get("payload") or {}).get("document") or {}) if isinstance(payload.get("payload"), Mapping) else {}
    metadata = document.get("metadata") if isinstance(document, Mapping) else {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    return {
        "target_id": str(
            payload.get("target_id")
            or payload.get("artifact_id")
            or metadata.get("artifact_id")
            or metadata.get("knowledge_id")
            or payload.get("idempotencyKey")
            or payload.get("idempotency_key")
            or ""
        ),
        "payload_hash": payload_hash,
        "project": str(payload.get("project") or metadata.get("project") or ""),
        "provider": str(payload.get("provider") or metadata.get("provider") or ""),
        "session_id_hash": str(payload.get("session_id_hash") or metadata.get("session_id_hash") or ""),
        "kind": str(payload.get("kind") or payload.get("documentKind") or metadata.get("kind") or ""),
        "supersedes": [str(item) for item in _list_or_empty(payload.get("supersedes"))],
    }


def _entity_type_for_card(card_type: str) -> str:
    return {
        "decision": "Decision",
        "task": "Task",
        "drift": "Drift",
        "preference": "PersonaFact",
        "evidence": "Evidence",
        "status": "Status",
    }.get(card_type, "MemoryCard")


def _source_ref_ids(card: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for ref in card.get("source_refs") or ():
        if isinstance(ref, str):
            ids.append(ref)
        elif isinstance(ref, Mapping):
            value = ref.get("source_ref_id") or ref.get("source_id")
            if value:
                ids.append(str(value))
    return ids

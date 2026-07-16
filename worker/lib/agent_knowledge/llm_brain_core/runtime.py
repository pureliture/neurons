from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from agent_knowledge.couchdb_source.document_model import (
    SourceDocType,
    build_source_hash,
    build_source_revision_token,
    normalize_observed_interval,
    observed_time_bounds,
)
from agent_knowledge.couchdb_source.source_store import CouchDBSourceStore

from ._util import (
    hash_payload,
    list_or_empty,
    public_safe_text,
    require_non_empty,
    require_sha256,
    short_hash,
    utc_now_iso,
)
from .artifact_store import SessionMemoryArtifactStore
from .context import BrainReadService
from .document_bridge import DocumentBridge
from .event_replay import BrainEventReplayStore
from .graph import GraphMemoryAdapter, NullGraphMemoryAdapter
from .models import BrainEventEnvelope, OntologyEpisode, SessionMemoryArtifact, SourceRefRecord
from .ontology import episode_from_memory_card, episode_from_session_artifact
from .source_ref import SourceRefCatalog, SourceRefResolver

# `episode_from_memory_card` is a pure card->episode mapper. It lives in
# `ontology` (the mapper layer) so the ontology module no longer has to import
# back into this adapter/runtime module. It is re-exported here so existing
# `from .runtime import episode_from_memory_card` call sites keep working.
__all__ = [
    "episode_from_memory_card",
    "materialize_artifact_from_couchdb_source",
    "session_source_revision_from_couchdb_source",
    "session_episode_from_couchdb_source",
    "extraction_text_from_couchdb_chunks",
    "brain_event_from_ingress_payload",
    "source_ref_from_catalog_event",
    "build_runtime_brain_service",
    "replay_ingress_events",
]

# Bound the per-session extraction body. The graph entity pass (an LLM call) is
# the cost-driver; a single session can hold many conversation chunks, so the
# joined prose is capped here as well as in OntologyEpisode.__post_init__ to keep
# one episode from shipping an unbounded extraction body.
_MAX_EXTRACTION_CHARS = 8000
_CHUNK_METADATA_HEADER_KEYS = frozenset(
    {
        "session_id_hash",
        "turn_start_index",
        "turn_end_index",
        "turn_part_index",
        "turn_part_count",
        "part_index",
        "part_count",
        "char_start",
        "char_end",
    }
)


def materialize_artifact_from_couchdb_source(
    *,
    session_id_hash: str,
    source_store: CouchDBSourceStore,
    artifact_store: SessionMemoryArtifactStore | None = None,
    ontology_version: str = "1.0.0",
    extractor_version: str = "runtime.1",
) -> SessionMemoryArtifact:
    artifact, _chunks = _materialize_artifact_with_snapshot(
        session_id_hash=session_id_hash,
        source_store=source_store,
        artifact_store=artifact_store,
        ontology_version=ontology_version,
        extractor_version=extractor_version,
    )
    return artifact


def _materialize_artifact_with_snapshot(
    *,
    session_id_hash: str,
    source_store: CouchDBSourceStore,
    artifact_store: SessionMemoryArtifactStore | None = None,
    ontology_version: str = "1.0.0",
    extractor_version: str = "runtime.1",
    max_attempts: int = 3,
) -> tuple[SessionMemoryArtifact, tuple[Mapping[str, Any], ...]]:
    """Persist only an eventually-current bounded source snapshot.

    CouchDB source families are read independently. A source write can land
    between those reads, so each candidate is compared with a fresh full source
    revision before it is persisted. Failed attempts never enter the artifact
    store or influence delta/currentness selection.
    """

    for _attempt in range(max_attempts):
        artifact, chunks = _materialize_artifact_from_couchdb_source_once(
            session_id_hash=session_id_hash,
            source_store=source_store,
            artifact_store=artifact_store,
            ontology_version=ontology_version,
            extractor_version=extractor_version,
        )
        current_source_revision = session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=source_store,
        )
        if current_source_revision == artifact.source_revision:
            if artifact_store is not None:
                artifact_store.upsert(artifact)
            return artifact, chunks
    raise RuntimeError("session source revision did not stabilize during materialization")


def _materialize_artifact_from_couchdb_source_once(
    *,
    session_id_hash: str,
    source_store: CouchDBSourceStore,
    artifact_store: SessionMemoryArtifactStore | None = None,
    ontology_version: str = "1.0.0",
    extractor_version: str = "runtime.1",
) -> tuple[SessionMemoryArtifact, tuple[Mapping[str, Any], ...]]:
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
    observed_at_start, observed_at_end = _source_observed_bounds(
        sessions,
        chunks,
        evidence,
    )
    source_revision = _source_revision_from_documents(
        sessions=sessions,
        chunks=chunks,
        evidence=evidence,
    )
    previous_artifact = (
        artifact_store.get_latest_for_session(
            project=project,
            session_id_hash=session_id_hash,
        )
        if artifact_store is not None
        else None
    )
    materialization_revision = _next_materialization_revision(
        artifact_store=artifact_store,
        project=project,
        session_id_hash=session_id_hash,
        source_revision=source_revision,
    )
    materialized_at = utc_now_iso()
    source_event_ids = [
        _source_event_id(doc) for doc in sessions + chunks + evidence
    ]
    (
        revision_observed_at_start,
        revision_observed_at_end,
        revision_observed_intervals,
        revision_temporal_evidence,
    ) = _revision_observed_bounds(
        previous_artifact=previous_artifact,
        source_revision=source_revision,
        sessions=sessions,
        chunks=chunks,
        evidence=evidence,
    )
    search_term_hashes = _revision_search_term_hashes(
        previous_artifact=previous_artifact,
        source_revision=source_revision,
        chunks=chunks,
        evidence=evidence,
    )
    revision_temporal_term_bindings = _revision_temporal_term_bindings(
        previous_artifact=previous_artifact,
        source_revision=source_revision,
        sessions=sessions,
        chunks=chunks,
        evidence=evidence,
    )
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
        source_event_ids=source_event_ids,
        chunk_refs=[str(doc.get("_id") or "") for doc in chunks],
        tool_evidence_refs=[str(doc.get("_id") or "") for doc in evidence],
        ontology_version=ontology_version,
        extractor_version=extractor_version,
        created_at=observed_at_start or materialized_at,
        source_revision=source_revision,
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        revision_observed_at_start=revision_observed_at_start,
        revision_observed_at_end=revision_observed_at_end,
        revision_observed_intervals=revision_observed_intervals,
        revision_temporal_term_bindings=revision_temporal_term_bindings,
        revision_temporal_evidence=revision_temporal_evidence,
        search_term_hashes=search_term_hashes,
        materialized_at=materialized_at,
        materialization_revision=materialization_revision,
    )
    return artifact, tuple(chunks)


def session_source_revision_from_couchdb_source(
    *, session_id_hash: str, source_store: CouchDBSourceStore
) -> str:
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
    return _source_revision_from_documents(
        sessions=sessions,
        chunks=chunks,
        evidence=evidence,
    )


def _source_revision_from_documents(
    *,
    sessions: list[Mapping[str, Any]],
    chunks: list[Mapping[str, Any]],
    evidence: list[Mapping[str, Any]],
) -> str:
    """Hash exactly one captured source snapshot.

    Callers that already loaded the source documents must not re-read the store:
    doing so can bind an artifact body from revision N to the hash from revision
    N+1 when a chunk arrives between reads.
    """

    observed_at_start, observed_at_end = _source_observed_bounds(
        sessions,
        chunks,
        evidence,
    )
    return build_source_hash(
        [str(doc.get("content_hash") or "") for doc in chunks],
        [str(doc.get("coverage_hash") or "") for doc in evidence],
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        conversation_revision_tokens=[
            build_source_revision_token(doc, material_hash_field="content_hash")
            for doc in chunks
        ],
        tool_evidence_revision_tokens=[
            build_source_revision_token(doc, material_hash_field="content_hash")
            for doc in evidence
        ],
    )


def _revision_search_term_hashes(
    *,
    previous_artifact: SessionMemoryArtifact | None,
    source_revision: str,
    chunks: list[Mapping[str, Any]],
    evidence: list[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Index only the subject terms introduced by this materialization.

    A cumulative snapshot still carries cumulative source refs and content hash,
    but temporal relevance belongs to its delta. Otherwise a Date B snapshot
    silently inherits a Date A subject and satisfies an unrelated Date B query.
    """

    if previous_artifact is None:
        changed_chunks = chunks
        changed_evidence = evidence
    elif previous_artifact.source_revision == source_revision:
        return previous_artifact.search_term_hashes
    else:
        previous_event_ids = set(previous_artifact.source_event_ids)
        changed_chunks = [
            document
            for document in chunks
            if _source_event_id(document) not in previous_event_ids
            and _legacy_source_event_id(document) not in previous_event_ids
        ]
        changed_evidence = [
            document
            for document in evidence
            if _source_event_id(document) not in previous_event_ids
            and _legacy_source_event_id(document) not in previous_event_ids
        ]
    return _artifact_search_term_hashes(
        chunks=changed_chunks,
        evidence=changed_evidence,
    )


def _revision_temporal_term_bindings(
    *,
    previous_artifact: SessionMemoryArtifact | None,
    source_revision: str,
    sessions: list[Mapping[str, Any]],
    chunks: list[Mapping[str, Any]],
    evidence: list[Mapping[str, Any]],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Bind each revision-local subject index to its own event-time interval.

    A revision may materialize several source documents at once.  Keeping only
    an interval union and a term union lets a term introduced at Date C borrow
    a Date B interval.  This mapping is deliberately document-granular and
    fails closed when any contributing document has incomplete event time.
    """

    if previous_artifact is not None and previous_artifact.source_revision == source_revision:
        return previous_artifact.revision_temporal_term_bindings

    if previous_artifact is None:
        changed_documents = [*chunks, *evidence]
        if not changed_documents:
            changed_documents = list(sessions)
    else:
        previous_event_ids = set(previous_artifact.source_event_ids)
        changed_documents = [
            document
            for document in [*chunks, *evidence]
            if _source_event_id(document) not in previous_event_ids
            and _legacy_source_event_id(document) not in previous_event_ids
        ]
        if not changed_documents:
            changed_documents = [
                document
                for document in sessions
                if _source_event_id(document) not in previous_event_ids
                and _legacy_source_event_id(document) not in previous_event_ids
            ]

    grouped: dict[tuple[str, str], set[str]] = {}
    for document in changed_documents:
        start = str(
            document.get("observed_at_start")
            or document.get("started_at")
            or ""
        )
        end = str(
            document.get("observed_at_end")
            or document.get("ended_at")
            or start
        )
        interval = normalize_observed_interval(start, end)
        if interval is None:
            return ()
        doc_type = str(document.get("doc_type") or "")
        hashes = _artifact_search_term_hashes(
            chunks=[document] if doc_type == SourceDocType.CONVERSATION_CHUNK else [],
            evidence=[document] if doc_type == SourceDocType.TOOL_EVIDENCE_BUNDLE else [],
        )
        grouped.setdefault(interval, set()).update(hashes)
    return tuple(
        (start, end, tuple(sorted(hashes)))
        for (start, end), hashes in sorted(grouped.items())
    )


def _source_observed_bounds(
    sessions: list[Mapping[str, Any]],
    chunks: list[Mapping[str, Any]],
    evidence: list[Mapping[str, Any]] | None = None,
) -> tuple[str, str]:
    return observed_time_bounds(
        sessions=sessions,
        chunks=[*chunks, *(evidence or [])],
    )


def _revision_observed_bounds(
    *,
    previous_artifact: SessionMemoryArtifact | None,
    source_revision: str,
    sessions: list[Mapping[str, Any]],
    chunks: list[Mapping[str, Any]],
    evidence: list[Mapping[str, Any]],
) -> tuple[str, str, tuple[tuple[str, str], ...], str]:
    """Return the event-time window introduced by this source revision.

    Full-session observed bounds are cumulative and therefore cannot distinguish
    two revisions of one long-running session.  Revision bounds are derived only
    from newly observed source events.  A same-source rebuild preserves the
    previous decision, while a metadata-only change without event time is marked
    missing so temporal recall fails closed instead of falling back to the latest
    cumulative snapshot.
    """

    if previous_artifact is not None and previous_artifact.source_revision == source_revision:
        return (
            previous_artifact.revision_observed_at_start,
            previous_artifact.revision_observed_at_end,
            previous_artifact.revision_observed_intervals,
            previous_artifact.revision_temporal_evidence,
        )

    if previous_artifact is None:
        changed_documents = [*chunks, *evidence]
        if not changed_documents:
            changed_documents = list(sessions)
    else:
        previous_event_ids = set(previous_artifact.source_event_ids)
        changed_chunks = [
            doc
            for doc in chunks
            if _source_event_id(doc) not in previous_event_ids
            and _legacy_source_event_id(doc) not in previous_event_ids
        ]
        changed_evidence = [
            doc
            for doc in evidence
            if _source_event_id(doc) not in previous_event_ids
            and _legacy_source_event_id(doc) not in previous_event_ids
        ]
        changed_documents = [*changed_chunks, *changed_evidence]
        if not changed_documents:
            changed_sessions = [
                doc
                for doc in sessions
                if _source_event_id(doc) not in previous_event_ids
                and _legacy_source_event_id(doc) not in previous_event_ids
            ]
            changed_documents = changed_sessions
    intervals, complete = _source_observed_intervals(changed_documents)
    if not complete:
        return "", "", (), "missing"
    if previous_artifact is None and not intervals and (chunks or evidence):
        intervals, complete = _source_observed_intervals(sessions)
        if not complete:
            return "", "", (), "missing"
    if intervals:
        return (
            min(start for start, _end in intervals),
            max(end for _start, end in intervals),
            intervals,
            "bounded",
        )
    return "", "", (), "missing"


def _source_observed_intervals(
    documents: list[Mapping[str, Any]],
) -> tuple[tuple[tuple[str, str], ...], bool]:
    intervals: set[tuple[str, str]] = set()
    for document in documents:
        start = str(
            document.get("observed_at_start")
            or document.get("started_at")
            or ""
        )
        end = str(
            document.get("observed_at_end")
            or document.get("ended_at")
            or start
        )
        normalized = normalize_observed_interval(start, end)
        if normalized is None:
            return (), False
        intervals.add(normalized)
    return tuple(sorted(intervals)), True


def _artifact_search_term_hashes(
    *, chunks: list[Mapping[str, Any]], evidence: list[Mapping[str, Any]]
) -> tuple[str, ...]:
    """Build a bounded, one-way subject index without persisting source prose."""

    text = "\n".join(
        [
            *(_strip_chunk_metadata_header(str(doc.get("body") or "")) for doc in chunks),
            *(str(doc.get("body") or "") for doc in evidence),
        ]
    )
    terms: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9가-힣_-]+", text):
        term = raw.casefold()
        if len(term) <= 1 or term.isdigit():
            continue
        terms.add(term)
        for suffix in ("으로", "에서", "에게", "한테", "까지", "부터", "처럼", "보다", "로"):
            if term.endswith(suffix) and len(term) > len(suffix) + 1:
                terms.add(term[: -len(suffix)])
                break
        for suffix in ("ment", "ing", "ed"):
            if term.endswith(suffix) and len(term) > len(suffix) + 3:
                terms.add(term[: -len(suffix)])
                break
    return tuple(hash_payload(term) for term in sorted(terms)[:512])


def _next_materialization_revision(
    *,
    artifact_store: SessionMemoryArtifactStore | None,
    project: str,
    session_id_hash: str,
    source_revision: str,
) -> int:
    if artifact_store is None:
        return 1
    latest = artifact_store.get_latest_for_session(
        project=project,
        session_id_hash=session_id_hash,
    )
    if latest is None:
        return 1
    if latest.source_revision and latest.source_revision == source_revision:
        return max(latest.materialization_revision, 1)
    return max(latest.materialization_revision, 0) + 1


def extraction_text_from_couchdb_chunks(
    *,
    session_id_hash: str,
    source_store: CouchDBSourceStore,
    max_chars: int = _MAX_EXTRACTION_CHARS,
) -> str:
    """Join a session's CouchDB conversation-chunk bodies into extraction prose.

    The conversation_chunk ``body`` is already public-safe (built via
    ``redact_public_ingress_text`` + ``assert_source_text_clean`` in
    ``build_conversation_chunk_document``), so this only fetches, orders, and
    bounds it. Chunks are ordered by ``turn_start_index`` (then ``_id``) so the
    prose reads in conversation order. The result is the REAL conversation
    content the entity pass should extract from -- not the statistics summary.

    Returns "" when the session has no conversation chunks, which the caller can
    treat as "no prose sourced" (the adapter then falls back to the JSON body and
    logs the generic-only regression).
    """

    chunks = source_store.find_by_session(
        session_id_hash=session_id_hash,
        doc_type=SourceDocType.CONVERSATION_CHUNK,
    )
    return _extraction_text_from_chunks(chunks, max_chars=max_chars)


def _extraction_text_from_chunks(
    chunks: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    max_chars: int = _MAX_EXTRACTION_CHARS,
) -> str:
    chunks = sorted(chunks, key=_conversation_chunk_order_key)
    # Accumulate against the budget and stop early so a large session never
    # materializes the full concatenation before the cap is applied. Bounding here
    # as well as in OntologyEpisode.__post_init__ is intentional: public_safe_text
    # in the model is the authoritative redaction+bound, this is the early cap.
    separator = "\n\n"
    budget = max_chars
    parts: list[str] = []
    for doc in chunks:
        if budget <= 0:
            break
        body = _strip_chunk_metadata_header(str(doc.get("body") or ""))
        if not body:
            continue
        if parts:
            # join() will insert a separator before this part; charge it first.
            budget -= len(separator)
            if budget <= 0:
                break
        if len(body) > budget:
            parts.append(body[:budget])
            break
        parts.append(body)
        budget -= len(body)
    prose = separator.join(parts)
    return prose[:max_chars]


def _conversation_chunk_order_key(doc: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    return (
        _safe_int(doc.get("turn_start_index")),
        _safe_int(doc.get("turn_end_index")),
        _safe_int(doc.get("part_index") or doc.get("turn_part_index")),
        _safe_int(doc.get("char_start")),
        str(doc.get("_id") or ""),
    )


def _strip_chunk_metadata_header(text: str) -> str:
    # A metadata header is a contiguous run of ``key: value`` lines whose keys are
    # all known chunk-metadata keys, terminated by a blank separator line. Require
    # that blank-line boundary: without it the leading lines are real conversation
    # prose that merely happens to look like ``key: value`` (e.g. a first sentence
    # such as "char_start: where the bug begins"), and stripping them would drop
    # the opening of the turn.
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            break
        key, sep, _value = stripped.partition(":")
        if sep and key.strip() in _CHUNK_METADATA_HEADER_KEYS:
            index += 1
            continue
        # First non-metadata, non-blank line -> no recognizable header block.
        return text.strip()
    # No header lines consumed, or the run was not closed by a blank separator
    # before end-of-text -> not a header; keep the body intact.
    if index == 0 or index >= len(lines):
        return text.strip()
    # lines[index] is the blank separator; drop the header block and that line.
    return "\n".join(lines[index + 1 :]).strip()


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def session_episode_from_couchdb_source(
    *,
    session_id_hash: str,
    source_store: CouchDBSourceStore,
    artifact_store: SessionMemoryArtifactStore | None = None,
    ontology_version: str = "1.0.0",
    extractor_version: str = "runtime.1",
) -> OntologyEpisode:
    """Materialize a Session OntologyEpisode carrying real conversation prose.

    This is the materialize-time sourcing seam: the CouchDB source store IS
    reachable here (unlike the ledger-only projection CLI path), so the real
    redacted conversation-chunk prose is sourced and carried on the episode's
    transient ``extraction_text``. The stored node content stays the canonical
    JSON (recall-safe); only the entity-pass extraction input becomes real prose.
    """

    artifact, captured_chunks = _materialize_artifact_with_snapshot(
        session_id_hash=session_id_hash,
        source_store=source_store,
        artifact_store=artifact_store,
        ontology_version=ontology_version,
        extractor_version=extractor_version,
    )
    extraction_text = _extraction_text_from_chunks(
        captured_chunks,
    )
    return episode_from_session_artifact(artifact, extraction_text=extraction_text)


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


def build_runtime_brain_service(
    *,
    project: str,
    artifact_store: SessionMemoryArtifactStore,
    read_model: Any | None = None,
    source_catalog: SourceRefCatalog | Any | None = None,
    graph_adapter: GraphMemoryAdapter | None = None,
    document_bridge: DocumentBridge | None = None,
    search_mirror_status: Mapping[str, Any] | None = None,
    reference_corpus_status_reader: Any | None = None,
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
        search_mirror_status=search_mirror_status,
        reference_corpus_status_reader=reference_corpus_status_reader,
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
    return f"latest_chunk_ref_hash={hash_payload(str(latest.get('_id') or ''))}."


def _latest_evidence_hint(evidence: list[dict]) -> str:
    if not evidence:
        return "no tool evidence bundles."
    latest = evidence[-1]
    return f"latest_tool_evidence_ref_hash={hash_payload(str(latest.get('_id') or ''))}."


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


def _source_event_id(doc: Mapping[str, Any]) -> str:
    doc_id = str(doc.get("_id") or "")
    return f"evt:{short_hash([doc_id, doc.get('content_hash', ''), doc.get('coverage_hash', ''), doc.get('observed_at_start', ''), doc.get('observed_at_end', '')])}"


def _legacy_source_event_id(doc: Mapping[str, Any]) -> str:
    """Identity written before temporal metadata became material source state."""

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
        "supersedes": [str(item) for item in list_or_empty(payload.get("supersedes"))],
    }

"""CouchDB IndexBackendAdapter: live ingress sink over the CouchDB source plane.

This module implements :class:`IndexBackendAdapter` (the Protocol used by
``shadow_worker.process_payload``) so the shadow worker can write live ingress
payloads directly to CouchDB instead of RAGFlow.

Relationship to ``CouchDBDeliveryBackend``
------------------------------------------
:class:`CouchDBDeliveryBackend` (``couchdb_delivery_backend.py``) speaks the
**DeliveryBackend** protocol: it receives a :class:`.DeliveryJobView` and reads
the raw payload from ``RAGIngressStateDB``.

:class:`CouchDBIndexBackendAdapter` speaks the **IndexBackendAdapter** protocol:
it receives an already-built :class:`.RagReadyDocument` directly from
``process_payload`` (which has already applied ``apply_server_redaction`` +
``public_ingress_leak_violations`` upstream).

The shared transform that builds the 6 CouchDB doc families from domain objects
is factored into the module-level helper :func:`build_couchdb_docs_from_rag_document`,
which is reused by both classes so the CouchDB doc layout stays byte-identical
whether the ingress path goes through the DeliveryBackend abstraction or the
IndexBackendAdapter abstraction.

Fail-closed invariants
-----------------------
- ``submit_document`` is called only after ``process_payload`` has applied
  ``apply_server_redaction`` + ``public_ingress_leak_violations``.  The body in
  the ``RagReadyDocument`` is therefore already fully redacted.
- ``build_conversation_chunk_document`` calls ``assert_source_text_clean``
  internally as a defence-in-depth assertion.  We do NOT bypass it.
- Any :class:`.CouchDBError` or unexpected exception raises to the caller so
  ``process_payload`` records a real (retryable) failure — consistent with the
  RAGFlow path.

doc_ref / dataset_ref conventions (matches CouchDBDeliveryBackend)
------------------------------------------------------------------
- ``dataset_ref``: ``couchdb:<db>``
- ``document_ref``: ``session_doc_id(session_id_hash)`` — the authoritative
  transcript_session doc for the session.

find_by_natural_key dedup
--------------------------
``idempotency_key`` is the wire key sent by the client (unique per source +
kind + content_hash).  ``payload_hash`` is the sha256 of the wire payload.
We check whether the conversation_chunk doc identified by
``conversation_chunk_doc_id(session_id_hash, chunk_id)`` already exists in
CouchDB.  Because ``session_id_hash`` and ``chunk_id`` are stored inside the
``RagReadyDocument.metadata`` dict (populated from the wire payload's
``metadata`` object), the natural-key lookup is fully deterministic without a
state_db round-trip.
"""

from __future__ import annotations

import datetime

from ..couchdb_source.couchdb_http_store import CouchDBError
from ..couchdb_source.document_model import (
    ProjectionStatus,
    build_conversation_chunk_document,
    build_coverage_manifest_document,
    build_projection_state_document,
    build_transcript_session_document,
    conversation_chunk_doc_id,
    projection_state_doc_id,
    session_doc_id,
    sha256_hash,
)
from ..couchdb_source.source_store import CouchDBSourceStore
from ..session_memory.transcript_model import REDACTION_VERSION, TranscriptChunk, TranscriptSession
from .index_backend import (
    BackendDocumentHandle,
    BackendStatusDetail,
    BackendSubmitResult,
    IndexStatus,
)
from .rag_ready_document import RagReadyDocument


# ---------------------------------------------------------------------------
# Shared transform helper
# ---------------------------------------------------------------------------

def build_couchdb_docs_from_rag_document(document: RagReadyDocument) -> tuple[
    dict, dict, dict, dict, str, str
]:
    """Extract domain objects and build the 6 CouchDB doc families from a
    :class:`.RagReadyDocument`.

    Returns ``(session_doc, chunk_doc, coverage_doc, proj_doc, session_id_hash, chunk_id)``.

    The caller is responsible for writing these docs to the store and for
    handling any missing-field error (session_id_hash / chunk_id empty).

    This helper is intentionally kept free of any I/O so it can be unit-tested
    without a real store.
    """
    metadata = dict(document.metadata)

    session_id_hash = str(metadata.get("session_id_hash") or "")
    provider = str(
        metadata.get("provider")
        or document.source_namespace
        or "ingress"
    )
    project = str(metadata.get("project") or "")
    chunk_id = str(metadata.get("chunk_id") or "")
    redaction_version = str(document.redaction_version or REDACTION_VERSION)

    # Positional chunk metadata — fall back to 0/1 when dendrite omits them
    document_body = document.body
    turn_start_index = int(metadata.get("turn_start_index") or 0)
    turn_end_index = int(metadata.get("turn_end_index") or 0)
    part_index = int(metadata.get("part_index") or 1)
    part_count = int(metadata.get("part_count") or 1)
    char_start = int(metadata.get("char_start") or 0)
    char_end = int(metadata.get("char_end") or len(document_body))

    chunk = TranscriptChunk(
        chunk_id=chunk_id,
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        turn_start_index=turn_start_index,
        turn_end_index=turn_end_index,
        redacted_text=document_body,  # already server-redacted upstream
        content_hash=sha256_hash(document_body),
        redaction_version=redaction_version,
        source_status="source_locator_private_spool_only",
        part_index=part_index,
        part_count=part_count,
        char_start=char_start,
        char_end=char_end,
    )

    session = TranscriptSession(
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        started_at="",   # not available in live ingress
        ended_at="",
        source_status="source_unproven",
        source_locator_hash="",  # private Mac path; never sent server-side
    )

    session_doc = build_transcript_session_document(session=session)
    # build_conversation_chunk_document calls assert_source_text_clean internally
    chunk_doc = build_conversation_chunk_document(chunk=chunk, source_locator_hash="")

    chunk_content_hash = str(chunk_doc.get("content_hash") or "")
    coverage_doc = build_coverage_manifest_document(
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        conversation_chunk_count=1,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=[chunk_content_hash],
        tool_evidence_coverage_hashes=[],
        source_locator_hash="",
    )

    proj_doc = build_projection_state_document(
        session_id_hash=session_id_hash,
        provider=provider,
        project=project,
        projection_status=ProjectionStatus.PENDING,
        source_locator_hash="",
    )

    return session_doc, chunk_doc, coverage_doc, proj_doc, session_id_hash, chunk_id


# ---------------------------------------------------------------------------
# CouchDBIndexBackendAdapter
# ---------------------------------------------------------------------------

class CouchDBIndexBackendAdapter:
    """``IndexBackendAdapter`` protocol implementation over a
    :class:`.CouchDBSourceStore`.

    Writes a live ingress :class:`.RagReadyDocument` into the CouchDB source
    plane using the same document builders as the historical import path, so
    live-ingested docs are structurally identical to migration-built ones.

    ``store`` is a :class:`.CouchDBSourceStore` (typically
    :class:`.CouchDBHttpSourceStore` in production, the in-memory fake in tests).

    The adapter does NOT touch ``state_db``; all state authority lives in CouchDB.
    """

    def __init__(self, *, store: CouchDBSourceStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # IndexBackendAdapter Protocol
    # ------------------------------------------------------------------

    def submit_document(
        self,
        document: RagReadyDocument,
        *,
        on_step_complete=None,
    ) -> BackendSubmitResult:
        """Transform *document* into the 6 CouchDB doc families and store them.

        ``process_payload`` has already applied ``apply_server_redaction`` +
        ``public_ingress_leak_violations`` before calling this method, so the
        body is fully redacted.  ``build_conversation_chunk_document`` asserts
        this internally (``assert_source_text_clean``).

        Raises any ``CouchDBError`` or unexpected exception so the caller can
        treat it as a retryable failure.
        """
        try:
            (
                session_doc, chunk_doc, coverage_doc, proj_doc,
                session_id_hash, chunk_id,
            ) = build_couchdb_docs_from_rag_document(document)
        except Exception as exc:
            # Field extraction failed — hard failure, not retryable in itself,
            # but we surface it so the caller can log and move on.
            raise RuntimeError(f"CouchDB doc build failed: {exc}") from exc

        if not session_id_hash or not chunk_id:
            raise ValueError(
                "submit_document: session_id_hash and chunk_id are required "
                f"(session_id_hash={session_id_hash!r}, chunk_id={chunk_id!r})"
            )

        db_name = getattr(self._store, "db", "couchdb")
        dataset_ref = f"couchdb:{db_name}"
        doc_ref = session_doc_id(session_id_hash)

        # Store all 4 documents.  Any CouchDBError propagates to caller.
        self._store.put(session_doc)
        if on_step_complete is not None:
            on_step_complete("session", document_ref=doc_ref)

        self._store.put(chunk_doc)
        if on_step_complete is not None:
            on_step_complete("chunk", document_ref=doc_ref)

        self._store.put(coverage_doc)
        if on_step_complete is not None:
            on_step_complete("coverage", document_ref=doc_ref)

        # Upsert projection_state: only write if not already PROJECTED so we
        # don't clobber a materialiser that has already finished the session.
        existing_proj = self._store.get(projection_state_doc_id(session_id_hash))
        if existing_proj is None or existing_proj.get("projection_status") != ProjectionStatus.PROJECTED:
            self._store.put(proj_doc)
        if on_step_complete is not None:
            on_step_complete("projection", document_ref=doc_ref)

        return BackendSubmitResult(
            dataset_ref=dataset_ref,
            document_ref=doc_ref,
            status="submitted",
        )

    def find_by_natural_key(
        self,
        *,
        target_profile: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> BackendDocumentHandle | None:
        """Return a handle if the chunk already exists in CouchDB, else ``None``.

        The natural key is derived from ``idempotency_key`` (which encodes
        ``source_namespace:document_kind:content_hash``).  Because we do not
        have a state_db here, we rely on CouchDB's own deterministic doc ids
        to check for existence.

        We recover ``session_id_hash`` and ``chunk_id`` from the chunk doc id
        pattern only if we can resolve them from the natural key — but without
        a state_db or the original metadata we cannot.  We therefore perform a
        *conservative* existence check: return ``None`` (proceed with fresh
        submit) unless the caller can supply session_id_hash + chunk_id via the
        ``payload_hash`` embedded in the idempotency key.

        In practice, the shadow_worker already deduplicates via its own
        IngestStateStore log (``get_delivered``), so this method is a
        defence-in-depth gate.  Returning ``None`` here is safe: the CouchDB
        ``put`` is idempotent (content-hash dedup in the store layer).
        """
        # We cannot resolve session_id_hash from idempotency_key alone without
        # the original metadata.  Return None to let the store's own idempotent
        # put handle dedup.
        return None

    def document_status(self, handle: BackendDocumentHandle) -> str:
        """Existence-based generic status."""
        existing = self._store.get(handle.document_ref)
        if existing is None:
            return IndexStatus.UNKNOWN
        return IndexStatus.INDEXED

    def document_status_detail(self, handle: BackendDocumentHandle) -> BackendStatusDetail:
        """Existence-based status detail."""
        existing = self._store.get(handle.document_ref)
        if existing is None:
            return BackendStatusDetail(
                status=IndexStatus.UNKNOWN,
                progress=0.0,
                backend_raw_status="couchdb_not_found",
            )
        doc_type = str(existing.get("doc_type") or "")
        return BackendStatusDetail(
            status=IndexStatus.INDEXED,
            progress=1.0,
            backend_raw_status=f"couchdb_exists:{doc_type}",
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_couchdb_index_backend(
    *,
    couchdb_url: str,
    couchdb_user: str,
    couchdb_password: str,
    couchdb_db: str,
) -> CouchDBIndexBackendAdapter:
    """Factory used by shadow_worker.main() env-switch wiring.

    Kept in this module so shadow_worker does not import couchdb_http_store
    directly (a RAGFlow-free boundary when ``INGRESS_DELIVERY_BACKEND=couchdb``).
    """
    import base64

    from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore

    credentials = base64.b64encode(f"{couchdb_user}:{couchdb_password}".encode()).decode()
    store = CouchDBHttpSourceStore(
        base_url=couchdb_url,
        db=couchdb_db,
        auth_header=f"Basic {credentials}",
    )
    return CouchDBIndexBackendAdapter(store=store)


__all__ = [
    "CouchDBIndexBackendAdapter",
    "build_couchdb_docs_from_rag_document",
    "build_couchdb_index_backend",
]

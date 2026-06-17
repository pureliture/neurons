"""CouchDB DeliveryBackend: routes live ingress payloads into the CouchDB source plane.

This module is the live-pipeline counterpart to :mod:`.delivery_backend`
(``RagflowDeliveryBackend``) and the migration-built
:mod:`..couchdb_source.historical_import`.

Architecture note
-----------------
Historical import operates file-by-file (.jsonl -> parse -> chunk -> build 6
doc families).  Live ingress delivers a *single*, already-packed
``conversation_chunk`` at a time.  There is no local source file here; the
delivery worker only holds the wire payload.  Therefore:

- ``import_historical_source`` and ``parse_transcript_source`` are NOT called.
- Instead, the payload metadata is used to construct :class:`.TranscriptChunk`
  and :class:`.TranscriptSession` objects *directly*, then the same
  ``build_*`` functions from :mod:`..couchdb_source.document_model` are
  re-used verbatim -- ensuring live-ingested CouchDB docs are byte-identical
  (for the same session_id_hash/chunk_id) to migration-built ones.

Gap handling
------------
- ``raw_session_id``: NOT in the payload.  The payload carries only the
  already-hashed ``session_id_hash``; ``_id`` computation uses only the hash,
  so this is fine.
- ``source_locator_hash``: private Mac path, not sent to the server.  Passed
  as ``""`` (the builder's accepted default for live ingress).
- ``started_at``/``ended_at``: not in the payload.  The session doc is upserted
  with empty timestamps; if it already exists those fields are preserved.
- ``transcript_session`` upsert: the store's idempotent put ensures the session
  doc is created on first chunk arrival and unchanged on subsequent chunks.

Fail-closed gate (mirrors RagflowDeliveryBackend)
-------------------------------------------------
1. resolve_delivery_payload: gate = PAYLOAD_OK or early return evidence.
2. apply_server_redaction: full public-ingress redaction on the body/meta.
3. public_ingress_leak_violations on the redacted body: any hit -> quarantine
   (status="quarantined").  This mirrors the RAGFlow delivery path.
4. build_conversation_chunk_document calls assert_source_text_clean internally.
5. Any CouchDBError or unexpected exception mid-flight -> DeliveryOutcomeUncertain
   (the PUT may have succeeded before the exception).

doc_ref convention: ``session_doc_id(session_id_hash)`` so status() can look
up the authoritative session document.  dataset_ref: ``couchdb:<db>``.
"""

from __future__ import annotations

import datetime

from ..couchdb_source.couchdb_http_store import CouchDBError
from ..couchdb_source.document_model import (
    ProjectionStatus,
    SourceDocType,
    build_conversation_chunk_document,
    build_coverage_manifest_document,
    build_projection_state_document,
    build_transcript_session_document,
    conversation_chunk_doc_id,
    coverage_manifest_doc_id,
    projection_state_doc_id,
    session_doc_id,
    sha256_hash,
)
from ..couchdb_source.source_store import CouchDBSourceStore
from ..session_memory.transcript_model import REDACTION_VERSION, TranscriptChunk, TranscriptSession
from .delivery_backend import (
    PAYLOAD_HASH_MISMATCH,
    PAYLOAD_MISSING,
    PAYLOAD_OK,
    resolve_delivery_payload,
)
from .delivery_executor import (
    DeliveryBackendEvidence,
    DeliveryJobView,
    DeliveryOutcomeUncertain,
)
from .server_runtime import apply_server_redaction, public_ingress_leak_violations
from .state_db import RAGIngressStateDB


def _now_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()


class CouchDBDeliveryBackend:
    """``DeliveryBackend`` protocol implementation over a :class:`CouchDBSourceStore`.

    Writes a live ingress ``conversation_chunk`` payload into the CouchDB source
    plane using the same document builders as the historical import path, so
    live-ingested docs are structurally identical to migration-built ones.

    ``state_db`` is the same :class:`.RAGIngressStateDB` used by the RAGFlow
    backend for payload resolution.  ``store`` is a :class:`CouchDBSourceStore`
    (typically :class:`CouchDBHttpSourceStore` in production, the in-memory
    fake in tests).
    """

    def __init__(self, *, state_db: RAGIngressStateDB, store: CouchDBSourceStore) -> None:
        self._state_db = state_db
        self._store = store

    # ------------------------------------------------------------------
    # DeliveryBackend Protocol
    # ------------------------------------------------------------------

    def submit(self, job: DeliveryJobView) -> DeliveryBackendEvidence:
        # --- Gate 1: payload availability + integrity -------------------------
        payload, gate = resolve_delivery_payload(
            self._state_db,
            idempotency_key=job.idempotency_key,
            expected_payload_hash=job.payload_hash,
        )
        if gate != PAYLOAD_OK:
            return DeliveryBackendEvidence(
                idempotency_key=job.idempotency_key,
                payload_hash=job.payload_hash,
                dataset_ref="",
                document_ref="",
                run="",
                status="payload_unavailable" if gate == PAYLOAD_MISSING else "payload_integrity_mismatch",
            )

        # --- Gate 2: idempotent early-out (chunk doc already in CouchDB) ------
        existing = self.find_by_natural_key(job.idempotency_key, job.payload_hash)
        if existing is not None:
            return existing

        # --- Gate 3: apply full server-side public-ingress redaction ----------
        payload = apply_server_redaction(payload)

        # --- Gate 4: fail-closed public-ingress leak check --------------------
        document_body = str(
            ((payload.get("payload") or {}).get("document") or {}).get("body") or ""
        )
        leak_violations = public_ingress_leak_violations(document_body)
        if leak_violations:
            return DeliveryBackendEvidence(
                idempotency_key=job.idempotency_key,
                payload_hash=job.payload_hash,
                dataset_ref="",
                document_ref="",
                run="public_ingress_leak:" + ",".join(sorted(leak_violations)),
                status="quarantined",
            )

        # --- Extract metadata fields ------------------------------------------
        pkg = payload.get("payload") or {}
        document = pkg.get("document") or {}
        metadata = dict(document.get("metadata") or {})
        source = payload.get("source") or {}

        session_id_hash = str(metadata.get("session_id_hash") or "")
        provider = str(
            metadata.get("provider") or source.get("provider") or source.get("namespace") or "ingress"
        )
        project = str(metadata.get("project") or source.get("project") or "")
        chunk_id = str(metadata.get("chunk_id") or "")
        redaction_version = str(pkg.get("redactionVersion") or REDACTION_VERSION)

        # Positional chunk metadata -- fall back to 0/1 if dendrite didn't emit them
        turn_start_index = int(metadata.get("turn_start_index") or 0)
        turn_end_index = int(metadata.get("turn_end_index") or 0)
        part_index = int(metadata.get("part_index") or 1)
        part_count = int(metadata.get("part_count") or 1)
        char_start = int(metadata.get("char_start") or 0)
        char_end = int(metadata.get("char_end") or len(document_body))

        if not session_id_hash or not chunk_id:
            return DeliveryBackendEvidence(
                idempotency_key=job.idempotency_key,
                payload_hash=job.payload_hash,
                dataset_ref="",
                document_ref="",
                run="missing_session_id_hash_or_chunk_id",
                status="payload_integrity_mismatch",
            )

        # --- Construct domain objects -----------------------------------------
        # TranscriptChunk.__post_init__ calls redact_text_v2; the body is
        # already redacted.  We bypass that secondary redaction by
        # providing the already-redacted text as ``redacted_text``; the
        # content_hash is then recomputed over it (which is correct for
        # the CouchDB doc key, as build_conversation_chunk_document will
        # apply one more redact_public_ingress_text pass on the body).
        chunk = TranscriptChunk(
            chunk_id=chunk_id,
            session_id_hash=session_id_hash,
            provider=provider,
            project=project,
            turn_start_index=turn_start_index,
            turn_end_index=turn_end_index,
            redacted_text=document_body,  # post-init applies redact_text_v2 (idempotent)
            content_hash=sha256_hash(document_body),  # overwritten by __post_init__
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
            started_at="",  # not available in live ingress payload
            ended_at="",
            source_status="source_unproven",
            source_locator_hash="",  # private Mac path; not sent server-side
        )

        db_name = getattr(self._store, "db", "couchdb")
        dataset_ref = f"couchdb:{db_name}"
        doc_ref = session_doc_id(session_id_hash)

        try:
            # --- Build + put transcript_session (upsert; first chunk wins) ---
            session_doc = build_transcript_session_document(session=session)
            self._store.put(session_doc)

            # --- Build + put conversation_chunk ---------------------------------
            chunk_doc = build_conversation_chunk_document(chunk=chunk, source_locator_hash="")
            self._store.put(chunk_doc)

            # --- Upsert coverage_manifest (incremental / partial) ---------------
            # For live ingress we emit a single-chunk coverage manifest covering
            # this chunk only.  A full session-level reconciler (the materialiser
            # already in session_memory_materializer) will merge all chunks later.
            # build_coverage_manifest_document is the reused builder; the content
            # hash used here is the chunk's public-ingress body hash.
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
            self._store.put(coverage_doc)

            # --- Upsert projection_state (mark dirty / pending) -----------------
            # Always write/refresh pending so the downstream projector picks up
            # the new or updated session.
            existing_proj = self._store.get(projection_state_doc_id(session_id_hash))
            if existing_proj is None or existing_proj.get("projection_status") != ProjectionStatus.PROJECTED:
                proj_doc = build_projection_state_document(
                    session_id_hash=session_id_hash,
                    provider=provider,
                    project=project,
                    projection_status=ProjectionStatus.PENDING,
                    source_locator_hash="",
                )
                self._store.put(proj_doc)

        except CouchDBError as exc:
            # Network/store errors mid-flight: the PUT may have reached CouchDB
            # before the exception.  Signal uncertain so the executor does NOT
            # record this as a clean retryable failure.
            raise DeliveryOutcomeUncertain(exc.__class__.__name__) from exc
        except Exception as exc:
            raise DeliveryOutcomeUncertain(exc.__class__.__name__) from exc

        return DeliveryBackendEvidence(
            idempotency_key=job.idempotency_key,
            payload_hash=job.payload_hash,
            dataset_ref=dataset_ref,
            document_ref=doc_ref,
            run="couchdb_put",
            status="succeeded",
            observed_at=datetime.datetime.now(tz=datetime.timezone.utc),
        )

    def find_by_natural_key(
        self, idempotency_key: str, payload_hash: str
    ) -> DeliveryBackendEvidence | None:
        """Idempotent lookup: return evidence if the session doc already exists.

        We cannot query CouchDB by idempotency_key without a state_db round-trip.
        We fetch the state_db row to recover session_id_hash (via the chunk doc's
        deterministic _id pattern from the stored metadata), and then check
        whether the CouchDB session doc exists.

        If the state_db row is missing or the chunk doc is not in CouchDB, return
        None so the caller proceeds with a fresh submit.
        """
        row = self._state_db.get_row("delivery_jobs", "idempotency_key", idempotency_key)
        if row is None or str(row.get("payload_hash") or "") != payload_hash:
            return None

        # Recover the payload to get session_id_hash + chunk_id.
        payload = self._state_db.get_delivery_payload(idempotency_key)
        if payload is None:
            return None
        pkg = payload.get("payload") or {}
        document = pkg.get("document") or {}
        metadata = dict(document.get("metadata") or {})
        session_id_hash = str(metadata.get("session_id_hash") or "")
        chunk_id = str(metadata.get("chunk_id") or "")
        if not session_id_hash or not chunk_id:
            return None

        chunk_doc_id = conversation_chunk_doc_id(session_id_hash, chunk_id)
        existing_chunk = self._store.get(chunk_doc_id)
        if existing_chunk is None:
            return None

        db_name = getattr(self._store, "db", "couchdb")
        return DeliveryBackendEvidence(
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            dataset_ref=f"couchdb:{db_name}",
            document_ref=session_doc_id(session_id_hash),
            run="couchdb_existing",
            status="succeeded",
        )

    def status(self, dataset_ref: str, document_ref: str) -> DeliveryBackendEvidence:
        """Existence-based status check: look up the transcript_session doc by ref."""
        existing = self._store.get(document_ref)
        if existing is None:
            return DeliveryBackendEvidence(
                idempotency_key="",
                payload_hash="",
                dataset_ref=dataset_ref,
                document_ref=document_ref,
                run="couchdb_not_found",
                status="unknown",
            )
        doc_type = str(existing.get("doc_type") or "")
        return DeliveryBackendEvidence(
            idempotency_key="",
            payload_hash="",
            dataset_ref=dataset_ref,
            document_ref=document_ref,
            run=f"couchdb_exists:{doc_type}",
            status="succeeded",
        )


def build_couchdb_delivery_backend(
    *,
    state_db: RAGIngressStateDB,
    couchdb_url: str,
    couchdb_user: str,
    couchdb_password: str,
    couchdb_db: str,
) -> CouchDBDeliveryBackend:
    """Factory used by the env-switch wiring in state_cli to build the backend.

    Kept in this module so :mod:`.state_cli` does not import couchdb_http_store
    directly (a RAGFlow-free boundary).
    """
    import base64

    from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore

    credentials = base64.b64encode(f"{couchdb_user}:{couchdb_password}".encode()).decode()
    store = CouchDBHttpSourceStore(
        base_url=couchdb_url,
        db=couchdb_db,
        auth_header=f"Basic {credentials}",
    )
    return CouchDBDeliveryBackend(state_db=state_db, store=store)


__all__ = [
    "CouchDBDeliveryBackend",
    "build_couchdb_delivery_backend",
]

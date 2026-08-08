"""CouchDB DeliveryBackend: routes live ingress payloads into the CouchDB source plane.

This module is the live-pipeline counterpart to :mod:`.delivery_backend`
(``RetiredIndexBridgeDeliveryBackend``) and the migration-built
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
- ``started_at``/``ended_at``: derived from the payload's redacted observed-time
  metadata. Aggregate coverage refresh widens the session bounds as chunks arrive.
- ``transcript_session`` upsert: the deterministic document id keeps exact
  duplicates idempotent while distinct chunks refresh source currentness.

Fail-closed gate (mirrors RetiredIndexBridgeDeliveryBackend)
-------------------------------------------------
1. resolve_delivery_payload: gate = PAYLOAD_OK or early return evidence.
2. apply_server_redaction: full public-ingress redaction on the body/meta.
3. public_ingress_leak_violations on the redacted body: any hit -> quarantine
   (status="quarantined").  This mirrors the RetiredIndexBridge delivery path.
4. build_conversation_chunk_document calls assert_source_text_clean internally.
5. Any CouchDBError or unexpected exception mid-flight -> DeliveryOutcomeUncertain
   (the PUT may have succeeded before the exception).

doc_ref convention: ``session_doc_id(session_id_hash)`` so status() can look
up the authoritative session document.  dataset_ref: ``couchdb:<db>``.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from typing import Any

from ..couchdb_source.couchdb_http_store import CouchDBError
from ..couchdb_source.document_model import (
    SourceDocType,
    active_source_revision_pointer_doc_id,
    build_conversation_chunk_document,
    build_transcript_session_document,
    conversation_chunk_doc_id,
    session_doc_id,
    sha256_hash,
)
from ..couchdb_source.session_memory_materializer import (
    mark_projection_pending_if_source_changed,
    upsert_transcript_session_aggregate,
    update_coverage_with_tool_evidence,
)
from ..couchdb_source.source_revision import (
    ResolvedSourceRevision,
    SourceRevisionResolutionError,
    active_source_origin_document_ids,
    activate_source_revision,
    resolve_active_source_revision,
)
from ..couchdb_source.source_store import (
    CouchDBSourceStore,
    SourceStoreConflict,
    payload_hash,
)
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
from .qdrant_dual_write import MirrorWriteOutcome
from .server_runtime import (
    apply_server_redaction,
    document_from_ingress_payload,
    public_ingress_leak_violations,
)
from .state_db import RAGIngressStateDB


CouchDBMirrorOutcomeHook = Callable[[MirrorWriteOutcome], None]
_PAYLOAD_PREPARATION_ERRORS = (AttributeError, OverflowError, TypeError, ValueError)
_CHUNK_IDENTITY_FIELDS = (
    "doc_type",
    "session_id_hash",
    "chunk_id",
    "provider",
    "project",
    "redaction_version",
    "source_status",
)


def _default_couchdb_mirror_outcome_logger(outcome: MirrorWriteOutcome) -> None:
    """Emit only redaction-safe best-effort mirror state."""
    if outcome.status != "mirrored":
        print(
            json.dumps(
                {
                    "event": "qdrant_mirror_write",
                    "status": outcome.status,
                    "error_class": outcome.error_class,
                }
            ),
            flush=True,
        )


def _payload_integrity_evidence(
    job: DeliveryJobView,
    *,
    run: str,
) -> DeliveryBackendEvidence:
    return DeliveryBackendEvidence(
        idempotency_key=job.idempotency_key,
        payload_hash=job.payload_hash,
        dataset_ref="",
        document_ref="",
        run=run,
        status="payload_integrity_mismatch",
    )


def _active_pointer_membership_evidence(
    job: DeliveryJobView,
    *,
    run: str,
) -> DeliveryBackendEvidence:
    """Prevent a stale generic status reference from proving an unactivated chunk."""

    return DeliveryBackendEvidence(
        idempotency_key=job.idempotency_key,
        payload_hash=job.payload_hash,
        dataset_ref="",
        document_ref="",
        run=run,
        status="failed_retryable",
    )


def _chunk_documents_match(
    existing_chunk: Mapping[str, Any],
    expected_chunk: Mapping[str, Any],
) -> bool:
    return (
        all(
            str(existing_chunk.get(field) or "")
            == str(expected_chunk.get(field) or "")
            for field in _CHUNK_IDENTITY_FIELDS
        )
        and payload_hash(dict(existing_chunk)) == payload_hash(dict(expected_chunk))
    )


def _chunk_document_for_payload_identity(payload: Mapping[str, Any]) -> dict | None:
    """Build the exact authoritative chunk identity for a stored delivery payload.

    This mirrors the post-redaction chunk construction in ``submit`` without
    touching the store, so a natural-key lookup cannot treat a same-id orphan
    or a differently shaped chunk as an exact delivery.
    """

    try:
        redacted_payload = apply_server_redaction(dict(payload))
        package = redacted_payload.get("payload")
        source = redacted_payload.get("source")
        document = package.get("document") if isinstance(package, Mapping) else None
        metadata = document.get("metadata") if isinstance(document, Mapping) else None
        if not all(
            isinstance(value, Mapping)
            for value in (package, source, document, metadata)
        ):
            return None
        metadata = dict(metadata)
        document_body = str(document.get("body") or "")
        session_id_hash = str(metadata.get("session_id_hash") or "")
        chunk_id = str(metadata.get("chunk_id") or "")
        if not session_id_hash or not chunk_id:
            return None
        chunk = TranscriptChunk(
            chunk_id=chunk_id,
            session_id_hash=session_id_hash,
            provider=str(
                metadata.get("provider")
                or source.get("provider")
                or source.get("namespace")
                or "ingress"
            ),
            project=str(metadata.get("project") or source.get("project") or ""),
            turn_start_index=int(metadata.get("turn_start_index") or 0),
            turn_end_index=int(metadata.get("turn_end_index") or 0),
            redacted_text=document_body,
            content_hash=sha256_hash(document_body),
            redaction_version=str(package.get("redactionVersion") or REDACTION_VERSION),
            source_status="source_locator_private_spool_only",
            part_index=int(metadata.get("part_index") or 1),
            part_count=int(metadata.get("part_count") or 1),
            char_start=int(metadata.get("char_start") or 0),
            char_end=int(metadata.get("char_end") or len(document_body)),
            observed_at_start=str(metadata.get("observed_at_start") or ""),
            observed_at_end=str(
                metadata.get("observed_at_end")
                or metadata.get("observed_at_start")
                or ""
            ),
        )
        return build_conversation_chunk_document(chunk=chunk, source_locator_hash="")
    except _PAYLOAD_PREPARATION_ERRORS:
        return None


def _active_source_revision_before_ingress(
    *,
    store: CouchDBSourceStore,
    session_id_hash: str,
) -> ResolvedSourceRevision | None:
    """Resolve a pinned source set before any ingress write can alter it."""

    if store.get(active_source_revision_pointer_doc_id(session_id_hash)) is None:
        return None
    return resolve_active_source_revision(
        store=store,
        session_id_hash=session_id_hash,
    )


def _assert_active_revision_matches_ingress(
    *,
    active_revision: ResolvedSourceRevision,
    provider: str,
    project: str,
) -> None:
    if len(active_revision.sessions) != 1:
        raise SourceStoreConflict("active source revision session contract is invalid")
    session = active_revision.sessions[0]
    if (
        str(session.get("provider") or "") != provider
        or str(session.get("project") or "") != project
    ):
        raise SourceStoreConflict("active source revision ingress contract is invalid")


def _active_revision_source_document_ids(
    active_revision: ResolvedSourceRevision,
) -> tuple[str, ...]:
    """Use origins so a successor never mixes active copies with raw ingress."""

    return active_source_origin_document_ids(active_revision)


def _active_revision_provenance(
    *,
    store: CouchDBSourceStore,
    active_revision: ResolvedSourceRevision,
) -> dict[str, str]:
    """Reuse immutable manifest provenance for an exact active duplicate."""

    if not active_revision.manifest_id:
        raise SourceStoreConflict("active source revision manifest is missing")
    manifest = store.get(active_revision.manifest_id)
    provenance = (manifest or {}).get("provenance")
    if not isinstance(provenance, dict):
        raise SourceStoreConflict("active source revision provenance is invalid")
    return {str(key): str(value) for key, value in provenance.items()}


class CouchDBDeliveryBackend:
    """``DeliveryBackend`` protocol implementation over a :class:`CouchDBSourceStore`.

    Writes a live ingress ``conversation_chunk`` payload into the CouchDB source
    plane using the same document builders as the historical import path, so
    live-ingested docs are structurally identical to migration-built ones.

    ``state_db`` is the same :class:`.RAGIngressStateDB` used by the RetiredIndexBridge
    backend for payload resolution.  ``store`` is a :class:`CouchDBSourceStore`
    (typically :class:`CouchDBHttpSourceStore` in production, the in-memory
    fake in tests).
    """

    def __init__(
        self,
        *,
        state_db: RAGIngressStateDB,
        store: CouchDBSourceStore,
        mirror: Any | None = None,
        on_mirror_outcome: CouchDBMirrorOutcomeHook | None = None,
    ) -> None:
        self._state_db = state_db
        self._store = store
        self._mirror = mirror
        self._on_mirror_outcome = on_mirror_outcome

    # ------------------------------------------------------------------
    # DeliveryBackend Protocol
    # ------------------------------------------------------------------

    def submit(self, job: DeliveryJobView) -> DeliveryBackendEvidence:
        # --- Gate 1: payload availability + integrity -------------------------
        try:
            payload, gate = resolve_delivery_payload(
                self._state_db,
                idempotency_key=job.idempotency_key,
                expected_payload_hash=job.payload_hash,
            )
        except _PAYLOAD_PREPARATION_ERRORS:
            return _payload_integrity_evidence(job, run="invalid_stored_payload_shape")
        except Exception as exc:
            raise DeliveryOutcomeUncertain(exc.__class__.__name__) from exc
        if gate != PAYLOAD_OK:
            return DeliveryBackendEvidence(
                idempotency_key=job.idempotency_key,
                payload_hash=job.payload_hash,
                dataset_ref="",
                document_ref="",
                run="",
                status="payload_unavailable" if gate == PAYLOAD_MISSING else "payload_integrity_mismatch",
            )

        # --- Gate 2: detect an idempotent retry --------------------------------
        # A previous attempt may have persisted the deterministic session/chunk
        # documents and then failed before coverage/projection currentness was
        # refreshed.  Remember that this is an existing delivery for evidence,
        # but always run the aggregate reconciliation below.
        try:
            existing = self.find_by_natural_key(job.idempotency_key, job.payload_hash)
        except CouchDBError as exc:
            # A read failure before persistence cannot prove that no prior
            # write exists, so preserve the replay/reconcile boundary.
            raise DeliveryOutcomeUncertain(exc.__class__.__name__) from exc
        except Exception as exc:
            raise DeliveryOutcomeUncertain(exc.__class__.__name__) from exc
        if existing is not None and existing.status == "payload_integrity_mismatch":
            return existing

        # --- Gate 3: apply full server-side public-ingress redaction ----------
        raw_package = payload.get("payload")
        raw_source = payload.get("source")
        raw_document = (
            raw_package.get("document") if isinstance(raw_package, Mapping) else None
        )
        raw_metadata = (
            raw_document.get("metadata") or {}
            if isinstance(raw_document, Mapping)
            else None
        )
        if not all(
            isinstance(value, Mapping)
            for value in (raw_package, raw_source, raw_document, raw_metadata)
        ):
            return _payload_integrity_evidence(job, run="invalid_stored_payload_shape")
        try:
            payload = apply_server_redaction(payload)
        except _PAYLOAD_PREPARATION_ERRORS:
            return _payload_integrity_evidence(job, run="invalid_stored_payload_shape")

        # --- Gate 4: fail-closed public-ingress leak check --------------------
        pkg = payload.get("payload") or {}
        document = pkg.get("document") or {}
        raw_metadata = document.get("metadata") or {}
        if not isinstance(raw_metadata, Mapping):
            return _payload_integrity_evidence(job, run="invalid_document_metadata")
        metadata = dict(raw_metadata)
        source = payload.get("source") or {}
        document_body = str(document.get("body") or "")
        leak_surface = json.dumps(
            {
                "body": document_body,
                "filename": document.get("filename") or "",
                "metadata": metadata,
                "source": {
                    "project": source.get("project") or "",
                    "host": source.get("host") or "",
                },
            },
            sort_keys=True,
            default=str,
        )
        leak_violations = public_ingress_leak_violations(leak_surface)
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
        session_id_hash = str(metadata.get("session_id_hash") or "")
        provider = str(
            metadata.get("provider") or source.get("provider") or source.get("namespace") or "ingress"
        )
        project = str(metadata.get("project") or source.get("project") or "")
        chunk_id = str(metadata.get("chunk_id") or "")
        redaction_version = str(pkg.get("redactionVersion") or REDACTION_VERSION)
        observed_at_start = str(metadata.get("observed_at_start") or "")
        observed_at_end = str(metadata.get("observed_at_end") or observed_at_start)

        # Positional chunk metadata -- fall back to 0/1 if dendrite didn't emit them
        try:
            turn_start_index = int(metadata.get("turn_start_index") or 0)
            turn_end_index = int(metadata.get("turn_end_index") or 0)
            part_index = int(metadata.get("part_index") or 1)
            part_count = int(metadata.get("part_count") or 1)
            char_start = int(metadata.get("char_start") or 0)
            char_end = int(metadata.get("char_end") or len(document_body))
        except (OverflowError, TypeError, ValueError):
            return _payload_integrity_evidence(job, run="invalid_positional_metadata")

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
            observed_at_start=observed_at_start,
            observed_at_end=observed_at_end,
        )

        session = TranscriptSession(
            session_id_hash=session_id_hash,
            provider=provider,
            project=project,
            started_at=observed_at_start,
            ended_at=observed_at_end,
            source_status="source_unproven",
            source_locator_hash="",  # private Mac path; not sent server-side
            observed_at_start=observed_at_start,
            observed_at_end=observed_at_end,
        )

        db_name = getattr(self._store, "db", "couchdb")
        dataset_ref = f"couchdb:{db_name}"
        doc_ref = session_doc_id(session_id_hash)

        active_duplicate = False
        try:
            session_doc = build_transcript_session_document(session=session)
            chunk_doc = build_conversation_chunk_document(chunk=chunk, source_locator_hash="")
            active_revision = _active_source_revision_before_ingress(
                store=self._store,
                session_id_hash=session_id_hash,
            )
            if active_revision is None:
                # Legacy/unpinned behavior remains the aggregate reconciliation
                # path: a retry can repair a partial earlier write.
                chunk_revision = self._store.put(chunk_doc)
                upsert_transcript_session_aggregate(
                    store=self._store,
                    incoming=session_doc,
                )
                coverage_doc = update_coverage_with_tool_evidence(
                    session_id_hash=session_id_hash,
                    store=self._store,
                )
                source_hash = str((coverage_doc or {}).get("source_hash") or "")
                mark_projection_pending_if_source_changed(
                    session_id_hash=session_id_hash,
                    provider=provider,
                    project=project,
                    source_hash=source_hash,
                    store=self._store,
                    source_changed=chunk_revision.outcome != "duplicate",
                )
            else:
                # A pointer makes the selected source set immutable. Resolve it
                # before the new write, then only append a genuinely new chunk
                # through an explicit allowlist activation.
                _assert_active_revision_matches_ingress(
                    active_revision=active_revision,
                    provider=provider,
                    project=project,
                )
                chunk_document_id = str(chunk_doc["_id"])
                active_chunk_ids = {
                    str(
                        document.get("source_snapshot_origin_id")
                        or document["_id"]
                    )
                    for document in active_revision.conversation_chunks
                }
                existing_chunk = self._store.get(chunk_document_id)
                if existing_chunk is not None:
                    if not _chunk_documents_match(existing_chunk, chunk_doc):
                        # This is a known immutable source-identity conflict,
                        # not an unobservable store outcome. Return terminal
                        # integrity evidence so the executor quarantines the
                        # job instead of replaying it over the existing chunk.
                        return _payload_integrity_evidence(
                            job,
                            run="chunk_id_payload_mismatch",
                        )
                    # An exact orphan can remain after an earlier activation CAS
                    # loss. Reuse it in the next explicit revision instead of
                    # treating it as a no-op duplicate.
                    active_duplicate = chunk_document_id in active_chunk_ids
                else:
                    try:
                        chunk_revision = self._store.put_if_absent(chunk_doc)
                    except SourceStoreConflict:
                        # A concurrent writer can create this deterministic id
                        # after our pre-read. Re-read only to classify an
                        # observable identity collision; all other conflicts
                        # remain uncertain and retain replay semantics.
                        raced_chunk = self._store.get(chunk_document_id)
                        if raced_chunk is not None and not _chunk_documents_match(
                            raced_chunk, chunk_doc
                        ):
                            return _payload_integrity_evidence(
                                job,
                                run="chunk_id_payload_mismatch",
                            )
                        raise
                    active_duplicate = (
                        chunk_revision.outcome == "duplicate"
                        and chunk_document_id in active_chunk_ids
                    )

                activated = activate_source_revision(
                    store=self._store,
                    session_id_hash=session_id_hash,
                    source_document_ids=tuple(
                        sorted(
                            {
                                *_active_revision_source_document_ids(active_revision),
                                chunk_document_id,
                            }
                        )
                    ),
                    provenance=(
                        _active_revision_provenance(
                            store=self._store,
                            active_revision=active_revision,
                        )
                        if active_duplicate
                        else None
                    ),
                    expected_predecessor=active_revision,
                )
                # A retry may find its chunk in the active revision after the
                # pointer CAS. Re-run activation with its complete mutable
                # origin set before reconciling secondary records: unchanged
                # input is an idempotent pointer duplicate, while an origin
                # drifted during that CAS becomes a new immutable successor.
                coverage_doc = update_coverage_with_tool_evidence(
                    session_id_hash=session_id_hash,
                    store=self._store,
                )
                if (
                    coverage_doc is None
                    or str(coverage_doc.get("source_hash") or "")
                    != activated.source_hash
                ):
                    raise SourceStoreConflict(
                        "active source revision coverage did not converge"
                    )
                mark_projection_pending_if_source_changed(
                    session_id_hash=session_id_hash,
                    provider=provider,
                    project=project,
                    source_hash=activated.source_hash,
                    store=self._store,
                    source_changed=True,
                )

        except CouchDBError as exc:
            # Network/store errors mid-flight: the PUT may have reached CouchDB
            # before the exception.  Signal uncertain so the executor does NOT
            # record this as a clean retryable failure.
            raise DeliveryOutcomeUncertain(exc.__class__.__name__) from exc
        except Exception as exc:
            raise DeliveryOutcomeUncertain(exc.__class__.__name__) from exc

        # The Qdrant mirror is strictly post-authoritative and best-effort. A
        # partial primary attempt may have stored the chunk but failed before
        # aggregate reconciliation; its successful retry must still reach the
        # mirror. Canonical succeeded-job duplicates never call ``submit`` from
        # DeliveryExecutor, so they still have exactly one mirror side effect.
        # The mirror sees the full server-redacted, leak-gated payload.
        self._submit_mirror_after_authoritative_success(payload)

        return DeliveryBackendEvidence(
            idempotency_key=job.idempotency_key,
            payload_hash=job.payload_hash,
            dataset_ref=dataset_ref,
            document_ref=doc_ref,
            run="couchdb_existing" if existing is not None or active_duplicate else "couchdb_put",
            status="succeeded",
            observed_at=datetime.datetime.now(tz=datetime.timezone.utc),
        )

    def _submit_mirror_after_authoritative_success(self, redacted_payload: dict) -> None:
        if self._mirror is None:
            return
        try:
            document = document_from_ingress_payload(redacted_payload)
            result = self._mirror.submit_document(document)
            outcome = MirrorWriteOutcome(
                status="mirrored",
                document_ref=str(getattr(result, "document_ref", "") or ""),
            )
        except Exception as exc:  # mirror cannot affect authoritative success
            outcome = MirrorWriteOutcome(
                status="mirror_error", error_class=exc.__class__.__name__
            )
        if self._on_mirror_outcome is not None:
            try:
                self._on_mirror_outcome(outcome)
            except Exception:
                pass

    def find_by_natural_key(
        self, idempotency_key: str, payload_hash: str
    ) -> DeliveryBackendEvidence | None:
        """Return success evidence only for an exact authoritative chunk.

        With an active source pointer, an orphan raw chunk after a failed
        activation CAS is not delivery evidence. The resolved pointer must
        include the exact payload under that chunk's origin id; resolution also
        verifies the active member and source-hash contracts.
        """
        row = self._state_db.get_row("delivery_jobs", "idempotency_key", idempotency_key)
        if row is None or str(row.get("payload_hash") or "") != payload_hash:
            return None
        job = DeliveryJobView.from_row(row)

        payload = self._state_db.get_delivery_payload(idempotency_key)
        if payload is None:
            return None
        expected_chunk = _chunk_document_for_payload_identity(payload)
        if expected_chunk is None:
            return None

        session_id_hash = str(expected_chunk.get("session_id_hash") or "")
        chunk_doc_id = str(expected_chunk.get("_id") or "")
        if not session_id_hash or not chunk_doc_id:
            return None

        try:
            active_revision = _active_source_revision_before_ingress(
                store=self._store,
                session_id_hash=session_id_hash,
            )
        except (SourceRevisionResolutionError, ValueError):
            # A malformed active control plane cannot prove this delivery.
            # ``submit`` will still attempt an authoritative repair, while
            # reconciliation receives no false success evidence from a stale
            # session reference.
            return _active_pointer_membership_evidence(
                job,
                run="active_pointer_control_unresolved",
            )
        if active_revision is not None:
            active_chunks = [
                document
                for document in active_revision.conversation_chunks
                if str(
                    document.get("source_snapshot_origin_id")
                    or document.get("_id")
                    or ""
                )
                == chunk_doc_id
            ]
            if len(active_chunks) == 1:
                active_chunk = active_chunks[0]
                if not _chunk_documents_match(active_chunk, expected_chunk):
                    return _payload_integrity_evidence(
                        job,
                        run="chunk_id_payload_mismatch",
                    )
            elif not active_chunks:
                # An unactivated exact orphan can still be reconciled by a
                # later submit. It is not success evidence: a stale session
                # reference must not let reconciliation bypass the active
                # source membership check. A conflicting orphan is terminal.
                existing_chunk = self._store.get(chunk_doc_id)
                if existing_chunk is not None and not _chunk_documents_match(
                    existing_chunk, expected_chunk
                ):
                    return _payload_integrity_evidence(
                        job,
                        run="chunk_id_payload_mismatch",
                    )
                return _active_pointer_membership_evidence(
                    job,
                    run="active_pointer_member_missing",
                )
            else:
                return _active_pointer_membership_evidence(
                    job,
                    run="active_pointer_member_ambiguous",
                )
        else:
            existing_chunk = self._store.get(chunk_doc_id)
            if existing_chunk is None:
                return None
            if not _chunk_documents_match(existing_chunk, expected_chunk):
                return _payload_integrity_evidence(
                    job,
                    run="chunk_id_payload_mismatch",
                )
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
    environ: Mapping[str, str] | None = None,
    mirror_builder: Callable[[Mapping[str, str]], Any | None] | None = None,
    on_mirror_outcome: CouchDBMirrorOutcomeHook | None = None,
) -> CouchDBDeliveryBackend:
    """Factory used by the env-switch wiring in state_cli to build the backend.

    Kept in this module so :mod:`.state_cli` does not import couchdb_http_store
    directly (a retired-index-bridge-free boundary).
    """
    import base64

    from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore

    required_config = {
        "COUCHDB_URL": str(couchdb_url or ""),
        "COUCHDB_USER": str(couchdb_user or ""),
        "COUCHDB_PASSWORD": str(couchdb_password or ""),
        "COUCHDB_DB": str(couchdb_db or ""),
    }
    missing = [name for name, value in required_config.items() if not value.strip()]
    if missing:
        raise ValueError(
            "CouchDB delivery requires non-empty " + ", ".join(missing)
        )
    credentials = base64.b64encode(f"{couchdb_user}:{couchdb_password}".encode()).decode()
    store = CouchDBHttpSourceStore(
        base_url=couchdb_url,
        db=couchdb_db,
        auth_header=f"Basic {credentials}",
    )
    effective_environ = environ or {}
    mirror = None
    outcome_hook = on_mirror_outcome or _default_couchdb_mirror_outcome_logger
    if str(effective_environ.get("MIRROR_DUAL_WRITE") or "").strip() == "1":
        from .qdrant_dual_write import build_qdrant_mirror_from_env

        try:
            mirror = (mirror_builder or build_qdrant_mirror_from_env)(effective_environ)
        except Exception as exc:
            try:
                outcome_hook(
                    MirrorWriteOutcome(
                        status="mirror_build_error", error_class=exc.__class__.__name__
                    )
                )
            except Exception:
                pass
    return CouchDBDeliveryBackend(
        state_db=state_db,
        store=store,
        mirror=mirror,
        on_mirror_outcome=outcome_hook if mirror is not None else None,
    )


__all__ = [
    "CouchDBDeliveryBackend",
    "CouchDBMirrorOutcomeHook",
    "build_couchdb_delivery_backend",
]

"""Server-owned state DB ingress sink.

This module keeps the old ``StateDBIngressSink`` entrypoint available on the
server side without importing the transcript-ingest worker, HTTP enqueue client,
or any client outbox code. It accepts the redacted ``rag_ingress_enqueue.v1``
payload contract and writes only to ``RAGIngressStateDB``.
"""

from __future__ import annotations

from typing import Mapping

from .backfill_apply import apply_backfill_to_state_db
from .rag_ready_document import (
    DEFAULT_CONTENT_TYPE,
    DEFAULT_INGRESS_PAYLOAD_KIND,
    DEFAULT_REDACTION_VERSION,
    INGRESS_SCHEMA_VERSION,
)
from .server_runtime import job_id_for_payload, validate_ingress_payload


def authority_shadow_report(client) -> dict | None:
    """Return in-memory shadow counters when a sink/client carries injections."""
    if client is None:
        return None
    if getattr(client, "journal", None) is None and getattr(client, "dual_write_state_db", None) is None:
        return None
    return {
        "journal_fail_count": int(getattr(client, "journal_fail_count", 0)),
        "dual_write_fail_count": int(getattr(client, "dual_write_fail_count", 0)),
        "dual_write_conflict_count": int(getattr(client, "dual_write_conflict_count", 0)),
    }


def build_state_sink_enqueue_payload(
    *,
    source: Mapping[str, object],
    packed,
    content_hash: str,
    target_profile: str,
    kind: str,
    idempotency_key: str,
) -> dict:
    """Build the existing enqueue wire payload from a packed document object."""
    metadata = getattr(packed, "metadata", {}) or {}
    return {
        "schemaVersion": INGRESS_SCHEMA_VERSION,
        "source": dict(source),
        "payload": {
            "kind": DEFAULT_INGRESS_PAYLOAD_KIND,
            "redactionVersion": DEFAULT_REDACTION_VERSION,
            "document": {
                "filename": str(getattr(packed, "filename", "")),
                "contentType": DEFAULT_CONTENT_TYPE,
                "body": str(getattr(packed, "body", "")),
                "metadata": _string_metadata(dict(metadata)),
            },
        },
        "contentHash": content_hash,
        "targetProfile": target_profile,
        "kind": kind,
        "idempotencyKey": idempotency_key,
    }


class StateDBIngressSink:
    """Accept redacted ingress payloads into the server state DB candidate."""

    def __init__(self, *, state_db, journal=None):
        if state_db is None:
            raise ValueError("state db is required")
        self.state_db = state_db
        self.journal = journal
        # Duck type retained for authority_shadow_report and migration shims.
        self.dual_write_state_db = state_db
        self.journal_fail_count = 0
        self.dual_write_fail_count = 0
        self.dual_write_conflict_count = 0

    def enqueue_document(
        self,
        *,
        source: Mapping[str, object],
        packed,
        content_hash: str,
        target_profile: str,
        kind: str,
        idempotency_key: str,
    ) -> dict:
        request_body = build_state_sink_enqueue_payload(
            source=source,
            packed=packed,
            content_hash=content_hash,
            target_profile=target_profile,
            kind=kind,
            idempotency_key=idempotency_key,
        )
        return self.accept_payload(request_body)

    def enqueue_payload(self, payload: Mapping[str, object]) -> dict:
        return self.accept_payload(payload)

    def accept_payload(self, payload: Mapping[str, object]) -> dict:
        request_body = dict(payload)
        validate_ingress_payload(request_body)
        self._record_journal(request_body)
        try:
            result = apply_backfill_to_state_db(
                state_db=self.state_db,
                payloads=[request_body],
                dry_run=False,
            )
        except Exception as exc:
            self.dual_write_fail_count += 1
            raise RuntimeError("state db accept failed") from exc
        conflict_count = int(result.get("conflict_count") or 0)
        self.dual_write_conflict_count += conflict_count
        if conflict_count:
            raise RuntimeError("state db accept rejected: conflict")
        return {"job_id": job_id_for_payload(request_body), "status": "queued"}

    def _record_journal(self, request_body: dict) -> None:
        if self.journal is None:
            return
        try:
            if self.journal.record(request_body) is not True:
                self.journal_fail_count += 1
        except Exception:
            self.journal_fail_count += 1


def _string_metadata(metadata: Mapping[str, object]) -> dict[str, str]:
    return {str(key): str(value) for key, value in metadata.items()}

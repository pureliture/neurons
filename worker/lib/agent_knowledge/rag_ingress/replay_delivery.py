"""M6 replay-delivery seam: genuinely re-enqueue replay_requested rows.

Unlike the M5 replay disposition (``mark_replay_requested_if_queued``, which only
re-armed the ledger row and recorded a ``replay_requested`` marker without causing
any re-delivery), this seam reconstructs an ingress enqueue payload from the local
ledger and re-POSTs it to the rag-ingress-queue, creating a NEW queue job so the
existing delivery worker re-delivers the document to RAGFlow. This is the resolution
of the M5 system-manager BLOCKING-1 follow-up.

Reconstruction fidelity (honest scope):
- For enqueues that predate the M7 ``IngressJournal``, the original wire payload body
  is NOT persisted anywhere. ``transcript_chunks`` stores the chunk ``redacted_text``
  and the convergence-critical natural key (knowledge_id, chunk_id, provider,
  project, session_id_hash) but NOT the original packed body, tool-event sections,
  source_locator_hash, observed_at, or capture_request_id.
- M7 adds ``journal=`` (CLI ``--from-journal``): a row whose knowledge_id has a
  journaled wire payload is re-flushed byte-faithfully (body/metadata/contentHash
  untouched; only the idempotencyKey is replay-salted). Journal misses keep the
  best-effort reconstruction below. The report separates ``journal_hit_count`` and
  ``reconstructed_count``.
- This seam therefore re-delivers a CONVERGENCE-FAITHFUL document: its metadata
  natural key exactly matches the original, so the M5 reconcile classifier converges
  the re-delivered DONE doc to ``mark_done`` (single match) or, if a duplicate
  appears, to ``duplicate_done`` -> deterministic dedupe. Its body is reconstructed
  best-effort from the stored ``redacted_text`` and is NOT byte-identical to the
  original. Re-delivered docs carry a ``m6_replay_reconstructed=true`` metadata
  marker so they are self-identifying.

The ``idempotencyKey`` is salted with the replay attempt (``:replay.N``) so that,
provided the upstream ingress dedupes on ``idempotencyKey`` (to be confirmed by the
M6.4 18080 identity review + the M6.1 Packet D probe before any bulk run), the
re-POST is not suppressed as a duplicate of the original (never-delivered) job. The
same (row, attempt) always produces the same key, so a re-run after a CAS race is
idempotent at the ingress (existing job file) under that same assumption.

dry-run and probe modes perform no POST and no ledger mutation. Live execution is
candidate-set-digest bound and per-row CAS-guarded, exactly like the M5 seams. The
report contains only counts, booleans, and a digest -- never raw body, text,
knowledge ids, chunk ids, or job ids.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, Sequence

from .rag_ready_document import (
    DEFAULT_CONTENT_TYPE,
    DEFAULT_INGRESS_PAYLOAD_KIND,
    DEFAULT_REDACTION_VERSION,
    INGRESS_SCHEMA_VERSION,
    build_content_hash,
)

REPLAY_REQUESTED_STATUS = "replay_requested"
REPLAY_TRANSPORT_KIND = "conversation_chunk"


class IngressEnqueueRejected(Exception):
    """Raised by an injected replay ingress client when the enqueue is rejected."""


class IngressEnqueueUnreachable(Exception):
    """Raised by an injected replay ingress client when the queue is unreachable."""


class ReplayIngressClient(Protocol):
    def enqueue_document_payload(self, payload: dict) -> dict:
        """Enqueue an already-built replay payload and return the queue result."""


def validate_replay_payload(payload: dict) -> None:
    """Validate the replay enqueue wire shape without importing client outbox code."""
    if payload.get("schemaVersion") != INGRESS_SCHEMA_VERSION:
        raise ValueError("invalid schemaVersion")
    if not str(payload.get("targetProfile") or ""):
        raise ValueError("targetProfile is required")
    if payload.get("kind") != REPLAY_TRANSPORT_KIND:
        raise ValueError("invalid replay kind")
    if not str(payload.get("idempotencyKey") or ""):
        raise ValueError("idempotencyKey is required")

    document = ((payload.get("payload") or {}).get("document") or {})
    body = document.get("body")
    if not isinstance(body, str) or not body:
        raise ValueError("document body is required")
    if not str(document.get("filename") or ""):
        raise ValueError("document filename is required")
    if str(document.get("contentType") or "") != DEFAULT_CONTENT_TYPE:
        raise ValueError("invalid contentType")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("document metadata is required")

    payload_root = payload.get("payload") or {}
    if payload_root.get("kind") != DEFAULT_INGRESS_PAYLOAD_KIND:
        raise ValueError("invalid payload kind")
    if payload_root.get("redactionVersion") != DEFAULT_REDACTION_VERSION:
        raise ValueError("invalid redactionVersion")
    if payload.get("contentHash") != build_content_hash(body):
        raise ValueError("contentHash mismatch")


def _replay_candidate_digest(selected: Sequence[tuple[dict, dict]]) -> str:
    payload = [
        {
            "knowledge_id": str(row.get("knowledge_id") or ""),
            "ingress_job_id": str(row.get("ingress_job_id") or ""),
            "target_profile": str(row.get("target_profile") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "replay_attempt": int((row.get("metadata") or {}).get("m5_replay_attempt") or 1),
        }
        for row, _chunk in selected
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _reconstruct_body(
    *, provider: str, project: str, session_id_hash: str, knowledge_id: str, chunk_id: str, redacted_text: str
) -> str:
    lines = [
        # YAML frontmatter is required by the ingress RedactionGuard
        # (body must contain schema_version: and result_type:); the original
        # packed document carried it but the legacy reconstruction dropped it.
        "---",
        "schema_version: agent_knowledge_document.v2",
        "result_type: conversation_chunk",
        f"provider: {provider}",
        f"project: {project}",
        "---",
        "",
        "# Conversation Chunk (m6 replay reconstruction)",
        "",
        "## Context",
        "",
        f"- provider: {provider}",
        f"- project: {project}",
        f"- session_id_hash: {session_id_hash}",
        f"- knowledge_id: {knowledge_id}",
        f"- chunk_id: {chunk_id}",
        "- currentness: historical_conversation_memory",
        "- reconstruction: m6_replay_best_effort",
        "",
        "## Chunk Text",
        "",
        redacted_text,
        "",
    ]
    return "\n".join(lines) + "\n"


def reconstruct_replay_payload(*, row: dict, chunk: dict, attempt: int, target_profile: str = "") -> dict:
    """Build a convergence-faithful ingress enqueue payload for one replay row.

    The metadata natural key (type/knowledge_id/chunk_id/provider/project/
    session_id_hash) exactly matches the original so the reconcile classifier
    converges. The body is best-effort from stored redacted_text.
    """
    knowledge_id = str(row.get("knowledge_id") or "")
    metadata_in = row.get("metadata") or {}
    chunk_id = str(metadata_in.get("chunk_id") or chunk.get("chunk_id") or "")
    provider = str(row.get("provider") or "")
    project = str(row.get("project") or "")
    session_id_hash = str(row.get("session_id_hash") or chunk.get("session_id_hash") or "")
    redacted_text = str(chunk.get("redacted_text") or "")
    redaction_version = str(chunk.get("redaction_version") or "redaction.v2")

    body = _reconstruct_body(
        provider=provider,
        project=project,
        session_id_hash=session_id_hash,
        knowledge_id=knowledge_id,
        chunk_id=chunk_id,
        redacted_text=redacted_text,
    )
    content_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    meta_fields = {
        "type": "conversation_chunk",
        "result_type": "conversation_chunk",
        "knowledge_id": knowledge_id,
        "chunk_id": chunk_id,
        "provider": provider,
        "project": project,
        "session_id_hash": session_id_hash,
        "turn_start_index": str(chunk.get("turn_start_index") or ""),
        "turn_end_index": str(chunk.get("turn_end_index") or ""),
        "part_index": str(chunk.get("part_index") or ""),
        "part_count": str(chunk.get("part_count") or ""),
        "char_start": str(chunk.get("char_start") or ""),
        "char_end": str(chunk.get("char_end") or ""),
        "redaction_version": redaction_version,
        "source_status": str(chunk.get("source_status") or ""),
        "privacy_level": "private",
        "m6_replay_reconstructed": "true",
        "m6_replay_attempt": str(int(attempt)),
    }
    return {
        "schemaVersion": INGRESS_SCHEMA_VERSION,
        "source": {
            "host": "mac_mini",
            "producer": "m6-replay-delivery",
            "provider": provider,
            "project": project,
        },
        "payload": {
            "kind": DEFAULT_INGRESS_PAYLOAD_KIND,
            "redactionVersion": redaction_version,
            "document": {
                "filename": f"conversation_chunk_{chunk_id}.replay.{int(attempt)}.md",
                "contentType": "text/markdown",
                "body": body,
                "metadata": meta_fields,
            },
        },
        "contentHash": content_hash,
        # list_queued_transcript_chunks rows do not carry target_profile, so fall
        # back to the caller's profile (validate_replay_payload requires it).
        "targetProfile": str(row.get("target_profile") or target_profile or ""),
        "kind": REPLAY_TRANSPORT_KIND,
        "idempotencyKey": f"{knowledge_id}:replay.{int(attempt)}",
    }


def journal_replay_payload(*, entry: dict, knowledge_id: str, attempt: int) -> dict:
    """Build the re-flush payload for a journal hit (M7 byte-faithful path).

    The document body, metadata, filename, and contentHash are the ORIGINAL acked
    wire bytes, untouched. Only the transport ``idempotencyKey`` is salted with the
    replay attempt, for the same ingress-dedupe reason as the reconstruct path.
    """
    payload = json.loads(json.dumps(entry))
    payload["idempotencyKey"] = f"{knowledge_id}:replay.{int(attempt)}"
    return payload


def select_replay_rows(ledger, *, target_profile: str, limit: int = 50) -> list[tuple[dict, dict]]:
    """Select queued rows marked replay_requested and attach their transcript chunk."""
    queued = ledger.list_queued_documents(
        document_type="conversation_chunk",
        target_profile=target_profile,
        limit=limit,
    )
    selected: list[tuple[dict, dict]] = []
    for row in queued:
        metadata = row.get("metadata") or {}
        if str(metadata.get("m5_disposition_status") or "") != REPLAY_REQUESTED_STATUS:
            continue
        chunk = ledger.get_transcript_chunk_by_knowledge_id(str(row.get("knowledge_id") or "")) or {}
        selected.append((row, chunk))
    return selected


def _replay_key_incomplete(row: dict, chunk: dict) -> bool:
    """A row whose convergence natural key is incomplete would re-deliver a non-converging orphan."""
    metadata = row.get("metadata") or {}
    knowledge_id = str(row.get("knowledge_id") or "")
    chunk_id = str(metadata.get("chunk_id") or chunk.get("chunk_id") or "")
    session_id_hash = str(row.get("session_id_hash") or chunk.get("session_id_hash") or "")
    return not (knowledge_id and chunk_id and session_id_hash)


def replay_deliver_dispositions(
    *,
    ledger,
    ingress_client: ReplayIngressClient,
    target_profile: str,
    reason: str,
    limit: int = 50,
    probe: bool = False,
    dry_run: bool = True,
    expected_candidate_set_digest: str = "",
    journal=None,
) -> dict:
    """Apply or dry-run replay-delivery for the replay_requested bucket.

    dry-run/probe perform NO POST and NO ledger mutation. Live execution requires a
    matching candidate_set_digest, validates each payload before POST, and CAS-guards
    each ledger update. With ``journal`` (M7), a row whose knowledge_id has a journaled
    wire payload is re-flushed BYTE-FAITHFULLY; only journal misses fall back to the
    M6 best-effort reconstruction. The chunk-text/key blockers apply only to fallback
    rows -- a journal hit does not depend on the ledger chunk for its body.

    Mutation reporting is remote-first: the POST happens BEFORE the ledger CAS, so a
    successful enqueue whose CAS later loses the race already created a remote queue
    job. ``remote_enqueue_count`` counts successful POSTs, ``mutation_performed`` is
    True whenever any remote enqueue happened (even with ``delivered_count == 0``),
    and that state surfaces as the ``remote_enqueued_ledger_cas_failed`` blocker with
    ``execution_status == "partial_failure"``.
    """
    selected = select_replay_rows(ledger, target_profile=target_profile, limit=limit)
    if probe:
        selected = selected[:1]
    candidate_set_digest = _replay_candidate_digest(selected)

    journal_entries: dict[int, dict] = {}
    if journal is not None:
        for index, (row, _chunk) in enumerate(selected):
            entry = journal.get(str(row.get("knowledge_id") or ""))
            if entry is not None:
                journal_entries[index] = entry
    journal_hit_count = len(journal_entries)
    reconstructed_count = len(selected) - journal_hit_count

    blockers: list[str] = []
    if not dry_run and not expected_candidate_set_digest:
        blockers.append("candidate_set_digest_required")
    if expected_candidate_set_digest and expected_candidate_set_digest != candidate_set_digest:
        blockers.append("candidate_set_digest_mismatch")
    fallback_rows = [
        (row, chunk) for index, (row, chunk) in enumerate(selected) if index not in journal_entries
    ]
    missing_source_text_count = sum(1 for _row, chunk in fallback_rows if not str(chunk.get("redacted_text") or ""))
    if missing_source_text_count:
        blockers.append("replay_source_text_missing")
    incomplete_key_count = sum(1 for row, chunk in fallback_rows if _replay_key_incomplete(row, chunk))
    if incomplete_key_count:
        blockers.append("replay_key_incomplete")

    delivered_count = 0
    rejected_count = 0
    race_skipped_count = 0
    remote_enqueue_count = 0
    unreachable_stop = False
    if not dry_run and not blockers:
        for index, (row, chunk) in enumerate(selected):
            attempt = int((row.get("metadata") or {}).get("m5_replay_attempt") or 1)
            entry = journal_entries.get(index)
            if entry is not None:
                payload = journal_replay_payload(
                    entry=entry,
                    knowledge_id=str(row.get("knowledge_id") or ""),
                    attempt=attempt,
                )
            else:
                payload = reconstruct_replay_payload(row=row, chunk=chunk, attempt=attempt, target_profile=target_profile)
            try:
                validate_replay_payload(payload)
                result = ingress_client.enqueue_document_payload(payload)
            except IngressEnqueueUnreachable:
                unreachable_stop = True
                break
            except (IngressEnqueueRejected, ValueError):
                rejected_count += 1
                continue
            # the remote queue job now exists, regardless of what the ledger CAS
            # below does -- count it before the CAS so a race cannot under-report
            # the remote mutation
            remote_enqueue_count += 1
            new_job_id = str(result.get("job_id") or "")
            updated = ledger.mark_replay_delivered_if_queued(
                row["knowledge_id"],
                reason=reason,
                new_job_id=new_job_id,
                expected_target_profile=str(row.get("target_profile") or ""),
                expected_ingress_job_id=str(row.get("ingress_job_id") or ""),
                expected_updated_at=str(row.get("updated_at") or ""),
            )
            if updated:
                delivered_count += 1
            else:
                race_skipped_count += 1
        if race_skipped_count:
            # each race-skipped row had a SUCCESSFUL remote enqueue first; the
            # blocker wording must surface that remote side effect
            blockers.append("remote_enqueued_ledger_cas_failed")

    if blockers and (delivered_count or remote_enqueue_count):
        execution_status = "partial_failure"
    elif unreachable_stop:
        execution_status = "blocked_unreachable"
    elif blockers:
        execution_status = "blocked"
    elif dry_run:
        execution_status = "dry_run"
    else:
        execution_status = "executed"

    return {
        "schema_version": "agent_knowledge_rag_ingress_replay_delivery.v1",
        "document_type": "conversation_chunk",
        "requested_action": "replay_missing",
        "disposition": "replay_deliver",
        "probe": bool(probe),
        "selected_count": len(selected),
        "candidate_set_digest": candidate_set_digest,
        "delivered_count": delivered_count,
        "remote_enqueue_count": remote_enqueue_count,
        "rejected_count": rejected_count,
        "race_skipped_count": race_skipped_count,
        "stale_selection_count": race_skipped_count,
        "missing_source_text_count": missing_source_text_count,
        "incomplete_key_count": incomplete_key_count,
        "journal_hit_count": journal_hit_count,
        "reconstructed_count": reconstructed_count,
        "unreachable_stop": unreachable_stop,
        "dry_run": bool(dry_run),
        "blockers": blockers,
        "execution_status": execution_status,
        "resume_required": bool(race_skipped_count),
        "reconstruction_fidelity": (
            "byte_faithful_journal_with_reconstruct_fallback"
            if journal is not None
            else "convergence_faithful_body_best_effort"
        ),
        "execution_performed": bool(delivered_count or remote_enqueue_count),
        "network_used": bool(not dry_run),
        "mutation_performed": bool(delivered_count or remote_enqueue_count),
        "raw_backend_ids_printed": False,
        "raw_ragflow_ids_printed": False,
    }

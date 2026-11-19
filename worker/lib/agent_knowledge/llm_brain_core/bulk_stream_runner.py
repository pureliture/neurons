"""Trial-only streamed bulk runner: validated source, fenced writes, shadow-only."""

from __future__ import annotations

import os
import hashlib
import time
import uuid
from typing import Any

from agent_knowledge.couchdb_source.document_model import SourceDocType
from agent_knowledge.couchdb_source.spool_reader import SpoolBudgetExceeded, SpoolBusy
from .bulk_authority import revalidate_authority_sources
from agent_knowledge.couchdb_source.source_revision import SourceRevisionCurrentnessLimitError
from .bulk_authority import authorized_sources_by_hash
from .bulk_full_input_reader import PgAuthorityCardStore
from .bulk_full_input import SourceInvalid, shadow_group_ids
from .bulk_semantic import BulkRequestLimitExceeded, BulkSemanticSessionInput
from .bulk_source_validation import plan_validated_source
from .bulk_streaming import (
    expected_source_bytes, iter_batches, iter_planned_chunks, planned_chunk_key, safe_reason,
)
from .couchdb_projection_cli import _select_sessions_bounded
from .ledger_adapter import EXTRACTION_LEVEL_ENTITY, LedgerGraphProjectionStateStore
from .models import OntologyEpisode
from .runtime import session_source_revision_from_couchdb_source
from agent_knowledge.ledger import Ledger


def _pg_authority_by_hash(dsn: str, project: str, hashes: list[str], *, as_of=None):
    return authorized_sources_by_hash(dsn, project, hashes, as_of=as_of)

def _opaque(prefix: str, material: str) -> str:
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _supersede(state: Any, keys: list[str]) -> None:
    if hasattr(state, "supersede_keys"):
        state.supersede_keys(keys)




def _close_spooled_items(batch_items: list[tuple[Any, Any]]) -> None:
    for _session, item in batch_items:
        close = getattr(item.text, "close", None)
        if close is not None:
            close()
def run_streamed_full_input(
    *,
    ledger_path: Any,
    source_store: Any,
    extractor: Any,
    writer: Any,
    full_input_state: Any,
    policy_version: str,
    project: str,
    limit: int,
    forbidden_markers: tuple[bytes, ...] = (),
    batch_size: int = 8,
    max_chunk_bytes: int | None = None,
) -> dict[str, Any]:
    effective_max_chunk_bytes = (
        int(max_chunk_bytes) if max_chunk_bytes and int(max_chunk_bytes) > 0 else None
    )
    ledger = Ledger(ledger_path)
    projection_state_store = LedgerGraphProjectionStateStore(ledger)
    selection = _select_sessions_bounded(
        source_store, project=project, provider="", limit=limit,
        projection_state_store=None, extraction_level=EXTRACTION_LEVEL_ENTITY,
    )
    processed = projected = skipped = deferred = 0
    failed_sessions = failed_chunks = 0
    resumed_chunks = 0
    stream_bytes = 0
    expected_bytes_total = 0
    expected_separator_bytes = 0
    expected_full_bytes = 0
    zero_byte_members = 0
    expected_ordered_hashes: list[str] = []
    writer_receipt_ordered_hashes: list[str] = []
    client_serialized_request_digests: list[str] = []
    client_serialized_ordered_hashes: list[str] = []
    canonical_hash_owner: dict[str, str] = {}
    verified_ordered_hashes: list[str] = []
    session_coverage: dict[str, dict[str, Any]] = {}
    superseded = 0
    reasons: dict[str, int] = {}
    limit_causes: dict[str, int] = {}
    failure_types: list[str] = []
    started = time.monotonic()
    received_keys: set[str] = set()
    received_hashes: set[str] = set()

    for session in selection.sessions[:limit]:
        processed += 1
        sid = str(session.get("session_id_hash") or "")
        session_project = str(session.get("project") or "")
        try:
            source_hash = session_source_revision_from_couchdb_source(
                session_id_hash=sid, source_store=source_store
            )
            plan = plan_validated_source(
                source_store, session_id_hash=sid, expected_source_hash=source_hash
            )
            if effective_max_chunk_bytes is None:
                keys = [planned_chunk_key(plan, member) for member in plan.members]
                map_digest = "sha256:" + hashlib.sha256(
                    "|".join(keys).encode("utf-8")
                ).hexdigest()
                if hasattr(full_input_state, "register_plan"):
                    full_input_state.register_plan(
                        run_id=f"stream-{sid}", project=session_project,
                        session_hash=sid, policy_version=policy_version,
                        source_hash=source_hash, map_digest=map_digest, chunk_keys=keys,
                    )
                if full_input_state.is_session_complete(
                    project=session_project, policy_version=policy_version,
                    source_hash=source_hash, map_digest=map_digest, chunk_keys=keys,
                ):
                    skipped += 1
                    resumed_chunks += len(keys)
                    session_ref = "sess_" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
                    session_coverage[session_ref] = {
                        "skipped_resumed": True,
                        "metadata_only": True,
                        "planned_chunks": len(keys),
                        "verified_chunks": len(keys),
                        "planned_bytes": 0,
                        "verified_bytes": 0,
                        "expected_separator_bytes": 0,
                        "zero_byte_members": 0,
                        "expected_ordered_hashes": [],
                        "verified_ordered_hashes": [],
                        "hash_order_match": True,
                        "coverage_complete": True,
                    }
                    continue

                expectation = expected_source_bytes(
                    source_store, plan, forbidden_markers=forbidden_markers,
                )
            else:
                expectation = expected_source_bytes(
                    source_store,
                    plan,
                    forbidden_markers=forbidden_markers,
                    max_chunk_bytes=effective_max_chunk_bytes,
                )
                keys = [item["key"] for item in expectation["ordered"]]
                map_digest = "sha256:" + hashlib.sha256(
                    "|".join(keys).encode("utf-8")
                ).hexdigest()
                if hasattr(full_input_state, "register_plan"):
                    full_input_state.register_plan(
                        run_id=f"stream-{sid}", project=session_project,
                        session_hash=sid, policy_version=policy_version,
                        source_hash=source_hash, map_digest=map_digest, chunk_keys=keys,
                    )
                if full_input_state.is_session_complete(
                    project=session_project, policy_version=policy_version,
                    source_hash=source_hash, map_digest=map_digest, chunk_keys=keys,
                ):
                    skipped += 1
                    resumed_chunks += len(keys)
                    session_ref = "sess_" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
                    session_coverage[session_ref] = {
                        "skipped_resumed": True,
                        "metadata_only": False,
                        "planned_chunks": len(keys),
                        "verified_chunks": len(keys),
                        "planned_bytes": expectation["full_bytes"],
                        "verified_bytes": expectation["body_bytes"],
                        "expected_separator_bytes": expectation["separator_bytes"],
                        "zero_byte_members": expectation["zero_byte_members"],
                        "expected_ordered_hashes": [
                            item["canonical_hash"] for item in expectation["ordered"]
                            if item["byte_length"] > 0
                        ],
                        "verified_ordered_hashes": [
                            item["canonical_hash"] for item in expectation["ordered"]
                            if item["byte_length"] > 0
                        ],
                        "hash_order_match": True,
                        "coverage_complete": True,
                    }
                    continue

            expected_items = expectation["ordered"]
            expected_by_key = {item["key"]: item for item in expected_items}
            for item in expected_items:
                if item["byte_length"] == 0:
                    continue
                canonical_hash = item["canonical_hash"]
                collision_key = f"{session_project}|{canonical_hash}"
                owner_key = canonical_hash_owner.setdefault(collision_key, item["key"])
                if owner_key != item["key"]:
                    raise SourceInvalid("canonical chunk hash identity collision")
            expected_hashes = [
                item["canonical_hash"] for item in expected_items
                if item["byte_length"] > 0
            ]
            expected_bytes_total += expectation["body_bytes"]
            expected_separator_bytes += expectation["separator_bytes"]
            expected_full_bytes += expectation["full_bytes"]
            zero_byte_members += expectation["zero_byte_members"]
            expected_ordered_hashes.extend(expected_hashes)

            shadow = shadow_group_ids(f"/project/{session_project}", policy_version)
            if not shadow:
                raise RuntimeError("shadow group unavailable")
            shadow_group = shadow[0]
            owner = f"stream-{uuid.uuid4().hex}"
            authority_by_hash = _pg_authority_by_hash(
                os.environ.get("TRIAL_PG_DSN", ""),
                session_project,
                [member.content_hash for member in plan.members],
            )
            done_keys = (
                full_input_state.is_complete_many(keys)
                if hasattr(full_input_state, "is_complete_many")
                else {key for key in keys if full_input_state.is_complete(key)}
            )
            ranges: dict[str, tuple[int, int]] = {}
            verified_ranges: dict[str, tuple[int, int]] = {}
            tokens: dict[str, int] = {}
            receipt_keys: set[str] = set()
            resumed_chunks += len(done_keys)

            def _process_sub_batch(
                sub_chunks: list[Any], sub_items: list[tuple[dict[str, Any], Any]]
            ) -> None:
                nonlocal projected, failed_chunks, stream_bytes
                sub_keys = [chunk.key for chunk in sub_chunks]
                if hasattr(full_input_state, "renew_lease"):
                    full_input_state.renew_lease(sub_keys, tokens, owner=owner)
                try:
                    extraction = extractor.extract([item for _, item in sub_items])
                except BulkRequestLimitExceeded:
                    if len(sub_items) > 1:
                        middle = len(sub_items) // 2
                        _process_sub_batch(sub_chunks[:middle], sub_items[:middle])
                        _process_sub_batch(sub_chunks[middle:], sub_items[middle:])
                        return
                    raise
                client_serialization = getattr(extractor, "last_client_serialization", None)
                if isinstance(client_serialization, dict):
                    request_digest = client_serialization.get("request_digest")
                    if request_digest:
                        client_serialized_request_digests.append(str(request_digest))
                    sessions_evidence = client_serialization.get("sessions") or {}
                    for key in sub_keys:
                        item_evidence = sessions_evidence.get(key) or {}
                        text_digest = item_evidence.get("text_digest")
                        if text_digest:
                            client_serialized_ordered_hashes.append(str(text_digest))
                report = writer.write_batch(
                    [item for _, item in sub_items], extraction,
                    allow_empty_sessions=False,
                )
                receipts = dict(getattr(report, "chunk_receipts", {}) or {})
                if hasattr(full_input_state, "renew_lease"):
                    full_input_state.renew_lease(sub_keys, tokens, owner=owner)

                batch_entries: list[dict[str, Any]] = []
                for _session, item in sub_items:
                    key = item.session_key
                    receipt = receipts.get(key)
                    if not receipt:
                        continue
                    batch_entries.append({
                        "key": key,
                        "fence_token": tokens[key],
                        "output_digest": hashlib.sha256(receipt.encode("utf-8")).hexdigest(),
                        "write_receipt": receipt,
                    })
                for _session, item in sub_items:
                    sources = item.episode.payload.get("authority_sources") or []
                    if sources and not revalidate_authority_sources(
                        os.environ.get("TRIAL_PG_DSN", ""), session_project, sources
                    ):
                        raise SourceInvalid("authority revoked before completion")
                if hasattr(full_input_state, "complete_many"):
                    full_input_state.complete_many(batch_entries, owner=owner)
                else:
                    for entry in batch_entries:
                        if entry["key"] in tokens:
                            full_input_state.complete(**entry, owner=owner)
                        else:
                            full_input_state.record_chunk(
                                key=entry["key"], project=session_project,
                                policy_version=policy_version, source_hash=source_hash,
                                map_digest=map_digest,
                                output_digest=entry["output_digest"],
                                receipt=entry["write_receipt"],
                            )
                confirmed = {
                    key for key in receipts
                    if receipts.get(key) and key in expected_by_key
                }
                receipt_keys.update(confirmed)
                received_keys.update(confirmed)
                received_hashes.update(
                    expected_by_key[key]["canonical_hash"] for key in confirmed
                )
                projected += len(batch_entries)
                failed_chunks += sum(
                    1 for _session, item in sub_items
                    if not receipts.get(item.session_key)
                )
                for chunk in sub_chunks:
                    ranges[chunk.key] = (chunk.byte_start, chunk.byte_end)
                    if receipts.get(chunk.key):
                        verified_ranges[chunk.key] = (chunk.byte_start, chunk.byte_end)
                stream_bytes += sum(
                    c.byte_end - c.byte_start for c in sub_chunks
                    if c.key not in done_keys
                )

            streams = iter_planned_chunks(
                source_store,
                plan,
                forbidden_markers=forbidden_markers,
                max_chunk_bytes=effective_max_chunk_bytes,
            )
            try:
                for batch_chunks in iter_batches(streams, batch_size):
                    pending_chunks = [chunk for chunk in batch_chunks if chunk.key not in done_keys]
                    if not pending_chunks:
                        continue
                    batch_keys = [chunk.key for chunk in pending_chunks]
                    if hasattr(full_input_state, "claim_many"):
                        tokens.update(full_input_state.claim_many(batch_keys, owner=owner))
                    else:
                        for key in batch_keys:
                            tokens[key] = full_input_state.claim(key=key, owner=owner)
                    batch_items: list[tuple[dict[str, Any], Any]] = []
                    for chunk in pending_chunks:
                        episode = OntologyEpisode.from_payload(
                            event_id=_opaque("evt_", chunk.key),
                            natural_id=_opaque("nat_", chunk.key),
                            entity_type="Session",
                            payload={
                                "project": session_project,
                                "brain_id": f"/project/{session_project}",
                                "source_hash": source_hash,
                                "source_revision": source_hash,
                                "input_lane": chunk.lane,
                                "canonical_chunk_hash": chunk.canonical_hash,
                                "redaction_version": chunk.redaction_version,
                                "fence_token": tokens.get(chunk.key, 0),
                                "graph_group_id": shadow_group,
                                "authority_sources": authority_by_hash.get(
                                    chunk.source_hash or chunk.canonical_hash, []
                                ),
                            },
                        )
                        batch_items.append((
                            session,
                            BulkSemanticSessionInput(
                                session_key=chunk.key, episode=episode, text=chunk.text,
                            ),
                        ))
                    try:
                        _process_sub_batch(pending_chunks, batch_items)
                    finally:
                        _close_spooled_items(batch_items)
            finally:
                close_stream = getattr(streams, "close", None)
                if callable(close_stream):
                    close_stream()
            verified_keys = {
                key for key in (done_keys | receipt_keys) if key in expected_by_key
            }
            writer_receipt_ordered_hashes_local = [
                item["canonical_hash"] for item in expected_items
                if item["byte_length"] > 0 and item["key"] in receipt_keys
            ]
            verified_ordered_hashes_local = [
                item["canonical_hash"] for item in expected_items
                if item["byte_length"] > 0 and item["key"] in verified_keys
            ]
            hash_order_match = verified_ordered_hashes_local == expected_hashes
            verified_bytes = sum(expected_by_key[key]["byte_length"] for key in verified_keys)
            conversation_count = sum(1 for member in plan.members if member.lane == "conversation")
            current_after = session_source_revision_from_couchdb_source(
                session_id_hash=sid, source_store=source_store
            )
            if current_after != plan.source_hash:
                _supersede(full_input_state, keys)
                superseded += len(keys)
            session_ref = "sess_" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
            writer_receipt_ordered_hashes.extend(writer_receipt_ordered_hashes_local)
            verified_ordered_hashes.extend(verified_ordered_hashes_local)
            session_coverage[session_ref] = {
                "source_hash": plan.source_hash,
                "current_source_hash": current_after,
                "planned_chunks": len(keys),
                "verified_chunks": len(verified_keys),
                "planned_bytes": expectation["full_bytes"],
                "verified_bytes": verified_bytes,
                "expected_separator_bytes": expectation["separator_bytes"],
                "zero_byte_members": expectation["zero_byte_members"],
                "expected_ordered_hashes": expected_hashes,
                "writer_receipt_ordered_hashes": writer_receipt_ordered_hashes_local,
                "verified_ordered_hashes": verified_ordered_hashes_local,
                "hash_order_match": hash_order_match,
                "conversation_chunks": conversation_count,
                "evidence_chunks": len(plan.members) - conversation_count,
                "coverage_complete": (
                    len(verified_keys) == len(keys)
                    and verified_bytes == expectation["body_bytes"]
                    and hash_order_match
                    and current_after == plan.source_hash
                ),
            }
        except (
            SourceRevisionCurrentnessLimitError,
            SpoolBudgetExceeded,
            SpoolBusy,
            BulkRequestLimitExceeded,
        ) as exc:
            # Hard currentness, spool, or client-request limits are explicit
            # pending/defer signals. Keep the session out of projected set and
            # preserve completed earlier chunks for safe resume.
            deferred += 1
            if isinstance(exc, SourceRevisionCurrentnessLimitError):
                reason = getattr(exc, "reason_code", "") or "source_revision_currentness_limit_exceeded"
            else:
                reason = safe_reason(exc)
            reasons[reason] = reasons.get(reason, 0) + 1
            cause = str(
                getattr(exc, "limit_cause", "")
                or getattr(exc, "reason_code", "")
                or type(exc).__name__
            )
            limit_causes[cause] = limit_causes.get(cause, 0) + 1
            failure_types.append(f"{type(exc).__name__}:{reason}")
        except Exception as exc:
            failed_sessions += 1
            reason = safe_reason(exc)
            reasons[reason] = reasons.get(reason, 0) + 1
            failure_types.append(f"{type(exc).__name__}:{getattr(exc, 'name', '')}")

    failed = failed_sessions + failed_chunks
    status = (
        "ok" if failed == 0 and deferred == 0
        else ("partial" if projected or skipped or deferred else "failed")
    )
    return {
        "schema_version": "llm_brain_stream_trial.v1",
        "status": status,
        "processed": processed,
        "projected": projected,
        "failed": failed,
        "deferred": deferred,
        "skipped_resumed": skipped,
        "superseded": superseded,
        "failed_sessions": failed_sessions,
        "failed_chunks": failed_chunks,
        "deferred_sessions": deferred,
        "resumed_chunks": resumed_chunks,
        "full_input_chunks": projected or 0,
        "stream_total_bytes": stream_bytes,
        "stream_coverage_complete": bool(session_coverage)
        and all(item["coverage_complete"] for item in session_coverage.values()),
        "session_coverage": session_coverage,
        "expected_total_bytes": expected_bytes_total,
        "expected_separator_bytes": expected_separator_bytes,
        "expected_full_bytes": expected_full_bytes,
        "zero_byte_members": zero_byte_members,
        "expected_ordered_hashes": expected_ordered_hashes,
        "writer_receipt_ordered_hashes": writer_receipt_ordered_hashes,
        "verified_ordered_hashes": verified_ordered_hashes,
        "hash_order_match": expected_ordered_hashes == verified_ordered_hashes,
        "client_serialized_request_digests": client_serialized_request_digests,
        "client_serialized_ordered_hashes": client_serialized_ordered_hashes,
        "server_received_hashes": None,
        "server_received_hash_status": "unknown_no_server_evidence",
        "writer_receipt_chunks": len(received_keys),
        "writer_receipt_hashes": sorted(received_hashes),
        "failure_reasons": reasons,
        "limit_causes": limit_causes,
        "failure_types": failure_types,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "raw_paths_printed": False,
    }

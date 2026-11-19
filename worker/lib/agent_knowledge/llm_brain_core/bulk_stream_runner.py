"""Trial-only streamed bulk runner: validated source, fenced writes, shadow-only."""

from __future__ import annotations

import os
import hashlib
import time
import uuid
from typing import Any

from agent_knowledge.couchdb_source.document_model import SourceDocType
from .bulk_authority import revalidate_authority_sources
from agent_knowledge.couchdb_source.source_revision import SourceRevisionCurrentnessLimitError
from .bulk_authority import authorized_sources_by_hash
from .bulk_full_input_reader import PgAuthorityCardStore
from .bulk_full_input import shadow_group_ids
from .bulk_semantic import BulkSemanticSessionInput
from .bulk_source_validation import plan_validated_source
from .bulk_streaming import iter_batches, iter_planned_chunks, planned_chunk_key, safe_reason
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
) -> dict[str, Any]:
    ledger = Ledger(ledger_path)
    projection_state_store = LedgerGraphProjectionStateStore(ledger)
    selection = _select_sessions_bounded(
        source_store, project=project, provider="", limit=limit,
        projection_state_store=None, extraction_level=EXTRACTION_LEVEL_ENTITY,
    )
    processed = projected = failed = skipped = deferred = 0
    stream_bytes = 0
    expected_bytes_total = 0
    session_coverage: dict[str, dict[str, Any]] = {}
    superseded = 0
    reasons: dict[str, int] = {}
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
                session_ref = "sess_" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
                session_coverage[session_ref] = {
                    "skipped_resumed": True,
                    "planned_chunks": len(keys),
                    "verified_chunks": len(keys),
                    "coverage_complete": bool(keys),
                }
                continue

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
            received_keys: set[str] = set()
            received_hashes: set[str] = set()

            streams = iter_planned_chunks(
                source_store, plan, forbidden_markers=forbidden_markers
            )
            for batch_chunks in iter_batches(streams, batch_size):
                batch_items: list[tuple[dict[str, Any], Any]] = []
                batch_keys = [chunk.key for chunk in batch_chunks if chunk.key not in done_keys]
                if hasattr(full_input_state, "claim_many"):
                    tokens.update(full_input_state.claim_many(batch_keys, owner=owner))
                else:
                    for key in batch_keys:
                        tokens[key] = full_input_state.claim(key=key, owner=owner)
                for chunk in batch_chunks:
                    if chunk.key in done_keys:
                        continue
                    episode = OntologyEpisode.from_payload(
                        event_id=_opaque("evt_", chunk.key),
                        natural_id=_opaque("nat_", chunk.key),
                        entity_type="Session",
                        payload={
                            "project": session_project,
                            "brain_id": f"/project/{session_project}",
                            "source_hash": source_hash,
                            "input_lane": chunk.lane,
                            "canonical_chunk_hash": chunk.canonical_hash,
                            "redaction_version": chunk.redaction_version,
                            "fence_token": tokens.get(chunk.key, 0),
                            "graph_group_id": shadow_group,
                            "authority_sources": authority_by_hash.get(
                                chunk.canonical_hash, []
                            ),
                        },
                    )
                    batch_items.append((
                        session,
                        BulkSemanticSessionInput(
                            session_key=chunk.key, episode=episode, text=chunk.text,
                        ),
                    ))
                received_keys.update(item.session_key for _, item in batch_items)
                received_hashes.update(
                    str(item.episode.payload.get("canonical_chunk_hash") or "")
                    for _, item in batch_items
                )
                if not batch_items:
                    continue
                try:
                    if hasattr(full_input_state, "renew_lease"):
                        full_input_state.renew_lease(batch_keys, tokens, owner=owner)
                    extraction = extractor.extract([item for _, item in batch_items])
                    report = writer.write_batch(
                        [item for _, item in batch_items], extraction,
                        allow_empty_sessions=False,
                    )
                    receipts = dict(getattr(report, "chunk_receipts", {}) or {})
                    if hasattr(full_input_state, "renew_lease"):
                        full_input_state.renew_lease(batch_keys, tokens, owner=owner)

                    batch_entries: list[dict[str, Any]] = []
                    for _session, item in batch_items:
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
                    for _session, item in batch_items:
                        sources = item.episode.payload.get("authority_sources") or []
                        if sources and not revalidate_authority_sources(
                            os.environ.get("TRIAL_PG_DSN", ""), project, sources
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
                    projected += len(batch_entries)
                    failed += len(batch_items) - len(receipts)
                    for chunk in batch_chunks:
                        ranges[chunk.key] = (chunk.byte_start, chunk.byte_end)
                        if receipts.get(chunk.key):
                            verified_ranges[chunk.key] = (chunk.byte_start, chunk.byte_end)
                    stream_bytes += sum(c.byte_end - c.byte_start for c in batch_chunks)
                finally:
                    _close_spooled_items(batch_items)
            expected_bytes = sum(end - start for start, end in ranges.values())
            verified_bytes = sum(end - start for start, end in verified_ranges.values())
            conversation_count = sum(1 for member in plan.members if member.lane == "conversation")
            current_after = session_source_revision_from_couchdb_source(
                session_id_hash=sid, source_store=source_store
            )
            if current_after != plan.source_hash:
                _supersede(full_input_state, keys)
                superseded += len(keys)
            session_ref = "sess_" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
            session_coverage[session_ref] = {
                "source_hash": plan.source_hash,
                "current_source_hash": current_after,
                "planned_chunks": len(keys),
                "verified_chunks": len(verified_ranges),
                "planned_bytes": expected_bytes + max(0, conversation_count - 1),
                "verified_bytes": verified_bytes,
                "conversation_chunks": conversation_count,
                "evidence_chunks": len(plan.members) - conversation_count,
                "coverage_complete": (
                    len(verified_ranges) == len(keys)
                    and verified_bytes == expected_bytes
                    and current_after == plan.source_hash
                ),
            }
        except SourceRevisionCurrentnessLimitError as exc:
            # Hard currentness caps are an explicit pending/defer signal. Keep
            # the session out of the projected set and continue with the rest.
            deferred += 1
            reason = getattr(exc, "reason_code", "") or "source_revision_currentness_limit_exceeded"
            reasons[reason] = reasons.get(reason, 0) + 1
            failure_types.append(f"{type(exc).__name__}:{reason}")
        except Exception as exc:
            failed += 1
            reason = safe_reason(exc)
            reasons[reason] = reasons.get(reason, 0) + 1
            failure_types.append(f"{type(exc).__name__}:{getattr(exc, 'name', '')}")

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
        "full_input_chunks": projected or 0,
        "stream_total_bytes": stream_bytes,
        "stream_coverage_complete": bool(session_coverage)
        and all(item["coverage_complete"] for item in session_coverage.values()),
        "session_coverage": session_coverage,
        "expected_total_bytes": expected_bytes_total,
        "model_received_chunks": len(received_keys),
        "model_received_hashes": sorted(received_hashes),
        "failure_reasons": reasons,
        "failure_types": failure_types,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "raw_paths_printed": False,
    }

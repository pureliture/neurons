"""Bounded live canary for source-to-projection currentness.

The canary owns one synthetic, project-scoped session.  It delivers a baseline
chunk, projects both canonical session-memory and graph lanes, delivers one
distinct chunk, proves both lanes become non-current and are re-selected, then
re-delivers the exact distinct payload and proves the two lanes are not selected
again.  The source mutations are additive; this command never deletes source,
projection, graph, or artifact state.

Only digests, counts, and booleans leave the command.  Source/session/chunk/job
identities and canary bodies remain internal to the process.

Partial failures are resumed without deletion by rerunning with a fresh
``probe_nonce_sha256``.  The stable baseline phase first catches both projection
lanes up to the authoritative source left by the failed run, then the fresh
chunk proves a new invalidation transition.  Reusing the failed nonce cannot
prove another source-hash transition and therefore fails closed.

The CLI wraps all live work in a POSIX process alarm at
``max_runtime_seconds``.  Operations must retain an external process timeout as
a second containment boundary for native drivers that might not unwind cleanly
on a Python signal.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import datetime
import json
import math
import os
import re
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..couchdb_source.build_cli import (
    _build_qdrant_projector,
    _select_sessions_needing_projection,
)
from ..couchdb_source.document_model import (
    ProjectionStatus,
    conversation_chunk_doc_id,
    projection_state_doc_id,
    sha256_hash,
)
from ..couchdb_source.session_memory_materializer import materialize_and_project
from ..couchdb_source.source_store import CouchDBSourceStore
from ..ledger import Ledger
from ..llm_brain_core.couchdb_projection_cli import run_couchdb_projection
from ..llm_brain_core.ledger_adapter import (
    EXTRACTION_LEVEL_EPISODIC,
    LedgerGraphProjectionStateStore,
)
from ..llm_brain_core.runtime import session_source_revision_from_couchdb_source
from ..llm_brain_core.runtime_graph import build_graph_adapter_from_env
from ..session_memory.native_memory_sync_approval import (
    ApprovalError,
    validate_memory_enqueue_approval,
)
from .couchdb_delivery_backend import CouchDBDeliveryBackend
from .delivery_executor import DeliveryExecutor
from .state_cli import DEFAULT_TRANSCRIPT_TARGET_PROFILE
from .state_db import RAGIngressStateDB
from .state_sink import StateDBIngressSink


CANARY_OPERATION = "couchdb_projection_invalidation_canary"
CANARY_SCHEMA_VERSION = "couchdb_projection_invalidation_canary.v1"
CANARY_PROVIDER = "lbrain-temporal-canary"
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_RUNTIME_SECONDS = 300.0


@dataclass(frozen=True)
class _ResolvedCanaryTargets:
    """Immutable execution snapshot; raw target values never leave this module."""

    couchdb_url: str = field(repr=False)
    couchdb_db: str = field(repr=False)
    couchdb_user: str = field(repr=False)
    couchdb_password_env: str = field(repr=False)
    couchdb_password: str = field(repr=False)
    state_db_path: Path = field(repr=False)
    ledger_path: Path = field(repr=False)
    runtime_dir: Path = field(repr=False)
    environ: dict[str, str] = field(repr=False)
    target_fingerprints: dict[str, str]


class CanaryExecutionError(RuntimeError):
    """Redaction-safe stage failure; no lower-level message is retained."""

    def __init__(self, *, stage: str, error_class: str) -> None:
        super().__init__(stage)
        self.stage = str(stage or "unknown")
        self.error_class = str(error_class or "CanaryExecutionError")


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return sha256_hash(_stable_json(value))


def _normalized_target_fingerprints(
    target_fingerprints: Mapping[str, object] | None,
) -> dict[str, str]:
    if target_fingerprints is None:
        return {}
    if not isinstance(target_fingerprints, Mapping):
        raise ValueError("target fingerprints must be a mapping")
    normalized: dict[str, str] = {}
    for name, fingerprint in target_fingerprints.items():
        target_name = str(name or "").strip()
        target_value = str(fingerprint or "").strip()
        if not target_name or _SHA256_REF_RE.fullmatch(target_value) is None:
            raise ValueError("target fingerprint is invalid")
        normalized[target_name] = target_value
    return dict(sorted(normalized.items()))


def _target_fingerprint(value: Mapping[str, object]) -> str:
    return _digest(dict(value))


def _target_fingerprint_digest(target_fingerprints: Mapping[str, object] | None) -> str:
    return _target_fingerprint(_normalized_target_fingerprints(target_fingerprints))


def _resolved_path(value: object) -> Path:
    return Path(str(value or "")).expanduser().resolve(strict=False)


def _resolved_qdrant_collection(environ: Mapping[str, str]) -> str:
    # Keep this default identical to build_cli._build_qdrant_projector without
    # loading optional Qdrant/Docling dependencies during plan construction.
    return str(environ.get("QDRANT_COLLECTION") or "neurons_searchable_mirror_poc").strip()


def _resolve_canary_targets(
    args: argparse.Namespace,
    environ: Mapping[str, str],
) -> _ResolvedCanaryTargets:
    """Resolve every writable target once, before approval and live setup.

    CLI argv can omit source and projection destinations because parser defaults
    and runtime builders read environment.  Binding only argv left room for an
    environment/default drift to redirect an approved canary.  The plan exposes
    only fingerprints while this private snapshot keeps the exact values used by
    the later live setup.
    """

    frozen_environ = {str(key): str(value) for key, value in environ.items()}
    couchdb_url = str(args.couchdb_url or "").strip().rstrip("/")
    couchdb_db = str(args.couchdb_db or "").strip()
    state_db_path = _resolved_path(args.state_db)
    ledger_path = _resolved_path(args.ledger)
    runtime_dir = _resolved_path(args.runtime_dir)
    graph_uri = str(
        frozen_environ.get(
            "LLM_BRAIN_NEO4J_URI",
            frozen_environ.get("NEO4J_URI", "bolt://localhost:7687"),
        )
        or ""
    ).strip()
    graph_group = str(frozen_environ.get("LLM_BRAIN_GRAPH_GROUP_ID") or "").strip()
    target_fingerprints = {
        "ingress_state_db": _target_fingerprint(
            {"kind": "ingress_state_db", "path": str(state_db_path)}
        ),
        "projection_ledger": _target_fingerprint(
            {"kind": "projection_ledger", "path": str(ledger_path)}
        ),
        "runtime_workspace": _target_fingerprint(
            {"kind": "runtime_workspace", "path": str(runtime_dir)}
        ),
        "couchdb_source": _target_fingerprint(
            {
                "kind": "couchdb_source",
                "base_url": couchdb_url,
                "database": couchdb_db,
            }
        ),
        "qdrant_collection": _target_fingerprint(
            {
                "kind": "qdrant_collection",
                "base_url": str(frozen_environ.get("QDRANT_URL") or "").strip().rstrip("/"),
                "collection": _resolved_qdrant_collection(frozen_environ),
            }
        ),
        "graph_store": _target_fingerprint(
            {
                "kind": "graph_store",
                "uri": graph_uri,
                "group_id": graph_group,
            }
        ),
    }
    couchdb_password_env = str(args.couchdb_password_env or "").strip()
    return _ResolvedCanaryTargets(
        couchdb_url=couchdb_url,
        couchdb_db=couchdb_db,
        couchdb_user=str(args.couchdb_user or "").strip(),
        couchdb_password_env=couchdb_password_env,
        couchdb_password=str(frozen_environ.get(couchdb_password_env) or ""),
        state_db_path=state_db_path,
        ledger_path=ledger_path,
        runtime_dir=runtime_dir,
        environ=frozen_environ,
        target_fingerprints=_normalized_target_fingerprints(target_fingerprints),
    )


def _require_approved_target_fingerprints(
    approval: Mapping[str, object],
    *,
    target_fingerprints: Mapping[str, object],
) -> None:
    target = approval.get("target")
    approved = target.get("target_fingerprints") if isinstance(target, Mapping) else None
    if _normalized_target_fingerprints(approved) != _normalized_target_fingerprints(
        target_fingerprints
    ):
        raise ApprovalError("approval target fingerprint mismatch")


def _validate_observed_at(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_inputs(
    *,
    project: str,
    provider: str,
    probe_nonce_sha256: str,
    expected_source_commit: str,
    observed_at: str,
    limit: int,
    max_runtime_seconds: float,
) -> dict[str, object]:
    normalized_project = str(project or "").strip()
    normalized_provider = str(provider or "").strip()
    normalized_nonce = str(probe_nonce_sha256 or "").strip().lower()
    normalized_commit = str(expected_source_commit or "").strip().lower()
    if not normalized_project:
        raise ValueError("project is required")
    if normalized_provider != CANARY_PROVIDER:
        raise ValueError("provider must use the isolated canary scope")
    if int(limit) != 1:
        raise ValueError("limit must equal 1")
    timeout = float(max_runtime_seconds)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > _MAX_RUNTIME_SECONDS:
        raise ValueError("max_runtime_seconds must be within the bounded range")
    if not _SHA256_REF_RE.fullmatch(normalized_nonce):
        raise ValueError("probe_nonce_sha256 must be a sha256 reference")
    if not _COMMIT_RE.fullmatch(normalized_commit):
        raise ValueError("expected_source_commit must be a 40-character commit")
    return {
        "project": normalized_project,
        "provider": normalized_provider,
        "probe_nonce_sha256": normalized_nonce,
        "expected_source_commit": normalized_commit,
        "observed_at": _validate_observed_at(observed_at),
        "limit": 1,
        "max_runtime_seconds": timeout,
    }


def _canary_identity(validated: dict[str, object]) -> dict[str, str]:
    identity = {
        "contract": CANARY_SCHEMA_VERSION,
        "project": str(validated["project"]),
        "provider": str(validated["provider"]),
        "probe_nonce_sha256": str(validated["probe_nonce_sha256"]),
        "expected_source_commit": str(validated["expected_source_commit"]),
        "observed_at": str(validated["observed_at"]),
    }
    canary_ref = _digest(identity)
    # The source session is stable across activations.  A fresh nonce creates a
    # new bounded chunk revision inside this one isolated session instead of
    # growing source-session/entity-backlog cardinality forever.
    session_ref = _digest(
        {
            "contract": CANARY_SCHEMA_VERSION,
            "project": str(validated["project"]),
            "provider": str(validated["provider"]),
            "kind": "stable_session",
        }
    )
    return {
        "canary_ref": canary_ref,
        "session_ref": session_ref,
        "session_id_hash": _digest({"session_ref": session_ref, "kind": "session"}),
        "baseline_chunk_id": _digest({"session_ref": session_ref, "phase": "baseline"}),
        "distinct_chunk_id": _digest({"canary_ref": canary_ref, "phase": "distinct"}),
    }


def build_canary_plan(
    *,
    project: str,
    provider: str,
    probe_nonce_sha256: str,
    expected_source_commit: str,
    observed_at: str,
    limit: int,
    max_runtime_seconds: float,
    target_fingerprints: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    validated = _validated_inputs(
        project=project,
        provider=provider,
        probe_nonce_sha256=probe_nonce_sha256,
        expected_source_commit=expected_source_commit,
        observed_at=observed_at,
        limit=limit,
        max_runtime_seconds=max_runtime_seconds,
    )
    identity = _canary_identity(validated)
    resolved_target_fingerprints = _normalized_target_fingerprints(
        target_fingerprints
    )
    plan_digest = _digest(
        {
            "operation": CANARY_OPERATION,
            **validated,
            "canary_ref": identity["canary_ref"],
            "planned_ingress_enqueue_count": 3,
            "planned_source_chunk_insert_count": 2,
            "target_fingerprint": _target_fingerprint_digest(
                resolved_target_fingerprints
            ),
        }
    )
    return {
        "schema_version": CANARY_SCHEMA_VERSION,
        "status": "planned",
        "plan_digest": plan_digest,
        "canary_ref": identity["canary_ref"],
        "target_fingerprints": resolved_target_fingerprints,
        "target_fingerprint": _target_fingerprint_digest(
            resolved_target_fingerprints
        ),
        "bounded_limit": 1,
        "timeout_seconds": _json_number(float(validated["max_runtime_seconds"])),
        "planned_ingress_enqueue_count": 3,
        "planned_source_chunk_insert_minimum": 1,
        "planned_source_chunk_insert_count": 2,
        "rollback_restore_available": True,
        "rollback_restore_strategy": "fresh_nonce_catch_up_then_probe_from_authoritative_source",
        "resumable_after_partial_failure": True,
        "resume_requires_fresh_probe_nonce": True,
        "hard_timeout_required": True,
        "external_timeout_required": True,
        "mutation_performed": False,
        "destructive_mutation_performed": False,
        "network_used": False,
        "raw_ids_printed": False,
        "raw_bodies_printed": False,
        "secret_printed": False,
        "host_topology_printed": False,
    }


def _json_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _payload_body(*, phase: str, canary_ref: str) -> str:
    return (
        "---\n"
        "schema_version: agent_knowledge_document.v2\n"
        "result_type: conversation_chunk\n"
        f"provider: {CANARY_PROVIDER}\n"
        "privacy_level: private\n"
        "synthetic_canary: true\n"
        "---\n\n"
        "# LBrain projection currentness canary\n\n"
        f"Synthetic additive {phase} evidence for projection currentness.\n"
        f"canary_ref: {canary_ref}\n"
    )


def _build_payload(
    *,
    phase: str,
    project: str,
    provider: str,
    session_id_hash: str,
    chunk_id: str,
    canary_ref: str,
    observed_at: str,
    idempotency_ref: str = "",
    idempotency_phase: str = "",
) -> dict[str, Any]:
    body = _payload_body(phase=phase, canary_ref=canary_ref)
    turn_index = 0 if phase == "baseline" else 1
    metadata = {
        "schema_version": "agent_knowledge_document.v2",
        "type": "conversation_chunk",
        "result_type": "conversation_chunk",
        "provider": provider,
        "project": project,
        "session_id_hash": session_id_hash,
        "chunk_id": chunk_id,
        "turn_start_index": str(turn_index),
        "turn_end_index": str(turn_index),
        "part_index": "1",
        "part_count": "1",
        "char_start": "0",
        "char_end": str(len(body)),
        "observed_at_start": observed_at,
        "observed_at_end": observed_at,
        "privacy_level": "private",
        "synthetic_canary": "true",
    }
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {
            "host": "server_canary",
            "producer": "projection-invalidation-canary",
            "provider": provider,
            "project": project,
        },
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "filename": f"projection-canary-{phase}.md",
                "contentType": "text/markdown",
                "body": body,
                "metadata": metadata,
            },
        },
        "contentHash": sha256_hash(body),
        "targetProfile": DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        "kind": "conversation_chunk",
        "idempotencyKey": _digest(
            {
                "canary_ref": idempotency_ref or canary_ref,
                "phase": idempotency_phase or phase,
                "kind": "enqueue",
            }
        ),
    }


def _run_stage(stage: str, action: Callable[[], Any]) -> Any:
    try:
        return action()
    except CanaryExecutionError:
        raise
    except Exception as exc:
        raise CanaryExecutionError(stage=stage, error_class=type(exc).__name__) from exc


def _require(condition: bool, *, stage: str, error_class: str) -> None:
    if not condition:
        raise CanaryExecutionError(stage=stage, error_class=error_class)


def _deadline_check(*, started: float, max_runtime_seconds: float, stage: str) -> None:
    if time.monotonic() - started > float(max_runtime_seconds):
        raise CanaryExecutionError(stage=stage, error_class="CanaryTimeout")


def _enqueue_and_deliver(
    *,
    payload: dict[str, Any],
    state_db: RAGIngressStateDB,
    source_store: CouchDBSourceStore,
    delivery_backend: CouchDBDeliveryBackend,
    session_id_hash: str,
    chunk_id: str,
) -> tuple[bool, bool]:
    chunk_doc_id = conversation_chunk_doc_id(session_id_hash, chunk_id)
    existed_before = source_store.get(chunk_doc_id) is not None
    queued = StateDBIngressSink(state_db=state_db).enqueue_payload(payload)
    job_id = str(queued.get("job_id") or "")
    _require(bool(job_id), stage="ingress_enqueue", error_class="MissingJobReference")
    outcome = DeliveryExecutor(
        state_db=state_db,
        backend=delivery_backend,
        lease_owner="projection_invalidation_canary",
    ).execute_once(job_id, max_attempts=1)
    delivered = outcome == "succeeded"
    exists_after = source_store.get(chunk_doc_id) is not None
    return delivered, (not existed_before and exists_after)


def _run_session_memory_lane(
    *,
    source_store: CouchDBSourceStore,
    projector: Any,
    project: str,
    provider: str,
) -> tuple[int, int, str]:
    selected = _select_sessions_needing_projection(
        source_store,
        1,
        project=project,
        provider=provider,
    )
    projected = 0
    projected_source_hash = ""
    for session in selected:
        session_id_hash = str(session.get("session_id_hash") or "")
        result = materialize_and_project(
            session_id_hash=session_id_hash,
            store=source_store,
            projector=projector,
        )
        projection = result.get("projection") or {}
        if str(projection.get("status") or "") == ProjectionStatus.PROJECTED:
            projected += 1
            projected_source_hash = str(result.get("source_hash") or "")
    return len(selected), projected, projected_source_hash


def _run_graph_lane(
    *,
    ledger_path: Path,
    source_store: CouchDBSourceStore,
    graph_adapter: Any,
    runtime_dir: Path,
    project: str,
    provider: str,
) -> tuple[int, int]:
    report = run_couchdb_projection(
        ledger_path=ledger_path,
        source_store=source_store,
        limit=1,
        project=project,
        provider=provider,
        enable_graph=True,
        graph_required=True,
        extract_entities=False,
        resume=True,
        report_every=1,
        max_projects=1,
        graph_adapter=graph_adapter,
        runtime_dir=runtime_dir,
    )
    _require(
        str(report.get("status") or "") == "ok",
        stage="graph_projection",
        error_class="GraphProjectionFailed",
    )
    canonical = report.get("canonical_counts") or {}
    projection = report.get("projection") or {}
    return (
        int(canonical.get("selected_sessions") or 0),
        # A backend duplicate is a successful catch-up when the graph write
        # committed before the local projection ledger (for example, a prior
        # process interruption).  The projection worker records that duplicate
        # as current, so count it as a completed projection here.
        int(projection.get("projected") or 0)
        + int(projection.get("duplicates") or 0),
    )


def _graph_has_source_hash(
    *, ledger_path: Path, project: str, session_id_hash: str, source_hash: str
) -> bool:
    store = LedgerGraphProjectionStateStore(Ledger(ledger_path))
    source_hashes = store.list_projected_source_hash_sets(
        project,
        extraction_level=EXTRACTION_LEVEL_EPISODIC,
        entity_type="Session",
    )
    natural_id = session_id_hash.replace(":", "_")
    return source_hash in source_hashes.get(natural_id, set())


def run_projection_invalidation_canary(
    *,
    state_db: RAGIngressStateDB,
    ledger_path: Path,
    source_store: CouchDBSourceStore,
    session_memory_projector: Any,
    graph_adapter: Any,
    runtime_dir: Path,
    project: str,
    provider: str,
    probe_nonce_sha256: str,
    expected_source_commit: str,
    observed_at: str,
    limit: int,
    max_runtime_seconds: float,
    target_fingerprints: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Execute one baseline/distinct/duplicate proof with a hard cardinality of one."""

    validated = _validated_inputs(
        project=project,
        provider=provider,
        probe_nonce_sha256=probe_nonce_sha256,
        expected_source_commit=expected_source_commit,
        observed_at=observed_at,
        limit=limit,
        max_runtime_seconds=max_runtime_seconds,
    )
    plan = build_canary_plan(
        **validated,
        target_fingerprints=target_fingerprints,
    )
    identity = _canary_identity(validated)
    session_id_hash = identity["session_id_hash"]
    baseline_doc = source_store.get(
        conversation_chunk_doc_id(session_id_hash, identity["baseline_chunk_id"])
    )
    baseline_observed_at = str(
        (baseline_doc or {}).get("observed_at_start") or validated["observed_at"]
    )
    baseline_payload = _build_payload(
        phase="baseline",
        project=str(validated["project"]),
        provider=str(validated["provider"]),
        session_id_hash=session_id_hash,
        chunk_id=identity["baseline_chunk_id"],
        canary_ref=identity["session_ref"],
        observed_at=baseline_observed_at,
        # Keep the source document stable but give each fresh-nonce recovery
        # attempt its own delivery job.  A prior DeliveryOutcomeUncertain may
        # have quarantined the old job even when CouchDB committed; reusing its
        # idempotency key would make the documented recovery path impossible.
        idempotency_ref=identity["canary_ref"],
    )
    distinct_payload = _build_payload(
        phase="distinct",
        project=str(validated["project"]),
        provider=str(validated["provider"]),
        session_id_hash=session_id_hash,
        chunk_id=identity["distinct_chunk_id"],
        canary_ref=identity["canary_ref"],
        observed_at=str(validated["observed_at"]),
    )
    duplicate_payload = copy.deepcopy(distinct_payload)
    duplicate_payload["idempotencyKey"] = _digest(
        {
            "canary_ref": identity["canary_ref"],
            "phase": "duplicate",
            "kind": "enqueue",
        }
    )
    delivery_backend = CouchDBDeliveryBackend(state_db=state_db, store=source_store)
    started = time.monotonic()
    enqueue_count = delivery_count = source_insert_count = 0

    delivered, inserted = _run_stage(
        "baseline_delivery",
        lambda: _enqueue_and_deliver(
            payload=baseline_payload,
            state_db=state_db,
            source_store=source_store,
            delivery_backend=delivery_backend,
            session_id_hash=session_id_hash,
            chunk_id=identity["baseline_chunk_id"],
        ),
    )
    enqueue_count += 1
    delivery_count += int(delivered)
    source_insert_count += int(inserted)
    _require(delivered, stage="baseline_delivery", error_class="BaselineDeliveryFailed")
    baseline_source_hash = _run_stage(
        "baseline_source_hash",
        lambda: session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=source_store,
        ),
    )
    _require(
        bool(_SHA256_REF_RE.fullmatch(str(baseline_source_hash or ""))),
        stage="baseline_source_hash",
        error_class="MissingSourceHash",
    )
    baseline_session_selected, baseline_session_projected, baseline_session_hash = _run_stage(
        "baseline_session_memory_projection",
        lambda: _run_session_memory_lane(
            source_store=source_store,
            projector=session_memory_projector,
            project=str(validated["project"]),
            provider=str(validated["provider"]),
        ),
    )
    baseline_session_current = (
        baseline_session_selected in {0, 1}
        and baseline_session_projected == baseline_session_selected
        and (
            baseline_session_hash == baseline_source_hash
            if baseline_session_selected
            else str(
                (
                    source_store.get(projection_state_doc_id(session_id_hash)) or {}
                ).get("projected_source_hash")
                or ""
            )
            == baseline_source_hash
        )
    )
    _require(
        baseline_session_current,
        stage="baseline_session_memory_projection",
        error_class="BaselineSessionProjectionFailed",
    )
    baseline_graph_selected, baseline_graph_projected = _run_stage(
        "baseline_graph_projection",
        lambda: _run_graph_lane(
            ledger_path=ledger_path,
            source_store=source_store,
            graph_adapter=graph_adapter,
            runtime_dir=runtime_dir,
            project=str(validated["project"]),
            provider=str(validated["provider"]),
        ),
    )
    _require(
        baseline_graph_selected in {0, 1}
        and baseline_graph_projected == baseline_graph_selected
        and _graph_has_source_hash(
            ledger_path=ledger_path,
            project=str(validated["project"]),
            session_id_hash=session_id_hash,
            source_hash=str(baseline_source_hash),
        ),
        stage="baseline_graph_projection",
        error_class="BaselineGraphProjectionFailed",
    )
    _deadline_check(
        started=started,
        max_runtime_seconds=float(validated["max_runtime_seconds"]),
        stage="baseline_postcheck",
    )

    delivered, inserted = _run_stage(
        "distinct_delivery",
        lambda: _enqueue_and_deliver(
            payload=distinct_payload,
            state_db=state_db,
            source_store=source_store,
            delivery_backend=delivery_backend,
            session_id_hash=session_id_hash,
            chunk_id=identity["distinct_chunk_id"],
        ),
    )
    enqueue_count += 1
    delivery_count += int(delivered)
    source_insert_count += int(inserted)
    _require(delivered and inserted, stage="distinct_delivery", error_class="DistinctDeliveryFailed")
    distinct_source_hash = _run_stage(
        "distinct_source_hash",
        lambda: session_source_revision_from_couchdb_source(
            session_id_hash=session_id_hash,
            source_store=source_store,
        ),
    )
    source_hash_changed = (
        bool(_SHA256_REF_RE.fullmatch(str(distinct_source_hash or "")))
        and distinct_source_hash != baseline_source_hash
    )
    state_after_distinct = source_store.get(projection_state_doc_id(session_id_hash)) or {}
    projection_dirty_observed = (
        str(state_after_distinct.get("projection_status") or "") == ProjectionStatus.PENDING
        and str(state_after_distinct.get("source_hash") or "") == distinct_source_hash
        and str(state_after_distinct.get("projected_source_hash") or "") == baseline_source_hash
    )
    _require(
        source_hash_changed and projection_dirty_observed,
        stage="distinct_invalidation",
        error_class="ProjectionInvalidationNotObserved",
    )
    session_selected, session_projected, session_projected_hash = _run_stage(
        "distinct_session_memory_projection",
        lambda: _run_session_memory_lane(
            source_store=source_store,
            projector=session_memory_projector,
            project=str(validated["project"]),
            provider=str(validated["provider"]),
        ),
    )
    session_hash_caught_up = session_projected_hash == distinct_source_hash
    _require(
        (session_selected, session_projected) == (1, 1) and session_hash_caught_up,
        stage="distinct_session_memory_projection",
        error_class="SessionMemoryCurrentnessFailed",
    )
    graph_selected, graph_projected = _run_stage(
        "distinct_graph_projection",
        lambda: _run_graph_lane(
            ledger_path=ledger_path,
            source_store=source_store,
            graph_adapter=graph_adapter,
            runtime_dir=runtime_dir,
            project=str(validated["project"]),
            provider=str(validated["provider"]),
        ),
    )
    graph_hash_caught_up = _graph_has_source_hash(
        ledger_path=ledger_path,
        project=str(validated["project"]),
        session_id_hash=session_id_hash,
        source_hash=str(distinct_source_hash),
    )
    _require(
        (graph_selected, graph_projected) == (1, 1) and graph_hash_caught_up,
        stage="distinct_graph_projection",
        error_class="GraphCurrentnessFailed",
    )
    _deadline_check(
        started=started,
        max_runtime_seconds=float(validated["max_runtime_seconds"]),
        stage="distinct_postcheck",
    )

    delivered, inserted = _run_stage(
        "duplicate_delivery",
        lambda: _enqueue_and_deliver(
            payload=duplicate_payload,
            state_db=state_db,
            source_store=source_store,
            delivery_backend=delivery_backend,
            session_id_hash=session_id_hash,
            chunk_id=identity["distinct_chunk_id"],
        ),
    )
    enqueue_count += 1
    delivery_count += int(delivered)
    source_insert_count += int(inserted)
    _require(delivered and not inserted, stage="duplicate_delivery", error_class="DuplicateDeliveryFailed")
    duplicate_source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=session_id_hash,
        source_store=source_store,
    )
    duplicate_source_hash_unchanged = duplicate_source_hash == distinct_source_hash
    state_after_duplicate = source_store.get(projection_state_doc_id(session_id_hash)) or {}
    projection_state_remained_projected = (
        str(state_after_duplicate.get("projection_status") or "") == ProjectionStatus.PROJECTED
        and str(state_after_duplicate.get("source_hash") or "") == distinct_source_hash
        and str(state_after_duplicate.get("projected_source_hash") or "")
        == distinct_source_hash
    )
    duplicate_session_selected = len(
        _select_sessions_needing_projection(
            source_store,
            1,
            project=str(validated["project"]),
            provider=str(validated["provider"]),
        )
    )
    duplicate_graph_selected, duplicate_graph_projected = _run_stage(
        "duplicate_graph_nonselection",
        lambda: _run_graph_lane(
            ledger_path=ledger_path,
            source_store=source_store,
            graph_adapter=graph_adapter,
            runtime_dir=runtime_dir,
            project=str(validated["project"]),
            provider=str(validated["provider"]),
        ),
    )
    _require(
        duplicate_source_hash_unchanged
        and projection_state_remained_projected
        and duplicate_session_selected == 0
        and duplicate_graph_selected == 0
        and duplicate_graph_projected == 0,
        stage="duplicate_nonselection",
        error_class="DuplicateWasReselected",
    )
    _deadline_check(
        started=started,
        max_runtime_seconds=float(validated["max_runtime_seconds"]),
        stage="final_postcheck",
    )

    _require(
        enqueue_count == 3
        and delivery_count == 3
        and source_insert_count in {1, 2},
        stage="final_cardinality",
        error_class="CanaryCardinalityMismatch",
    )
    return {
        "schema_version": CANARY_SCHEMA_VERSION,
        "status": "passed",
        "plan_digest": plan["plan_digest"],
        "bounded_limit": 1,
        "timeout_seconds": _json_number(float(validated["max_runtime_seconds"])),
        "canary_ref": identity["canary_ref"],
        "baseline_source_hash": baseline_source_hash,
        "distinct_source_hash": distinct_source_hash,
        "session_memory_projected_source_hash": session_projected_hash,
        "graph_projected_source_hash": distinct_source_hash,
        "ingress_enqueue_count": enqueue_count,
        "delivery_succeeded_count": delivery_count,
        "source_chunk_insert_count": source_insert_count,
        "baseline_source_initialized": bool(source_insert_count == 2),
        "distinct": {
            "source_hash_changed": source_hash_changed,
            "projection_dirty_observed": projection_dirty_observed,
            "session_memory_selected": session_selected,
            "session_memory_projected": session_projected,
            "session_memory_hash_caught_up": session_hash_caught_up,
            "graph_selected": graph_selected,
            "graph_projected": graph_projected,
            "graph_hash_caught_up": graph_hash_caught_up,
        },
        "duplicate": {
            "source_hash_unchanged": duplicate_source_hash_unchanged,
            "projection_state_remained_projected": projection_state_remained_projected,
            "session_memory_selected": duplicate_session_selected,
            "session_memory_projected": 0,
            "graph_selected": duplicate_graph_selected,
            "graph_projected": duplicate_graph_projected,
        },
        "rollback_restore_available": True,
        "rollback_restore_strategy": "fresh_nonce_catch_up_then_probe_from_authoritative_source",
        "resumable_after_partial_failure": True,
        "resume_requires_fresh_probe_nonce": True,
        "hard_timeout_required": True,
        "external_timeout_required": True,
        "mutation_performed": True,
        "destructive_mutation_performed": False,
        "network_used": True,
        "raw_ids_printed": False,
        "raw_bodies_printed": False,
        "secret_printed": False,
        "host_topology_printed": False,
    }


def _close_if_supported(resource: Any) -> None:
    closer = getattr(resource, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


@contextlib.contextmanager
def _hard_runtime_timeout(seconds: float):
    """Interrupt live execution at the approved bound or fail closed."""

    if not hasattr(signal, "setitimer") or not hasattr(signal, "SIGALRM"):
        raise CanaryExecutionError(
            stage="runtime_setup", error_class="HardTimeoutUnavailable"
        )
    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)

    def _raise_timeout(_signum, _frame) -> None:
        raise CanaryExecutionError(stage="hard_timeout", error_class="CanaryTimeout")

    try:
        signal.signal(signal.SIGALRM, _raise_timeout)
        previous_delay, previous_interval = signal.setitimer(
            signal.ITIMER_REAL, float(seconds)
        )
    except (OSError, ValueError) as exc:
        raise CanaryExecutionError(
            stage="runtime_setup", error_class="HardTimeoutUnavailable"
        ) from exc
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_delay > 0:
            elapsed = time.monotonic() - started
            signal.setitimer(
                signal.ITIMER_REAL,
                max(previous_delay - elapsed, 0.000001),
                previous_interval,
            )


def _execute_live(
    args: argparse.Namespace,
    plan: dict[str, Any],
    *,
    resolved_targets: _ResolvedCanaryTargets | None = None,
) -> dict[str, Any]:
    from ..llm_brain_core.couchdb_projection_cli import _build_source_store

    targets = resolved_targets or _resolve_canary_targets(args, os.environ)
    plan_fingerprints = _normalized_target_fingerprints(
        plan.get("target_fingerprints")
    )
    if plan_fingerprints != targets.target_fingerprints:
        raise CanaryExecutionError(
            stage="target_binding", error_class="ResolvedTargetDrift"
        )
    if str(targets.environ.get("SESSION_MEMORY_PROJECTION_BACKEND") or "").strip().lower() != "qdrant":
        raise CanaryExecutionError(stage="runtime_setup", error_class="CanonicalBackendNotQdrant")
    source_store = _build_source_store(
        couchdb_url=targets.couchdb_url,
        couchdb_db=targets.couchdb_db,
        couchdb_user=targets.couchdb_user,
        couchdb_password_env=targets.couchdb_password_env,
        couchdb_password=targets.couchdb_password,
    )
    projector = _build_qdrant_projector(targets.environ)
    if projector is None:
        raise CanaryExecutionError(stage="runtime_setup", error_class="QdrantProjectorUnavailable")
    graph_environ = dict(targets.environ)
    graph_environ["LLM_BRAIN_GRAPH_EXTRACT_ENTITIES"] = "false"
    graph_adapter = build_graph_adapter_from_env(
        environ=graph_environ,
        enable_flag=True,
        required_flag=True,
    )
    try:
        return run_projection_invalidation_canary(
            state_db=RAGIngressStateDB(targets.state_db_path),
            ledger_path=targets.ledger_path,
            source_store=source_store,
            session_memory_projector=projector,
            graph_adapter=graph_adapter,
            runtime_dir=targets.runtime_dir,
            project=args.project,
            provider=args.provider,
            probe_nonce_sha256=args.probe_nonce_sha256,
            expected_source_commit=args.expected_source_commit,
            observed_at=args.observed_at,
            limit=args.limit,
            max_runtime_seconds=args.max_runtime_seconds,
            target_fingerprints=targets.target_fingerprints,
        )
    finally:
        _close_if_supported(graph_adapter)
        _close_if_supported(projector)


def _failure_report(
    *,
    status: str,
    plan: dict[str, Any] | None = None,
    error_class: str = "",
    stage: str = "",
    execution_started: bool = False,
    hard_timeout_enforced: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": CANARY_SCHEMA_VERSION,
        "status": status,
        "plan_digest": str((plan or {}).get("plan_digest") or ""),
        "failure_stage": str(stage or ""),
        "error_class": str(error_class or ""),
        "rollback_restore_available": True,
        "rollback_restore_strategy": "fresh_nonce_catch_up_then_probe_from_authoritative_source",
        "resumable_after_partial_failure": True,
        "resume_requires_fresh_probe_nonce": True,
        "hard_timeout_required": True,
        "hard_timeout_enforced": bool(hard_timeout_enforced),
        "external_timeout_required": True,
        # A timed-out or failed network call may have committed before its
        # acknowledgement returned.  Never claim no mutation after execute began.
        "mutation_performed": bool(execution_started),
        "mutation_may_have_occurred": bool(execution_started),
        "destructive_mutation_performed": False,
        "network_used": bool(execution_started),
        "raw_ids_printed": False,
        "raw_bodies_printed": False,
        "secret_printed": False,
        "host_topology_printed": False,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge couchdb-projection-invalidation-canary"
    )
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--provider", default=CANARY_PROVIDER)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--max-runtime-seconds", type=float, default=60.0)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--probe-nonce-sha256", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--couchdb-url", default=os.environ.get("COUCHDB_URL", ""))
    parser.add_argument("--couchdb-db", default=os.environ.get("COUCHDB_DB", "transcript_source"))
    parser.add_argument("--couchdb-user", default=os.environ.get("COUCHDB_USER", ""))
    parser.add_argument("--couchdb-password-env", default="COUCHDB_PASSWORD")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-plan-digest", default="")
    parser.add_argument("--approval", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    args = _build_parser().parse_args(effective_argv)
    try:
        resolved_targets = _resolve_canary_targets(args, os.environ)
        plan = build_canary_plan(
            project=args.project,
            provider=args.provider,
            probe_nonce_sha256=args.probe_nonce_sha256,
            expected_source_commit=args.expected_source_commit,
            observed_at=args.observed_at,
            limit=args.limit,
            max_runtime_seconds=args.max_runtime_seconds,
            target_fingerprints=resolved_targets.target_fingerprints,
        )
    except (TypeError, ValueError) as exc:
        print(
            json.dumps(
                _failure_report(
                    status="invalid_arguments",
                    error_class=type(exc).__name__,
                    stage="plan_validation",
                ),
                sort_keys=True,
            )
        )
        return 2
    if not args.execute:
        print(json.dumps(plan, sort_keys=True))
        return 0
    if str(args.expected_plan_digest or "") != str(plan["plan_digest"]):
        print(json.dumps(_failure_report(status="plan_digest_mismatch", plan=plan), sort_keys=True))
        return 2
    try:
        approval = validate_memory_enqueue_approval(
            args.approval or None,
            operation=CANARY_OPERATION,
            command_argv=effective_argv,
        )
        if float(approval.get("timeout_seconds") or 0) < float(args.max_runtime_seconds):
            raise ApprovalError("approval timeout is below command timeout")
        _require_approved_target_fingerprints(
            approval,
            target_fingerprints=resolved_targets.target_fingerprints,
        )
    except (ApprovalError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                _failure_report(
                    status="approval_rejected",
                    plan=plan,
                    error_class=type(exc).__name__,
                    stage="approval",
                ),
                sort_keys=True,
            )
        )
        return 2
    live_call_started = False
    hard_timeout_enforced = False
    try:
        with _hard_runtime_timeout(float(args.max_runtime_seconds)):
            hard_timeout_enforced = True
            live_call_started = True
            report = _execute_live(
                args,
                plan,
                resolved_targets=resolved_targets,
            )
    except CanaryExecutionError as exc:
        print(
            json.dumps(
                _failure_report(
                    status="failed",
                    plan=plan,
                    error_class=exc.error_class,
                    stage=exc.stage,
                    execution_started=live_call_started,
                    hard_timeout_enforced=hard_timeout_enforced,
                ),
                sort_keys=True,
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                _failure_report(
                    status="failed",
                    plan=plan,
                    error_class=type(exc).__name__,
                    stage="runtime",
                    execution_started=live_call_started,
                    hard_timeout_enforced=hard_timeout_enforced,
                ),
                sort_keys=True,
            )
        )
        return 1
    report["hard_timeout_required"] = True
    report["hard_timeout_enforced"] = True
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CANARY_OPERATION",
    "CANARY_SCHEMA_VERSION",
    "build_canary_plan",
    "run_projection_invalidation_canary",
    "main",
]

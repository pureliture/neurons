"""Read-only temporal acceptance authority-baseline derivation.

Positive temporal acceptance is bound to the historical artifact revisions that
the runtime read path consumes.  A mutable CouchDB coverage manifest is useful
for current aggregate reconciliation, but cannot prove what a same-session
revision contained at an earlier event time.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_knowledge.llm_brain_core.artifact_store import SessionMemoryArtifactStore
from agent_knowledge.llm_brain_core.context import (
    _recall_safe_artifacts,
    _temporal_candidate_is_relevant,
    _temporal_relevance_terms,
)
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSessionMemoryArtifactStore
from agent_knowledge.ledger import Ledger

from .._util import hash_payload, public_safe_text, require_sha256
from ..temporal import TemporalSelectorError, parse_temporal_selector


TEMPORAL_ACCEPTANCE_SELECTION_SCHEMA = "temporal_acceptance_selection.v3"
TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA = "temporal_acceptance_authority_baseline.v3"
TEMPORAL_ACCEPTANCE_DERIVE_RECEIPT_SCHEMA = "temporal_acceptance_derive_receipt.v3"
SOURCE_LEDGER_BINDING_SCHEMA = "temporal_acceptance_source_ledger_binding.v1"
SELECTION_POLICY = "latest_relevant_bounded_artifact_revision_v1"
AUTHORITY_SOURCE = "ledger_artifact_revision_history"
SOURCE_KIND = "session_memory_artifact"
SOURCE_OBJECT_TYPE = "SessionMemoryArtifact"
WORK_UNIT_OBJECT_TYPE = "WorkUnit"
AUTHORITY_LANE = "reference_only"
DEFAULT_INVENTORY_LIMIT = 100
MAX_INVENTORY_LIMIT = 1000


def _as_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_temporal_selector_value(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    try:
        selector = parse_temporal_selector(**{field.rsplit(".", 1)[-1]: text})
    except TemporalSelectorError as exc:
        raise ValueError(f"{field} must be an ISO-8601 temporal selector") from exc
    if selector is None:
        raise ValueError(f"{field} must be an ISO-8601 temporal selector")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    parsed = _as_utc(text)
    if parsed is None or "T" not in text.upper():
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 selector")
    return parsed.isoformat().replace("+00:00", "Z")


def _validate_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("temporal acceptance selection must be an object")
    allowed = {
        "schema_version",
        "policy",
        "temporal_query",
        "date_a",
        "date_b",
        "range_boundary",
    }
    if set(value) - allowed:
        raise ValueError("temporal acceptance selection must not contain raw source identifiers")
    if value.get("schema_version") != TEMPORAL_ACCEPTANCE_SELECTION_SCHEMA:
        raise ValueError("temporal acceptance selection schema is invalid")
    if value.get("policy") != SELECTION_POLICY:
        raise ValueError("temporal acceptance selection policy is invalid")
    temporal_query = str(value.get("temporal_query") or "").strip()
    if not temporal_query or public_safe_text(temporal_query, max_chars=240) != temporal_query:
        raise ValueError("temporal acceptance selection temporal_query must be public-safe")
    normalized: dict[str, Any] = {
        "schema_version": TEMPORAL_ACCEPTANCE_SELECTION_SCHEMA,
        "policy": SELECTION_POLICY,
        "temporal_query": temporal_query,
    }
    for label in ("date_a", "date_b"):
        item = value.get(label)
        if not isinstance(item, Mapping) or set(item) != {"as_of"}:
            if isinstance(item, Mapping) and any("id" in str(key).lower() for key in item):
                raise ValueError("temporal acceptance selection must not contain raw source identifiers")
            raise ValueError(f"temporal acceptance selection {label} is invalid")
        normalized[label] = {
            "as_of": normalize_temporal_selector_value(item.get("as_of"), field=f"{label}.as_of")
        }
    boundary = value.get("range_boundary")
    if not isinstance(boundary, Mapping) or set(boundary) != {"date_from", "date_to"}:
        raise ValueError("temporal acceptance selection range_boundary is invalid")
    date_from = normalize_temporal_selector_value(
        boundary.get("date_from"), field="range_boundary.date_from"
    )
    date_to = normalize_temporal_selector_value(boundary.get("date_to"), field="range_boundary.date_to")
    try:
        parse_temporal_selector(date_from=date_from, date_to=date_to)
    except TemporalSelectorError as exc:
        raise ValueError("temporal acceptance selection range boundary is reversed") from exc
    normalized["range_boundary"] = {"date_from": date_from, "date_to": date_to}
    return normalized


def _validate_bounds(*, project: str, limit: int, max_runtime_seconds: float) -> None:
    if not str(project or "").strip():
        raise ValueError("project is required")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 2 <= limit <= MAX_INVENTORY_LIMIT
    ):
        raise ValueError(f"limit must be between 2 and {MAX_INVENTORY_LIMIT}")
    if (
        isinstance(max_runtime_seconds, bool)
        or not isinstance(max_runtime_seconds, (int, float))
        or not 0 < float(max_runtime_seconds) <= 60
    ):
        raise ValueError("max_runtime_seconds is invalid")


def _check_deadline(*, started: float, max_runtime_seconds: float) -> None:
    if time.monotonic() - started > max_runtime_seconds:
        raise ValueError("artifact revision inventory runtime limit exceeded")


def authority_fingerprint_from_provenance(provenance: Mapping[str, Any]) -> str:
    """Hash the exact authority tuple, excluding object/title/summary/id fields."""

    content_hash = str(provenance.get("content_hash") or "")
    require_sha256(content_hash, "authority provenance content_hash")
    observed_at_start = str(provenance.get("observed_at_start") or "")
    observed_at_end = str(provenance.get("observed_at_end") or "")
    if _as_utc(observed_at_start) is None or _as_utc(observed_at_end) is None:
        raise ValueError("authority provenance requires bounded observed interval")
    if _as_utc(observed_at_start) > _as_utc(observed_at_end):
        raise ValueError("authority provenance requires bounded observed interval")
    source_revision = str(provenance.get("source_revision") or "").strip()
    source_kind = str(provenance.get("source_kind") or "").strip()
    source_object_type = str(provenance.get("source_object_type") or "").strip()
    authority_lane = str(provenance.get("authority_lane") or "").strip()
    if not source_revision or not source_kind or not source_object_type or not authority_lane:
        raise ValueError("authority provenance is incomplete")
    return hash_payload(
        {
            "source_kind": source_kind,
            "source_object_type": source_object_type,
            "content_hash": content_hash,
            "source_revision": source_revision,
            "observed_at_start": observed_at_start,
            "observed_at_end": observed_at_end,
            "authority_lane": authority_lane,
        }
    )


def authority_fingerprint_from_work_unit(work_unit: Mapping[str, Any]) -> str:
    """Extract the source-native tuple from a returned WorkUnit without fallback."""

    if str(work_unit.get("object_type") or "") != WORK_UNIT_OBJECT_TYPE:
        raise ValueError("work unit provenance object type is invalid")
    payload = work_unit.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("work unit provenance is missing")
    return authority_fingerprint_from_provenance(
        {
            "source_kind": payload.get("source_kind"),
            "source_object_type": payload.get("source_object_type"),
            "content_hash": payload.get("source_revision"),
            "source_revision": payload.get("source_revision"),
            "observed_at_start": payload.get("observed_at_start"),
            "observed_at_end": payload.get("observed_at_end"),
            "authority_lane": work_unit.get("authority_lane"),
        }
    )


def _artifact_intervals(artifact: Any) -> tuple[tuple[str, str], ...]:
    intervals = tuple(getattr(artifact, "revision_observed_intervals", ()) or ())
    if intervals:
        normalized = []
        for start, end in intervals:
            if _as_utc(start) is None or _as_utc(end) is None or _as_utc(start) > _as_utc(end):
                return ()
            normalized.append((str(start), str(end)))
        return tuple(sorted(set(normalized)))
    start = str(getattr(artifact, "revision_observed_at_start", "") or "")
    end = str(getattr(artifact, "revision_observed_at_end", "") or "")
    if not start or not end or _as_utc(start) is None or _as_utc(end) is None or _as_utc(start) > _as_utc(end):
        return ()
    return ((start, end),)


def _revision_snapshot(
    artifact: Any,
    *,
    selector: Mapping[str, str],
    relevance_terms: set[str],
) -> tuple[dict[str, Any], tuple[Any, ...], str, str] | None:
    if str(getattr(artifact, "revision_temporal_evidence", "") or "") != "bounded":
        return None
    source_revision = str(getattr(artifact, "source_revision", "") or "")
    try:
        require_sha256(source_revision, "artifact source_revision")
    except ValueError:
        return None
    try:
        temporal_selector = parse_temporal_selector(**dict(selector))
    except TemporalSelectorError:
        return None
    if temporal_selector is None:
        return None
    intervals = _artifact_intervals(artifact)
    matching_intervals = [
        interval
        for interval in intervals
        if temporal_selector.matches(observed_at_start=interval[0], observed_at_end=interval[1])
    ]
    if not matching_intervals:
        return None
    bindings = tuple(getattr(artifact, "revision_temporal_term_bindings", ()) or ())
    if bindings:
        matching_bindings = [
            binding
            for binding in bindings
            if temporal_selector.matches(
                observed_at_start=str(binding[0]), observed_at_end=str(binding[1])
            )
        ]
        if not matching_bindings:
            return None
        relevance_hashes = tuple(
            sorted({str(term_hash) for binding in matching_bindings for term_hash in binding[2]})
        )
    elif len(intervals) == 1:
        relevance_hashes = tuple(getattr(artifact, "search_term_hashes", ()) or ())
    else:
        return None
    if relevance_terms and not _temporal_candidate_is_relevant(
        str(getattr(artifact, "summary", "") or ""), relevance_terms, relevance_hashes
    ):
        return None
    selected_interval = max(matching_intervals)
    artifact_id = str(getattr(artifact, "artifact_id", "") or "")
    if not artifact_id:
        return None
    session_id_hash = str(getattr(artifact, "session_id_hash", "") or "")
    try:
        require_sha256(session_id_hash, "artifact session_id_hash")
    except ValueError:
        return None
    materialization_revision = getattr(artifact, "materialization_revision", 0)
    if isinstance(materialization_revision, bool) or not isinstance(materialization_revision, int):
        return None
    materialized_at = str(getattr(artifact, "materialized_at", "") or "")
    created_at = str(getattr(artifact, "created_at", "") or "")
    currentness = (
        materialization_revision,
        materialized_at,
        source_revision,
        created_at,
        artifact_id,
    )
    snapshot = {
        "source_reference_hash": hash_payload(artifact_id),
        "source_revision": source_revision,
        "observed_at_start": selected_interval[0],
        "observed_at_end": selected_interval[1],
        "materialization_revision": materialization_revision,
        "materialization_currentness_hash": hash_payload(currentness),
    }
    return snapshot, currentness, session_id_hash, artifact_id


def _selector_inventory(
    *,
    artifact_store: SessionMemoryArtifactStore,
    project: str,
    selector: Mapping[str, str],
    temporal_query: str,
    limit: int,
    label: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    try:
        temporal_selector = parse_temporal_selector(**dict(selector))
    except TemporalSelectorError as exc:
        raise ValueError(f"{label} selector is invalid") from exc
    if temporal_selector is None:
        raise ValueError(f"{label} selector is invalid")
    bounds = temporal_selector.to_audit_dict()
    revisions = artifact_store.list_observed_interval_revisions(
        project=project,
        observed_at_start=str(bounds["start"]),
        observed_at_end=str(bounds["end"]),
        limit=limit + 1,
    )
    if not isinstance(revisions, list):
        raise ValueError("artifact revision inventory is unavailable")
    if len(revisions) > limit:
        raise ValueError("artifact revision inventory is incomplete")
    revisions = _recall_safe_artifacts(revisions)
    relevance_terms = _temporal_relevance_terms(temporal_query, project=project)
    latest_relevant_by_session: dict[str, tuple[dict[str, Any], tuple[Any, ...], str]] = {}
    for artifact in revisions:
        candidate = _revision_snapshot(
            artifact,
            selector=selector,
            relevance_terms=relevance_terms,
        )
        if candidate is None:
            continue
        snapshot, currentness, session_id_hash, artifact_id = candidate
        existing = latest_relevant_by_session.get(session_id_hash)
        if existing is None or currentness > existing[1]:
            latest_relevant_by_session[session_id_hash] = (snapshot, currentness, artifact_id)
    candidates = list(latest_relevant_by_session.values())
    if not candidates:
        raise ValueError(f"{label} candidate_missing")
    candidates.sort(
        key=lambda item: (
            item[0]["observed_at_start"],
            item[2],
        ),
        reverse=True,
    )
    selected, _, _ = candidates[0]
    return dict(selected), [dict(snapshot) for snapshot, _, _ in candidates]


def _inventory_hash(inventory: Mapping[str, list[Mapping[str, Any]]]) -> str:
    return hash_payload(
        {
            label: sorted(
                [dict(snapshot) for snapshot in snapshots],
                key=lambda item: (
                    item["observed_at_start"],
                    item["observed_at_end"],
                    item["source_revision"],
                    item["source_reference_hash"],
                ),
            )
            for label, snapshots in sorted(inventory.items())
        }
    )


def _inventory_count(inventory: Mapping[str, list[Mapping[str, Any]]]) -> int:
    return len(
        {
            str(snapshot["source_reference_hash"])
            for snapshots in inventory.values()
            for snapshot in snapshots
        }
    )


def authority_baseline_receipt_is_valid(value: Mapping[str, Any]) -> bool:
    """Validate a v3 receipt without exposing its ledger inventory preimage."""

    required = {
        "schema_version",
        "selection_policy",
        "authority_source",
        "temporal_query_hash",
        "source_inventory_hash",
        "source_inventory_count",
        "date_a",
        "date_b",
        "range_boundary",
        "authority_receipt_hash",
    }
    if set(value) != required:
        return False
    core = {key: value[key] for key in required - {"authority_receipt_hash"}}
    try:
        return (
            value.get("schema_version") == TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA
            and value.get("selection_policy") == SELECTION_POLICY
            and value.get("authority_source") == AUTHORITY_SOURCE
            and isinstance(value.get("source_inventory_count"), int)
            and not isinstance(value.get("source_inventory_count"), bool)
            and value["source_inventory_count"] > 0
            and hash_payload(core) == str(value.get("authority_receipt_hash") or "")
        )
    except (TypeError, ValueError):
        return False


def validate_temporal_acceptance_authority_baseline(
    value: Mapping[str, Any],
    *,
    temporal_query: str,
) -> dict[str, Any]:
    """Require a runtime-derived v3 baseline to bind the exact public query."""

    if not authority_baseline_receipt_is_valid(value):
        raise ValueError("temporal acceptance v3 authority baseline receipt is invalid")
    if str(value.get("temporal_query_hash") or "") != hash_payload(temporal_query):
        raise ValueError("temporal acceptance v3 temporal query hash does not match")
    baseline = dict(value)
    for name in ("date_a", "date_b", "range_boundary"):
        expected = baseline.get(name)
        if not isinstance(expected, Mapping):
            raise ValueError(f"temporal acceptance v3 {name} baseline is required")
        for field in ("expected_authority_fingerprint", "expected_source_revision"):
            require_sha256(str(expected.get(field) or ""), f"temporal acceptance v3 {name}.{field}")
    if baseline["date_a"].get("expected_authority_fingerprint") == baseline["date_b"].get(
        "expected_authority_fingerprint"
    ):
        raise ValueError("temporal acceptance v3 Date A/B authority fingerprints must be distinct")
    return baseline


def _baseline_probe(snapshot: Mapping[str, Any], *, selector: Mapping[str, str]) -> dict[str, str]:
    provenance = {
        "source_kind": SOURCE_KIND,
        "source_object_type": SOURCE_OBJECT_TYPE,
        "content_hash": snapshot["source_revision"],
        "source_revision": snapshot["source_revision"],
        "observed_at_start": snapshot["observed_at_start"],
        "observed_at_end": snapshot["observed_at_end"],
        "authority_lane": AUTHORITY_LANE,
    }
    return {
        **dict(selector),
        "source_kind": SOURCE_KIND,
        "source_object_type": SOURCE_OBJECT_TYPE,
        "authority_lane": AUTHORITY_LANE,
        "expected_authority_fingerprint": authority_fingerprint_from_provenance(provenance),
        "expected_source_revision": str(snapshot["source_revision"]),
    }


def _derive_inventory(
    *,
    artifact_store: SessionMemoryArtifactStore,
    project: str,
    selection: Mapping[str, Any],
    limit: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    selected: dict[str, dict[str, Any]] = {}
    inventory: dict[str, list[dict[str, Any]]] = {}
    for label in ("date_a", "date_b", "range_boundary"):
        snapshot, snapshots = _selector_inventory(
            artifact_store=artifact_store,
            project=project,
            selector=selection[label],
            temporal_query=str(selection["temporal_query"]),
            limit=limit,
            label=label,
        )
        selected[label] = snapshot
        inventory[label] = snapshots
    return selected, inventory


def derive_temporal_acceptance_baseline(
    *,
    artifact_store: SessionMemoryArtifactStore,
    project: str,
    selection: Mapping[str, Any],
    limit: int,
    max_runtime_seconds: float,
    _started_at: float | None = None,
) -> dict[str, Any]:
    """Derive one ledger-backed temporal baseline without a brain/MCP read."""

    _validate_bounds(project=project, limit=limit, max_runtime_seconds=max_runtime_seconds)
    normalized_selection = _validate_selection(selection)
    started = time.monotonic() if _started_at is None else _started_at
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
    before_selected, before_inventory = _derive_inventory(
        artifact_store=artifact_store,
        project=project,
        selection=normalized_selection,
        limit=limit,
    )
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
    before_hash = _inventory_hash(before_inventory)
    date_a_probe = _baseline_probe(before_selected["date_a"], selector=normalized_selection["date_a"])
    date_b_probe = _baseline_probe(before_selected["date_b"], selector=normalized_selection["date_b"])
    if date_a_probe["expected_authority_fingerprint"] == date_b_probe["expected_authority_fingerprint"]:
        raise ValueError("date A/B authority fingerprints must be distinct")
    if date_a_probe["expected_source_revision"] == date_b_probe["expected_source_revision"]:
        raise ValueError("date A/B source revisions must be distinct")
    _, after_inventory = _derive_inventory(
        artifact_store=artifact_store,
        project=project,
        selection=normalized_selection,
        limit=limit,
    )
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
    if before_hash != _inventory_hash(after_inventory):
        raise ValueError("artifact revision inventory drifted during derivation")
    baseline_core = {
        "schema_version": TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA,
        "selection_policy": SELECTION_POLICY,
        "authority_source": AUTHORITY_SOURCE,
        "temporal_query_hash": hash_payload(normalized_selection["temporal_query"]),
        "source_inventory_hash": before_hash,
        "source_inventory_count": _inventory_count(before_inventory),
        "date_a": date_a_probe,
        "date_b": date_b_probe,
        "range_boundary": _baseline_probe(
            before_selected["range_boundary"],
            selector=normalized_selection["range_boundary"],
        ),
    }
    receipt_hash = hash_payload(baseline_core)
    baseline = {**baseline_core, "authority_receipt_hash": receipt_hash}
    receipt = {
        "schema_version": TEMPORAL_ACCEPTANCE_DERIVE_RECEIPT_SCHEMA,
        "status": "derived",
        "authority_receipt_hash": receipt_hash,
        "source_inventory_hash": before_hash,
        "source_inventory_count": baseline_core["source_inventory_count"],
        "authority_source": AUTHORITY_SOURCE,
        "temporal_query_hash": baseline_core["temporal_query_hash"],
        "mutation_performed": False,
        "network_used": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
        "artifact_ledger_metadata_read_only": True,
    }
    return {"status": "derived", "authority_baseline": baseline, "receipt": receipt}


def _ledger_backend(ledger: Ledger) -> str:
    adapter = getattr(ledger, "_db_adapter", None)
    if getattr(adapter, "is_file_backed", True):
        return "sqlite"
    return "postgres"


def _ledger_network_attempted(ledger: Ledger) -> bool:
    adapter = getattr(ledger, "_db_adapter", None)
    return bool(getattr(adapter, "network_attempted", False))


def _mark_network_attempted(exc: Exception) -> None:
    try:
        exc.network_attempted = True
    except (AttributeError, TypeError):
        return


def _source_file_digest(
    path: Path,
    *,
    started: float,
    max_runtime_seconds: float,
) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
            digest.update(chunk)
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("source ledger changed while its read-only binding was captured")
    return f"sha256:{digest.hexdigest()}"


def _sqlite_source_ledger_fingerprint(
    ledger_path: str,
    *,
    started: float,
    max_runtime_seconds: float,
) -> str:
    source_path = Path(ledger_path)
    source_files = sorted(source_path.parent.glob(f"{source_path.name}*"), key=lambda item: item.name)
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
    if not source_files or not source_path.is_file():
        raise ValueError("source ledger is unavailable for read-only binding")
    entries: list[dict[str, Any]] = []
    for source_file in source_files:
        if not source_file.is_file():
            raise ValueError("source ledger binding includes an unsupported file")
        stat = source_file.stat()
        entries.append(
            {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "content_hash": _source_file_digest(
                    source_file,
                    started=started,
                    max_runtime_seconds=max_runtime_seconds,
                ),
            }
        )
    return hash_payload({"backend": "sqlite", "files": entries})


def _source_ledger_binding(
    *,
    backend: str,
    before_fingerprint: str,
    after_fingerprint: str,
) -> dict[str, Any]:
    require_sha256(before_fingerprint, "source ledger binding before_fingerprint")
    require_sha256(after_fingerprint, "source ledger binding after_fingerprint")
    if before_fingerprint != after_fingerprint:
        raise ValueError("source ledger drifted during derivation")
    return {
        "schema_version": SOURCE_LEDGER_BINDING_SCHEMA,
        "backend": backend,
        "before_fingerprint": before_fingerprint,
        "after_fingerprint": after_fingerprint,
        "stable": True,
    }


def derive_temporal_acceptance_baseline_from_ledger(
    *,
    ledger_path: str,
    project: str,
    selection: Mapping[str, Any],
    limit: int,
    max_runtime_seconds: float,
) -> dict[str, Any]:
    """Open the local deployed ledger read-only and derive a v3 baseline."""

    _validate_bounds(project=project, limit=limit, max_runtime_seconds=max_runtime_seconds)
    started = time.monotonic()
    source_before = (
        _sqlite_source_ledger_fingerprint(
            ledger_path,
            started=started,
            max_runtime_seconds=max_runtime_seconds,
        )
        if not os.environ.get("NEURON_LEDGER_PG_DSN", "").strip()
        else ""
    )
    ledger: Ledger | None = None
    try:
        ledger = Ledger.open_read_only(
            ledger_path,
            deadline_monotonic=started + float(max_runtime_seconds),
        )
        _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
        backend = _ledger_backend(ledger)
        if backend == "sqlite" and not source_before:
            raise ValueError("source ledger binding must be captured before read-only open")
        result = derive_temporal_acceptance_baseline(
            artifact_store=LedgerSessionMemoryArtifactStore(ledger),
            project=project,
            selection=selection,
            limit=limit,
            max_runtime_seconds=max_runtime_seconds,
            _started_at=started,
        )
        receipt = result.get("receipt")
        if not isinstance(receipt, dict):
            raise ValueError("temporal acceptance derivation receipt is unavailable")
        if backend == "sqlite":
            source_after = _sqlite_source_ledger_fingerprint(
                ledger_path,
                started=started,
                max_runtime_seconds=max_runtime_seconds,
            )
        else:
            source_after = str(receipt.get("source_inventory_hash") or "")
            source_before = source_after
        receipt.update(
            {
                "artifact_ledger_metadata_read_only": True,
                "ledger_backend": backend,
                "network_used": backend == "postgres",
                "source_ledger_binding": _source_ledger_binding(
                    backend=backend,
                    before_fingerprint=source_before,
                    after_fingerprint=source_after,
                ),
            }
        )
        _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
        return result
    except Exception as exc:
        if ledger is not None and _ledger_network_attempted(ledger):
            _mark_network_attempted(exc)
        raise


def public_result_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)

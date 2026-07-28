"""Read-only temporal acceptance v2 authority-baseline derivation.

The baseline is deliberately derived from CouchDB source snapshot metadata, not
from an MCP response, a query service, or a projected artifact.  It uses the
already-public-safe source snapshot digest that the materializer exposes as a
temporal WorkUnit's ``source_revision``.  CouchDB document ids and revisions
are only bound inside the non-reversible inventory receipt and are never
returned or used for a per-probe live equality check.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.document_model import SourceDocType, normalize_observed_interval

from .._util import hash_payload, require_sha256
from ..temporal import TemporalSelectorError, parse_temporal_selector


TEMPORAL_ACCEPTANCE_SELECTION_SCHEMA = "temporal_acceptance_selection.v2"
TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA = "temporal_acceptance_authority_baseline.v2"
TEMPORAL_ACCEPTANCE_DERIVE_RECEIPT_SCHEMA = "temporal_acceptance_derive_receipt.v2"
SELECTION_POLICY = "latest_bounded_source_snapshot_v1"
SOURCE_KIND = "session_memory_artifact"
SOURCE_OBJECT_TYPE = "SessionMemoryArtifact"
WORK_UNIT_OBJECT_TYPE = "WorkUnit"
AUTHORITY_LANE = "reference_only"
_SOURCE_FIELDS = (
    "_id",
    "_rev",
    "doc_type",
    "project",
    "source_hash",
    "observed_at_start",
    "observed_at_end",
)


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


def _normalized_selector_value(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    try:
        selector = parse_temporal_selector(**{field.rsplit(".", 1)[-1]: text})
    except TemporalSelectorError as exc:
        raise ValueError(f"{field} must be an ISO-8601 temporal selector") from exc
    if selector is None:
        raise ValueError(f"{field} must be an ISO-8601 temporal selector")
    # Keep a calendar day as a calendar day.  Coercing it to midnight would
    # diverge from TemporalSelector, which treats a date as the entire day.
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    parsed = _as_utc(text)
    if parsed is None or "T" not in text.upper():
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 selector")
    return parsed.isoformat().replace("+00:00", "Z")


def _validate_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("temporal acceptance selection must be an object")
    allowed = {"schema_version", "policy", "date_a", "date_b", "range_boundary"}
    if set(value) - allowed:
        raise ValueError("temporal acceptance selection must not contain raw source identifiers")
    if value.get("schema_version") != TEMPORAL_ACCEPTANCE_SELECTION_SCHEMA:
        raise ValueError("temporal acceptance selection schema is invalid")
    if value.get("policy") != SELECTION_POLICY:
        raise ValueError("temporal acceptance selection policy is invalid")
    normalized: dict[str, Any] = {
        "schema_version": TEMPORAL_ACCEPTANCE_SELECTION_SCHEMA,
        "policy": SELECTION_POLICY,
    }
    for label in ("date_a", "date_b"):
        item = value.get(label)
        if not isinstance(item, Mapping) or set(item) != {"as_of"}:
            if isinstance(item, Mapping) and any("id" in str(key).lower() for key in item):
                raise ValueError("temporal acceptance selection must not contain raw source identifiers")
            raise ValueError(f"temporal acceptance selection {label} is invalid")
        normalized[label] = {
            "as_of": _normalized_selector_value(item.get("as_of"), field=f"{label}.as_of")
        }
    boundary = value.get("range_boundary")
    if not isinstance(boundary, Mapping) or set(boundary) != {"date_from", "date_to"}:
        raise ValueError("temporal acceptance selection range_boundary is invalid")
    date_from = _normalized_selector_value(boundary.get("date_from"), field="range_boundary.date_from")
    date_to = _normalized_selector_value(boundary.get("date_to"), field="range_boundary.date_to")
    try:
        parse_temporal_selector(date_from=date_from, date_to=date_to)
    except TemporalSelectorError as exc:
        raise ValueError("temporal acceptance selection range boundary is reversed") from exc
    normalized["range_boundary"] = {"date_from": date_from, "date_to": date_to}
    return normalized


def _validate_bounds(*, project: str, limit: int, max_runtime_seconds: float) -> None:
    if not str(project or "").strip():
        raise ValueError("project is required")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 2:
        raise ValueError("limit must be at least two")
    if isinstance(max_runtime_seconds, bool) or not isinstance(max_runtime_seconds, (int, float)) or not 0 < float(max_runtime_seconds) <= 60:
        raise ValueError("max_runtime_seconds is invalid")


def _check_deadline(*, started: float, max_runtime_seconds: float) -> None:
    if time.monotonic() - started > max_runtime_seconds:
        raise ValueError("source inventory runtime limit exceeded")


def _source_snapshot(source: Mapping[str, Any]) -> dict[str, str]:
    if not str(source.get("observed_at_start") or "").strip() or not str(
        source.get("observed_at_end") or ""
    ).strip():
        raise ValueError("source snapshot is missing bounded observed interval")
    interval = normalize_observed_interval(
        source.get("observed_at_start"), source.get("observed_at_end")
    )
    if interval is None:
        raise ValueError("source snapshot is missing bounded observed interval")
    content_hash = str(source.get("source_hash") or "")
    try:
        require_sha256(content_hash, "source snapshot source_hash")
    except ValueError as exc:
        raise ValueError("source snapshot source hash is invalid") from exc
    inventory_id = str(source.get("_id") or "").strip()
    inventory_revision = str(source.get("_rev") or "").strip()
    if not inventory_id or not inventory_revision:
        raise ValueError("source snapshot candidate_missing_historical_revision")
    # source_hash is built from the same direct source members and temporal
    # metadata as runtime._source_revision_from_documents.  It is intentionally
    # not CouchDB's mutable _rev or an artifact materialization content hash.
    source_revision = content_hash
    if not source_revision:
        raise ValueError("source snapshot candidate_missing_historical_revision")
    return {
        "source_revision": source_revision,
        "observed_at_start": interval[0],
        "observed_at_end": interval[1],
        "inventory_id": inventory_id,
        "inventory_revision": inventory_revision,
    }


def authority_fingerprint_from_provenance(provenance: Mapping[str, Any]) -> str:
    """Hash the exact v2 authority tuple, excluding object/title/summary/id fields."""

    content_hash = str(provenance.get("content_hash") or "")
    require_sha256(content_hash, "authority provenance content_hash")
    interval = normalize_observed_interval(
        provenance.get("observed_at_start"), provenance.get("observed_at_end")
    )
    if interval is None:
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
            "observed_at_start": interval[0],
            "observed_at_end": interval[1],
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
            # ``source_revision`` is the canonical direct-source snapshot
            # digest.  Never substitute mutable CouchDB _rev or artifact hash.
            "content_hash": payload.get("source_revision"),
            "source_revision": payload.get("source_revision"),
            "observed_at_start": payload.get("observed_at_start"),
            "observed_at_end": payload.get("observed_at_end"),
            "authority_lane": work_unit.get("authority_lane"),
        }
    )


def _inventory_rows(*, source_store: Any, project: str, limit: int) -> list[dict[str, Any]]:
    rows = source_store.find_by_type(
        SourceDocType.COVERAGE_MANIFEST,
        fields=list(_SOURCE_FIELDS),
        selector={"project": project},
        limit=limit,
    )
    if not isinstance(rows, list):
        raise ValueError("source inventory is unavailable")
    if len(rows) >= limit:
        raise ValueError("source inventory is incomplete")
    snapshots = [_source_snapshot(row) for row in rows if isinstance(row, Mapping)]
    if len(snapshots) != len(rows) or not snapshots:
        raise ValueError("source inventory candidate_missing")
    return snapshots


def _inventory_hash(snapshots: list[Mapping[str, str]]) -> str:
    return hash_payload(
        sorted(
            [
                dict(snapshot)
                for snapshot in snapshots
            ],
            key=lambda item: (
                item["observed_at_start"],
                item["observed_at_end"],
                item["source_revision"],
            ),
        )
    )


def authority_baseline_receipt_is_valid(value: Mapping[str, Any]) -> bool:
    """Validate the derive receipt without exposing its inventory preimage."""

    required = {
        "schema_version",
        "selection_policy",
        "source_inventory_hash",
        "source_inventory_current",
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
            and value.get("source_inventory_current") is True
            and isinstance(value.get("source_inventory_count"), int)
            and not isinstance(value.get("source_inventory_count"), bool)
            and value["source_inventory_count"] > 0
            and hash_payload(core) == str(value.get("authority_receipt_hash") or "")
        )
    except (TypeError, ValueError):
        return False


def _select_latest_snapshot(
    snapshots: list[Mapping[str, str]], *, selector: Mapping[str, str], label: str
) -> Mapping[str, str]:
    try:
        temporal_selector = parse_temporal_selector(**dict(selector))
    except TemporalSelectorError as exc:
        raise ValueError(f"{label} selector is invalid") from exc
    assert temporal_selector is not None
    candidates = [
        snapshot
        for snapshot in snapshots
        if temporal_selector.matches(
            observed_at_start=snapshot["observed_at_start"],
            observed_at_end=snapshot["observed_at_end"],
        )
    ]
    if not candidates:
        raise ValueError(f"{label} candidate_missing")
    # Match context._temporal_work_object_pack ordering.  We cannot use its
    # raw natural key as a tie-breaker in a public baseline, so a start-time tie
    # is deliberately ambiguous rather than silently taking the first row.
    latest_start = max(_as_utc(candidate["observed_at_start"]) for candidate in candidates)
    latest = [
        candidate
        for candidate in candidates
        if _as_utc(candidate["observed_at_start"]) == latest_start
    ]
    if len(latest) != 1:
        raise ValueError(f"{label} candidate_ambiguous")
    return latest[0]


def _baseline_probe(snapshot: Mapping[str, str], *, selector: Mapping[str, str]) -> dict[str, str]:
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
        "expected_source_revision": snapshot["source_revision"],
    }


def derive_temporal_acceptance_baseline(
    *,
    source_store: Any,
    project: str,
    selection: Mapping[str, Any],
    limit: int,
    max_runtime_seconds: float,
) -> dict[str, Any]:
    """Derive one stable source-native baseline without querying the brain read path."""

    _validate_bounds(project=project, limit=limit, max_runtime_seconds=max_runtime_seconds)
    normalized_selection = _validate_selection(selection)
    started = time.monotonic()
    before = _inventory_rows(source_store=source_store, project=project, limit=limit)
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
    before_hash = _inventory_hash(before)
    date_a = _select_latest_snapshot(before, selector=normalized_selection["date_a"], label="date_a")
    date_b = _select_latest_snapshot(before, selector=normalized_selection["date_b"], label="date_b")
    boundary = _select_latest_snapshot(before, selector=normalized_selection["range_boundary"], label="range_boundary")
    date_a_probe = _baseline_probe(date_a, selector=normalized_selection["date_a"])
    date_b_probe = _baseline_probe(date_b, selector=normalized_selection["date_b"])
    if date_a_probe["expected_authority_fingerprint"] == date_b_probe["expected_authority_fingerprint"]:
        raise ValueError("date A/B authority fingerprints must be distinct")
    if date_a_probe["expected_source_revision"] == date_b_probe["expected_source_revision"]:
        raise ValueError("date A/B source revisions must be distinct")
    after = _inventory_rows(source_store=source_store, project=project, limit=limit)
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds)
    after_hash = _inventory_hash(after)
    if before_hash != after_hash:
        raise ValueError("source inventory drifted during derivation")
    baseline_core = {
        "schema_version": TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA,
        "selection_policy": SELECTION_POLICY,
        "source_inventory_hash": before_hash,
        "source_inventory_current": True,
        "source_inventory_count": len(before),
        "date_a": date_a_probe,
        "date_b": date_b_probe,
        "range_boundary": _baseline_probe(
            boundary, selector=normalized_selection["range_boundary"]
        ),
    }
    receipt_hash = hash_payload(baseline_core)
    baseline = {**baseline_core, "authority_receipt_hash": receipt_hash}
    receipt = {
        "schema_version": TEMPORAL_ACCEPTANCE_DERIVE_RECEIPT_SCHEMA,
        "status": "derived",
        "authority_receipt_hash": receipt_hash,
        "source_inventory_hash": before_hash,
        "source_inventory_current": True,
        "source_inventory_count": len(before),
        "selection_policy": SELECTION_POLICY,
        "mutation_performed": False,
        "network_used": True,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    return {"status": "derived", "authority_baseline": baseline, "receipt": receipt}


def _auth_header(user: str, password: str) -> str:
    if not user:
        return ""
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def build_source_store_from_env() -> CouchDBHttpSourceStore:
    url = str(os.environ.get("COUCHDB_URL") or "").strip()
    if not url:
        raise ValueError("COUCHDB_URL is required")
    user = str(os.environ.get("COUCHDB_USER") or "")
    password = str(os.environ.get("COUCHDB_PASSWORD") or "")
    return CouchDBHttpSourceStore(
        base_url=url,
        db=str(os.environ.get("COUCHDB_DB") or "transcript_source"),
        auth_header=_auth_header(user, password),
    )


def public_result_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True)

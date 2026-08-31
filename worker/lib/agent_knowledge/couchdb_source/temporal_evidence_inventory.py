"""Fail-closed, read-only CouchDB-native temporal evidence inventory.

Only source-native metadata is read.  The scanner never opens the ingress state
database, writes CouchDB indexes/documents, or prints source identifiers,
revision values, bodies, locators, connection details, or secrets.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from .document_model import (
    SourceDocType,
    active_source_revision_pointer_doc_id,
    build_coverage_hash,
    build_source_hash,
    build_source_revision_token,
    observed_time_bounds,
    source_revision_manifest_doc_id,
    source_revision_member_doc_id,
)
from .source_revision import _member_hash, _parse_manifest, _source_hash, _source_revision


INVENTORY_SCHEMA_VERSION = "couchdb_temporal_evidence_inventory.v1"
INVENTORY_AUTHORITY = "couchdb_source_native"
DEFAULT_INDEX_NAME = "temporal_evidence_by_project_doc_type"
DEFAULT_INDEX_DESIGN_DOCUMENT = "_design/temporal_evidence"
_FAMILY_TYPES = (
    SourceDocType.TRANSCRIPT_SESSION,
    SourceDocType.CONVERSATION_CHUNK,
    SourceDocType.TOOL_EVIDENCE_BUNDLE,
    SourceDocType.COVERAGE_MANIFEST,
)
_TEMPORAL_CHILD_TYPES = (
    SourceDocType.CONVERSATION_CHUNK,
    SourceDocType.TOOL_EVIDENCE_BUNDLE,
)
_CHILD_FIELDS = ["_id", "_rev", "doc_type", "session_id_hash", "observed_at_start", "observed_at_end", "content_hash"]
_REVISION_SCOPE_FIELDS = [
    "source_snapshot_schema_version",
    "source_snapshot_scope",
    "source_snapshot_origin_id",
    "current_source_scope",
    "supersedes_source_document_hash",
]
_SOURCE_SNAPSHOT_SCHEMA_VERSION = "couchdb_source_revision_snapshot.v1"


def _revision_generation(document: Mapping[str, object]) -> tuple[str, str]:
    """Return ``(state, generation_key)`` for one source document.

    ``valid`` is emitted only for the exact immutable-copy contracts owned by
    ``source_revision.py``. Any partially-present or malformed marker is
    ``invalid`` so it can never silently disappear from inventory accounting.
    """

    marker_present = any(field in document for field in _REVISION_SCOPE_FIELDS)
    if not marker_present:
        return "legacy", ""

    snapshot_fields_present = any(
        field in document
        for field in (
            "source_snapshot_schema_version",
            "source_snapshot_scope",
            "source_snapshot_origin_id",
        )
    )
    current_fields_present = any(
        field in document
        for field in ("current_source_scope", "supersedes_source_document_hash")
    )
    if snapshot_fields_present:
        schema = str(document.get("source_snapshot_schema_version") or "")
        scope = str(document.get("source_snapshot_scope") or "")
        origin_id = str(document.get("source_snapshot_origin_id") or "")
        if (
            schema != _SOURCE_SNAPSHOT_SCHEMA_VERSION
            or _SHA256_RE.fullmatch(scope) is None
            or not origin_id
        ):
            return "invalid", ""
        if current_fields_present:
            current_scope = str(document.get("current_source_scope") or "")
            superseded_hash = str(document.get("supersedes_source_document_hash") or "")
            if (
                _SHA256_RE.fullmatch(current_scope) is None
                or _SHA256_RE.fullmatch(superseded_hash) is None
            ):
                return "invalid", ""
        return "valid", f"snapshot:{scope}"

    scope = str(document.get("current_source_scope") or "")
    superseded_hash = str(document.get("supersedes_source_document_hash") or "")
    if _SHA256_RE.fullmatch(scope) is None or _SHA256_RE.fullmatch(superseded_hash) is None:
        return "invalid", ""
    return "valid", f"current:{scope}"


def _is_revision_scoped_copy(document: Mapping[str, object]) -> bool:
    state, _generation = _revision_generation(document)
    return state == "valid"


def _load_active_generation_controls(
    source_store: object,
    *,
    project: str,
    limit: int,
    index_name: str,
    index_design_document: str,
) -> tuple[
    dict[str, dict[str, dict[str, str]]],
    dict[str, str],
    dict[str, str],
    set[str],
    dict[str, int],
]:
    """Load active source-revision controls with exact bounded membership.

    Pointer and revision-manifest documents are read first. Their validated
    member IDs then form the selector for the single member query, so the
    control scan is bounded by the actual active generation rather than an
    unrelated fixed global ceiling.
    """

    pointer_fields = [
        "_id", "doc_type", "session_id_hash", "project", "active_revision",
        "manifest_id", "manifest_hash", "source_hash",
    ]
    manifest_fields = [
        "_id", "doc_type", "session_id_hash", "project", "source_revision",
        "manifest_hash", "source_hash", "members", "provenance",
    ]
    member_fields = [
        "_id", "doc_type", "session_id_hash", "project", "source_revision",
        "member_id", "source_document_id", "source_doc_type",
        "material_hash_field", "material_hash", "member_revision_hash",
        "source_document_hash", "member_hash",
    ]
    selector = {"project": project}
    for doc_type, fields in (
        (SourceDocType.ACTIVE_SOURCE_REVISION, pointer_fields),
        (SourceDocType.SOURCE_REVISION_MANIFEST, manifest_fields),
    ):
        _require_indexed_preflight(
            source_store,
            selector={**selector, "doc_type": doc_type},
            fields=fields,
            bounded_limit=limit + 1,
            index_name=index_name,
            index_design_document=index_design_document,
        )

    pointer_documents, pointer_stats = _bounded_docs(
        source_store,
        doc_type=SourceDocType.ACTIVE_SOURCE_REVISION,
        project=project,
        fields=pointer_fields,
        limit=limit,
        index_name=index_name,
        index_design_document=index_design_document,
    )
    manifest_documents, manifest_stats = _bounded_docs(
        source_store,
        doc_type=SourceDocType.SOURCE_REVISION_MANIFEST,
        project=project,
        fields=manifest_fields,
        limit=limit,
        index_name=index_name,
        index_design_document=index_design_document,
    )

    manifests_by_id = {
        str(document.get("_id") or ""): document
        for document in manifest_documents
    }
    pointers_by_sid: dict[str, list[dict[str, object]]] = {}
    blocked: set[str] = set()
    for pointer in pointer_documents:
        sid = str(pointer.get("session_id_hash") or "")
        if not sid or str(pointer.get("_id") or "") != active_source_revision_pointer_doc_id(sid):
            blocked.add(sid)
            continue
        pointers_by_sid.setdefault(sid, []).append(pointer)

    pending: list[
        tuple[
            str,
            dict[str, object],
            str,
            str,
            list[dict[str, object]],
            list[dict[str, object]],
        ]
    ] = []
    member_ids: set[str] = set()
    active_manifest_ids: dict[str, str] = {}
    active_source_hashes: dict[str, str] = {}
    # Membership is bounded by the same ceilings as the family scans: a
    # session can reference at most every scanned transcript/chunk/bundle
    # (2 * limit + 1), and the whole project's active membership can never
    # exceed the global document ceiling (4 * limit) because every member
    # must resolve to a distinct scanned source document to be selectable.
    # Anything beyond those bounds is a malformed control set, and honouring
    # it would let one manifest dictate an unbounded `$in` query.
    max_members_per_session = 2 * limit + 1
    max_total_members = limit * 4
    for sid, candidates in pointers_by_sid.items():
        if len(candidates) != 1:
            blocked.add(sid)
            continue
        pointer = candidates[0]
        active_revision = str(pointer.get("active_revision") or "")
        manifest_id = str(pointer.get("manifest_id") or "")
        pointer_manifest_hash = str(pointer.get("manifest_hash") or "")
        pointer_source_hash = str(pointer.get("source_hash") or "")
        if (
            _SHA256_RE.fullmatch(active_revision) is None
            or _SHA256_RE.fullmatch(pointer_manifest_hash) is None
            or _SHA256_RE.fullmatch(pointer_source_hash) is None
            or manifest_id != source_revision_manifest_doc_id(sid, active_revision)
        ):
            blocked.add(sid)
            continue
        manifest = manifests_by_id.get(manifest_id)
        try:
            parsed_members = _parse_manifest(
                manifest or {},
                session_id_hash=sid,
                active_revision=active_revision,
                expected_manifest_hash=pointer_manifest_hash,
                expected_source_hash=pointer_source_hash,
            )
        except Exception:
            blocked.add(sid)
            continue
        descriptors: list[dict[str, object]] = []
        valid = len(parsed_members) <= max_members_per_session
        for membership in parsed_members:
            source_id = str(membership.get("source_document_id") or "")
            member_id = str(membership.get("member_id") or "")
            source_type = str(membership.get("source_doc_type") or "")
            material_field = str(membership.get("material_hash_field") or "")
            if (
                source_type not in {
                    SourceDocType.TRANSCRIPT_SESSION,
                    SourceDocType.CONVERSATION_CHUNK,
                    SourceDocType.TOOL_EVIDENCE_BUNDLE,
                }
                or not source_id
                or member_id != source_revision_member_doc_id(sid, active_revision, source_id)
                or material_field not in {"content_hash", "coverage_hash", "source_hash", "document_hash"}
                or any(
                    _SHA256_RE.fullmatch(str(membership.get(field) or "")) is None
                    for field in ("material_hash", "member_revision_hash", "source_document_hash", "member_hash")
                )
            ):
                valid = False
                break
            descriptor = {
                key: membership.get(key)
                for key in (
                    "source_document_id",
                    "source_doc_type",
                    "material_hash_field",
                    "material_hash",
                    "member_revision_hash",
                    "source_document_hash",
                )
            }
            descriptors.append(descriptor)
            member_ids.add(member_id)
        if not valid or not descriptors or _source_revision(descriptors) != active_revision:
            blocked.add(sid)
            continue
        pending.append((sid, pointer, active_revision, manifest_id, parsed_members, descriptors))
        active_manifest_ids[sid] = manifest_id
        active_source_hashes[sid] = pointer_source_hash

    member_documents: list[dict[str, object]] = []
    member_stats = {"total_docs_examined": 0, "total_keys_examined": 0}
    if not member_ids:
        pass
    elif len(member_ids) > max_total_members:
        # The aggregate control set already exceeds the global document
        # ceiling; fail closed instead of issuing an unbounded `$in` query.
        raise _InventoryBlocked("membership_bound_exceeded")
    else:
        _require_indexed_preflight(
            source_store,
            selector={**selector, "doc_type": SourceDocType.SOURCE_REVISION_MEMBER, "_id": {"$in": sorted(member_ids)}},
            fields=member_fields,
            bounded_limit=len(member_ids) + 1,
            index_name=index_name,
            index_design_document=index_design_document,
        )
        member_documents, member_stats = _bounded_docs(
            source_store,
            doc_type=SourceDocType.SOURCE_REVISION_MEMBER,
            project=project,
            fields=member_fields,
            limit=len(member_ids),
            index_name=index_name,
            index_design_document=index_design_document,
            selector_extra={"_id": {"$in": sorted(member_ids)}},
        )
    members_by_id = {str(document.get("_id") or ""): document for document in member_documents}
    controls: dict[str, dict[str, dict[str, str]]] = {}
    for sid, _pointer, active_revision, _manifest_id, parsed_members, _descriptors in pending:
        details: dict[str, dict[str, str]] = {}
        valid = True
        for membership in parsed_members:
            member_id = str(membership.get("member_id") or "")
            member = members_by_id.get(member_id)
            if member is None:
                valid = False
                break
            member_fields_for_hash = {
                key: membership.get(key)
                for key in (
                    "source_document_id",
                    "source_doc_type",
                    "material_hash_field",
                    "material_hash",
                    "member_revision_hash",
                    "source_document_hash",
                )
            }
            if (
                str(member.get("doc_type") or "") != SourceDocType.SOURCE_REVISION_MEMBER
                or str(member.get("session_id_hash") or "") != sid
                or str(member.get("project") or "") != project
                or str(member.get("source_revision") or "") != active_revision
                or any(member.get(key) != value for key, value in membership.items())
                or str(member.get("member_hash") or "") != _member_hash(
                    session_id_hash=sid,
                    source_revision=active_revision,
                    descriptor=member_fields_for_hash,
                    member_id=member_id,
                )
            ):
                valid = False
                break
            source_id = str(membership.get("source_document_id") or "")
            details[source_id] = {
                "source_doc_type": str(membership.get("source_doc_type") or ""),
                "material_hash_field": str(membership.get("material_hash_field") or ""),
                "material_hash": str(membership.get("material_hash") or ""),
                "member_revision_hash": str(membership.get("member_revision_hash") or ""),
                "source_document_hash": str(membership.get("source_document_hash") or ""),
            }
        if not valid:
            blocked.add(sid)
            active_manifest_ids.pop(sid, None)
            active_source_hashes.pop(sid, None)
            continue
        controls[sid] = details

    stats = {
        "total_docs_examined": (
            pointer_stats["total_docs_examined"]
            + manifest_stats["total_docs_examined"]
            + member_stats["total_docs_examined"]
        ),
        "total_keys_examined": (
            pointer_stats["total_keys_examined"]
            + manifest_stats["total_keys_examined"]
            + member_stats["total_keys_examined"]
        ),
    }
    return controls, active_manifest_ids, active_source_hashes, blocked, stats



def _select_logical_family_documents(
    families: Mapping[str, list[dict[str, object]]],
    *,
    active_member_ids: Mapping[str, Mapping[str, Mapping[str, str]]],
    active_manifest_ids: Mapping[str, str],
    active_source_hashes: Mapping[str, str],
    blocked_sessions: set[str],
) -> tuple[dict[str, list[dict[str, object]]], int]:
    """Select only a verified active generation, otherwise legacy documents.

    A snapshot is selected only when its exact document-id set equals the active
    source-revision manifest membership. Partial snapshots, multiple scopes, and
    malformed markers are counted as blocked and never reported complete.
    """

    source_families = {
        doc_type: documents
        for doc_type, documents in families.items()
        if doc_type != SourceDocType.COVERAGE_MANIFEST
    }
    groups: dict[str, dict[str, list[dict[str, object]]]] = {}
    invalid_sessions: set[str] = set()
    for documents in source_families.values():
        for document in documents:
            sid = str(document.get("session_id_hash") or "")
            state, generation = _revision_generation(document)
            if state == "invalid":
                invalid_sessions.add(sid)
            elif state == "valid" and sid:
                groups.setdefault(sid, {}).setdefault(generation, []).append(document)

    selected: dict[str, list[dict[str, object]]] = {}
    blocked_count = len(blocked_sessions) + len(invalid_sessions)
    valid_snapshot_sessions = set(groups)
    blocked_count += len(valid_snapshot_sessions - set(active_member_ids))
    source_by_id = {
        str(document.get("_id") or ""): document
        for documents in source_families.values()
        for document in documents
        if str(document.get("_id") or "")
    }
    selected_generation: dict[str, str] = {}
    for sid, expected_details in active_member_ids.items():
        expected_types = {
            source_id: str(details.get("source_doc_type") or "")
            for source_id, details in expected_details.items()
        }
        matching_generations = [
            (generation, group)
            for generation, group in groups.get(sid, {}).items()
            if {str(row.get("_id") or "") for row in group} == set(expected_types)
        ]
        if len(matching_generations) != 1:
            blocked_count += 1
            continue
        generation, group = matching_generations[0]
        source_valid = True
        for source_id, source_type in expected_types.items():
            source = source_by_id.get(source_id)
            detail = expected_details[source_id]
            material_field = str(detail.get("material_hash_field") or "")
            if (
                source is None
                or str(source.get("doc_type") or "") != source_type
                or str(source.get("session_id_hash") or "") != sid
                or material_field not in {"content_hash", "coverage_hash", "source_hash", "document_hash"}
            ):
                source_valid = False
                break
            snapshot_origin_id = str(source.get("source_snapshot_origin_id") or "")
            if snapshot_origin_id:
                # Production contract (source_revision.py
                # build_revision_scoped_source_documents) stores the origin
                # document's raw `_id` here, not a typed copy id, so the
                # binding is an origin-resolvability contract rather than a
                # prefix one. The snapshot copy is only trusted when its
                # origin is actually visible in this scanned family view,
                # shares the session and type, and is itself not another
                # marked copy (generic-over-generic chains are not an
                # active-source contract, mirroring
                # _validated_immediate_snapshot_origin_id).
                origin = source_by_id.get(snapshot_origin_id)
                if (
                    origin is None
                    or str(origin.get("session_id_hash") or "") != sid
                    or str(origin.get("doc_type") or "") != source_type
                    or _revision_generation(origin)[0] == "valid"
                ):
                    source_valid = False
                    break
            current_scope = str(source.get("current_source_scope") or "")
            superseded_hash = str(source.get("supersedes_source_document_hash") or "")
            if current_scope and not superseded_hash:
                source_valid = False
                break
            if not current_scope and superseded_hash:
                source_valid = False
                break
            if material_field != "document_hash":
                try:
                    source_valid = (
                        str(source.get(material_field) or "") == str(detail.get("material_hash") or "")
                        and build_source_revision_token(
                            source,
                            material_hash_field=material_field,
                        )
                        == str(detail.get("member_revision_hash") or "")
                    )
                except (TypeError, ValueError):
                    source_valid = False
            else:
                # The inventory deliberately does not project private bodies.
                # A document-hash source cannot be proven from this redacted
                # view, so it remains blocked rather than being accepted on an
                # incomplete descriptor.
                source_valid = False
            if not source_valid:
                break
        if source_valid:
            try:
                canonical_source_hash = _source_hash(group)
                source_valid = canonical_source_hash == active_source_hashes.get(sid)
            except (TypeError, ValueError):
                source_valid = False
        if not source_valid:
            blocked_count += 1
            continue
        selected_generation[sid] = generation

    for doc_type, documents in families.items():
        if doc_type == SourceDocType.COVERAGE_MANIFEST:
            manifest_rows: list[dict[str, object]] = []
            for document in documents:
                state, _generation = _revision_generation(document)
                if state != "legacy":
                    blocked_count += 1
                sid = str(document.get("session_id_hash") or "")
                expected_manifest_id = active_manifest_ids.get(sid)
                actual_manifest_id = str(document.get("active_source_manifest_id") or "")
                if (
                    expected_manifest_id is not None
                    and actual_manifest_id != expected_manifest_id
                ) or (
                    expected_manifest_id is None
                    and actual_manifest_id
                ):
                    blocked_count += 1
                manifest_rows.append(document)
            selected[doc_type] = manifest_rows
            continue
        rows: list[dict[str, object]] = []
        for document in documents:
            sid = str(document.get("session_id_hash") or "")
            state, generation = _revision_generation(document)
            if not sid and state != "legacy":
                # Preserve malformed marked documents so the existing missing
                # session-id counters observe them instead of dropping them.
                rows.append(document)
                continue
            if sid in blocked_sessions or sid in invalid_sessions:
                # Keep both generations visible so a blocked control state cannot
                # accidentally look complete; the explicit blocked count below
                # also handles the single-partial-generation case.
                rows.append(document)
                continue
            if sid in selected_generation:
                if state == "valid" and generation == selected_generation[sid]:
                    rows.append(document)
            elif state == "legacy":
                rows.append(document)
        selected[doc_type] = rows
    return selected, blocked_count
_CHUNK_INTEGRITY_FIELDS = [
    "turn_start_index",
    "turn_end_index",
    "part_index",
    "part_count",
    "char_start",
    "char_end",
]
_BUNDLE_INTEGRITY_FIELDS = [
    "coverage_hash",
    "part_index",
    "part_count",
    "evidence_index_start",
    "evidence_index_end",
]
_COVERAGE_FIELDS = [
    "_id",
    "_rev",
    "doc_type",
    "session_id_hash",
    "observed_at_start",
    "observed_at_end",
    "conversation_chunk_count",
    "tool_evidence_bundle_count",
    "conversation_coverage_hash",
    "tool_evidence_coverage_hash",
    "source_hash",
    "active_source_manifest_id",
]
_EXECUTION_SCAN_MULTIPLIER = 2
_EXPECTED_INVENTORY_REQUEST_COUNT = 2 + (2 * (len(_FAMILY_TYPES) + 3))
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SESSION_FIELDS = [
    "_id",
    "_rev",
    "doc_type",
    "session_id_hash",
    "observed_at_start",
    "observed_at_end",
    "started_at",
    "ended_at",
    "source_hash",
]
_CONFIGURATION_ERRORS = frozenset(
    {
        "complete_scan_acknowledgement_required",
        "couchdb_environment_unavailable",
        "source_index_preflight_unavailable",
        "source_index_preflight_failed",
        "source_index_preflight_invalid",
        "source_index_preflight_unindexed",
        "source_index_preflight_partial",
        "source_change_sequence_unavailable",
        "source_change_sequence_failed",
        "source_change_sequence_invalid",
        "source_find_unavailable",
        "source_scan_failed",
        "source_scan_invalid",
        "source_execution_stats_unavailable",
        "source_execution_stats_invalid",
    }
)


class _InventoryBlocked(RuntimeError):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _per_request_timeout_seconds(max_runtime_seconds: float) -> float:
    """Reserve the whole bounded budget across the inventory's fixed request set."""

    return min(30.0, float(max_runtime_seconds) / _EXPECTED_INVENTORY_REQUEST_COUNT)


def _validate_bounds(*, project: str, limit: int, max_runtime_seconds: float) -> None:
    if not str(project or "").strip():
        raise ValueError("project scope is required")
    if int(limit) <= 0:
        raise ValueError("limit must be positive")
    if not math.isfinite(float(max_runtime_seconds)) or float(max_runtime_seconds) <= 0:
        raise ValueError("max runtime must be positive")


def _classify_pair(start: object, end: object) -> str:
    """Classify one field family without crossing into any other field family."""

    raw_start = str(start or "").strip()
    raw_end = str(end or "").strip()
    if not raw_start and not raw_end:
        return "absent"
    if not raw_start or not raw_end:
        return "missing"
    try:
        parsed_start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        parsed_end = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
    except ValueError:
        return "invalid"
    if parsed_start.tzinfo is None or parsed_end.tzinfo is None:
        return "invalid"
    if parsed_start.astimezone(timezone.utc) > parsed_end.astimezone(timezone.utc):
        return "reversed"
    return "valid"


def _index_field_names(index: Mapping[str, object]) -> set[str]:
    definition = index.get("def")
    source = definition if isinstance(definition, Mapping) else index
    raw_fields = source.get("fields") if isinstance(source, Mapping) else None
    names: set[str] = set()
    if not isinstance(raw_fields, list):
        return names
    for field in raw_fields:
        if isinstance(field, str):
            names.add(field)
        elif isinstance(field, Mapping):
            names.update(str(name) for name in field)
    return names


def _require_indexed_preflight(
    store: object,
    *,
    selector: Mapping[str, object],
    fields: list[str],
    bounded_limit: int,
    index_name: str,
    index_design_document: str,
) -> None:
    explain = getattr(store, "explain_find", None)
    if not callable(explain):
        raise _InventoryBlocked("source_index_preflight_unavailable")
    try:
        payload = explain(
            selector=dict(selector),
            fields=fields,
            limit=bounded_limit,
            index_name=index_name,
            index_design_document=index_design_document,
            allow_fallback=False,
        )
    except Exception as exc:
        raise _InventoryBlocked("source_index_preflight_failed") from exc
    index = payload.get("index") if isinstance(payload, Mapping) else None
    if not isinstance(index, Mapping):
        raise _InventoryBlocked("source_index_preflight_invalid")
    if str(index.get("type") or "") != "json":
        raise _InventoryBlocked("source_index_preflight_unindexed")
    if str(index.get("name") or "") != index_name:
        raise _InventoryBlocked("source_index_preflight_unindexed")
    if str(index.get("ddoc") or "") != index_design_document:
        raise _InventoryBlocked("source_index_preflight_unindexed")
    if not {"project", "doc_type"}.issubset(_index_field_names(index)):
        raise _InventoryBlocked("source_index_preflight_unindexed")
    definition = index.get("def")
    partial_filter_selector = (
        definition.get("partial_filter_selector")
        if isinstance(definition, Mapping)
        else index.get("partial_filter_selector")
    )
    if partial_filter_selector:
        # The selected Mango index can omit source documents.  We deliberately
        # do not infer selector implication from a caller-provided predicate.
        raise _InventoryBlocked("source_index_preflight_partial")


def _execution_stats_summary(
    value: object,
    *,
    bounded_limit: int,
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise _InventoryBlocked("source_execution_stats_unavailable")
    totals: dict[str, int] = {}
    for field in ("total_docs_examined", "total_keys_examined"):
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise _InventoryBlocked("source_execution_stats_invalid")
        totals[field] = raw
    per_family_bound = bounded_limit * _EXECUTION_SCAN_MULTIPLIER
    for total in totals.values():
        if total > per_family_bound:
            raise _InventoryBlocked("source_index_scan_bound_exceeded")
    return totals


def _bounded_docs(
    store: object,
    *,
    doc_type: str,
    project: str,
    fields: list[str],
    limit: int,
    index_name: str,
    index_design_document: str,
    selector_extra: Mapping[str, object] | None = None,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    find_by_type = getattr(store, "find_by_type_with_execution_stats", None)
    if not callable(find_by_type):
        raise _InventoryBlocked("source_find_unavailable")
    try:
        payload = find_by_type(
            doc_type,
            fields=fields,
            selector={"project": project, **dict(selector_extra or {})},
            limit=limit + 1,
            use_index=[index_design_document, index_name],
            allow_fallback=False,
        )
    except Exception as exc:
        raise _InventoryBlocked("source_scan_failed") from exc
    if not isinstance(payload, Mapping):
        raise _InventoryBlocked("source_scan_invalid")
    documents = payload.get("documents")
    if not isinstance(documents, list):
        raise _InventoryBlocked("source_scan_invalid")
    if not all(isinstance(document, Mapping) for document in documents):
        raise _InventoryBlocked("source_scan_invalid")
    if len(documents) > limit:
        raise _InventoryBlocked("scope_limit_exceeded")
    return [dict(document) for document in documents], _execution_stats_summary(
        payload.get("execution_stats"),
        bounded_limit=limit + 1,
    )


def _read_change_sequence(store: object) -> str:
    reader = getattr(store, "read_change_sequence", None)
    if not callable(reader):
        raise _InventoryBlocked("source_change_sequence_unavailable")
    try:
        value = reader()
    except Exception as exc:
        raise _InventoryBlocked("source_change_sequence_failed") from exc
    if value is None or not str(value).strip():
        raise _InventoryBlocked("source_change_sequence_invalid")
    return str(value)


def _check_deadline(*, started: float, max_runtime_seconds: float, monotonic: Callable[[], float]) -> None:
    if monotonic() - started > max_runtime_seconds:
        raise _InventoryBlocked("runtime_bound_exceeded")


def _revision_hash(document: Mapping[str, object]) -> str:
    # `_id` and `_rev` never leave the process; this one-way hash is the manifest.
    return _sha256(f"{document.get('_id') or ''}\n{document.get('_rev') or ''}")


def _inventory_digest(*, project: str, families: Mapping[str, list[dict[str, object]]]) -> str:
    payload = {
        "project_scope_hash": _sha256(project),
        "family_revision_hashes": {
            family: sorted(_revision_hash(document) for document in documents)
            for family, documents in sorted(families.items())
        },
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _classify_child(document: Mapping[str, object], parent: Mapping[str, object] | None) -> tuple[str, str]:
    direct = _classify_pair(document.get("observed_at_start"), document.get("observed_at_end"))
    if direct == "valid":
        return "direct_observed", direct
    if direct != "absent":
        return "malformed", direct
    if parent is None:
        return "no_evidence", direct
    parent_observed = _classify_pair(parent.get("observed_at_start"), parent.get("observed_at_end"))
    if parent_observed == "valid":
        return "parent_observed_fallback", direct
    if parent_observed != "absent":
        return "malformed", parent_observed
    parent_legacy = _classify_pair(parent.get("started_at"), parent.get("ended_at"))
    if parent_legacy == "valid":
        return "parent_legacy_fallback", direct
    if parent_legacy != "absent":
        return "malformed", parent_legacy
    return "no_evidence", direct


def _malformed_observed_pair_details(documents: list[dict[str, object]]) -> list[str]:
    """Return only present-but-invalid observed-time pairs.

    Parent and coverage-manifest timestamps are not permitted to satisfy a
    child evidence requirement.  If present, though, they remain part of the
    source integrity contract and must not be silently ignored just because a
    child has valid direct evidence.  An entirely absent pair is intentionally
    not counted here: it is not an alternate source of temporal authority.
    """

    details = [
        _classify_pair(document.get("observed_at_start"), document.get("observed_at_end"))
        for document in documents
    ]
    return [detail for detail in details if detail not in {"absent", "valid"}]


def _documents_by_session(documents: list[dict[str, object]]) -> tuple[dict[str, list[dict[str, object]]], int]:
    grouped: dict[str, list[dict[str, object]]] = {}
    missing_session_id_count = 0
    for document in documents:
        session_id_hash = str(document.get("session_id_hash") or "").strip()
        if not session_id_hash:
            missing_session_id_count += 1
            continue
        grouped.setdefault(session_id_hash, []).append(document)
    return grouped, missing_session_id_count


def _nonnegative_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _is_nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_sha256_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _canonical_input_integrity_counts(
    *,
    families: Mapping[str, list[dict[str, object]]],
) -> dict[str, int]:
    """Count unusable canonical-hash inputs without exposing their values."""

    session_documents = families[SourceDocType.TRANSCRIPT_SESSION]
    chunk_documents = families[SourceDocType.CONVERSATION_CHUNK]
    bundle_documents = families[SourceDocType.TOOL_EVIDENCE_BUNDLE]
    manifest_documents = families[SourceDocType.COVERAGE_MANIFEST]

    def invalid_identity_count(documents: list[dict[str, object]]) -> int:
        return sum(
            not _is_nonempty_text(document.get("_id"))
            or not _is_nonempty_text(document.get("_rev"))
            for document in documents
        )

    return {
        "invalid_transcript_session_identity_count": invalid_identity_count(session_documents),
        "invalid_conversation_chunk_identity_count": invalid_identity_count(chunk_documents),
        "invalid_tool_evidence_bundle_identity_count": invalid_identity_count(bundle_documents),
        "invalid_coverage_manifest_identity_count": invalid_identity_count(manifest_documents),
        "invalid_transcript_session_session_id_hash_count": sum(
            not _is_sha256_hash(document.get("session_id_hash")) for document in session_documents
        ),
        "invalid_conversation_chunk_session_id_hash_count": sum(
            not _is_sha256_hash(document.get("session_id_hash")) for document in chunk_documents
        ),
        "invalid_tool_evidence_bundle_session_id_hash_count": sum(
            not _is_sha256_hash(document.get("session_id_hash")) for document in bundle_documents
        ),
        "invalid_coverage_manifest_session_id_hash_count": sum(
            not _is_sha256_hash(document.get("session_id_hash")) for document in manifest_documents
        ),
        "invalid_conversation_chunk_content_hash_count": sum(
            not _is_sha256_hash(document.get("content_hash")) for document in chunk_documents
        ),
        "invalid_tool_evidence_bundle_content_hash_count": sum(
            not _is_sha256_hash(document.get("content_hash")) for document in bundle_documents
        ),
        "invalid_tool_evidence_coverage_hash_count": sum(
            not _is_sha256_hash(document.get("coverage_hash")) for document in bundle_documents
        ),
        "invalid_manifest_conversation_coverage_hash_count": sum(
            not _is_sha256_hash(document.get("conversation_coverage_hash"))
            for document in manifest_documents
        ),
        "invalid_manifest_tool_evidence_coverage_hash_count": sum(
            not _is_sha256_hash(document.get("tool_evidence_coverage_hash"))
            for document in manifest_documents
        ),
        "invalid_manifest_source_hash_count": sum(
            not _is_sha256_hash(document.get("source_hash")) for document in manifest_documents
        ),
    }


def _manifest_integrity_counts(
    *,
    manifests_by_session: Mapping[str, list[dict[str, object]]],
    chunks_by_session: Mapping[str, list[dict[str, object]]],
    bundles_by_session: Mapping[str, list[dict[str, object]]],
    parents_by_session: Mapping[str, list[dict[str, object]]],
) -> dict[str, int]:
    """Compare each manifest with its direct source children without rendering values."""

    counts = {
        "conversation_coverage_hash_mismatch_count": 0,
        "tool_evidence_coverage_hash_mismatch_count": 0,
        "source_hash_mismatch_count": 0,
    }
    for session_id_hash, manifests in manifests_by_session.items():
        chunks = chunks_by_session.get(session_id_hash, [])
        bundles = bundles_by_session.get(session_id_hash, [])
        parents = parents_by_session.get(session_id_hash, [])
        conversation_coverage_hash = build_coverage_hash(
            str(chunk.get("content_hash") or "") for chunk in chunks
        )
        tool_evidence_coverage_hash = build_coverage_hash(
            str(bundle.get("coverage_hash") or "") for bundle in bundles
        )
        # A duplicated transcript session is separately blocked and must not
        # select an arbitrary parent for source-hash reconstruction.
        source_sessions = parents if len(parents) == 1 else []
        observed_at_start, observed_at_end = observed_time_bounds(
            sessions=source_sessions,
            chunks=[*chunks, *bundles],
        )
        source_hash = build_source_hash(
            [str(chunk.get("content_hash") or "") for chunk in chunks],
            [str(bundle.get("coverage_hash") or "") for bundle in bundles],
            observed_at_start=observed_at_start,
            observed_at_end=observed_at_end,
            conversation_revision_tokens=[
                build_source_revision_token(chunk, material_hash_field="content_hash")
                for chunk in chunks
            ],
            tool_evidence_revision_tokens=[
                build_source_revision_token(bundle, material_hash_field="content_hash")
                for bundle in bundles
            ],
        )
        for manifest in manifests:
            if str(manifest.get("conversation_coverage_hash") or "") != conversation_coverage_hash:
                counts["conversation_coverage_hash_mismatch_count"] += 1
            if str(manifest.get("tool_evidence_coverage_hash") or "") != tool_evidence_coverage_hash:
                counts["tool_evidence_coverage_hash_mismatch_count"] += 1
            if str(manifest.get("source_hash") or "") != source_hash:
                counts["source_hash_mismatch_count"] += 1
    return counts


def inventory_temporal_evidence(
    *,
    source_store: object,
    project: str,
    limit: int,
    max_runtime_seconds: float,
    require_complete_scan: bool,
    index_name: str = DEFAULT_INDEX_NAME,
    index_design_document: str = DEFAULT_INDEX_DESIGN_DOCUMENT,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Read four families with ``limit`` per family and a global ``4 * limit`` ceiling."""

    _validate_bounds(project=project, limit=limit, max_runtime_seconds=max_runtime_seconds)
    if not require_complete_scan:
        raise _InventoryBlocked("complete_scan_acknowledgement_required")
    if not str(index_name or "").strip() or not str(index_design_document or "").strip():
        raise ValueError("index name is required")

    started = monotonic()
    before_sequence = _read_change_sequence(source_store)
    families: dict[str, list[dict[str, object]]] = {}
    execution_stats = {"total_docs_examined": 0, "total_keys_examined": 0}
    for doc_type in _FAMILY_TYPES:
        if doc_type == SourceDocType.TRANSCRIPT_SESSION:
            fields = [*_SESSION_FIELDS, *_REVISION_SCOPE_FIELDS]
        elif doc_type == SourceDocType.COVERAGE_MANIFEST:
            fields = [*_COVERAGE_FIELDS, *_REVISION_SCOPE_FIELDS]
        elif doc_type == SourceDocType.CONVERSATION_CHUNK:
            fields = [*_CHILD_FIELDS, *_CHUNK_INTEGRITY_FIELDS, *_REVISION_SCOPE_FIELDS]
        else:
            fields = [*_CHILD_FIELDS, *_BUNDLE_INTEGRITY_FIELDS, *_REVISION_SCOPE_FIELDS]
        selector = {"project": project, "doc_type": doc_type}
        _require_indexed_preflight(
            source_store,
            selector=selector,
            fields=fields,
            bounded_limit=limit + 1,
            index_name=index_name,
            index_design_document=index_design_document,
        )
        _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic)
        documents, family_stats = _bounded_docs(
            source_store,
            doc_type=doc_type,
            project=project,
            fields=fields,
            limit=limit,
            index_name=index_name,
            index_design_document=index_design_document,
        )
        families[doc_type] = documents
        for field, value in family_stats.items():
            execution_stats[field] += value
        _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic)
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic)
    active_member_ids, active_manifest_ids, active_source_hashes, blocked_sessions, control_stats = _load_active_generation_controls(
        source_store,
        project=project,
        limit=limit,
        index_name=index_name,
        index_design_document=index_design_document,
    )
    for field, value in control_stats.items():
        execution_stats[field] += value
    families, generation_blocked_count = _select_logical_family_documents(
        families,
        active_member_ids=active_member_ids,
        active_manifest_ids=active_manifest_ids,
        active_source_hashes=active_source_hashes,
        blocked_sessions=blocked_sessions,
    )
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic)
    global_document_limit = limit * len(_FAMILY_TYPES)
    if sum(len(documents) for documents in families.values()) > global_document_limit:
        raise _InventoryBlocked("scope_limit_exceeded")
    after_sequence = _read_change_sequence(source_store)
    _check_deadline(started=started, max_runtime_seconds=max_runtime_seconds, monotonic=monotonic)

    parent_by_session, missing_parent_session_id_count = _documents_by_session(
        families[SourceDocType.TRANSCRIPT_SESSION]
    )
    child_documents_by_type = {
        doc_type: families[doc_type] for doc_type in _TEMPORAL_CHILD_TYPES
    }
    chunks_by_session, missing_chunk_session_id_count = _documents_by_session(
        child_documents_by_type[SourceDocType.CONVERSATION_CHUNK]
    )
    bundles_by_session, missing_bundle_session_id_count = _documents_by_session(
        child_documents_by_type[SourceDocType.TOOL_EVIDENCE_BUNDLE]
    )
    children = [
        document
        for documents in child_documents_by_type.values()
        for document in documents
    ]
    children_by_session, missing_child_session_id_count = _documents_by_session(children)
    manifests_by_session, missing_manifest_session_id_count = _documents_by_session(
        families[SourceDocType.COVERAGE_MANIFEST]
    )
    outcomes = [
        _classify_child(
            document,
            (
                parent_documents[0]
                if len(
                    parent_documents := parent_by_session.get(
                        str(document.get("session_id_hash") or ""), []
                    )
                )
                == 1
                else None
            ),
        )
        for document in children
    ]
    classifications = [classification for classification, _detail in outcomes]
    # Fallback resolution can encounter the same parent pair for multiple
    # children.  Count physical source pairs independently so a single
    # malformed document is never inflated by its child fan-out.
    malformed_child_observed_details = _malformed_observed_pair_details(children)
    malformed_parent_observed_details = _malformed_observed_pair_details(
        families[SourceDocType.TRANSCRIPT_SESSION]
    )
    malformed_manifest_observed_details = _malformed_observed_pair_details(
        families[SourceDocType.COVERAGE_MANIFEST]
    )
    malformed_all_details = [
        *malformed_child_observed_details,
        *malformed_parent_observed_details,
        *malformed_manifest_observed_details,
    ]
    no_child_evidence = 1 if not children else 0
    direct_valid = classifications.count("direct_observed")
    missing_direct = sum(
        _classify_pair(document.get("observed_at_start"), document.get("observed_at_end")) == "absent"
        for document in children
    )
    direct_invalid = sum(
        _classify_pair(document.get("observed_at_start"), document.get("observed_at_end")) in {"invalid", "missing"}
        for document in children
    )
    direct_reversed = sum(
        _classify_pair(document.get("observed_at_start"), document.get("observed_at_end")) == "reversed"
        for document in children
    )
    parent_sessions = set(parent_by_session)
    child_sessions = set(children_by_session)
    manifest_sessions = set(manifests_by_session)
    manifest_only_session_count = sum(
        1 for session_id_hash in manifest_sessions if not children_by_session.get(session_id_hash)
    )
    orphan_child_session_count = len(child_sessions - parent_sessions)
    missing_manifest_session_count = len((parent_sessions | child_sessions) - manifest_sessions)
    duplicate_manifest_count = sum(
        max(0, len(documents) - 1) for documents in manifests_by_session.values()
    )
    duplicate_transcript_session_count = sum(
        max(0, len(documents) - 1) for documents in parent_by_session.values()
    )
    session_mismatch_count = len(manifest_sessions - parent_sessions)
    coverage_count_mismatch_count = 0
    coverage_count_invalid_count = 0
    for session_id_hash, manifests in manifests_by_session.items():
        observed_chunk_count = len(chunks_by_session.get(session_id_hash, []))
        observed_bundle_count = len(bundles_by_session.get(session_id_hash, []))
        for manifest in manifests:
            expected_chunk_count = _nonnegative_count(manifest.get("conversation_chunk_count"))
            expected_bundle_count = _nonnegative_count(manifest.get("tool_evidence_bundle_count"))
            if expected_chunk_count is None or expected_bundle_count is None:
                coverage_count_invalid_count += 1
            elif (
                expected_chunk_count != observed_chunk_count
                or expected_bundle_count != observed_bundle_count
            ):
                coverage_count_mismatch_count += 1
    manifest_integrity = _manifest_integrity_counts(
        manifests_by_session=manifests_by_session,
        chunks_by_session=chunks_by_session,
        bundles_by_session=bundles_by_session,
        parents_by_session=parent_by_session,
    )
    canonical_input_integrity = _canonical_input_integrity_counts(families=families)
    temporal_gap_count = (
        len(children)
        - direct_valid
        + no_child_evidence
        + len(malformed_parent_observed_details)
        + len(malformed_manifest_observed_details)
    )
    coverage_gap_count = (
        manifest_only_session_count
        + orphan_child_session_count
        + missing_manifest_session_count
        + duplicate_manifest_count
        + duplicate_transcript_session_count
        + session_mismatch_count
        + coverage_count_mismatch_count
        + coverage_count_invalid_count
        + missing_parent_session_id_count
        + missing_chunk_session_id_count
        + missing_bundle_session_id_count
        + missing_manifest_session_id_count
        + generation_blocked_count
        + sum(manifest_integrity.values())
        + sum(canonical_input_integrity.values())
    )
    changed = before_sequence != after_sequence
    gap_count = temporal_gap_count + coverage_gap_count + (1 if changed else 0)
    temporal_complete = (
        bool(children)
        and direct_valid == len(children)
        and not malformed_parent_observed_details
        and not malformed_manifest_observed_details
        and coverage_gap_count == 0
        and not changed
    )
    return {
        "family_document_counts": {family: len(documents) for family, documents in sorted(families.items())},
        "per_family_limit": limit,
        "global_document_limit": global_document_limit,
        "max_docs_examined_per_family": (limit + 1) * _EXECUTION_SCAN_MULTIPLIER,
        "execution_stats_summary": execution_stats,
        "direct_observed_at_valid_count": direct_valid,
        "coverage_manifest_temporal_evidence_count": 0,
        "parent_observed_fallback_count": classifications.count("parent_observed_fallback"),
        "parent_legacy_fallback_count": classifications.count("parent_legacy_fallback"),
        "no_temporal_evidence_count": classifications.count("no_evidence") + no_child_evidence,
        "malformed_temporal_evidence_count": len(malformed_all_details),
        "malformed_transcript_session_observed_at_count": len(malformed_parent_observed_details),
        "malformed_coverage_manifest_observed_at_count": len(malformed_manifest_observed_details),
        "missing_direct_observed_at_count": missing_direct,
        "invalid_direct_observed_at_count": direct_invalid,
        "reversed_direct_observed_at_count": direct_reversed,
        "malformed_missing_pair_count": malformed_all_details.count("missing"),
        "malformed_invalid_value_count": malformed_all_details.count("invalid"),
        "malformed_reversed_value_count": malformed_all_details.count("reversed"),
        "manifest_only_session_count": manifest_only_session_count,
        "orphan_child_session_count": orphan_child_session_count,
        "missing_manifest_session_count": missing_manifest_session_count,
        "duplicate_manifest_count": duplicate_manifest_count,
        "duplicate_transcript_session_count": duplicate_transcript_session_count,
        "session_mismatch_count": session_mismatch_count,
        "coverage_count_mismatch_count": coverage_count_mismatch_count,
        "coverage_count_invalid_count": coverage_count_invalid_count,
        **manifest_integrity,
        **canonical_input_integrity,
        "missing_parent_session_id_count": missing_parent_session_id_count,
        "missing_child_session_id_count": missing_child_session_id_count,
        "missing_chunk_session_id_count": missing_chunk_session_id_count,
        "missing_bundle_session_id_count": missing_bundle_session_id_count,
        "missing_manifest_session_id_count": missing_manifest_session_id_count,
        "gap_count": gap_count,
        "repair_required": temporal_gap_count + coverage_gap_count > 0,
        "source_changed_during_scan": changed,
        "source_update_seq_start_hash": _sha256(before_sequence),
        "source_update_seq_end_hash": _sha256(after_sequence),
        "temporal_complete": temporal_complete,
        "inventory_digest": _inventory_digest(project=project, families=families),
    }


def _base_report(*, project: str, limit: int, max_runtime_seconds: float) -> dict[str, object]:
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "authority": INVENTORY_AUTHORITY,
        "runtime_category": "read_only",
        "mutation_performed": False,
        "project_scope_hash": _sha256(project),
        "limit": limit,
        "per_family_limit": limit,
        "global_document_limit": limit * len(_FAMILY_TYPES),
        "max_runtime_seconds": max_runtime_seconds,
        "require_complete_scan": False,
        "status": "blocked",
        "scan_exhausted": False,
        "source_changed_during_scan": False,
        "repair_required": False,
        "gap_count": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge couchdb-temporal-evidence-inventory")
    parser.add_argument("--project", default="")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="positive per-family document ceiling; global ceiling is four times this value",
    )
    parser.add_argument("--max-runtime-seconds", type=float, default=0)
    parser.add_argument("--index-name", default=DEFAULT_INDEX_NAME)
    parser.add_argument("--index-design-document", default=DEFAULT_INDEX_DESIGN_DOCUMENT)
    parser.add_argument("--require-complete-scan", action="store_true")
    args = parser.parse_args(argv)
    report = _base_report(
        project=str(args.project or ""),
        limit=int(args.limit),
        max_runtime_seconds=float(args.max_runtime_seconds),
    )
    report["require_complete_scan"] = bool(args.require_complete_scan)

    try:
        _validate_bounds(project=str(args.project or ""), limit=int(args.limit), max_runtime_seconds=float(args.max_runtime_seconds))
        if not args.require_complete_scan:
            raise _InventoryBlocked("complete_scan_acknowledgement_required")
        couchdb_url = str(os.environ.get("COUCHDB_URL") or "").strip()
        if not couchdb_url:
            raise _InventoryBlocked("couchdb_environment_unavailable")
        from .couchdb_http_store import CouchDBHttpSourceStore

        user = str(os.environ.get("COUCHDB_USER") or "")
        password = str(os.environ.get("COUCHDB_PASSWORD") or "")
        source_store = CouchDBHttpSourceStore(
            base_url=couchdb_url,
            db=str(os.environ.get("COUCHDB_DB") or "transcript_source"),
            auth_header=_auth_header(user, password) if user else "",
            request_timeout_seconds=_per_request_timeout_seconds(
                float(args.max_runtime_seconds)
            ),
        )
        inventory = inventory_temporal_evidence(
            source_store=source_store,
            project=str(args.project),
            limit=int(args.limit),
            max_runtime_seconds=float(args.max_runtime_seconds),
            require_complete_scan=True,
            index_name=str(args.index_name),
            index_design_document=str(args.index_design_document),
        )
    except ValueError:
        report["error"] = "invalid_bound"
        report["gap_count"] = 1
        print(json.dumps(report, sort_keys=True))
        return 2
    except _InventoryBlocked as exc:
        report["error"] = exc.error
        report["gap_count"] = 1
        print(json.dumps(report, sort_keys=True))
        return 2 if exc.error in _CONFIGURATION_ERRORS else 1
    except Exception:
        report["error"] = "source_inventory_failed"
        report["gap_count"] = 1
        print(json.dumps(report, sort_keys=True))
        return 2

    report.update(inventory)
    # Scan completion says only that the bounded source read was stable.  A
    # complete scan may still expose temporal/source-integrity gaps.
    report["scan_exhausted"] = not bool(report["source_changed_during_scan"])
    if bool(report["temporal_complete"]):
        report["status"] = "complete"
        print(json.dumps(report, sort_keys=True))
        return 0
    report["error"] = "temporal_repair_required" if report["repair_required"] else "source_changed_during_scan"
    print(json.dumps(report, sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

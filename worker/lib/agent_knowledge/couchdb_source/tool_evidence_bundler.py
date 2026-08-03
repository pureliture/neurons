"""Bounded tool-evidence bundling into CouchDB source documents.

A session's tool evidence records are split into bounded bundles (smaller than a
session, larger than an item) using the existing
``chunk_tool_evidence_records`` budget, then written as ``tool_evidence_bundle``
source documents carrying their evidence index range, member content hashes, and
a coverage hash. Records are already public-ingress-redacted
(``redact_and_bound_evidence_text``), so the bundle body is public-safe.
"""

from __future__ import annotations

import json

from . import document_model as dm
from .document_model import (
    build_tool_evidence_bundle_document,
    normalize_observed_interval,
)
from .source_revision import (
    ResolvedSourceRevision,
    SourceRevisionResolutionError,
    activate_source_revision,
    resolve_active_source_revision,
)
from .session_memory_materializer import (
    mark_projection_pending_if_source_changed,
    update_coverage_with_tool_evidence,
)
from .source_store import CouchDBSourceStore, SourceStoreConflict, StoredRevision
from ..session_memory.transcript_model import (
    MAX_PACKED_TRANSCRIPT_BODY_CHARS,
    ToolEvidenceSummaryRecord,
)
from ..session_memory.transcript_packer import chunk_tool_evidence_records


def _bundle_body(records: list[ToolEvidenceSummaryRecord]) -> str:
    lines: list[str] = []
    for record in records:
        lines.append(f"### {record.evidence_index} {record.category}/{record.outcome}")
        lines.append(f"- tool: {record.tool_name}")
        lines.append(f"- command: {record.command_summary}")
        lines.append(f"- result: {record.redacted_summary}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_tool_evidence_bundle_documents(
    records: list[ToolEvidenceSummaryRecord],
    *,
    max_chars: int = MAX_PACKED_TRANSCRIPT_BODY_CHARS,
) -> list[dict]:
    """Build (but do not store) bounded tool_evidence_bundle documents.

    All records must belong to one session (the bundle is within a session).
    """

    ordered = list(records)
    if not ordered:
        return []
    session_ids = {record.session_id_hash for record in ordered}
    if len(session_ids) != 1:
        raise ValueError("tool evidence bundling is per-session; mixed session_id_hash given")

    sized_parts = chunk_tool_evidence_records(ordered, max_chars=max_chars)
    # A bundle is the smallest temporal relevance unit downstream.  Never put
    # records with different (or invalid/missing) event times behind one bounded
    # interval, otherwise one record's terms can borrow another record's date.
    parts: list[list[ToolEvidenceSummaryRecord]] = []
    for sized_part in sized_parts:
        current: list[ToolEvidenceSummaryRecord] = []
        current_interval: tuple[str, str] | None | object = object()
        for record in sized_part:
            interval = normalize_observed_interval(
                str(record.observed_at or ""),
                str(record.observed_at or ""),
            )
            if current and interval != current_interval:
                parts.append(current)
                current = []
            current.append(record)
            current_interval = interval
        if current:
            parts.append(current)
    part_count = len(parts)
    docs: list[dict] = []
    for part_index, part in enumerate(parts, start=1):
        interval = normalize_observed_interval(
            str(part[0].observed_at or ""),
            str(part[0].observed_at or ""),
        )
        observed_at_start, observed_at_end = interval or ("", "")
        docs.append(
            build_tool_evidence_bundle_document(
                session_id_hash=part[0].session_id_hash,
                provider=part[0].provider,
                project=part[0].project,
                part_index=part_index,
                part_count=part_count,
                evidence_index_start=min(record.evidence_index for record in part),
                evidence_index_end=max(record.evidence_index for record in part),
                record_content_hashes=[record.content_hash for record in part],
                body=_bundle_body(part),
                observed_at_start=observed_at_start,
                observed_at_end=observed_at_end,
            )
        )
    return docs


_BUNDLE_BOOKKEEPING_FIELDS = frozenset(
    {"_id", "_rev", "idempotency_key", "payload_hash"}
)


def _bundle_material_hash(document: dict) -> str:
    """Return the public-safe bundle identity without store bookkeeping."""

    material = {
        str(key): value
        for key, value in document.items()
        if key not in _BUNDLE_BOOKKEEPING_FIELDS
    }
    return dm.sha256_hash(
        json.dumps(
            material,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _revision_scoped_bundle_document(
    document: dict,
    *,
    active_source_hash: str,
) -> dict:
    """Assign an additive bundle id derived from prior and incoming source content."""

    session_id_hash = str(document.get("session_id_hash") or "")
    legacy_id = str(document.get("_id") or "")
    dm.assert_hash_like("session_id_hash", session_id_hash)
    dm.assert_hash_like("active_source_hash", active_source_hash)
    if (
        str(document.get("doc_type") or "") != dm.SourceDocType.TOOL_EVIDENCE_BUNDLE
        or not legacy_id
    ):
        raise ValueError("tool evidence bundle contract is invalid")

    revision_scope = dm.sha256_hash(
        json.dumps(
            {
                "schema_version": "active_tool_evidence_bundle.v1",
                "active_source_hash": active_source_hash,
                "bundle_material_hash": _bundle_material_hash(document),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    revision_scoped = dict(document)
    revision_scoped["_id"] = ":".join(
        (
            dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
            "revision",
            session_id_hash.removeprefix("sha256:"),
            revision_scope.removeprefix("sha256:"),
            dm.sha256_hash(legacy_id).removeprefix("sha256:"),
        )
    )
    return revision_scoped


def _active_provenance(
    *,
    resolved: ResolvedSourceRevision,
    store: CouchDBSourceStore,
) -> dict[str, str]:
    """Carry safe provenance forward while recording this immutable predecessor."""

    if not resolved.manifest_id:
        raise SourceRevisionResolutionError("active source revision manifest is missing")
    manifest = store.get(resolved.manifest_id)
    if manifest is None:
        raise SourceRevisionResolutionError("active source revision manifest is missing")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise SourceRevisionResolutionError("active source revision provenance is invalid")
    manifest_hash = str(manifest.get("manifest_hash") or "")
    try:
        dm.assert_hash_like("predecessor_manifest_hash", manifest_hash)
    except ValueError as exc:
        raise SourceRevisionResolutionError(
            "active source revision manifest is invalid"
        ) from exc
    return {
        **{str(key): str(value) for key, value in provenance.items()},
        "predecessor_manifest_hash": manifest_hash,
    }


def _active_bundle_revisions(
    documents: list[dict],
    *,
    resolved: ResolvedSourceRevision,
    store: CouchDBSourceStore,
) -> list[StoredRevision]:
    """Stage new bundles additively, then move the active allowlist once."""

    active_by_material = {
        _bundle_material_hash(bundle): bundle
        for bundle in resolved.tool_evidence_bundles
    }
    active_duplicates: dict[str, StoredRevision] = {}
    staged_by_material: dict[str, dict] = {}
    ordered_materials: list[str] = []
    for document in documents:
        material_hash = _bundle_material_hash(document)
        ordered_materials.append(material_hash)
        active_duplicate = active_by_material.get(material_hash)
        if active_duplicate is not None:
            active_duplicates[material_hash] = StoredRevision(
                doc_id=str(active_duplicate.get("_id") or ""),
                rev=str(active_duplicate.get("_rev") or ""),
                outcome="duplicate",
            )
            continue
        staged_by_material.setdefault(
            material_hash,
            _revision_scoped_bundle_document(
                document,
                active_source_hash=resolved.source_hash,
            ),
        )

    stored_by_material = {
        material_hash: store.put_if_absent(document)
        for material_hash, document in staged_by_material.items()
    }
    activated = resolved
    if staged_by_material:
        source_document_ids = tuple(
            sorted(
                {
                    str(member["_id"])
                    for member in (
                        *resolved.sessions,
                        *resolved.conversation_chunks,
                        *resolved.tool_evidence_bundles,
                        *staged_by_material.values(),
                    )
                }
            )
        )
        activated = activate_source_revision(
            store=store,
            session_id_hash=resolved.session_id_hash,
            source_document_ids=source_document_ids,
            provenance=_active_provenance(resolved=resolved, store=store),
            expected_predecessor=resolved,
        )

    # If a prior attempt moved the pointer but failed before these secondary
    # records converged, the retry finds only active duplicates. Reconcile the
    # current revision in that case too; matching-hash updates are idempotent.
    coverage = update_coverage_with_tool_evidence(
        session_id_hash=activated.session_id_hash,
        store=store,
    )
    if (
        coverage is None
        or str(coverage.get("source_hash") or "") != activated.source_hash
    ):
        raise SourceStoreConflict("active source revision coverage did not converge")
    if len(activated.sessions) != 1:
        raise SourceStoreConflict("active source revision session contract is invalid")
    session = activated.sessions[0]
    mark_projection_pending_if_source_changed(
        session_id_hash=activated.session_id_hash,
        provider=str(session.get("provider") or ""),
        project=str(session.get("project") or ""),
        source_hash=activated.source_hash,
        store=store,
        source_changed=True,
    )
    return [
        active_duplicates[material_hash]
        if material_hash in active_duplicates
        else stored_by_material[material_hash]
        for material_hash in ordered_materials
    ]


def store_tool_evidence_bundles(
    records: list[ToolEvidenceSummaryRecord],
    *,
    store: CouchDBSourceStore,
    max_chars: int = MAX_PACKED_TRANSCRIPT_BODY_CHARS,
) -> list[StoredRevision]:
    documents = build_tool_evidence_bundle_documents(records, max_chars=max_chars)
    if not documents:
        return []

    session_id_hash = str(documents[0]["session_id_hash"])
    pointer_id = dm.active_source_revision_pointer_doc_id(session_id_hash)
    if store.get(pointer_id) is None:
        # Compatibility path: no active pointer means the deterministic legacy
        # id continues to use the store's ordinary idempotent upsert behavior.
        # A competing activation can appear after these writes, however, so do
        # not return a bundle that the new immutable allowlist silently omits.
        legacy_revisions = [store.put(document) for document in documents]
        if store.get(pointer_id) is None:
            return legacy_revisions
        resolved = resolve_active_source_revision(
            session_id_hash=session_id_hash,
            store=store,
        )
        if resolved.is_legacy_unpinned:
            raise SourceStoreConflict(
                "active source revision changed during legacy tool evidence storage"
            )
        return _active_bundle_revisions(documents, resolved=resolved, store=store)

    # Resolve before staging any new bundle.  A malformed or incomplete pointer
    # must fail closed without widening to legacy discovery or writing evidence.
    resolved = resolve_active_source_revision(
        session_id_hash=session_id_hash,
        store=store,
    )
    return _active_bundle_revisions(documents, resolved=resolved, store=store)


__all__ = [
    "build_tool_evidence_bundle_documents",
    "store_tool_evidence_bundles",
]

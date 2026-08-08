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
    active_source_origin_document_ids,
    activate_source_revision,
    build_revision_scoped_source_documents,
    resolve_active_source_revision,
    source_document_set_revision,
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
    {
        "_id",
        "_rev",
        "idempotency_key",
        "payload_hash",
        "source_snapshot_schema_version",
        "source_snapshot_scope",
        "source_snapshot_origin_id",
        "current_source_scope",
        "supersedes_source_document_hash",
    }
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
    include_predecessor: bool = True,
) -> dict[str, str]:
    """Carry safe provenance, optionally binding a new predecessor."""

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
    carried_provenance = {
        **{str(key): str(value) for key, value in provenance.items()},
    }
    if include_predecessor:
        carried_provenance["predecessor_manifest_hash"] = manifest_hash
    return carried_provenance


def _bundle_origin_document_id(document: dict) -> str:
    """Return the mutable origin represented by an active bundle snapshot."""

    origin_document_id = str(
        document.get("source_snapshot_origin_id") or document.get("_id") or ""
    )
    if not origin_document_id:
        raise SourceStoreConflict("active tool evidence bundle origin is invalid")
    return origin_document_id


def _full_generation_source_document(document: dict) -> dict:
    """Use an active snapshot's proven content, never its mutable raw body."""

    source_document = {
        str(key): value
        for key, value in document.items()
        if key not in _BUNDLE_BOOKKEEPING_FIELDS
    }
    source_document["_id"] = _bundle_origin_document_id(document)
    return source_document


def _assert_complete_bundle_generation(documents: list[dict]) -> None:
    """Require an explicit full-session replacement to contain every bundle part."""

    try:
        part_counts = {int(document["part_count"]) for document in documents}
        part_indexes = {int(document["part_index"]) for document in documents}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("full tool evidence generation contract is invalid") from exc
    if len(part_counts) != 1:
        raise ValueError("full tool evidence generation contract is invalid")
    part_count = next(iter(part_counts))
    if (
        part_count <= 0
        or len(documents) != part_count
        or part_indexes != set(range(1, part_count + 1))
    ):
        raise ValueError("full tool evidence generation is incomplete")


def _refresh_active_bundle_currentness(
    *,
    activated: ResolvedSourceRevision,
    store: CouchDBSourceStore,
) -> None:
    """Converge secondary records to one already-published source revision."""

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


def _activate_full_bundle_generation(
    documents: list[dict],
    *,
    resolved: ResolvedSourceRevision,
    store: CouchDBSourceStore,
) -> tuple[ResolvedSourceRevision, dict[str, StoredRevision]]:
    """Pin caller-selected bundles with the resolved session/chunk snapshot."""

    source_documents = [
        *(_full_generation_source_document(document) for document in resolved.sessions),
        *(
            _full_generation_source_document(document)
            for document in resolved.conversation_chunks
        ),
        *(_full_generation_source_document(document) for document in documents),
    ]
    generation_snapshot_hash = source_document_set_revision(
        documents=source_documents,
        session_id_hash=resolved.session_id_hash,
    )
    snapshot_documents = build_revision_scoped_source_documents(
        documents=source_documents,
        source_snapshot_hash=generation_snapshot_hash,
    )
    revisions_by_origin: dict[str, StoredRevision] = {}
    for document in snapshot_documents:
        revision = store.put_if_absent(document)
        origin_document_id = _bundle_origin_document_id(document)
        if origin_document_id in revisions_by_origin:
            raise SourceStoreConflict("full tool evidence generation source is invalid")
        revisions_by_origin[origin_document_id] = revision
    activated = activate_source_revision(
        store=store,
        session_id_hash=resolved.session_id_hash,
        source_document_ids=tuple(
            str(document["_id"]) for document in snapshot_documents
        ),
        expected_predecessor=resolved,
    )
    return activated, revisions_by_origin


def _active_bundle_revisions(
    documents: list[dict],
    *,
    resolved: ResolvedSourceRevision,
    store: CouchDBSourceStore,
    replace_active_bundles: bool = False,
) -> list[StoredRevision]:
    """Stage bundles, then move one active allowlist to their intended generation."""

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

    if (
        replace_active_bundles
        and not staged_by_material
        and len(ordered_materials) == len(active_by_material)
        and set(ordered_materials) == set(active_by_material)
    ):
        # An exact full-generation retry is already represented by the active
        # immutable snapshots. Do not read mutable raw origins or reselect it,
        # but do retry secondary convergence after a prior post-CAS failure.
        _refresh_active_bundle_currentness(activated=resolved, store=store)
        return [active_duplicates[material_hash] for material_hash in ordered_materials]

    if replace_active_bundles:
        activated, revisions_by_origin = _activate_full_bundle_generation(
            documents,
            resolved=resolved,
            store=store,
        )
        _refresh_active_bundle_currentness(activated=activated, store=store)
        return [
            revisions_by_origin[str(document["_id"])]
            for document in documents
        ]

    if not staged_by_material:
        # A normal duplicate must not turn an unrelated mutable-origin change
        # into a new active snapshot. It can still repair post-CAS secondary
        # currentness against the immutable revision already selected.
        _refresh_active_bundle_currentness(activated=resolved, store=store)
        return [active_duplicates[material_hash] for material_hash in ordered_materials]

    stored_by_material = {
        material_hash: store.put_if_absent(document)
        for material_hash, document in staged_by_material.items()
    }
    active_source_document_ids = set(active_source_origin_document_ids(resolved))
    source_document_ids = tuple(
        sorted(
            active_source_document_ids
            | {str(document["_id"]) for document in staged_by_material.values()}
        )
    )
    activated = activate_source_revision(
        store=store,
        session_id_hash=resolved.session_id_hash,
        source_document_ids=source_document_ids,
        provenance=_active_provenance(
            resolved=resolved,
            store=store,
            include_predecessor=(
                set(source_document_ids) != active_source_document_ids
            ),
        ),
        expected_predecessor=resolved,
    )

    # Only a generation with newly staged evidence may advance the active
    # allowlist; converge secondary records after that CAS completes.
    _refresh_active_bundle_currentness(activated=activated, store=store)
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
    full_session_generation: bool = False,
    session_id_hash: str = "",
) -> list[StoredRevision]:
    """Store bounded evidence records for one session.

    ``full_session_generation`` is for extractors which re-read the complete
    session and therefore replace, rather than append to, the active evidence
    generation. It fails before writes unless all declared bundle parts are
    present. An empty full generation must pass ``session_id_hash`` so it can
    remove earlier active evidence without inferring an identity from content.
    """

    documents = build_tool_evidence_bundle_documents(records, max_chars=max_chars)
    document_session_id_hash = (
        str(documents[0].get("session_id_hash") or "") if documents else ""
    )
    if (
        session_id_hash
        and document_session_id_hash
        and session_id_hash != document_session_id_hash
    ):
        raise ValueError("tool evidence session_id_hash contract is invalid")
    session_id_hash = document_session_id_hash or session_id_hash
    if not documents and not full_session_generation:
        return []
    if not session_id_hash:
        raise ValueError("full tool evidence generation requires session_id_hash")
    dm.assert_hash_like("session_id_hash", session_id_hash)
    if full_session_generation and documents:
        _assert_complete_bundle_generation(documents)

    pointer_id = dm.active_source_revision_pointer_doc_id(session_id_hash)
    if store.get(pointer_id) is None:
        # Compatibility path: no active pointer means the deterministic legacy
        # id continues to use the store's ordinary idempotent upsert behavior.
        # A competing activation can appear after these writes, however, so do
        # not return a bundle that the new immutable allowlist silently omits.
        legacy_revisions = [store.put(document) for document in documents]
        if store.get(pointer_id) is None:
            if full_session_generation:
                resolved = resolve_active_source_revision(
                    session_id_hash=session_id_hash,
                    store=store,
                )
                if resolved.is_legacy_unpinned:
                    activated, revisions_by_origin = _activate_full_bundle_generation(
                        documents,
                        resolved=resolved,
                        store=store,
                    )
                    _refresh_active_bundle_currentness(activated=activated, store=store)
                    return [
                        revisions_by_origin[str(document["_id"])]
                        for document in documents
                    ]
                else:
                    return _active_bundle_revisions(
                        documents,
                        resolved=resolved,
                        store=store,
                        replace_active_bundles=True,
                    )
            return legacy_revisions
        resolved = resolve_active_source_revision(
            session_id_hash=session_id_hash,
            store=store,
        )
        if resolved.is_legacy_unpinned:
            raise SourceStoreConflict(
                "active source revision changed during legacy tool evidence storage"
            )
        return _active_bundle_revisions(
            documents,
            resolved=resolved,
            store=store,
            replace_active_bundles=full_session_generation,
        )

    # Resolve before staging any new bundle.  A malformed or incomplete pointer
    # must fail closed without widening to legacy discovery or writing evidence.
    resolved = resolve_active_source_revision(
        session_id_hash=session_id_hash,
        store=store,
    )
    return _active_bundle_revisions(
        documents,
        resolved=resolved,
        store=store,
        replace_active_bundles=full_session_generation,
    )


__all__ = [
    "build_tool_evidence_bundle_documents",
    "store_tool_evidence_bundles",
]

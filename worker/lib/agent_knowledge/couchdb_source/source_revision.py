"""Immutable, session-scoped source-revision activation and resolution.

Legacy CouchDB source documents retain their deterministic ids.  A revision adds
immutable member records and one immutable manifest, then moves a separate
active-pointer with a single compare-and-swap.  Once that pointer exists,
resolution never broadens back to a session-wide query: a missing or changed
control/source document is an explicit fail-closed error.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from . import document_model as dm
from .source_store import CouchDBSourceStore, SourceStoreConflict, SourceStoreError


_SOURCE_MEMBER_TYPES = frozenset(
    {
        dm.SourceDocType.TRANSCRIPT_SESSION,
        dm.SourceDocType.CONVERSATION_CHUNK,
        dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
    }
)
_PROVENANCE_HASH_KEYS = frozenset(
    {"source_snapshot_hash", "predecessor_manifest_hash"}
)
_PROVENANCE_VERSION_KEYS = frozenset({"parser_version", "chunker_version"})
_PROVENANCE_KEYS = _PROVENANCE_HASH_KEYS | _PROVENANCE_VERSION_KEYS
_SAFE_PROVENANCE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}")
_SOURCE_SNAPSHOT_SCHEMA_VERSION = "couchdb_source_revision_snapshot.v1"
_SOURCE_SNAPSHOT_ORIGIN_ID = "source_snapshot_origin_id"
_SOURCE_SNAPSHOT_SCOPE = "source_snapshot_scope"


class SourceRevisionResolutionError(SourceStoreError):
    """An active source revision cannot be proven complete and unchanged."""


@dataclass(frozen=True)
class ResolvedSourceRevision:
    """The only source set an integration caller may materialize or project."""

    session_id_hash: str
    sessions: tuple[dict, ...]
    conversation_chunks: tuple[dict, ...]
    tool_evidence_bundles: tuple[dict, ...]
    source_hash: str
    manifest_id: str | None
    is_legacy_unpinned: bool


def _canonical_hash(value: Mapping[str, object] | list[object]) -> str:
    return dm.sha256_hash(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )


def _source_document_hash(document: Mapping[str, object]) -> str:
    """Hash all public-safe source fields while excluding CouchDB bookkeeping."""

    payload = {
        str(key): value
        for key, value in document.items()
        if key not in {"_rev", "idempotency_key", "payload_hash"}
    }
    return _canonical_hash(payload)


def _material_identity(document: Mapping[str, object]) -> tuple[str, str]:
    for field in ("content_hash", "coverage_hash", "source_hash"):
        value = str(document.get(field) or "")
        if value:
            dm.assert_hash_like(field, value)
            return field, value
    return "document_hash", _source_document_hash(document)


def _revision_fingerprint(document: Mapping[str, object], *, material_hash_field: str) -> str:
    candidate = dict(document)
    if material_hash_field == "document_hash":
        candidate[material_hash_field] = _source_document_hash(document)
    return dm.build_source_revision_token(
        candidate,
        material_hash_field=material_hash_field,
    )


def _source_member_descriptor(document: Mapping[str, object], *, session_id_hash: str) -> dict:
    doc_id = str(document.get("_id") or "")
    doc_type = str(document.get("doc_type") or "")
    if not doc_id or doc_type not in _SOURCE_MEMBER_TYPES:
        raise SourceRevisionResolutionError("source revision member contract is invalid")
    if str(document.get("session_id_hash") or "") != session_id_hash:
        raise SourceRevisionResolutionError("source revision member session does not match")
    try:
        material_hash_field, material_hash = _material_identity(document)
        member_revision_hash = _revision_fingerprint(
            document, material_hash_field=material_hash_field
        )
        source_document_hash = _source_document_hash(document)
    except (TypeError, ValueError) as exc:
        raise SourceRevisionResolutionError(
            "source revision member material is invalid"
        ) from exc
    return {
        "source_document_id": doc_id,
        "source_doc_type": doc_type,
        "material_hash_field": material_hash_field,
        "material_hash": material_hash,
        "member_revision_hash": member_revision_hash,
        "source_document_hash": source_document_hash,
    }


def _source_revision(descriptors: Iterable[Mapping[str, object]]) -> str:
    return _canonical_hash(
        {
            "schema_version": "couchdb_session_source_revision.v1",
            "members": sorted(
                (dict(item) for item in descriptors),
                key=lambda item: str(item["source_document_id"]),
            ),
        }
    )


def _source_document_set_revision(
    documents: Iterable[Mapping[str, object]],
    *,
    session_id_hash: str,
) -> str:
    """Fingerprint exact member identities and content for an activation input."""

    try:
        return _source_revision(
            _source_member_descriptor(document, session_id_hash=session_id_hash)
            for document in documents
        )
    except (SourceRevisionResolutionError, TypeError, ValueError) as exc:
        raise SourceStoreConflict("source revision source contract is invalid") from exc


def source_document_set_revision(
    *,
    documents: Iterable[Mapping[str, object]],
    session_id_hash: str,
) -> str:
    """Return the deterministic origin-set revision used by generic snapshots."""

    return _source_document_set_revision(
        documents,
        session_id_hash=session_id_hash,
    )


def _source_snapshot_scope(
    *,
    documents: Iterable[Mapping[str, object]],
    source_snapshot_hash: str,
) -> str:
    """Bind immutable snapshot ids to one verified origin document set."""

    dm.assert_hash_like("source_snapshot_hash", source_snapshot_hash)
    return _canonical_hash(
        {
            "source_snapshot_hash": source_snapshot_hash,
            "documents": [
                {
                    str(key): value
                    for key, value in document.items()
                    if key not in {"_id", "_rev", "idempotency_key", "payload_hash"}
                }
                for document in documents
            ],
        }
    )


def _revision_scoped_source_document_id(
    *,
    document: Mapping[str, object],
    source_scope: str,
    scope_kind: str,
) -> str:
    doc_type = str(document.get("doc_type") or "")
    session_id_hash = str(document.get("session_id_hash") or "")
    origin_id = str(document.get("_id") or "")
    dm.assert_hash_like("session_id_hash", session_id_hash)
    dm.assert_hash_like("source_scope", source_scope)
    if doc_type not in _SOURCE_MEMBER_TYPES or not origin_id:
        raise ValueError("source revision snapshot document contract is invalid")
    return ":".join(
        (
            doc_type,
            scope_kind,
            session_id_hash.removeprefix("sha256:"),
            source_scope.removeprefix("sha256:"),
            dm.sha256_hash(origin_id).removeprefix("sha256:"),
        )
    )


def build_revision_scoped_source_documents(
    *,
    documents: Iterable[Mapping[str, object]],
    source_snapshot_hash: str,
    scope_kind: str = "snapshot",
) -> list[dict]:
    """Build immutable, revision-scoped copies of one source-document set.

    ``scope_kind='current'`` preserves the established corrective Codex import
    shape. The default marks ordinary activation copies with their mutable
    origin id so future successors can resnapshot origins rather than nesting
    active copy ids.
    """

    if scope_kind not in {"snapshot", "current"}:
        raise ValueError("source revision snapshot scope kind is invalid")
    origins = [copy.deepcopy(dict(document)) for document in documents]
    if scope_kind == "snapshot":
        # The generic helper is reused by ingress and repair paths that may
        # supply equivalent allowlists in different orders. Keep one source
        # set on one deterministic copy-id scope so an exact duplicate never
        # rotates the pointer merely because its caller ordered ids differently.
        origins.sort(key=lambda document: str(document.get("_id") or ""))
    source_scope = _source_snapshot_scope(
        documents=origins,
        source_snapshot_hash=source_snapshot_hash,
    )
    staged: list[dict] = []
    for origin in origins:
        origin_id = str(origin.get("_id") or "")
        staged_document = dict(origin)
        staged_document.pop("_rev", None)
        staged_document.pop("idempotency_key", None)
        staged_document.pop("payload_hash", None)
        staged_document["_id"] = _revision_scoped_source_document_id(
            document=origin,
            source_scope=source_scope,
            scope_kind=scope_kind,
        )
        if scope_kind == "current":
            staged_document["current_source_scope"] = source_scope
            staged_document["supersedes_source_document_hash"] = dm.sha256_hash(origin_id)
        else:
            staged_document.update(
                {
                    "source_snapshot_schema_version": _SOURCE_SNAPSHOT_SCHEMA_VERSION,
                    _SOURCE_SNAPSHOT_SCOPE: source_scope,
                    _SOURCE_SNAPSHOT_ORIGIN_ID: origin_id,
                }
            )
        staged.append(staged_document)
    return staged


def _is_revision_snapshot_copy(document: Mapping[str, object]) -> bool:
    return (
        str(document.get("source_snapshot_schema_version") or "")
        == _SOURCE_SNAPSHOT_SCHEMA_VERSION
    )


def _is_current_source_copy(document: Mapping[str, object]) -> bool:
    source_scope = str(document.get("current_source_scope") or "")
    superseded_id_hash = str(document.get("supersedes_source_document_hash") or "")
    try:
        dm.assert_hash_like("current_source_scope", source_scope)
        dm.assert_hash_like("supersedes_source_document_hash", superseded_id_hash)
    except ValueError:
        return False
    return True


def _is_immutable_source_copy(document: Mapping[str, object]) -> bool:
    return _is_revision_snapshot_copy(document) or _is_current_source_copy(document)


def _source_document_logical_identity(document: Mapping[str, object]) -> str:
    """Return the stable origin identity shared by raw and corrective copies.

    A corrective ``current`` copy has no raw origin id.  Its
    ``supersedes_source_document_hash`` is the hash of that id and must take
    precedence even when a later generic snapshot wraps the corrective copy.
    """

    current_scope = document.get("current_source_scope")
    superseded_id_hash = document.get("supersedes_source_document_hash")
    if current_scope is not None or superseded_id_hash is not None:
        try:
            dm.assert_hash_like("current_source_scope", str(current_scope or ""))
            dm.assert_hash_like(
                "supersedes_source_document_hash", str(superseded_id_hash or "")
            )
        except ValueError as exc:
            raise SourceRevisionResolutionError(
                "source revision current copy logical identity is invalid"
            ) from exc
        return str(superseded_id_hash)

    origin_id = str(document.get(_SOURCE_SNAPSHOT_ORIGIN_ID) or document.get("_id") or "")
    if not origin_id:
        raise SourceRevisionResolutionError("source revision logical identity is invalid")
    return dm.sha256_hash(origin_id)


def _snapshot_source_documents(
    *,
    documents: Iterable[Mapping[str, object]],
    session_id_hash: str,
) -> list[dict]:
    """Return one immutable source set without nested snapshot copies."""

    source_documents = [copy.deepcopy(dict(document)) for document in documents]
    generic_snapshots = [
        document for document in source_documents if _is_revision_snapshot_copy(document)
    ]
    if generic_snapshots:
        if len(generic_snapshots) != len(source_documents):
            raise SourceStoreConflict("source revision snapshot input mixes active copies and origins")
        for document in generic_snapshots:
            origin_id = str(document.get(_SOURCE_SNAPSHOT_ORIGIN_ID) or "")
            source_scope = str(document.get(_SOURCE_SNAPSHOT_SCOPE) or "")
            if not origin_id:
                raise SourceStoreConflict("source revision snapshot origin is invalid")
            try:
                dm.assert_hash_like("source_snapshot_scope", source_scope)
            except ValueError as exc:
                raise SourceStoreConflict("source revision snapshot origin is invalid") from exc
        return source_documents
    if source_documents and all(_is_current_source_copy(document) for document in source_documents):
        # Corrective Codex imports already write a revision-scoped immutable
        # source set. Retain those established ids exactly on replay.
        return source_documents
    return build_revision_scoped_source_documents(
        documents=source_documents,
        source_snapshot_hash=_source_document_set_revision(
            source_documents,
            session_id_hash=session_id_hash,
        ),
    )


def active_source_origin_document_ids(
    active_revision: ResolvedSourceRevision,
) -> tuple[str, ...]:
    """Recover current mutable origin ids for one active source revision.

    Generic activation snapshots keep this mapping explicitly. Established
    corrective ``current`` documents predate that field and intentionally use
    their own immutable ids as the next successor's origins.
    """

    if not isinstance(active_revision, ResolvedSourceRevision):
        raise SourceRevisionResolutionError("active source revision is invalid")
    origin_ids: list[str] = []
    for document in (
        *active_revision.sessions,
        *active_revision.conversation_chunks,
        *active_revision.tool_evidence_bundles,
    ):
        origin_id = (
            str(document.get(_SOURCE_SNAPSHOT_ORIGIN_ID) or "")
            if _is_revision_snapshot_copy(document)
            else str(document.get("_id") or "")
        )
        if not origin_id or origin_id in origin_ids:
            raise SourceRevisionResolutionError("active source revision origins are invalid")
        origin_ids.append(origin_id)
    return tuple(origin_ids)


def _member_hash(
    *,
    session_id_hash: str,
    source_revision: str,
    descriptor: Mapping[str, object],
    member_id: str,
) -> str:
    return _canonical_hash(
        {
            "session_id_hash": session_id_hash,
            "source_revision": source_revision,
            "member_id": member_id,
            **dict(descriptor),
        }
    )


def _manifest_hash(
    *,
    session_id_hash: str,
    source_revision: str,
    source_hash: str,
    members: Iterable[Mapping[str, object]],
    provenance: Mapping[str, str],
) -> str:
    return _canonical_hash(
        {
            "schema_version": "couchdb_session_source_revision_manifest.v1",
            "session_id_hash": session_id_hash,
            "source_revision": source_revision,
            "source_hash": source_hash,
            "provenance": dict(provenance),
            "members": sorted(
                (dict(item) for item in members), key=lambda item: str(item["member_id"])
            ),
        }
    )


def _source_hash(documents: Iterable[Mapping[str, object]]) -> str:
    items = list(documents)
    sessions = [item for item in items if item.get("doc_type") == dm.SourceDocType.TRANSCRIPT_SESSION]
    chunks = [item for item in items if item.get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK]
    bundles = [item for item in items if item.get("doc_type") == dm.SourceDocType.TOOL_EVIDENCE_BUNDLE]
    observed_at_start, observed_at_end = dm.observed_time_bounds(sessions=sessions, chunks=chunks)
    return dm.build_source_hash(
        (str(item.get("content_hash") or "") for item in chunks),
        (str(item.get("coverage_hash") or "") for item in bundles),
        observed_at_start=observed_at_start,
        observed_at_end=observed_at_end,
        conversation_revision_tokens=(
            _revision_fingerprint(item, material_hash_field=_material_identity(item)[0])
            for item in chunks
        ),
        tool_evidence_revision_tokens=(
            _revision_fingerprint(item, material_hash_field=_material_identity(item)[0])
            for item in bundles
        ),
    )


def _resolved_source_hash(documents: Iterable[Mapping[str, object]]) -> str:
    try:
        return _source_hash(documents)
    except (TypeError, ValueError) as exc:
        raise SourceRevisionResolutionError(
            "active source revision source is invalid"
        ) from exc


def _control_document_base(
    *,
    doc_id: str,
    doc_type: str,
    session_document: Mapping[str, object],
    session_id_hash: str,
) -> dict:
    return {
        "_id": doc_id,
        "doc_type": doc_type,
        "schema_version": dm.COUCHDB_SOURCE_SCHEMA_VERSION,
        "owner": dm.COUCHDB_SOURCE_OWNER,
        "provider": str(session_document.get("provider") or ""),
        "project": str(session_document.get("project") or ""),
        "session_id_hash": session_id_hash,
        "source_locator_hash": str(session_document.get("source_locator_hash") or ""),
        "redaction_version": str(session_document.get("redaction_version") or ""),
    }


def _validate_provenance(
    provenance: Mapping[str, str] | None,
    *,
    resolution: bool = False,
) -> dict[str, str]:
    """Return only bounded public-safe snapshot and artifact provenance."""

    try:
        if provenance is None:
            return {}
        if not isinstance(provenance, Mapping):
            raise ValueError("provenance must be a mapping")
        if any(not isinstance(key, str) or key not in _PROVENANCE_KEYS for key in provenance):
            raise ValueError("provenance contains an unsupported key")

        normalized: dict[str, str] = {}
        for key in sorted(provenance):
            value = provenance[key]
            if not isinstance(value, str):
                raise ValueError("provenance value must be a string")
            if key in _PROVENANCE_HASH_KEYS:
                dm.assert_hash_like(key, value)
            elif not _SAFE_PROVENANCE_VERSION.fullmatch(value):
                raise ValueError("provenance version is invalid")
            normalized[key] = value
        return normalized
    except (TypeError, ValueError) as exc:
        if resolution:
            raise SourceRevisionResolutionError(
                "active source revision provenance is invalid"
            ) from exc
        raise SourceStoreConflict("source revision provenance is invalid") from exc


def _load_legacy_documents(
    *,
    store: CouchDBSourceStore,
    session_id_hash: str,
) -> list[dict]:
    documents: list[dict] = []
    for doc_type in (
        dm.SourceDocType.TRANSCRIPT_SESSION,
        dm.SourceDocType.CONVERSATION_CHUNK,
        dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
    ):
        documents.extend(
            document
            for document in store.find_by_session(
                session_id_hash=session_id_hash,
                doc_type=doc_type,
            )
            if not _is_immutable_source_copy(document)
        )
    documents.sort(key=lambda document: str(document.get("_id") or ""))
    return documents


def _normalize_source_document_ids(source_document_ids: Iterable[str]) -> tuple[str, ...]:
    if isinstance(source_document_ids, (str, bytes)):
        raise SourceStoreConflict("source revision allowlist is invalid")
    try:
        document_ids = tuple(source_document_ids)
    except TypeError as exc:
        raise SourceStoreConflict("source revision allowlist is invalid") from exc
    if (
        not document_ids
        or any(not isinstance(document_id, str) or not document_id for document_id in document_ids)
        or len(document_ids) != len(set(document_ids))
    ):
        raise SourceStoreConflict("source revision allowlist is invalid")
    return document_ids


def _load_allowlisted_documents(
    *,
    store: CouchDBSourceStore,
    session_id_hash: str,
    source_document_ids: Iterable[str],
) -> list[dict]:
    documents: list[dict] = []
    for document_id in _normalize_source_document_ids(source_document_ids):
        document = store.get(document_id)
        if document is None:
            raise SourceStoreConflict("source revision allowlist member is missing")
        if (
            str(document.get("doc_type") or "") not in _SOURCE_MEMBER_TYPES
            or str(document.get("session_id_hash") or "") != session_id_hash
        ):
            raise SourceStoreConflict("source revision allowlist member is invalid")
        documents.append(document)
    documents.sort(key=lambda document: str(document.get("_id") or ""))
    return documents


def _resolved(
    *,
    session_id_hash: str,
    documents: Iterable[Mapping[str, object]],
    source_hash: str,
    manifest_id: str | None,
    is_legacy_unpinned: bool,
) -> ResolvedSourceRevision:
    grouped = {
        doc_type: tuple(
            copy.deepcopy(dict(document))
            for document in documents
            if document.get("doc_type") == doc_type
        )
        for doc_type in _SOURCE_MEMBER_TYPES
    }
    return ResolvedSourceRevision(
        session_id_hash=session_id_hash,
        sessions=grouped[dm.SourceDocType.TRANSCRIPT_SESSION],
        conversation_chunks=grouped[dm.SourceDocType.CONVERSATION_CHUNK],
        tool_evidence_bundles=grouped[dm.SourceDocType.TOOL_EVIDENCE_BUNDLE],
        source_hash=source_hash,
        manifest_id=manifest_id,
        is_legacy_unpinned=is_legacy_unpinned,
    )


def _require_hash(value: object) -> str:
    candidate = str(value or "")
    try:
        dm.assert_hash_like("source_revision_hash", candidate)
    except ValueError as exc:
        raise SourceRevisionResolutionError(
            "active source revision hash is invalid"
        ) from exc
    return candidate


def _parse_pointer(pointer: Mapping[str, object], *, session_id_hash: str) -> dict:
    if (
        str(pointer.get("doc_type") or "") != dm.SourceDocType.ACTIVE_SOURCE_REVISION
        or str(pointer.get("_id") or "") != dm.active_source_revision_pointer_doc_id(session_id_hash)
        or str(pointer.get("session_id_hash") or "") != session_id_hash
    ):
        raise SourceRevisionResolutionError("active source revision pointer is invalid")
    active_revision = _require_hash(pointer.get("active_revision"))
    manifest_id = str(pointer.get("manifest_id") or "")
    if manifest_id != dm.source_revision_manifest_doc_id(session_id_hash, active_revision):
        raise SourceRevisionResolutionError("active source revision pointer manifest is invalid")
    return {
        "active_revision": active_revision,
        "manifest_id": manifest_id,
        "manifest_hash": _require_hash(pointer.get("manifest_hash")),
        "source_hash": _require_hash(pointer.get("source_hash")),
    }


def _assert_expected_predecessor(
    expected_predecessor: ResolvedSourceRevision | None,
    *,
    current_pointer: Mapping[str, object] | None,
    session_id_hash: str,
) -> None:
    """Fence a caller's resolved predecessor before staging immutable records."""

    if expected_predecessor is None:
        return
    if not isinstance(expected_predecessor, ResolvedSourceRevision):
        raise SourceStoreConflict("expected predecessor is invalid")
    if expected_predecessor.session_id_hash != session_id_hash:
        raise SourceStoreConflict("expected predecessor session does not match")
    if expected_predecessor.is_legacy_unpinned:
        if expected_predecessor.manifest_id is not None or current_pointer is not None:
            raise SourceStoreConflict("expected predecessor does not match current pointer")
        return
    if expected_predecessor.manifest_id is None:
        raise SourceStoreConflict("expected predecessor is invalid")
    try:
        dm.assert_hash_like("expected_predecessor_source_hash", expected_predecessor.source_hash)
    except ValueError as exc:
        raise SourceStoreConflict("expected predecessor is invalid") from exc
    if current_pointer is None:
        raise SourceStoreConflict("expected predecessor does not match current pointer")
    if (
        str(current_pointer.get("manifest_id") or "") != expected_predecessor.manifest_id
        or str(current_pointer.get("source_hash") or "") != expected_predecessor.source_hash
    ):
        raise SourceStoreConflict("expected predecessor does not match current pointer")


def _assert_legacy_predecessor_unchanged(
    *,
    expected_predecessor: ResolvedSourceRevision,
    store: CouchDBSourceStore,
    session_id_hash: str,
) -> None:
    """Fence the exact legacy input again immediately before an initial CAS."""

    current_pointer = store.get(dm.active_source_revision_pointer_doc_id(session_id_hash))
    try:
        parsed_current_pointer = (
            _parse_pointer(current_pointer, session_id_hash=session_id_hash)
            if current_pointer is not None
            else None
        )
    except SourceRevisionResolutionError as exc:
        raise SourceStoreConflict("expected predecessor does not match current pointer") from exc
    _assert_expected_predecessor(
        expected_predecessor,
        current_pointer=parsed_current_pointer,
        session_id_hash=session_id_hash,
    )

    expected_documents = (
        *expected_predecessor.sessions,
        *expected_predecessor.conversation_chunks,
        *expected_predecessor.tool_evidence_bundles,
    )
    current_documents = _load_legacy_documents(
        store=store,
        session_id_hash=session_id_hash,
    )
    try:
        dm.assert_hash_like("expected_predecessor_source_hash", expected_predecessor.source_hash)
        expected_descriptor_revision = _source_document_set_revision(
            expected_documents,
            session_id_hash=session_id_hash,
        )
        current_descriptor_revision = _source_document_set_revision(
            current_documents,
            session_id_hash=session_id_hash,
        )
        current_source_hash = _resolved_source_hash(current_documents)
    except (SourceRevisionResolutionError, SourceStoreConflict, TypeError, ValueError) as exc:
        raise SourceStoreConflict("expected predecessor legacy source changed") from exc
    if (
        expected_predecessor.source_hash != current_source_hash
        or expected_descriptor_revision != current_descriptor_revision
    ):
        raise SourceStoreConflict("expected predecessor legacy source changed")


def _successor_provenance(
    *,
    store: CouchDBSourceStore,
    predecessor: ResolvedSourceRevision,
    provenance: Mapping[str, str],
) -> dict[str, str]:
    """Preserve caller provenance while explicitly binding a repaired successor."""

    if predecessor.manifest_id is None:
        raise SourceStoreConflict("active source revision manifest is missing")
    manifest = store.get(predecessor.manifest_id)
    if manifest is None:
        raise SourceStoreConflict("active source revision manifest is missing")
    manifest_hash = str(manifest.get("manifest_hash") or "")
    try:
        dm.assert_hash_like("predecessor_manifest_hash", manifest_hash)
    except ValueError as exc:
        raise SourceStoreConflict("active source revision manifest is invalid") from exc
    return {**dict(provenance), "predecessor_manifest_hash": manifest_hash}


def _parse_manifest(
    manifest: Mapping[str, object],
    *,
    session_id_hash: str,
    active_revision: str,
    expected_manifest_hash: str,
    expected_source_hash: str,
) -> list[dict]:
    if (
        str(manifest.get("doc_type") or "") != dm.SourceDocType.SOURCE_REVISION_MANIFEST
        or str(manifest.get("_id") or "")
        != dm.source_revision_manifest_doc_id(session_id_hash, active_revision)
        or str(manifest.get("session_id_hash") or "") != session_id_hash
        or str(manifest.get("source_revision") or "") != active_revision
        or str(manifest.get("source_hash") or "") != expected_source_hash
    ):
        raise SourceRevisionResolutionError("active source revision manifest is invalid")
    raw_members = manifest.get("members")
    if not isinstance(raw_members, list) or not raw_members:
        raise SourceRevisionResolutionError("active source revision membership is invalid")
    members = [dict(member) for member in raw_members if isinstance(member, Mapping)]
    if len(members) != len(raw_members):
        raise SourceRevisionResolutionError("active source revision membership is invalid")
    provenance = _validate_provenance(manifest.get("provenance", {}), resolution=True)
    if _manifest_hash(
        session_id_hash=session_id_hash,
        source_revision=active_revision,
        source_hash=expected_source_hash,
        members=members,
        provenance=provenance,
    ) != expected_manifest_hash or str(manifest.get("manifest_hash") or "") != expected_manifest_hash:
        raise SourceRevisionResolutionError("active source revision manifest changed")
    member_ids = [str(member.get("member_id") or "") for member in members]
    if not all(member_ids) or len(member_ids) != len(set(member_ids)):
        raise SourceRevisionResolutionError("active source revision membership is invalid")
    return sorted(members, key=lambda member: str(member["member_id"]))


def _validate_member_and_source(
    *,
    get_document: Callable[[str], dict | None],
    session_id_hash: str,
    source_revision: str,
    membership: Mapping[str, object],
) -> dict:
    source_document_id = str(membership.get("source_document_id") or "")
    member_id = str(membership.get("member_id") or "")
    source_doc_type = str(membership.get("source_doc_type") or "")
    material_hash_field = str(membership.get("material_hash_field") or "")
    if source_doc_type not in _SOURCE_MEMBER_TYPES or not source_document_id or not member_id:
        raise SourceRevisionResolutionError("active source revision member is invalid")
    expected_member_id = dm.source_revision_member_doc_id(
        session_id_hash,
        source_revision,
        source_document_id,
    )
    if member_id != expected_member_id:
        raise SourceRevisionResolutionError("active source revision member is invalid")
    member_hash = _require_hash(membership.get("member_hash"))
    member = get_document(member_id)
    if member is None:
        raise SourceRevisionResolutionError("active source revision member is missing")
    member_fields = {
        "member_id": member_id,
        "source_document_id": source_document_id,
        "source_doc_type": source_doc_type,
        "material_hash_field": material_hash_field,
        "material_hash": _require_hash(membership.get("material_hash")),
        "member_revision_hash": _require_hash(membership.get("member_revision_hash")),
        "source_document_hash": _require_hash(membership.get("source_document_hash")),
    }
    if (
        str(member.get("doc_type") or "") != dm.SourceDocType.SOURCE_REVISION_MEMBER
        or str(member.get("session_id_hash") or "") != session_id_hash
        or str(member.get("source_revision") or "") != source_revision
        or any(member.get(key) != value for key, value in member_fields.items())
        or str(member.get("member_hash") or "")
        != _member_hash(
            session_id_hash=session_id_hash,
            source_revision=source_revision,
            descriptor={key: value for key, value in member_fields.items() if key != "member_id"},
            member_id=member_id,
        )
        or str(member.get("member_hash") or "") != member_hash
    ):
        raise SourceRevisionResolutionError("active source revision member changed")
    source = get_document(source_document_id)
    if source is None:
        raise SourceRevisionResolutionError("active source revision source is missing")
    descriptor = _source_member_descriptor(source, session_id_hash=session_id_hash)
    if any(descriptor.get(key) != member_fields.get(key) for key in descriptor):
        raise SourceRevisionResolutionError("active source revision source changed")
    return source


def _snapshot_document_map(documents: Iterable[Mapping[str, object]]) -> dict[str, dict]:
    snapshot: dict[str, dict] = {}
    for document in documents:
        if not isinstance(document, Mapping):
            raise SourceRevisionResolutionError("active source revision snapshot is invalid")
        copied = copy.deepcopy(dict(document))
        document_id = str(copied.get("_id") or "")
        if not document_id:
            continue
        if document_id in snapshot:
            raise SourceRevisionResolutionError("active source revision snapshot is invalid")
        snapshot[document_id] = copied
    return snapshot


def _snapshot_legacy_documents(
    *,
    session_id_hash: str,
    documents: Iterable[Mapping[str, object]],
) -> list[dict]:
    legacy_documents = [
        copy.deepcopy(dict(document))
        for document in documents
        if str(document.get("session_id_hash") or "") == session_id_hash
        and str(document.get("doc_type") or "") in _SOURCE_MEMBER_TYPES
        and not _is_immutable_source_copy(document)
    ]
    legacy_documents.sort(key=lambda document: str(document.get("_id") or ""))
    return legacy_documents


def resolve_active_source_revision(
    *,
    session_id_hash: str,
    store: CouchDBSourceStore,
) -> ResolvedSourceRevision:
    """Resolve an active immutable revision, or the legacy set only if unpinned."""

    dm.assert_hash_like("session_id_hash", session_id_hash)
    pointer = store.get(dm.active_source_revision_pointer_doc_id(session_id_hash))
    if pointer is None:
        documents = _load_legacy_documents(store=store, session_id_hash=session_id_hash)
        return _resolved(
            session_id_hash=session_id_hash,
            documents=documents,
            source_hash=_resolved_source_hash(documents),
            manifest_id=None,
            is_legacy_unpinned=True,
        )

    parsed_pointer = _parse_pointer(pointer, session_id_hash=session_id_hash)
    manifest = store.get(parsed_pointer["manifest_id"])
    if manifest is None:
        raise SourceRevisionResolutionError("active source revision manifest is missing")
    members = _parse_manifest(
        manifest,
        session_id_hash=session_id_hash,
        active_revision=parsed_pointer["active_revision"],
        expected_manifest_hash=parsed_pointer["manifest_hash"],
        expected_source_hash=parsed_pointer["source_hash"],
    )
    documents = [
        _validate_member_and_source(
            get_document=store.get,
            session_id_hash=session_id_hash,
            source_revision=parsed_pointer["active_revision"],
            membership=member,
        )
        for member in members
    ]
    if _resolved_source_hash(documents) != parsed_pointer["source_hash"]:
        raise SourceRevisionResolutionError("active source revision hash changed")
    return _resolved(
        session_id_hash=session_id_hash,
        documents=documents,
        source_hash=parsed_pointer["source_hash"],
        manifest_id=parsed_pointer["manifest_id"],
        is_legacy_unpinned=False,
    )


def resolve_active_source_revision_from_snapshot(
    *,
    session_id_hash: str,
    documents: Iterable[Mapping[str, object]],
) -> ResolvedSourceRevision:
    """Strictly resolve one session from a single preloaded store snapshot.

    This is read-only and performs no store access.  It is intended for selection
    paths that already hold every document returned by ``find_by_session``; a
    present active control document is validated as strictly as the store-backed
    resolver and never broadens to legacy discovery on corruption.
    """

    dm.assert_hash_like("session_id_hash", session_id_hash)
    # Keep the caller-provided result set as the complete read snapshot.  Do
    # not fall back to a store lookup: a selection pass must not mix documents
    # from two reads, and must make malformed active control records visible.
    snapshot_documents: list[dict] = []
    for document in documents:
        if not isinstance(document, Mapping):
            raise SourceRevisionResolutionError("active source revision snapshot is invalid")
        snapshot_documents.append(copy.deepcopy(dict(document)))
    documents_by_id = _snapshot_document_map(snapshot_documents)
    pointer_id = dm.active_source_revision_pointer_doc_id(session_id_hash)
    pointer_candidates = [
        document
        for document in snapshot_documents
        if str(document.get("_id") or "") == pointer_id
        or str(document.get("doc_type") or "") == dm.SourceDocType.ACTIVE_SOURCE_REVISION
    ]
    if not pointer_candidates:
        legacy_documents = _snapshot_legacy_documents(
            session_id_hash=session_id_hash,
            documents=snapshot_documents,
        )
        return _resolved(
            session_id_hash=session_id_hash,
            documents=legacy_documents,
            source_hash=_resolved_source_hash(legacy_documents),
            manifest_id=None,
            is_legacy_unpinned=True,
        )
    if len(pointer_candidates) != 1:
        raise SourceRevisionResolutionError("active source revision pointer is invalid")

    pointer = pointer_candidates[0]
    parsed_pointer = _parse_pointer(pointer, session_id_hash=session_id_hash)
    manifest = documents_by_id.get(parsed_pointer["manifest_id"])
    if manifest is None:
        raise SourceRevisionResolutionError("active source revision manifest is missing")
    members = _parse_manifest(
        manifest,
        session_id_hash=session_id_hash,
        active_revision=parsed_pointer["active_revision"],
        expected_manifest_hash=parsed_pointer["manifest_hash"],
        expected_source_hash=parsed_pointer["source_hash"],
    )
    resolved_documents = [
        _validate_member_and_source(
            get_document=documents_by_id.get,
            session_id_hash=session_id_hash,
            source_revision=parsed_pointer["active_revision"],
            membership=member,
        )
        for member in members
    ]
    if _resolved_source_hash(resolved_documents) != parsed_pointer["source_hash"]:
        raise SourceRevisionResolutionError("active source revision hash changed")
    return _resolved(
        session_id_hash=session_id_hash,
        documents=resolved_documents,
        source_hash=parsed_pointer["source_hash"],
        manifest_id=parsed_pointer["manifest_id"],
        is_legacy_unpinned=False,
    )


def activate_source_revision(
    *,
    session_id_hash: str,
    store: CouchDBSourceStore,
    source_document_ids: Iterable[str] | None = None,
    provenance: Mapping[str, str] | None = None,
    expected_predecessor: ResolvedSourceRevision | None = None,
) -> ResolvedSourceRevision:
    """Create immutable revision records and CAS the active pointer once.

    The current member set is read twice.  Any changed, missing, or newly-added
    source member between immutable manifest construction and pointer movement
    aborts activation instead of publishing a partial revision. When a caller
    supplies a resolved predecessor, its active identity (or explicit legacy
    pointer absence) must still match before any immutable records are staged.
    """

    dm.assert_hash_like("session_id_hash", session_id_hash)
    normalized_provenance = _validate_provenance(provenance)
    pointer_id = dm.active_source_revision_pointer_doc_id(session_id_hash)
    previous_pointer = store.get(pointer_id)
    parsed_previous_pointer: dict | None = None
    expected_pointer_rev = ""
    if previous_pointer is not None:
        parsed_previous_pointer = _parse_pointer(
            previous_pointer,
            session_id_hash=session_id_hash,
        )
        expected_pointer_rev = str(previous_pointer.get("_rev") or "")
        if not expected_pointer_rev:
            raise SourceStoreConflict("active source revision pointer revision is missing")
    _assert_expected_predecessor(
        expected_predecessor,
        current_pointer=parsed_previous_pointer,
        session_id_hash=session_id_hash,
    )

    selected_source_document_ids = (
        _normalize_source_document_ids(source_document_ids)
        if source_document_ids is not None
        else None
    )
    source_documents = (
        _load_allowlisted_documents(
            store=store,
            session_id_hash=session_id_hash,
            source_document_ids=selected_source_document_ids,
        )
        if selected_source_document_ids is not None
        else _load_legacy_documents(store=store, session_id_hash=session_id_hash)
    )
    if not source_documents:
        raise SourceStoreConflict("source revision has no source documents")
    session_documents = [
        document
        for document in source_documents
        if document.get("doc_type") == dm.SourceDocType.TRANSCRIPT_SESSION
    ]
    if len(session_documents) != 1:
        raise SourceStoreConflict("source revision session contract is invalid")
    session_document = session_documents[0]
    provider = str(session_document.get("provider") or "")
    project = str(session_document.get("project") or "")
    if not provider or not project or any(
        str(document.get("provider") or "") != provider
        or str(document.get("project") or "") != project
        for document in source_documents
    ):
        raise SourceStoreConflict("source revision source contract is invalid")

    try:
        origin_source_revision = _source_document_set_revision(
            source_documents,
            session_id_hash=session_id_hash,
        )
        snapshot_documents = _snapshot_source_documents(
            documents=source_documents,
            session_id_hash=session_id_hash,
        )
        snapshot_source_document_ids = tuple(
            str(document.get("_id") or "") for document in snapshot_documents
        )
        if not all(snapshot_source_document_ids):
            raise SourceStoreConflict("source revision snapshot document is invalid")
        for document in snapshot_documents:
            store.put_if_absent(document)
        staged_source_documents = _load_allowlisted_documents(
            store=store,
            session_id_hash=session_id_hash,
            source_document_ids=snapshot_source_document_ids,
        )
        descriptors = [
            _source_member_descriptor(document, session_id_hash=session_id_hash)
            for document in staged_source_documents
        ]
        source_hash = _source_hash(staged_source_documents)
    except (SourceRevisionResolutionError, SourceStoreConflict, TypeError, ValueError) as exc:
        raise SourceStoreConflict("source revision source contract is invalid") from exc
    source_revision = _source_revision(descriptors)
    members: list[dict] = []
    for descriptor in descriptors:
        member_id = dm.source_revision_member_doc_id(
            session_id_hash,
            source_revision,
            str(descriptor["source_document_id"]),
        )
        member_hash = _member_hash(
            session_id_hash=session_id_hash,
            source_revision=source_revision,
            descriptor=descriptor,
            member_id=member_id,
        )
        membership = {"member_id": member_id, **descriptor, "member_hash": member_hash}
        member_document = _control_document_base(
            doc_id=member_id,
            doc_type=dm.SourceDocType.SOURCE_REVISION_MEMBER,
            session_document=session_document,
            session_id_hash=session_id_hash,
        )
        member_document.update({"source_revision": source_revision, **membership})
        store.put_if_absent(member_document)
        members.append(membership)

    manifest_id = dm.source_revision_manifest_doc_id(session_id_hash, source_revision)
    manifest_hash = _manifest_hash(
        session_id_hash=session_id_hash,
        source_revision=source_revision,
        source_hash=source_hash,
        members=members,
        provenance=normalized_provenance,
    )
    manifest = _control_document_base(
        doc_id=manifest_id,
        doc_type=dm.SourceDocType.SOURCE_REVISION_MANIFEST,
        session_document=session_document,
        session_id_hash=session_id_hash,
    )
    manifest.update(
        {
            "source_revision": source_revision,
            "source_hash": source_hash,
            "members": sorted(members, key=lambda member: str(member["member_id"])),
            "provenance": normalized_provenance,
            "manifest_hash": manifest_hash,
        }
    )
    store.put_if_absent(manifest)

    current_documents = _load_allowlisted_documents(
        store=store,
        session_id_hash=session_id_hash,
        source_document_ids=snapshot_source_document_ids,
    )
    try:
        current_descriptors = [
            _source_member_descriptor(document, session_id_hash=session_id_hash)
            for document in current_documents
        ]
    except (SourceRevisionResolutionError, TypeError, ValueError) as exc:
        raise SourceStoreConflict("source revision source changed during activation") from exc
    if sorted(current_descriptors, key=lambda item: str(item["source_document_id"])) != sorted(
        descriptors, key=lambda item: str(item["source_document_id"])
    ):
        raise SourceStoreConflict("source revision source changed during activation")

    pointer = _control_document_base(
        doc_id=pointer_id,
        doc_type=dm.SourceDocType.ACTIVE_SOURCE_REVISION,
        session_document=session_document,
        session_id_hash=session_id_hash,
    )
    pointer.update(
        {
            "active_revision": source_revision,
            "manifest_id": manifest_id,
            "manifest_hash": manifest_hash,
            "source_hash": source_hash,
        }
    )
    if expected_predecessor is not None and expected_predecessor.is_legacy_unpinned:
        _assert_legacy_predecessor_unchanged(
            expected_predecessor=expected_predecessor,
            store=store,
            session_id_hash=session_id_hash,
        )
    if previous_pointer is not None:
        # The pointer revision alone cannot attest the immutable member/source
        # records it references. Re-resolve the previous active revision at the
        # last possible point before its CAS. After this check, supported store
        # writes cannot change any membered source document; the store-level
        # immutable-member contract closes the remaining API-write race.
        try:
            resolve_active_source_revision(
                store=store,
                session_id_hash=session_id_hash,
            )
        except SourceRevisionResolutionError as exc:
            raise SourceStoreConflict(
                "active source revision integrity changed before pointer transition"
            ) from exc
    store.put_if_revision(pointer, expected_rev=expected_pointer_rev)
    activated = resolve_active_source_revision(store=store, session_id_hash=session_id_hash)

    # The pointer only references immutable copies, so a mutable origin write
    # cannot corrupt the active resolver. It can still make a just-published
    # revision stale. Initial legacy activation below has enough authority to
    # converge the entire discovered origin set; every other caller must see a
    # retryable fail-closed result rather than a false success.
    latest_input_documents = _load_allowlisted_documents(
        store=store,
        session_id_hash=session_id_hash,
        source_document_ids=tuple(
            str(document.get("_id") or "") for document in source_documents
        ),
    )
    origin_drifted = _source_document_set_revision(
        latest_input_documents,
        session_id_hash=session_id_hash,
    ) != origin_source_revision
    if origin_drifted and (previous_pointer is not None or selected_source_document_ids is not None):
        raise SourceStoreConflict("source revision origin changed during activation")

    # An initial unpinned activation necessarily discovers the legacy input
    # set. A writer can append a source document after the final pre-CAS reload
    # but before the pointer move. Recheck that same initial input once and,
    # when it grew, create one explicit allowlisted successor. This never
    # broadens an already-active resolver and stops with a conflict if a second
    # change prevents a stable result.
    if previous_pointer is not None or selected_source_document_ids is not None:
        return activated
    latest_legacy_documents = _load_legacy_documents(
        store=store,
        session_id_hash=session_id_hash,
    )
    if not origin_drifted and _source_document_set_revision(
        latest_legacy_documents,
        session_id_hash=session_id_hash,
    ) == origin_source_revision:
        return activated

    successor_source_document_ids = tuple(
        sorted(str(document.get("_id") or "") for document in latest_legacy_documents)
    )
    successor = activate_source_revision(
        store=store,
        session_id_hash=session_id_hash,
        source_document_ids=successor_source_document_ids,
        provenance=_successor_provenance(
            store=store,
            predecessor=activated,
            provenance=normalized_provenance,
        ),
        expected_predecessor=activated,
    )
    final_legacy_documents = _load_legacy_documents(
        store=store,
        session_id_hash=session_id_hash,
    )
    if _source_document_set_revision(
        final_legacy_documents,
        session_id_hash=session_id_hash,
    ) != _source_document_set_revision(
        latest_legacy_documents,
        session_id_hash=session_id_hash,
    ):
        raise SourceStoreConflict("initial legacy source set did not converge")
    return successor


__all__ = [
    "ResolvedSourceRevision",
    "SourceRevisionResolutionError",
    "active_source_origin_document_ids",
    "activate_source_revision",
    "build_revision_scoped_source_documents",
    "resolve_active_source_revision",
    "resolve_active_source_revision_from_snapshot",
    "source_document_set_revision",
]

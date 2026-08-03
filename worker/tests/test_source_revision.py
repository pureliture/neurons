from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionResolutionError,
    activate_source_revision,
    resolve_active_source_revision,
    resolve_active_source_revision_from_snapshot,
)
from agent_knowledge.couchdb_source.source_store import (
    InMemoryCouchDBSourceStore,
    SourceStoreConflict,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


def _session_id_hash() -> str:
    return dm.build_session_id_hash("codex", "revision-contract-session")


def _session_document() -> dict:
    return dm.build_transcript_session_document(
        session=TranscriptSession(
            session_id_hash=_session_id_hash(),
            provider="codex",
            project="neurons",
            started_at="2026-08-04T00:00:00Z",
            ended_at="2026-08-04T00:01:00Z",
        )
    )


def _conversation_chunk_document() -> dict:
    return dm.build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id="revision-contract-chunk",
            session_id_hash=_session_id_hash(),
            provider="codex",
            project="neurons",
            turn_start_index=0,
            turn_end_index=1,
            text="public-safe source summary",
        )
    )


def _tool_evidence_bundle_document() -> dict:
    return dm.build_tool_evidence_bundle_document(
        session_id_hash=_session_id_hash(),
        provider="codex",
        project="neurons",
        part_index=1,
        part_count=1,
        evidence_index_start=0,
        evidence_index_end=0,
        record_content_hashes=[dm.sha256_hash("evidence-record")],
        body="public-safe evidence summary",
    )


def _seed_session_source(store: InMemoryCouchDBSourceStore) -> tuple[dict, dict, dict]:
    documents = (
        _session_document(),
        _conversation_chunk_document(),
        _tool_evidence_bundle_document(),
    )
    for document in documents:
        store.put(document)
    return documents


def test_active_revision_resolves_only_pinned_members_and_never_falls_back() -> None:
    store = InMemoryCouchDBSourceStore()
    session, chunk, bundle = _seed_session_source(store)

    activated = activate_source_revision(store=store, session_id_hash=_session_id_hash())
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    assert activated.manifest_id is not None
    assert resolved.is_legacy_unpinned is False
    assert [document["_id"] for document in resolved.sessions] == [session["_id"]]
    assert [document["_id"] for document in resolved.conversation_chunks] == [chunk["_id"]]
    assert [document["_id"] for document in resolved.tool_evidence_bundles] == [bundle["_id"]]
    manifest = store.get(activated.manifest_id)
    assert manifest is not None
    for membership in manifest["members"]:
        assert set(membership) == {
            "member_id",
            "source_document_id",
            "source_doc_type",
            "material_hash_field",
            "material_hash",
            "member_revision_hash",
            "source_document_hash",
            "member_hash",
        }
        assert store.get(membership["member_id"]) is not None

    # Simulate a storage-layer bypass rather than the supported store write
    # contract. Revision members make ordinary source writes fail closed; the
    # resolver must still detect a corrupted backing record.
    changed_chunk = store._docs[chunk["_id"]]
    changed_chunk["body"] = "changed public-safe summary"
    changed_chunk["content_hash"] = dm.sha256_hash(changed_chunk["body"])

    with pytest.raises(SourceRevisionResolutionError):
        resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())


def test_pointer_absence_is_the_only_legacy_all_document_fallback() -> None:
    store = InMemoryCouchDBSourceStore()
    session, chunk, bundle = _seed_session_source(store)

    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    assert resolved.is_legacy_unpinned is True
    assert resolved.manifest_id is None
    assert [document["_id"] for document in resolved.sessions] == [session["_id"]]
    assert [document["_id"] for document in resolved.conversation_chunks] == [chunk["_id"]]
    assert [document["_id"] for document in resolved.tool_evidence_bundles] == [bundle["_id"]]


def test_matching_legacy_expected_predecessor_keeps_initial_activation_compatible() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session_source(store)
    legacy = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    activated = activate_source_revision(
        store=store,
        session_id_hash=_session_id_hash(),
        expected_predecessor=legacy,
    )

    assert activated.is_legacy_unpinned is False
    assert activated.manifest_id is not None


def test_legacy_expected_predecessor_rejects_a_pointer_created_after_its_read() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session_source(store)
    stale_legacy = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    activate_source_revision(store=store, session_id_hash=_session_id_hash())
    documents_before = store.all_docs()

    with pytest.raises(SourceStoreConflict, match="expected predecessor"):
        activate_source_revision(
            store=store,
            session_id_hash=_session_id_hash(),
            expected_predecessor=stale_legacy,
        )

    assert store.all_docs() == documents_before


def test_stale_expected_predecessor_cannot_drop_concurrent_active_members() -> None:
    store = InMemoryCouchDBSourceStore()
    session, chunk, bundle = _seed_session_source(store)
    activate_source_revision(store=store, session_id_hash=_session_id_hash())
    stale = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    concurrent_bundle = dm.build_tool_evidence_bundle_document(
        session_id_hash=_session_id_hash(),
        provider="codex",
        project="neurons",
        part_index=2,
        part_count=3,
        evidence_index_start=1,
        evidence_index_end=1,
        record_content_hashes=[dm.sha256_hash("concurrent successor evidence")],
        body="concurrent successor public-safe evidence",
    )
    store.put_if_absent(concurrent_bundle)
    current = activate_source_revision(
        store=store,
        session_id_hash=_session_id_hash(),
        source_document_ids=(
            session["_id"],
            chunk["_id"],
            bundle["_id"],
            concurrent_bundle["_id"],
        ),
        expected_predecessor=stale,
    )

    stale_writer_bundle = dm.build_tool_evidence_bundle_document(
        session_id_hash=_session_id_hash(),
        provider="codex",
        project="neurons",
        part_index=3,
        part_count=3,
        evidence_index_start=2,
        evidence_index_end=2,
        record_content_hashes=[dm.sha256_hash("stale writer evidence")],
        body="stale writer public-safe evidence",
    )
    store.put_if_absent(stale_writer_bundle)
    pointer_before = store.get(dm.active_source_revision_pointer_doc_id(_session_id_hash()))
    documents_before = store.all_docs()

    with pytest.raises(SourceStoreConflict, match="expected predecessor"):
        activate_source_revision(
            store=store,
            session_id_hash=_session_id_hash(),
            source_document_ids=(
                session["_id"],
                chunk["_id"],
                bundle["_id"],
                stale_writer_bundle["_id"],
            ),
            expected_predecessor=stale,
        )

    assert store.get(dm.active_source_revision_pointer_doc_id(_session_id_hash())) == pointer_before
    assert store.all_docs() == documents_before
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    assert resolved.source_hash == current.source_hash
    assert {document["_id"] for document in resolved.tool_evidence_bundles} == {
        bundle["_id"],
        concurrent_bundle["_id"],
    }


def test_snapshot_resolution_matches_store_resolution_for_active_revision() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session_source(store)
    activated = activate_source_revision(store=store, session_id_hash=_session_id_hash())

    resolved = resolve_active_source_revision_from_snapshot(
        session_id_hash=_session_id_hash(),
        documents=store.find_by_session(session_id_hash=_session_id_hash()),
    )

    assert resolved == activated


@pytest.mark.parametrize("corruption", ("pointer", "manifest", "member", "source"))
def test_snapshot_resolution_fails_closed_on_active_control_or_source_mutation(
    corruption: str,
) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session_source(store)
    activated = activate_source_revision(store=store, session_id_hash=_session_id_hash())
    snapshot = store.find_by_session(session_id_hash=_session_id_hash())
    assert activated.manifest_id is not None

    if corruption == "pointer":
        pointer = next(
            document
            for document in snapshot
            if document["_id"] == dm.active_source_revision_pointer_doc_id(_session_id_hash())
        )
        pointer["active_revision"] = dm.sha256_hash("changed-pointer")
    elif corruption == "manifest":
        manifest = next(document for document in snapshot if document["_id"] == activated.manifest_id)
        manifest["manifest_hash"] = dm.sha256_hash("changed-manifest")
    elif corruption == "member":
        member = next(
            document
            for document in snapshot
            if document["doc_type"] == dm.SourceDocType.SOURCE_REVISION_MEMBER
        )
        member["member_hash"] = dm.sha256_hash("changed-member")
    else:
        source = next(
            document
            for document in snapshot
            if document["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK
        )
        source["body"] = "changed public-safe source"
        source["content_hash"] = dm.sha256_hash(source["body"])

    with pytest.raises(SourceRevisionResolutionError):
        resolve_active_source_revision_from_snapshot(
            session_id_hash=_session_id_hash(),
            documents=snapshot,
        )


def test_malformed_active_pointer_raises_resolution_error() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session_source(store)
    store.put(
        {
            "_id": dm.active_source_revision_pointer_doc_id(_session_id_hash()),
            "doc_type": dm.SourceDocType.ACTIVE_SOURCE_REVISION,
            "session_id_hash": _session_id_hash(),
            "active_revision": "not-a-source-revision-hash",
            "manifest_id": "not-a-manifest-id",
            "manifest_hash": dm.sha256_hash("manifest"),
            "source_hash": dm.sha256_hash("source"),
        }
    )

    with pytest.raises(SourceRevisionResolutionError):
        resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())


class _ExactAllowlistStore(InMemoryCouchDBSourceStore):
    def find_by_session(self, *, session_id_hash: str, doc_type: str = "") -> list[dict]:
        raise AssertionError("explicit allowlist activation must not discover legacy documents")


def test_activation_allowlist_reads_exact_source_documents_without_legacy_discovery() -> None:
    store = _ExactAllowlistStore()
    session, chunk, _bundle = _seed_session_source(store)

    activated = activate_source_revision(
        store=store,
        session_id_hash=_session_id_hash(),
        source_document_ids=(session["_id"], chunk["_id"]),
    )

    assert [document["_id"] for document in activated.sessions] == [session["_id"]]
    assert [document["_id"] for document in activated.conversation_chunks] == [chunk["_id"]]
    assert activated.tool_evidence_bundles == ()


@pytest.mark.parametrize(
    ("source_document_ids", "prepare"),
    (
        ((), None),
        (("missing-source-document",), None),
        (("duplicate-source-document", "duplicate-source-document"), None),
        (("wrong-source-document",), "wrong_type"),
    ),
)
def test_activation_allowlist_rejects_empty_missing_duplicate_or_wrong_type(
    source_document_ids: tuple[str, ...],
    prepare: str | None,
) -> None:
    store = InMemoryCouchDBSourceStore()
    session, _chunk, _bundle = _seed_session_source(store)
    if prepare == "wrong_type":
        store.put(
            {
                "_id": "wrong-source-document",
                "doc_type": dm.SourceDocType.COVERAGE_MANIFEST,
                "session_id_hash": _session_id_hash(),
            }
        )
    elif source_document_ids == ("duplicate-source-document", "duplicate-source-document"):
        source_document_ids = (session["_id"], session["_id"])

    with pytest.raises(SourceStoreConflict):
        activate_source_revision(
            store=store,
            session_id_hash=_session_id_hash(),
            source_document_ids=source_document_ids,
        )


def test_activation_allowlist_rejects_source_document_from_another_session() -> None:
    store = InMemoryCouchDBSourceStore()
    session, chunk, _bundle = _seed_session_source(store)
    wrong_session_chunk = dict(chunk)
    wrong_session_chunk["session_id_hash"] = dm.build_session_id_hash(
        "codex", "another-session"
    )
    store._docs[chunk["_id"]] = wrong_session_chunk

    with pytest.raises(SourceStoreConflict):
        activate_source_revision(
            store=store,
            session_id_hash=_session_id_hash(),
            source_document_ids=(session["_id"], chunk["_id"]),
        )


def test_activation_binds_safe_provenance_into_immutable_manifest() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session_source(store)
    provenance = {
        "source_snapshot_hash": dm.sha256_hash("public-safe-source-snapshot"),
        "parser_version": "codex-parser.v1",
        "chunker_version": "chunker.v2",
        "predecessor_manifest_hash": dm.sha256_hash("previous-manifest"),
    }

    activated = activate_source_revision(
        store=store,
        session_id_hash=_session_id_hash(),
        provenance=provenance,
    )

    assert activated.manifest_id is not None
    manifest = store.get(activated.manifest_id)
    assert manifest is not None
    assert manifest["provenance"] == provenance
    changed = dict(manifest)
    changed["provenance"] = {"parser_version": "codex-parser.v2"}
    store._docs[activated.manifest_id] = changed

    with pytest.raises(SourceRevisionResolutionError):
        resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())


@pytest.mark.parametrize(
    "provenance",
    (
        {"unexpected_key": "value"},
        {"parser_version": ""},
        {"source_snapshot_hash": "not-a-hash"},
    ),
)
def test_activation_rejects_unsafe_or_unverifiable_provenance(
    provenance: dict[str, str],
) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session_source(store)

    with pytest.raises(SourceStoreConflict):
        activate_source_revision(
            store=store,
            session_id_hash=_session_id_hash(),
            provenance=provenance,
        )


@pytest.mark.parametrize("corruption", ("missing_manifest", "missing_member", "changed_manifest_hash"))
def test_existing_pointer_rejects_incomplete_or_changed_revision_without_fallback(
    corruption: str,
) -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session_source(store)
    activated = activate_source_revision(store=store, session_id_hash=_session_id_hash())
    assert activated.manifest_id is not None
    manifest = store.get(activated.manifest_id)
    assert manifest is not None

    if corruption == "missing_manifest":
        store.delete(activated.manifest_id)
    elif corruption == "missing_member":
        store.delete(manifest["members"][0]["member_id"])
    else:
        changed = dict(manifest)
        changed["manifest_hash"] = dm.sha256_hash("changed-manifest")
        store._docs[activated.manifest_id] = changed

    with pytest.raises(SourceRevisionResolutionError):
        resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())


def test_immutable_revision_records_reject_regular_replacement() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_session_source(store)
    activated = activate_source_revision(store=store, session_id_hash=_session_id_hash())
    assert activated.manifest_id is not None
    manifest = store.get(activated.manifest_id)
    assert manifest is not None

    changed = dict(manifest)
    changed["manifest_hash"] = dm.sha256_hash("changed-manifest")
    with pytest.raises(SourceStoreConflict):
        store.put(changed)


class _PointerRaceStore(InMemoryCouchDBSourceStore):
    def put_if_revision(self, document: dict, *, expected_rev: str):
        if document["doc_type"] == dm.SourceDocType.ACTIVE_SOURCE_REVISION:
            current = self.get(document["_id"])
            if current is not None:
                raced = dict(current)
                raced["active_revision"] = dm.sha256_hash("different-revision")
                super().put(raced)
        return super().put_if_revision(document, expected_rev=expected_rev)


class _SourceOverlapStore(InMemoryCouchDBSourceStore):
    def __init__(self, source_document_id: str) -> None:
        super().__init__()
        self._source_document_id = source_document_id
        self._changed = False

    def put_if_absent(self, document: dict):
        stored = super().put_if_absent(document)
        if (
            not self._changed
            and document["doc_type"] == dm.SourceDocType.SOURCE_REVISION_MANIFEST
        ):
            current = self.get(self._source_document_id)
            assert current is not None
            changed = dict(current)
            changed["body"] = "overlap changed public-safe source"
            changed["content_hash"] = dm.sha256_hash(changed["body"])
            super().put(changed)
            self._changed = True
        return stored


class _PinnedMemberMutationAtPointerCasStore(InMemoryCouchDBSourceStore):
    def __init__(self, source_document_id: str) -> None:
        super().__init__()
        self._source_document_id = source_document_id
        self.inject_at_pointer_cas = False

    def put_if_revision(self, document: dict, *, expected_rev: str):
        if (
            self.inject_at_pointer_cas
            and document["doc_type"] == dm.SourceDocType.ACTIVE_SOURCE_REVISION
        ):
            self.inject_at_pointer_cas = False
            current = self.get(self._source_document_id)
            assert current is not None
            changed = dict(current)
            changed["body"] = "raced changed public-safe source"
            changed["content_hash"] = dm.sha256_hash(changed["body"])
            # A supported source write cannot corrupt an already-membered
            # document. Propagating that failure also proves no pointer CAS
            # occurs after the failed concurrent mutation.
            super().put(changed)
        return super().put_if_revision(document, expected_rev=expected_rev)


class _PersistedMemberCorruptionBeforePointerCasStore(InMemoryCouchDBSourceStore):
    def __init__(self, source_document_id: str) -> None:
        super().__init__()
        self._source_document_id = source_document_id
        self._pointer_reads = 0
        self.inject_at_pre_cas_resolution = False

    def get(self, doc_id: str) -> dict | None:
        if (
            self.inject_at_pre_cas_resolution
            and doc_id == dm.active_source_revision_pointer_doc_id(_session_id_hash())
        ):
            self._pointer_reads += 1
            if self._pointer_reads == 2:
                # Model a direct persistent-store bypass which the normal
                # store contract cannot prevent. The activation pre-CAS
                # re-resolution must surface it and preserve the pointer.
                changed = self._docs[self._source_document_id]
                changed["body"] = "persisted changed public-safe source"
                changed["content_hash"] = dm.sha256_hash(changed["body"])
                self.inject_at_pre_cas_resolution = False
        return super().get(doc_id)


def test_activation_aborts_when_active_pointer_cas_is_stale() -> None:
    store = _PointerRaceStore()
    _seed_session_source(store)
    activate_source_revision(store=store, session_id_hash=_session_id_hash())

    with pytest.raises(SourceStoreConflict):
        activate_source_revision(store=store, session_id_hash=_session_id_hash())


def test_activation_aborts_when_source_members_change_before_pointer_transition() -> None:
    chunk = _conversation_chunk_document()
    store = _SourceOverlapStore(chunk["_id"])
    for document in (_session_document(), chunk, _tool_evidence_bundle_document()):
        store.put(document)

    with pytest.raises(SourceStoreConflict):
        activate_source_revision(store=store, session_id_hash=_session_id_hash())

    assert store.get(dm.active_source_revision_pointer_doc_id(_session_id_hash())) is None


def test_active_member_mutation_at_pointer_cas_keeps_previous_pointer_and_source_intact() -> None:
    session = _session_document()
    chunk = _conversation_chunk_document()
    bundle = _tool_evidence_bundle_document()
    store = _PinnedMemberMutationAtPointerCasStore(chunk["_id"])
    for document in (session, chunk, bundle):
        store.put(document)
    previous = activate_source_revision(store=store, session_id_hash=_session_id_hash())
    previous_pointer = store.get(dm.active_source_revision_pointer_doc_id(_session_id_hash()))
    previous_chunk = store.get(chunk["_id"])
    assert previous_pointer is not None
    assert previous_chunk is not None

    additional_bundle = dm.build_tool_evidence_bundle_document(
        session_id_hash=_session_id_hash(),
        provider="codex",
        project="neurons",
        part_index=2,
        part_count=2,
        evidence_index_start=1,
        evidence_index_end=1,
        record_content_hashes=[dm.sha256_hash("raced-evidence")],
        body="additional public-safe evidence",
    )
    store.put_if_absent(additional_bundle)
    store.inject_at_pointer_cas = True

    with pytest.raises(SourceStoreConflict, match="revision member"):
        activate_source_revision(
            store=store,
            session_id_hash=_session_id_hash(),
            source_document_ids=(
                session["_id"],
                chunk["_id"],
                bundle["_id"],
                additional_bundle["_id"],
            ),
        )

    assert store.get(dm.active_source_revision_pointer_doc_id(_session_id_hash())) == previous_pointer
    assert store.get(chunk["_id"]) == previous_chunk
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    assert resolved.source_hash == previous.source_hash
    assert [document["_id"] for document in resolved.tool_evidence_bundles] == [bundle["_id"]]


def test_pre_cas_reresolution_rejects_prior_active_source_corruption() -> None:
    session = _session_document()
    chunk = _conversation_chunk_document()
    bundle = _tool_evidence_bundle_document()
    store = _PersistedMemberCorruptionBeforePointerCasStore(chunk["_id"])
    for document in (session, chunk, bundle):
        store.put(document)
    activate_source_revision(store=store, session_id_hash=_session_id_hash())
    previous_pointer = store.get(dm.active_source_revision_pointer_doc_id(_session_id_hash()))
    assert previous_pointer is not None

    additional_bundle = dm.build_tool_evidence_bundle_document(
        session_id_hash=_session_id_hash(),
        provider="codex",
        project="neurons",
        part_index=2,
        part_count=2,
        evidence_index_start=1,
        evidence_index_end=1,
        record_content_hashes=[dm.sha256_hash("pre-cas-corruption-evidence")],
        body="additional public-safe evidence",
    )
    store.put_if_absent(additional_bundle)
    store._pointer_reads = 0
    store.inject_at_pre_cas_resolution = True

    with pytest.raises(SourceStoreConflict, match="integrity changed before pointer transition"):
        activate_source_revision(
            store=store,
            session_id_hash=_session_id_hash(),
            source_document_ids=(
                session["_id"],
                chunk["_id"],
                bundle["_id"],
                additional_bundle["_id"],
            ),
        )

    assert store.get(dm.active_source_revision_pointer_doc_id(_session_id_hash())) == previous_pointer
    with pytest.raises(SourceRevisionResolutionError):
        resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

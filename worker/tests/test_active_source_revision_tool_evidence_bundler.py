from __future__ import annotations

import copy

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionResolutionError,
    activate_source_revision,
    resolve_active_source_revision,
)
from agent_knowledge.couchdb_source.source_store import (
    InMemoryCouchDBSourceStore,
    SourceStoreConflict,
)
from agent_knowledge.couchdb_source.tool_evidence_bundler import (
    build_tool_evidence_bundle_documents,
    store_tool_evidence_bundles,
)
from agent_knowledge.session_memory.transcript_model import (
    ToolEvidenceSummaryRecord,
    TranscriptChunk,
    TranscriptSession,
)


PROJECT = "tool-evidence-revision"


def _session_id_hash() -> str:
    return dm.build_session_id_hash("codex", "tool-evidence-revision-session")


def _record(
    *,
    summary: str = "initial evidence",
    evidence_index: int = 0,
    observed_at: str = "2026-08-04T00:00:00Z",
) -> ToolEvidenceSummaryRecord:
    return ToolEvidenceSummaryRecord(
        session_id_hash=_session_id_hash(),
        provider="codex",
        project=PROJECT,
        category="test_result",
        outcome="pass",
        tool_name="bash",
        command_summary="uv run pytest -q",
        redacted_summary=summary,
        observed_at=observed_at,
        evidence_index=evidence_index,
    )


def _seed_unpinned_source(store: InMemoryCouchDBSourceStore) -> tuple[dict, dict]:
    session = dm.build_transcript_session_document(
        session=TranscriptSession(
            session_id_hash=_session_id_hash(),
            provider="codex",
            project=PROJECT,
            started_at="2026-08-04T00:00:00Z",
            ended_at="2026-08-04T00:01:00Z",
        )
    )
    chunk = dm.build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id="pinned-conversation",
            session_id_hash=_session_id_hash(),
            provider="codex",
            project=PROJECT,
            turn_start_index=0,
            turn_end_index=0,
            text="public-safe pinned conversation",
        )
    )
    store.put(session)
    store.put(chunk)
    return session, chunk


def _seed_active_source(store: InMemoryCouchDBSourceStore) -> tuple[dict, dict, dict]:
    session, chunk = _seed_unpinned_source(store)
    initial_revision = store_tool_evidence_bundles([_record()], store=store)[0]
    initial_bundle = store.get(initial_revision.doc_id)
    assert initial_bundle is not None
    activate_source_revision(store=store, session_id_hash=_session_id_hash())
    return session, chunk, initial_bundle


class _PointerActivationDuringLegacyBundleStore(InMemoryCouchDBSourceStore):
    """Make a competing pointer activation visible immediately after one legacy put."""

    def __init__(self) -> None:
        super().__init__()
        self._activation_source_document_ids: tuple[str, ...] = ()

    def activate_after_next_legacy_bundle(self, *, source_document_ids: tuple[str, ...]) -> None:
        self._activation_source_document_ids = source_document_ids

    def put(self, document: dict):
        stored = super().put(document)
        if (
            self._activation_source_document_ids
            and str(document.get("doc_type") or "") == dm.SourceDocType.TOOL_EVIDENCE_BUNDLE
        ):
            source_document_ids = self._activation_source_document_ids
            self._activation_source_document_ids = ()
            activate_source_revision(
                store=self,
                session_id_hash=_session_id_hash(),
                source_document_ids=source_document_ids,
            )
        return stored


class _LegacyBundleBeforeInitialPointerCasStore(InMemoryCouchDBSourceStore):
    """Write a legacy bundle after an initial activation's final reload."""

    def __init__(self) -> None:
        super().__init__()
        self.inject_at_initial_pointer_cas = False
        self.legacy_revisions = []

    def put_if_revision(self, document: dict, *, expected_rev: str):
        if (
            self.inject_at_initial_pointer_cas
            and str(document.get("doc_type") or "") == dm.SourceDocType.ACTIVE_SOURCE_REVISION
        ):
            self.inject_at_initial_pointer_cas = False
            self.legacy_revisions = store_tool_evidence_bundles(
                [_record(summary="bundle raced initial pointer activation")],
                store=self,
            )
        return super().put_if_revision(document, expected_rev=expected_rev)


class _InterleavedUnpinnedFullGenerationStore(InMemoryCouchDBSourceStore):
    """Overwrite one legacy part while a full replacement is still staging."""

    def __init__(self) -> None:
        super().__init__()
        self._competing_part: dict | None = None

    def interleave_next_bundle_write(self, document: dict) -> None:
        self._competing_part = dict(document)

    def put(self, document: dict):
        stored = super().put(document)
        if (
            self._competing_part is not None
            and str(document.get("doc_type") or "")
            == dm.SourceDocType.TOOL_EVIDENCE_BUNDLE
        ):
            competing_part = self._competing_part
            self._competing_part = None
            super().put(competing_part)
        return stored


class _FailOnceCoverageStore(InMemoryCouchDBSourceStore):
    """Inject one coverage write failure after a pointer transition."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_next_coverage_write = False

    def fail_next_coverage_write(self) -> None:
        self._fail_next_coverage_write = True

    def put(self, document: dict):
        if (
            self._fail_next_coverage_write
            and str(document.get("doc_type") or "") == dm.SourceDocType.COVERAGE_MANIFEST
        ):
            self._fail_next_coverage_write = False
            raise SourceStoreConflict("injected active coverage failure")
        return super().put(document)


def test_unpinned_tool_evidence_keeps_legacy_id_and_regular_upsert() -> None:
    store = InMemoryCouchDBSourceStore()

    initial = store_tool_evidence_bundles([_record()], store=store)[0]
    changed = store_tool_evidence_bundles(
        [_record(summary="changed unpinned evidence")], store=store
    )[0]

    legacy_id = dm.tool_evidence_bundle_doc_id(_session_id_hash(), 1)
    persisted = store.get(legacy_id)
    assert initial.doc_id == legacy_id
    assert changed.doc_id == legacy_id
    assert changed.outcome == "conflict_resolved"
    assert persisted is not None
    assert "changed unpinned evidence" in persisted["body"]


def test_pointer_appearing_after_legacy_bundle_write_converges_into_active_revision() -> None:
    store = _PointerActivationDuringLegacyBundleStore()
    session, chunk = _seed_unpinned_source(store)
    store.activate_after_next_legacy_bundle(
        source_document_ids=(session["_id"], chunk["_id"])
    )

    stored = store_tool_evidence_bundles([_record()], store=store)

    assert len(stored) == 1
    legacy_id = dm.tool_evidence_bundle_doc_id(_session_id_hash(), 1)
    assert stored[0].doc_id != legacy_id
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    coverage = store.get(dm.coverage_manifest_doc_id(_session_id_hash()))
    projection = store.get(dm.projection_state_doc_id(_session_id_hash()))
    assert {
        bundle["source_snapshot_origin_id"]
        for bundle in resolved.tool_evidence_bundles
    } == {stored[0].doc_id}
    assert coverage is not None
    assert coverage["source_hash"] == resolved.source_hash
    assert projection is not None
    assert projection["projection_status"] == dm.ProjectionStatus.PENDING
    assert projection["source_hash"] == resolved.source_hash


def test_initial_activation_converges_legacy_bundle_written_after_final_reload_before_cas() -> None:
    store = _LegacyBundleBeforeInitialPointerCasStore()
    _seed_unpinned_source(store)
    store.inject_at_initial_pointer_cas = True

    activated = activate_source_revision(store=store, session_id_hash=_session_id_hash())

    assert len(store.legacy_revisions) == 1
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    assert activated.source_hash == resolved.source_hash
    assert {
        bundle["source_snapshot_origin_id"]
        for bundle in resolved.tool_evidence_bundles
    } == {
        store.legacy_revisions[0].doc_id
    }


def test_active_pointer_adds_revision_scoped_bundle_and_activates_exact_allowlist() -> None:
    store = InMemoryCouchDBSourceStore()
    session, chunk, previous_bundle = _seed_active_source(store)
    previous_snapshot = copy.deepcopy(previous_bundle)

    legacy_only_chunk = dm.build_conversation_chunk_document(
        chunk=TranscriptChunk.from_text(
            chunk_id="legacy-only-conversation",
            session_id_hash=_session_id_hash(),
            provider="codex",
            project=PROJECT,
            turn_start_index=1,
            turn_end_index=1,
            text="must remain outside the active allowlist",
        )
    )
    legacy_only_bundle = dm.build_tool_evidence_bundle_document(
        session_id_hash=_session_id_hash(),
        provider="codex",
        project=PROJECT,
        part_index=2,
        part_count=2,
        evidence_index_start=1,
        evidence_index_end=1,
        record_content_hashes=[dm.sha256_hash("legacy-only-evidence")],
        body="must remain outside the active allowlist",
    )
    store.put(legacy_only_chunk)
    store.put(legacy_only_bundle)

    added = store_tool_evidence_bundles(
        [_record(summary="changed pinned evidence")], store=store
    )[0]
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    manifest = store.get(resolved.manifest_id or "")

    assert added.doc_id != previous_bundle["_id"]
    assert store.get(previous_bundle["_id"]) == previous_snapshot
    assert {
        document["source_snapshot_origin_id"]
        for document in resolved.tool_evidence_bundles
    } == {
        previous_bundle["_id"],
        added.doc_id,
    }
    assert manifest is not None
    assert {
        membership["source_document_id"] for membership in manifest["members"]
    } == {
        document["_id"]
        for document in (
            *resolved.sessions,
            *resolved.conversation_chunks,
            *resolved.tool_evidence_bundles,
        )
    }
    assert legacy_only_chunk["_id"] not in {
        membership["source_document_id"] for membership in manifest["members"]
    }
    assert legacy_only_bundle["_id"] not in {
        membership["source_document_id"] for membership in manifest["members"]
    }


def test_active_pointer_duplicate_bundle_does_not_churn_pointer() -> None:
    store = InMemoryCouchDBSourceStore()
    _session, _chunk, bundle = _seed_active_source(store)
    pointer_id = dm.active_source_revision_pointer_doc_id(_session_id_hash())
    pointer_before = store.get(pointer_id)

    duplicate = store_tool_evidence_bundles([_record()], store=store)[0]
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    assert duplicate.doc_id == resolved.tool_evidence_bundles[0]["_id"]
    assert resolved.tool_evidence_bundles[0]["source_snapshot_origin_id"] == bundle["_id"]
    assert duplicate.outcome == "duplicate"
    assert store.get(pointer_id) == pointer_before


def test_active_pointer_normal_duplicate_does_not_reselect_mutated_raw_origin() -> None:
    store = InMemoryCouchDBSourceStore()
    _session, raw_chunk, bundle = _seed_active_source(store)
    pointer_id = dm.active_source_revision_pointer_doc_id(_session_id_hash())
    pointer_before = store.get(pointer_id)
    store.put(
        dm.build_conversation_chunk_document(
            chunk=TranscriptChunk.from_text(
                chunk_id="pinned-conversation",
                session_id_hash=_session_id_hash(),
                provider="codex",
                project=PROJECT,
                turn_start_index=0,
                turn_end_index=0,
                text="mutable raw origin changed after activation",
            )
        )
    )

    duplicate = store_tool_evidence_bundles([_record()], store=store)[0]
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    assert raw_chunk["_id"] == resolved.conversation_chunks[0]["source_snapshot_origin_id"]
    assert duplicate.doc_id == resolved.tool_evidence_bundles[0]["_id"]
    assert resolved.tool_evidence_bundles[0]["source_snapshot_origin_id"] == bundle["_id"]
    assert resolved.conversation_chunks[0]["body"] == "public-safe pinned conversation"
    assert store.get(pointer_id) == pointer_before


def test_active_pointer_batches_new_bundles_into_one_allowlist_cas() -> None:
    store = InMemoryCouchDBSourceStore()
    _session, _chunk, previous_bundle = _seed_active_source(store)
    pointer_id = dm.active_source_revision_pointer_doc_id(_session_id_hash())
    pointer_before = store.get(pointer_id)
    assert pointer_before is not None

    added = store_tool_evidence_bundles(
        [
            _record(
                summary="first new pinned evidence",
                evidence_index=1,
                observed_at="2026-08-04T00:01:00Z",
            ),
            _record(
                summary="second new pinned evidence",
                evidence_index=2,
                observed_at="2026-08-04T00:02:00Z",
            ),
        ],
        store=store,
    )
    pointer_after = store.get(pointer_id)
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    assert pointer_after is not None
    assert int(str(pointer_after["_rev"]).split("-", 1)[0]) == int(
        str(pointer_before["_rev"]).split("-", 1)[0]
    ) + 1
    assert {revision.doc_id for revision in added}.isdisjoint({previous_bundle["_id"]})
    assert {
        document["source_snapshot_origin_id"]
        for document in resolved.tool_evidence_bundles
    } == {
        previous_bundle["_id"],
        *(revision.doc_id for revision in added),
    }


def test_full_tool_evidence_generation_replaces_prior_active_bundles() -> None:
    store = InMemoryCouchDBSourceStore()
    _session, _chunk, _previous_bundle = _seed_active_source(store)
    previous = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    pointer_id = dm.active_source_revision_pointer_doc_id(_session_id_hash())
    full_generation = [
        _record(
            summary="corrected full-session evidence first part",
            evidence_index=0,
            observed_at="2026-08-04T00:00:00Z",
        ),
        _record(
            summary="corrected full-session evidence second part",
            evidence_index=1,
            observed_at="2026-08-04T00:01:00Z",
        ),
    ]

    stored = store_tool_evidence_bundles(
        full_generation,
        store=store,
        full_session_generation=True,
    )
    pointer_after_replacement = store.get(pointer_id)
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    assert pointer_after_replacement is not None
    assert {
        document["_id"] for document in resolved.tool_evidence_bundles
    } == {revision.doc_id for revision in stored}
    assert {document["_id"] for document in resolved.tool_evidence_bundles}.isdisjoint(
        {document["_id"] for document in previous.tool_evidence_bundles}
    )

    retried = store_tool_evidence_bundles(
        full_generation,
        store=store,
        full_session_generation=True,
    )

    assert {revision.outcome for revision in retried} == {"duplicate"}
    assert store.get(pointer_id) == pointer_after_replacement


def test_exact_full_generation_retry_repairs_currentness_after_coverage_failure() -> None:
    store = _FailOnceCoverageStore()
    _session, _chunk, _bundle = _seed_active_source(store)
    previous = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    store.put(
        dm.build_projection_state_document(
            session_id_hash=_session_id_hash(),
            provider="codex",
            project=PROJECT,
            projection_status=dm.ProjectionStatus.PROJECTED,
            active_content_hash=dm.sha256_hash("old full-generation projection"),
            source_hash=previous.source_hash,
            projected_source_hash=previous.source_hash,
        )
    )
    generation = [
        _record(
            summary="full evidence requires retry convergence",
            observed_at="2026-08-04T00:00:00Z",
        )
    ]
    pointer_id = dm.active_source_revision_pointer_doc_id(_session_id_hash())
    store.fail_next_coverage_write()

    with pytest.raises(SourceStoreConflict, match="injected active coverage failure"):
        store_tool_evidence_bundles(
            generation,
            store=store,
            full_session_generation=True,
        )

    pointer_after_failure = store.get(pointer_id)
    assert pointer_after_failure is not None
    retried = store_tool_evidence_bundles(
        generation,
        store=store,
        full_session_generation=True,
    )

    current = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    coverage = store.get(dm.coverage_manifest_doc_id(_session_id_hash()))
    projection = store.get(dm.projection_state_doc_id(_session_id_hash()))
    assert {revision.outcome for revision in retried} == {"duplicate"}
    assert store.get(pointer_id) == pointer_after_failure
    assert current.source_hash != previous.source_hash
    assert coverage is not None
    assert coverage["source_hash"] == current.source_hash
    assert projection is not None
    assert projection["projection_status"] == dm.ProjectionStatus.PENDING
    assert projection["source_hash"] == current.source_hash


def test_unpinned_full_generation_excludes_removed_parts_before_initial_activation() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(store)
    store_tool_evidence_bundles(
        [
            _record(
                summary="first full generation first part",
                evidence_index=0,
                observed_at="2026-08-04T00:00:00Z",
            ),
            _record(
                summary="first full generation second part",
                evidence_index=1,
                observed_at="2026-08-04T00:01:00Z",
            ),
        ],
        store=store,
        full_session_generation=True,
    )
    replacement = store_tool_evidence_bundles(
        [
            _record(
                summary="replacement full generation only part",
                evidence_index=0,
                observed_at="2026-08-04T00:00:00Z",
            )
        ],
        store=store,
        full_session_generation=True,
    )

    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    assert resolved.is_legacy_unpinned is False
    assert {bundle["_id"] for bundle in resolved.tool_evidence_bundles} == {
        replacement[0].doc_id
    }


def test_first_unpinned_full_generation_returns_active_snapshot_revisions() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(store)

    stored = store_tool_evidence_bundles(
        [
            _record(summary="first active evidence part", evidence_index=0),
            _record(summary="second active evidence part", evidence_index=1),
        ],
        store=store,
        full_session_generation=True,
    )

    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    assert {revision.doc_id for revision in stored} == {
        bundle["_id"] for bundle in resolved.tool_evidence_bundles
    }


def test_unpinned_full_generation_never_publishes_interleaved_legacy_parts() -> None:
    store = _InterleavedUnpinnedFullGenerationStore()
    _seed_unpinned_source(store)
    full_generation = [
        _record(
            summary="selected full generation first part",
            evidence_index=0,
            observed_at="2026-08-04T00:00:00Z",
        ),
        _record(
            summary="selected full generation second part",
            evidence_index=1,
            observed_at="2026-08-04T00:01:00Z",
        ),
    ]
    competing_part = build_tool_evidence_bundle_documents(
        [
            _record(
                summary="competing generation first part",
                evidence_index=0,
                observed_at="2026-08-04T00:00:00Z",
            )
        ]
    )[0]
    store.interleave_next_bundle_write(competing_part)

    store_tool_evidence_bundles(
        full_generation,
        store=store,
        full_session_generation=True,
    )

    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    bodies = {str(bundle["body"]) for bundle in resolved.tool_evidence_bundles}

    assert len(bodies) == 2
    assert all("selected full generation" in body for body in bodies)
    assert all("competing generation" not in body for body in bodies)


def test_unpinned_full_generation_replay_does_not_rotate_pointer() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(store)
    full_generation = [
        _record(
            summary="replayed full generation first part",
            evidence_index=0,
            observed_at="2026-08-04T00:00:00Z",
        ),
        _record(
            summary="replayed full generation second part",
            evidence_index=1,
            observed_at="2026-08-04T00:01:00Z",
        ),
    ]

    store_tool_evidence_bundles(
        full_generation,
        store=store,
        full_session_generation=True,
    )
    pointer_id = dm.active_source_revision_pointer_doc_id(_session_id_hash())
    pointer_before = store.get(pointer_id)
    assert pointer_before is not None

    retried = store_tool_evidence_bundles(
        full_generation,
        store=store,
        full_session_generation=True,
    )

    assert {revision.outcome for revision in retried} == {"duplicate"}
    assert store.get(pointer_id) == pointer_before


def test_active_full_generation_replay_ignores_overwritten_raw_duplicate() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(store)
    full_generation = [
        _record(
            summary="active replay first part",
            evidence_index=0,
            observed_at="2026-08-04T00:00:00Z",
        ),
        _record(
            summary="active replay second part",
            evidence_index=1,
            observed_at="2026-08-04T00:01:00Z",
        ),
    ]
    store_tool_evidence_bundles(
        full_generation,
        store=store,
        full_session_generation=True,
    )
    pointer_id = dm.active_source_revision_pointer_doc_id(_session_id_hash())
    pointer_before = store.get(pointer_id)
    assert pointer_before is not None
    competing_part = build_tool_evidence_bundle_documents(
        [
            _record(
                summary="overwritten competing first part",
                evidence_index=0,
                observed_at="2026-08-04T00:00:00Z",
            ),
            _record(
                summary="overwritten competing second part",
                evidence_index=1,
                observed_at="2026-08-04T00:01:00Z",
            ),
        ]
    )[0]
    store.put(competing_part)

    store_tool_evidence_bundles(
        full_generation,
        store=store,
        full_session_generation=True,
    )

    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    assert store.get(pointer_id) == pointer_before
    assert all(
        "active replay" in str(bundle["body"])
        for bundle in resolved.tool_evidence_bundles
    )


def test_active_full_generation_returns_new_snapshot_for_prior_duplicate_when_another_part_changes() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(store)
    initial_generation = [
        _record(
            summary="preserved duplicate first part",
            evidence_index=0,
            observed_at="2026-08-04T00:00:00Z",
        ),
        _record(
            summary="replaced previous second part",
            evidence_index=1,
            observed_at="2026-08-04T00:01:00Z",
        ),
    ]
    store_tool_evidence_bundles(
        initial_generation,
        store=store,
        full_session_generation=True,
    )
    competing_part = build_tool_evidence_bundle_documents(
        [
            _record(
                summary="competing raw first part",
                evidence_index=0,
                observed_at="2026-08-04T00:00:00Z",
            ),
            _record(
                summary="competing raw second part",
                evidence_index=1,
                observed_at="2026-08-04T00:01:00Z",
            ),
        ]
    )[0]
    store.put(competing_part)

    stored = store_tool_evidence_bundles(
        [
            initial_generation[0],
            _record(
                summary="replacement second part",
                evidence_index=1,
                observed_at="2026-08-04T00:01:00Z",
            ),
        ],
        store=store,
        full_session_generation=True,
    )

    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    bodies = {str(bundle["body"]) for bundle in resolved.tool_evidence_bundles}
    assert {revision.doc_id for revision in stored} == {
        bundle["_id"] for bundle in resolved.tool_evidence_bundles
    }
    assert any("preserved duplicate first part" in body for body in bodies)
    assert any("replacement second part" in body for body in bodies)
    assert all("competing raw" not in body for body in bodies)


def test_empty_full_generation_removes_active_tool_evidence() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed_active_source(store)

    stored = store_tool_evidence_bundles(
        [],
        store=store,
        full_session_generation=True,
        session_id_hash=_session_id_hash(),
    )
    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())

    assert stored == []
    assert resolved.tool_evidence_bundles == ()


def test_active_pointer_change_refreshes_coverage_and_marks_projection_pending() -> None:
    store = InMemoryCouchDBSourceStore()
    _session, _chunk, _bundle = _seed_active_source(store)
    previous = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    store.put(
        dm.build_projection_state_document(
            session_id_hash=_session_id_hash(),
            provider="codex",
            project=PROJECT,
            projection_status=dm.ProjectionStatus.PROJECTED,
            active_content_hash=dm.sha256_hash("old projection"),
            source_hash=previous.source_hash,
            projected_source_hash=previous.source_hash,
        )
    )

    store_tool_evidence_bundles(
        [_record(summary="changed evidence requires reprojection")], store=store
    )

    current = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    coverage = store.get(dm.coverage_manifest_doc_id(_session_id_hash()))
    projection = store.get(dm.projection_state_doc_id(_session_id_hash()))
    assert coverage is not None
    assert coverage["source_hash"] == current.source_hash
    assert coverage["active_source_manifest_id"] == current.manifest_id
    assert projection is not None
    assert projection["projection_status"] == dm.ProjectionStatus.PENDING
    assert projection["source_hash"] == current.source_hash
    assert projection["projected_source_hash"] == previous.source_hash


def test_active_pointer_duplicate_bundle_retry_repairs_currentness_after_coverage_failure() -> None:
    store = _FailOnceCoverageStore()
    _session, _chunk, initial_bundle = _seed_active_source(store)
    previous = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    store.put(
        dm.build_projection_state_document(
            session_id_hash=_session_id_hash(),
            provider="codex",
            project=PROJECT,
            projection_status=dm.ProjectionStatus.PROJECTED,
            active_content_hash=dm.sha256_hash("old projection"),
            source_hash=previous.source_hash,
            projected_source_hash=previous.source_hash,
        )
    )
    records = [_record(summary="new evidence requires retry convergence")]
    store.fail_next_coverage_write()

    with pytest.raises(SourceStoreConflict):
        store_tool_evidence_bundles(records, store=store)

    stored = store_tool_evidence_bundles(records, store=store)

    resolved = resolve_active_source_revision(store=store, session_id_hash=_session_id_hash())
    coverage = store.get(dm.coverage_manifest_doc_id(_session_id_hash()))
    projection = store.get(dm.projection_state_doc_id(_session_id_hash()))
    assert stored[0].outcome == "duplicate"
    assert stored[0].doc_id in {
        bundle["_id"] for bundle in resolved.tool_evidence_bundles
    }
    assert len(resolved.tool_evidence_bundles) == 2
    assert initial_bundle["_id"] in {
        bundle["source_snapshot_origin_id"]
        for bundle in resolved.tool_evidence_bundles
    }
    assert coverage is not None
    assert coverage["source_hash"] == resolved.source_hash
    assert projection is not None
    assert projection["projection_status"] == dm.ProjectionStatus.PENDING
    assert projection["source_hash"] == resolved.source_hash


class _WriteTracingStore(InMemoryCouchDBSourceStore):
    def __init__(self) -> None:
        super().__init__()
        self.write_count = 0

    def put(self, document: dict):
        self.write_count += 1
        return super().put(document)

    def put_if_absent(self, document: dict):
        self.write_count += 1
        return super().put_if_absent(document)


def test_malformed_active_pointer_rejects_tool_evidence_before_any_write() -> None:
    store = _WriteTracingStore()
    _seed_active_source(store)
    pointer_id = dm.active_source_revision_pointer_doc_id(_session_id_hash())
    malformed_pointer = store.get(pointer_id)
    assert malformed_pointer is not None
    malformed_pointer["active_revision"] = "not-a-source-revision-hash"
    store.put(malformed_pointer)
    pointer_before = store.get(pointer_id)
    store.write_count = 0

    with pytest.raises(SourceRevisionResolutionError):
        store_tool_evidence_bundles([_record(summary="must not be stored")], store=store)

    assert store.write_count == 0
    assert store.get(pointer_id) == pointer_before

"""Regression tests for bodyless tool_evidence_bundle currentness and selection."""

from __future__ import annotations

import json
import pytest

from agent_knowledge.couchdb_source import couchdb_currentness_stream as cs
from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source import source_revision as sr
from agent_knowledge.couchdb_source.build_cli import _select_sessions_needing_projection
from agent_knowledge.couchdb_source.source_store import (
    InMemoryCouchDBSourceStore,
    SourceStoreResponseTooLarge,
)
from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.rag_ingress.pg_embedding_chunks import (
    INCONSISTENT_HERMES_PROJECT_SCOPE,
    PG_INPUT_POLICY_VERSION,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROVIDER = "codex"
PROJECT = "neurons"


def _seed_legacy_session(
    store: InMemoryCouchDBSourceStore,
    raw_id: str,
    *,
    with_bodyless_bundle: bool = True,
    bundle_project: str = PROJECT,
    extra_bundle_fields: dict | None = None,
) -> tuple[str, str]:
    sid = dm.build_session_id_hash(PROVIDER, raw_id)
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=sid,
                provider=PROVIDER,
                project=PROJECT,
                started_at="2026-11-01T00:00:00Z",
                ended_at="2026-11-01T00:01:00Z",
            )
        )
    )
    store.put(
        dm.build_conversation_chunk_document(
            chunk=TranscriptChunk.from_text(
                chunk_id=f"chunk-{raw_id}",
                session_id_hash=sid,
                provider=PROVIDER,
                project=PROJECT,
                turn_start_index=0,
                turn_end_index=0,
                text=f"synthetic body for {raw_id}",
            )
        )
    )
    bundle_id = dm.tool_evidence_bundle_doc_id(sid, 0)
    if with_bodyless_bundle:
        bundle_doc = {
            "_id": bundle_id,
            "doc_type": dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
            "session_id_hash": sid,
            "provider": PROVIDER,
            "project": bundle_project,
        }
        if extra_bundle_fields:
            bundle_doc.update(extra_bundle_fields)
        store._docs[bundle_id] = bundle_doc
    return sid, bundle_id


def _attach_local_projected_receipt(
    store: InMemoryCouchDBSourceStore,
    sid: str,
    source_hash: str,
) -> None:
    state = dm.build_projection_state_document(
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        projection_status=dm.ProjectionStatus.PROJECTED,
        active_content_hash=dm.sha256_hash(f"active-{sid}"),
        source_hash=source_hash,
        projected_source_hash=source_hash,
    )
    state["backend_receipts"] = {
        "postgres_pgvector_local": {
            LOCAL_EMBEDDING_PROFILE_ID: {
                "receipt_version": 3,
                "projection_status": dm.ProjectionStatus.PROJECTED,
                "projected_source_hash": source_hash,
                "active_content_hash": dm.sha256_hash(f"active-{sid}"),
                "session_memory_knowledge_id": f"smk-{sid[-8:]}",
                "embedding_profile": LOCAL_EMBEDDING_PROFILE_ID,
                "provider": PROVIDER,
                "project": PROJECT,
                "materialized_at": "2026-11-10T11:40:00Z",
            }
        }
    }
    store.put(state)


def _attach_local_blocked_attempt(
    store: InMemoryCouchDBSourceStore,
    sid: str,
    source_hash: str,
) -> None:
    state = dm.build_projection_state_document(
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        projection_status=dm.ProjectionStatus.PENDING,
    )
    state["projection_attempt"] = {
        "postgres_pgvector_local": {
            "status": dm.ProjectionStatus.FAILED,
            "reason": INCONSISTENT_HERMES_PROJECT_SCOPE,
            "source_hash": source_hash,
            "recorded_at": "2026-11-10T11:40:00Z",
            "policy_version": PG_INPUT_POLICY_VERSION,
        }
    }
    store.put(state)


def _streamed_descriptor(tmp_path, document: dict, name: str) -> dict:
    raw = json.dumps({"docs": [document], "bookmark": ""}, ensure_ascii=False).encode("utf-8")
    page = tmp_path / f"{name}.json"
    page.write_bytes(raw)
    rows, _ = cs.parse_spooled_descriptor_page(
        page, max_page_bytes=len(raw) + 1024, max_items=8
    )
    return rows[0]


def test_bodyless_bundle_legacy_hashes_match_per_session_and_snapshot():
    store = InMemoryCouchDBSourceStore()
    sid, _ = _seed_legacy_session(store, "bodyless-match")

    resolved = sr.resolve_active_source_revision(session_id_hash=sid, store=store)
    per_session = sr.resolve_source_hash_for_currentness(session_id_hash=sid, store=store)
    bulk = sr.iter_legacy_source_hashes(store)

    assert bulk[sid] == per_session
    assert bulk[sid] == resolved.source_hash


def test_bodyless_bundle_streaming_descriptor_matches_snapshot_hash(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sid, bundle_id = _seed_legacy_session(store, "bodyless-stream")
    expected = sr.resolve_active_source_revision(session_id_hash=sid, store=store).source_hash

    session_doc = store.get(dm.session_doc_id(sid))
    chunk_doc = store.find_by_session(
        session_id_hash=sid, doc_type=dm.SourceDocType.CONVERSATION_CHUNK
    )[0]
    bundle_doc = store.get(bundle_id)
    assert session_doc is not None and bundle_doc is not None

    streamed_bundle = _streamed_descriptor(tmp_path, bundle_doc, "bundle")
    assert "__body_hash" not in streamed_bundle
    assert "__source_document_hash" in streamed_bundle

    budget = {"rows": 3, "compact_bytes": 0}
    components = [
        sr._currentness_component(session_doc, budget=budget),
        sr._currentness_component(
            _streamed_descriptor(tmp_path, chunk_doc, "chunk"), budget=budget
        ),
        sr._currentness_component(streamed_bundle, budget=budget),
    ]
    assert sr._source_hash_from_currentness_components(components) == expected


def test_bodyless_bundle_provenance_change_updates_hash():
    store = InMemoryCouchDBSourceStore()
    sid, bundle_id = _seed_legacy_session(store, "bodyless-mutate")

    before_bulk = sr.iter_legacy_source_hashes(store)[sid]
    before_single = sr.resolve_source_hash_for_currentness(session_id_hash=sid, store=store)
    assert before_bulk == before_single

    store._docs[bundle_id]["project"] = "mutated-project"

    after_bulk = sr.iter_legacy_source_hashes(store)[sid]
    after_single = sr.resolve_source_hash_for_currentness(session_id_hash=sid, store=store)
    assert after_bulk == after_single
    assert after_bulk != before_bulk


def test_body_bearing_bundle_without_content_hash_fails_closed(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sid, bundle_id = _seed_legacy_session(
        store,
        "body-bearing-no-hash",
        extra_bundle_fields={"body": "nonempty unhashed body"},
    )
    bundle_doc = store.get(bundle_id)
    assert bundle_doc is not None

    assert sid not in sr.iter_legacy_source_hashes(store)
    with pytest.raises(sr.SourceRevisionResolutionError):
        sr.resolve_source_hash_for_currentness(session_id_hash=sid, store=store)
    with pytest.raises(sr.SourceRevisionResolutionError):
        sr._currentness_component(
            bundle_doc, budget={"rows": 1, "compact_bytes": 0}, full_document=True
        )
    with pytest.raises(sr.SourceRevisionResolutionError, match="body hash does not match"):
        sr._currentness_component(
            _streamed_descriptor(tmp_path, bundle_doc, "bad-bundle"),
            budget={"rows": 1, "compact_bytes": 0},
        )


@pytest.mark.parametrize("bad_body", [123, False, ["x"], {"k": "v"}])
def test_nonstring_body_bundle_without_content_hash_fails_closed(tmp_path, bad_body):
    store = InMemoryCouchDBSourceStore()
    sid, bundle_id = _seed_legacy_session(
        store,
        f"nonstring-body-{type(bad_body).__name__}",
        extra_bundle_fields={"body": bad_body},
    )
    bundle_doc = store.get(bundle_id)
    assert bundle_doc is not None

    assert sid not in sr.iter_legacy_source_hashes(store)
    with pytest.raises(sr.SourceRevisionResolutionError):
        sr.resolve_source_hash_for_currentness(session_id_hash=sid, store=store)
    with pytest.raises(sr.SourceRevisionResolutionError):
        sr._currentness_component(
            bundle_doc, budget={"rows": 1, "compact_bytes": 0}, full_document=True
        )
    with pytest.raises(sr.SourceRevisionResolutionError, match="body hash does not match"):
        sr._currentness_component(
            _streamed_descriptor(tmp_path, bundle_doc, f"nonstring-{type(bad_body).__name__}"),
            budget={"rows": 1, "compact_bytes": 0},
        )


def test_conversation_chunk_without_content_hash_still_fails_closed():
    store = InMemoryCouchDBSourceStore()
    sid, _ = _seed_legacy_session(store, "chunk-no-hash", with_bodyless_bundle=False)
    chunk_doc = store.find_by_session(
        session_id_hash=sid, doc_type=dm.SourceDocType.CONVERSATION_CHUNK
    )[0]
    chunk_doc.pop("content_hash", None)
    chunk_doc.pop("body", None)
    store._docs[chunk_doc["_id"]] = chunk_doc

    assert sid not in sr.iter_legacy_source_hashes(store)
    with pytest.raises(sr.SourceRevisionResolutionError, match="material hash"):
        sr.resolve_source_hash_for_currentness(session_id_hash=sid, store=store)


def test_projected_bundle_metadata_row_without_full_document_fails_closed():
    projected_row = {
        "_id": "tool_evidence_bundle:" + "a" * 64 + ":p000",
        "doc_type": dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
        "session_id_hash": "sha256:" + "a" * 64,
        "project": PROJECT,
        "provider": PROVIDER,
    }
    with pytest.raises(sr.SourceRevisionResolutionError, match="material hash"):
        sr._currentness_component(projected_row, budget={"rows": 1, "compact_bytes": 0})


def test_second_scan_missing_or_oversized_candidate_fails_closed(monkeypatch):
    store = InMemoryCouchDBSourceStore()
    sid, bundle_id = _seed_legacy_session(store, "second-scan-fail")

    orig_iter = store.iter_by_type

    def _is_verification_scan(kwargs: dict) -> bool:
        return "$or" in (kwargs.get("selector") or {})

    def _missing_second_scan(doc_type, **kwargs):
        if _is_verification_scan(kwargs):
            return iter([])
        return orig_iter(doc_type, **kwargs)

    monkeypatch.setattr(store, "iter_by_type", _missing_second_scan)
    assert sid not in sr.iter_legacy_source_hashes(store)

    def _oversized_second_scan(doc_type, **kwargs):
        if _is_verification_scan(kwargs):
            raise SourceStoreResponseTooLarge("too large")
        return orig_iter(doc_type, **kwargs)

    monkeypatch.setattr(store, "iter_by_type", _oversized_second_scan)
    assert sid not in sr.iter_legacy_source_hashes(store)
    assert bundle_id in store._docs


def test_bulk_selection_skips_projected_bodyless_sessions_and_selects_new():
    store = InMemoryCouchDBSourceStore()
    store.base_url = "http://couchdb.test"

    # Generate enough sessions to sort 5 projected ones strictly before 2 unprojected ones by _id.
    seeded: list[str] = []
    for idx in range(20):
        sid, _ = _seed_legacy_session(store, f"starve-{idx:02d}")
        seeded.append(sid)
    seeded.sort()
    projected_sids = seeded[:5]
    unprojected_sids = seeded[5:7]
    # Keep only the 5 projected + 2 unprojected sessions to match the exact scenario.
    keep_set = set(projected_sids) | set(unprojected_sids)
    for doc_id in list(store._docs):
        if store._docs[doc_id].get("session_id_hash") not in keep_set:
            store._docs.pop(doc_id)

    for sid in projected_sids:
        current = sr.resolve_active_source_revision(
            session_id_hash=sid, store=store
        ).source_hash
        _attach_local_projected_receipt(store, sid, current)

    selected = _select_sessions_needing_projection(
        store,
        limit=2,
        backend="postgres_pgvector",
        project="",
        provider="",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )

    assert [row["session_id_hash"] for row in selected] == unprojected_sids


def test_bulk_hash_missing_fallback_preserves_projected_blocked_and_propagates_error(
    monkeypatch,
):
    store = InMemoryCouchDBSourceStore()
    store.base_url = "http://couchdb.test"

    all_sids = []
    for idx in range(10):
        sid, _ = _seed_legacy_session(store, f"fallback-{idx:02d}")
        all_sids.append(sid)
    all_sids.sort()
    projected_sid, blocked_sid, target_sid = all_sids[0], all_sids[1], all_sids[2]
    keep = {projected_sid, blocked_sid, target_sid}
    for doc_id in list(store._docs):
        if store._docs[doc_id].get("session_id_hash") not in keep:
            store._docs.pop(doc_id)

    proj_hash = sr.resolve_source_hash_for_currentness(
        session_id_hash=projected_sid, store=store
    )
    _attach_local_projected_receipt(store, projected_sid, proj_hash)

    blk_hash = sr.resolve_source_hash_for_currentness(
        session_id_hash=blocked_sid, store=store
    )
    _attach_local_blocked_attempt(store, blocked_sid, blk_hash)

    monkeypatch.setattr(sr, "iter_legacy_source_hashes", lambda store: {})

    selected = _select_sessions_needing_projection(
        store,
        limit=1,
        backend="postgres_pgvector",
        project="",
        provider="",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )
    assert [row["session_id_hash"] for row in selected] == [target_sid]

    # invalid_active falls back to per-session resolver and preserves projected skip
    monkeypatch.setattr(sr, "iter_active_source_hashes", lambda store: ({}, {projected_sid}))

    def _boom(session_id_hash, store):
        if session_id_hash == blocked_sid:
            raise ValueError("fallback failed")
        return proj_hash

    monkeypatch.setattr(sr, "resolve_source_hash_for_currentness", _boom)
    with pytest.raises(ValueError, match="fallback failed"):
        _select_sessions_needing_projection(
            store,
            limit=10,
            backend="postgres_pgvector",
            project="",
            provider="",
            embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        )


def test_bodyless_bundle_with_null_content_hash_matches_per_session() -> None:
    store = InMemoryCouchDBSourceStore()
    sid, _ = _seed_legacy_session(
        store,
        "bodyless-null-ch",
        extra_bundle_fields={"content_hash": None},
    )

    bulk = sr.iter_legacy_source_hashes(store)
    assert sid in bulk
    assert bulk[sid] == sr.resolve_source_hash_for_currentness(
        session_id_hash=sid,
        store=store,
    )
    assert bulk[sid] == sr.resolve_active_source_revision(
        session_id_hash=sid,
        store=store,
    ).source_hash


def test_bulk_selection_isolates_per_session_resolution_error_and_continues(capsys) -> None:
    store = InMemoryCouchDBSourceStore()
    store.base_url = "http://couchdb.test"
    sids = [
        _seed_legacy_session(store, f"iso-{idx}")[0]
        for idx in range(10)
    ]
    sorted_sids = sorted(sids)
    bad_sid, ok_sid_1, ok_sid_2 = sorted_sids[0], sorted_sids[1], sorted_sids[2]
    bad_bundle_id = dm.tool_evidence_bundle_doc_id(bad_sid, 0)
    store._docs[bad_bundle_id]["body"] = "unhashed body"

    selected = _select_sessions_needing_projection(
        store,
        limit=2,
        backend="postgres_pgvector",
        project="",
        provider="",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )

    assert [row["session_id_hash"] for row in selected] == [ok_sid_1, ok_sid_2]
    err = capsys.readouterr().err
    assert "[selection] unresolved_skipped=1 last_reason=sourcerevisionresolutionerror" in err
    assert bad_sid not in err

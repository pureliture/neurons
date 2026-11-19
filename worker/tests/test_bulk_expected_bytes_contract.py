"""Bounded synthetic contract for independent full-input expected-byte accounting."""

from __future__ import annotations

import uuid

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_revision import SourceRevisionCurrentnessLimitError
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import bulk_stream_runner as runner_mod
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
)
from agent_knowledge.llm_brain_core.bulk_source_validation import plan_validated_source
from agent_knowledge.llm_brain_core.bulk_stream_runner import run_streamed_full_input
from agent_knowledge.llm_brain_core.bulk_streaming import planned_chunk_key
from agent_knowledge.llm_brain_core.runtime import session_source_revision_from_couchdb_source
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

P = "synthetic-provider"
PFX = uuid.uuid4().hex[:8] + "-"


class _Extractor:
    model = "free-model"
    provider = P
    endpoint_class = "approved_free"
    price_status = "free_confirmed"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def extract(self, batch):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic extraction failure")
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=PFX + "E", type="Concept", summary="s"),),
            )
            for item in batch
        ))


class _Writer:
    def __init__(self, *, omit=()) -> None:
        self.omit = set(omit)

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
        receipts = {
            item.session_key: "receipt:" + item.session_key
            for item in inputs if item.session_key not in self.omit
        }
        return BulkSemanticWriteReport(
            projected=len(inputs), entities_written=len(inputs), relations_written=0,
            chunk_receipts=receipts,
        )


class _State:
    def __init__(self, *, complete=()) -> None:
        self.complete = set(complete)

    def register_plan(self, **kwargs):
        return None
    def is_complete(self, key):
        return key in self.complete

    def is_complete_many(self, keys):
        return {key for key in keys if key in self.complete}

    def is_session_complete(self, *, chunk_keys, **kwargs):
        return bool(chunk_keys) and all(key in self.complete for key in chunk_keys)

    def claim_many(self, keys, owner):
        return {key: 1 for key in keys}

    def complete_many(self, entries, owner):
        for entry in entries:
            self.complete.add(entry["key"])

    def supersede_keys(self, keys):
        return None


def _seed(store, project, bodies, evidence=0):
    sid = dm.build_session_id_hash(P, project + "-session")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=project,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for i, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(chunk_id=f"chunk_{i}", session_id_hash=sid,
                                          provider=P, project=project, turn_start_index=i,
                                          turn_end_index=i, text=body)
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    ev_hashes = []
    for i in range(evidence):
        doc = dm.build_tool_evidence_bundle_document(
            session_id_hash=sid, provider=P, project=project, part_index=i,
            part_count=evidence, evidence_index_start=i, evidence_index_end=i,
            record_content_hashes=[], body=f"evidence-{i}")
        store.put(doc)
        ev_hashes.append(str(doc["coverage_hash"]))
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=project,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=evidence,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=ev_hashes,
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True}))
    return sid


def _plan_keys(store, sid):
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store)
    plan = plan_validated_source(store, session_id_hash=sid,
                                 expected_source_hash=source_hash)
    return [planned_chunk_key(plan, member, policy_version=PFX + "v1") for member in plan.members]


def _run(tmp_path, monkeypatch, store, project, sid, *, writer=None, extractor=None, state=None):
    monkeypatch.setattr(runner_mod, "_pg_authority_by_hash", lambda *a, **k: {})
    return run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db", source_store=store,
        extractor=extractor or _Extractor(), writer=writer or _Writer(),
        full_input_state=state or _State(), policy_version=PFX + "v1", project=project,
        limit=1, batch_size=16,
    )


@pytest.mark.parametrize("omit_index", [0, 1, 2, 3], ids=["front", "middle", "end", "evidence"])
def test_omitted_planned_member_is_detected_with_independent_expected_bytes(
    tmp_path, monkeypatch, omit_index,
):
    store = InMemoryCouchDBSourceStore()
    project = PFX + f"omit{omit_index}"
    sid = _seed(store, project, ["FRONT", "MIDDLE", "END"], evidence=1)
    keys = _plan_keys(store, sid)
    report = _run(tmp_path, monkeypatch, store, project, sid,
                  writer=_Writer(omit={keys[omit_index]}))

    assert report["expected_total_bytes"] > 0
    assert report["expected_full_bytes"] == (
        report["expected_total_bytes"] + report["expected_separator_bytes"]
    )
    assert len(report["expected_ordered_hashes"]) == 4
    assert report["writer_receipt_ordered_hashes"] != report["expected_ordered_hashes"]
    assert report["hash_order_match"] is False
    assert report["stream_coverage_complete"] is False
    assert report["failed_chunks"] == 1
    assert report["failed_sessions"] == 0


def test_source_independent_expected_bytes_do_not_shrink_after_partial_failure(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "partial"
    sid = _seed(store, project, ["FRONT", "MIDDLE", "END"], evidence=1)
    baseline = _run(tmp_path, monkeypatch, store, project, sid)
    failed = _run(tmp_path, monkeypatch, store, project, sid, extractor=_Extractor(fail=True))

    assert failed["expected_total_bytes"] == baseline["expected_total_bytes"]
    assert failed["expected_ordered_hashes"] == baseline["expected_ordered_hashes"]
    assert failed["failed_sessions"] == 1
    assert failed["stream_coverage_complete"] is False


def test_deferred_session_is_separate_from_failure(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "defer"
    sid = _seed(store, project, ["FRONT"])

    def _defer(**kwargs):
        raise SourceRevisionCurrentnessLimitError("synthetic cap")

    monkeypatch.setattr(runner_mod, "session_source_revision_from_couchdb_source", _defer)
    report = _run(tmp_path, monkeypatch, store, project, sid)

    assert report["deferred_sessions"] == 1
    assert report["failed_sessions"] == 0
    assert report["failed_chunks"] == 0
    assert report["status"] == "partial"


def test_resumed_chunks_count_separately_and_remain_coverage_complete(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "resume"
    sid = _seed(store, project, ["FRONT", "END"], evidence=1)
    keys = _plan_keys(store, sid)
    report = _run(tmp_path, monkeypatch, store, project, sid, state=_State(complete={keys[0]}))

    assert report["resumed_chunks"] == 1
    assert report["skipped_resumed"] == 0
    assert report["verified_ordered_hashes"] == report["expected_ordered_hashes"]
    assert report["stream_coverage_complete"] is True


def test_empty_source_has_zero_expected_bytes_and_is_complete(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "empty"
    sid = _seed(store, project, [], evidence=0)
    report = _run(tmp_path, monkeypatch, store, project, sid)

    assert report["expected_total_bytes"] == 0
    assert report["expected_ordered_hashes"] == []
    assert report["hash_order_match"] is True
    assert report["stream_coverage_complete"] is True


def test_zero_byte_members_are_excluded_from_hash_order_contract(tmp_path, monkeypatch):
    from agent_knowledge.llm_brain_core.bulk_streaming import expected_source_bytes

    store = InMemoryCouchDBSourceStore()
    project = PFX + "zero"
    sid = _seed(store, project, ["", "END"], evidence=0)
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store)
    plan = plan_validated_source(store, session_id_hash=sid, expected_source_hash=source_hash)
    expectation = expected_source_bytes(store, plan)
    report = _run(tmp_path, monkeypatch, store, project, sid)

    assert expectation["zero_byte_members"] == 1
    assert len(expectation["ordered"]) == 2
    assert len(expectation["ordered"]) - len(report["expected_ordered_hashes"]) == 1
    assert report["zero_byte_members"] == 1
    assert report["hash_order_match"] is True
    assert report["stream_coverage_complete"] is True


def test_canonical_hash_identity_collision_fails_closed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_knowledge.llm_brain_core import bulk_stream_runner as runner_mod

    def _member(doc_id, source_hash):
        return SimpleNamespace(
            doc_id=doc_id, lane="evidence", ordinal=0,
            order_key=("synthetic", doc_id), content_hash=source_hash,
            redaction_version="v1",
        )

    body = "SAME-BODY"
    source_hash_a = "sha256:" + "a" * 64
    source_hash_b = "sha256:" + "b" * 64
    docs = {
        "doc-a": {"body": body, "content_hash": source_hash_a, "redaction_version": "v1"},
        "doc-b": {"body": body, "content_hash": source_hash_b, "redaction_version": "v1"},
    }
    plan = SimpleNamespace(
        source_hash="sha256:source",
        members=(_member("doc-a", source_hash_a), _member("doc-b", source_hash_b)),
    )
    store = SimpleNamespace(get=lambda doc_id: docs[doc_id])
    original_select = runner_mod._select_sessions_bounded
    original_revision = runner_mod.session_source_revision_from_couchdb_source
    original_plan = runner_mod.plan_validated_source
    original_authority = runner_mod._pg_authority_by_hash
    runner_mod._select_sessions_bounded = lambda *a, **k: SimpleNamespace(sessions=[{
        "session_id_hash": "collision-session", "project": "collision-project",
    }])
    runner_mod.session_source_revision_from_couchdb_source = lambda **k: plan.source_hash
    runner_mod.plan_validated_source = lambda *a, **k: plan
    runner_mod._pg_authority_by_hash = lambda *a, **k: {}
    try:
        report = runner_mod.run_streamed_full_input(
            ledger_path=tmp_path / "ledger.db", source_store=store,
            extractor=_Extractor(), writer=_Writer(), full_input_state=_State(),
            policy_version="collision", project="collision-project", limit=1,
        )
    finally:
        runner_mod._select_sessions_bounded = original_select
        runner_mod.session_source_revision_from_couchdb_source = original_revision
        runner_mod.plan_validated_source = original_plan
        runner_mod._pg_authority_by_hash = original_authority
    assert report["status"] == "failed"
    assert report["failed_sessions"] == 1
    assert report["projected"] == 0
    assert report["failure_reasons"] == {"source_invalid": 1}


def test_genuine_canonical_hash_collision_via_metadata_headers_fails_closed(tmp_path, monkeypatch):
    import hashlib
    from agent_knowledge.llm_brain_core import bulk_stream_runner as runner_mod

    store = InMemoryCouchDBSourceStore()
    project = PFX + "hdrcollision"
    sid = dm.build_session_id_hash(P, project + "-session")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=project,
                                  started_at="2026-01-01T00:00:00Z")))
    raw0 = "session_id_hash: s\nturn_start_index: 0\n\nIDENTICAL-BODY"
    raw1 = "session_id_hash: s\nturn_start_index: 1\n\nIDENTICAL-BODY"
    docs = []
    for idx, raw in enumerate((raw0, raw1)):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{idx}", session_id_hash=sid, provider=P, project=project,
            turn_start_index=idx, turn_end_index=idx, text=raw,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        doc["body"] = raw
        doc["content_hash"] = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        store.put(doc)
        docs.append(doc)
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=project,
        conversation_chunk_count=2, tool_evidence_bundle_count=0,
        conversation_content_hashes=[str(d["content_hash"]) for d in docs],
        tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True}))

    report = _run(tmp_path, monkeypatch, store, project, sid)
    assert report["status"] == "failed"
    assert report["failed_sessions"] == 1
    assert report["projected"] == 0
    assert report["failure_reasons"] == {"source_invalid": 1}


def test_batch_exceeding_request_limit_splits_and_succeeds(tmp_path, monkeypatch):
    import json
    from agent_knowledge.llm_brain_core.bulk_semantic import OpenAICompatibleBulkSemanticExtractor

    store = InMemoryCouchDBSourceStore()
    project = PFX + "splitbatch"
    _seed(store, project, ["FRONT-" + "A" * 600, "END-" + "B" * 600], evidence=0)
    captured_batches: list[list[str]] = []

    def _post(url, *, headers, body, timeout):
        parsed = json.loads(body)
        sessions = json.loads(parsed["messages"][1]["content"])["sessions"]
        captured_batches.append([item["text"] for item in sessions])
        return json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "sessions": [{
                            "session_key": item["session_key"],
                            "entities": [{"name": PFX + "Entity", "type": "Concept", "summary": "ok"}],
                            "relations": [],
                        } for item in sessions]
                    })
                }
            }]
        })

    # Measure single-session request size (~2,410 B) so 1 chunk fits, 2 chunks (~3,195 B) exceed.
    extractor = OpenAICompatibleBulkSemanticExtractor(
        base_url="http://127.0.0.1:9/v1",
        model="synthetic-model",
        post_fn=_post,
        max_streamed_request_bytes=2800,
    )
    monkeypatch.setattr(runner_mod, "_pg_authority_by_hash", lambda *a, **k: {})
    report = run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db",
        source_store=store,
        extractor=extractor,
        writer=_Writer(),
        full_input_state=_State(),
        policy_version=PFX + "v1",
        project=project,
        limit=1,
        batch_size=4,
    )
    assert report["status"] == "ok"
    assert report["stream_coverage_complete"] is True
    assert report["hash_order_match"] is True
    assert len(captured_batches) == 2
    assert all(len(batch) == 1 for batch in captured_batches)


def test_oversized_single_chunk_defers_fail_closed_then_splits_utf8_and_resumes(tmp_path, monkeypatch):
    import json
    from agent_knowledge.llm_brain_core.bulk_semantic import OpenAICompatibleBulkSemanticExtractor

    store = InMemoryCouchDBSourceStore()
    project = PFX + "oversize"
    front_seg = "".join(f"[{i}:가나다😀]" for i in range(250))
    back_seg = "".join(f"[{i}:라마바]" for i in range(250))
    big_body = "FRONT-" + front_seg + "-MIDDLE-" + back_seg + "-END"
    _seed(store, project, ["SMALL-CHUNK", big_body], evidence=1)
    seen_texts: list[str] = []

    def _post(url, *, headers, body, timeout):
        parsed = json.loads(body)
        sessions = json.loads(parsed["messages"][1]["content"])["sessions"]
        for item in sessions:
            seen_texts.append(item["text"])
        return json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "sessions": [{
                            "session_key": item["session_key"],
                            "entities": [{"name": PFX + "Entity", "type": "Concept", "summary": "ok"}],
                            "relations": [],
                        } for item in sessions]
                    })
                }
            }]
        })

    state = _State()
    extractor = OpenAICompatibleBulkSemanticExtractor(
        base_url="http://127.0.0.1:9/v1",
        model="synthetic-model",
        post_fn=_post,
        max_streamed_request_bytes=5000,
    )
    monkeypatch.setattr(runner_mod, "_pg_authority_by_hash", lambda *a, **k: {})

    # Pass 1 (without max_chunk_bytes): SMALL-CHUNK succeeds, big_body fails closed -> deferred.
    report1 = run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db",
        source_store=store,
        extractor=extractor,
        writer=_Writer(),
        full_input_state=state,
        policy_version=PFX + "v1",
        project=project,
        limit=1,
        batch_size=1,
    )
    assert report1["status"] == "partial"
    assert report1["deferred_sessions"] == 1
    assert report1["failed_sessions"] == 0
    assert report1["projected"] == 1
    assert report1["limit_causes"] == {"client_request_byte_limit_exceeded_split_required": 1}
    assert seen_texts == ["SMALL-CHUNK"]

    # Pass 2 (with max_chunk_bytes): SMALL-CHUNK is resumed (not resent!), big_body is split on UTF-8 boundaries, and evidence completes.
    seen_texts.clear()
    report2 = run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db",
        source_store=store,
        extractor=extractor,
        writer=_Writer(),
        full_input_state=state,
        policy_version=PFX + "v1",
        project=project,
        limit=1,
        batch_size=1,
        max_chunk_bytes=800,
    )
    assert report2["status"] == "ok"
    assert report2["resumed_chunks"] == 1
    assert report2["stream_coverage_complete"] is True
    assert report2["hash_order_match"] is True
    assert "SMALL-CHUNK" not in seen_texts
    assert "evidence-0" in seen_texts
    reconstructed = "".join(t for t in seen_texts if t != "evidence-0")
    assert reconstructed == big_body


def test_privacy_marker_across_split_boundary_fails_closed_with_max_chunk_bytes(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "privsplit"
    # Place marker right across byte offset 20 (max_chunk_bytes=20).
    body = ("A" * 17) + "SECRET" + ("B" * 20)
    _seed(store, project, [body], evidence=0)
    extractor = _Extractor()
    monkeypatch.setattr(runner_mod, "_pg_authority_by_hash", lambda *a, **k: {})
    report = run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db",
        source_store=store,
        extractor=extractor,
        writer=_Writer(),
        full_input_state=_State(),
        policy_version=PFX + "v1",
        project=project,
        limit=1,
        forbidden_markers=(b"SECRET",),
        max_chunk_bytes=20,
    )
    assert report["status"] == "failed"
    assert report["failure_reasons"] == {"privacy_violation": 1}
    assert extractor.calls == 0


def test_authority_revalidation_uses_session_project_when_filter_project_empty(tmp_path, monkeypatch):
    store = InMemoryCouchDBSourceStore()
    session_project = PFX + "reval"
    _seed(store, session_project, ["BODY"], evidence=0)
    seen_projects: list[str] = []

    monkeypatch.setattr(
        runner_mod,
        "_pg_authority_by_hash",
        lambda dsn, proj, hashes, **kw: {
            hashes[0]: [{"authority_memory_id": "mem-1", "content_hash": hashes[0]}]
        },
    )

    def _reval(dsn, proj, sources):
        seen_projects.append(proj)
        return proj == session_project

    monkeypatch.setattr(runner_mod, "revalidate_authority_sources", _reval)
    report = run_streamed_full_input(
        ledger_path=tmp_path / "ledger.db",
        source_store=store,
        extractor=_Extractor(),
        writer=_Writer(),
        full_input_state=_State(),
        policy_version=PFX + "v1",
        project="",
        limit=1,
    )
    assert report["status"] == "ok"
    assert seen_projects == [session_project]


def test_active_revision_preserves_turn_order_over_snapshot_hash_order():
    import hashlib
    from agent_knowledge.couchdb_source.source_revision import activate_source_revision
    from agent_knowledge.llm_brain_core.bulk_streaming import expected_source_bytes

    store = InMemoryCouchDBSourceStore()
    project = PFX + "actorder"
    bodies = [f"TURN-{i}" for i in range(5)]
    sid = _seed(store, project, bodies, evidence=0)
    activate_source_revision(store=store, session_id_hash=sid)

    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store
    )
    plan = plan_validated_source(store, session_id_hash=sid, expected_source_hash=source_hash)
    expectation = expected_source_bytes(store, plan)

    expected_turn_hashes = [
        "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest() for body in bodies
    ]
    assert [item["canonical_hash"] for item in expectation["ordered"]] == expected_turn_hashes
    assert [member.ordinal for member in plan.members] == [0, 1, 2, 3, 4]


def test_max_chunk_bytes_change_a_to_b_and_b_to_a_never_collides_on_p0_or_leaves_byte_gap(
    tmp_path, monkeypatch,
):
    store = InMemoryCouchDBSourceStore()
    project = PFX + "resize"
    big_body = "".join(f"[chunk:{idx:03d}:한글😀]" + ("Z" * 16) for idx in range(70))
    assert len(big_body.encode("utf-8")) > 2000
    _seed(store, project, [big_body], evidence=1)
    monkeypatch.setattr(runner_mod, "_pg_authority_by_hash", lambda *a, **k: {})

    class _RecordingExtractor:
        model = "free-model"
        provider = P
        endpoint_class = "approved_free"
        price_status = "free_confirmed"

        def __init__(self) -> None:
            self.calls = 0
            self.seen_texts: list[str] = []

        def extract(self, batch):
            self.calls += 1
            for item in batch:
                self.seen_texts.append(
                    item.text if isinstance(item.text, str) else "".join(item.text.iter_pages())
                )
            return BulkSemanticExtractionResult(tuple(
                BulkSemanticSessionResult(
                    session_key=item.session_key,
                    entities=(BulkSemanticEntity(name=PFX + "E", type="Concept", summary="s"),),
                )
                for item in batch
            ))

    class _PartialPrefixWriter:
        def __init__(self, accept_count: int) -> None:
            self._remaining = accept_count
            self.accepted_texts: list[str] = []

        def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
            receipts: dict[str, str] = {}
            for item in inputs:
                if self._remaining > 0:
                    self._remaining -= 1
                    receipts[item.session_key] = "receipt:" + item.session_key
                    self.accepted_texts.append(
                        item.text if isinstance(item.text, str) else "".join(item.text.iter_pages())
                    )
            return BulkSemanticWriteReport(
                projected=len(receipts), entities_written=len(receipts), relations_written=0,
                chunk_receipts=receipts,
            )

    policy = PFX + "v1"

    # Sub-case A: A -> B (400 -> 800) after p0 completes under A=400.
    state_a = _State()
    ext_a1 = _RecordingExtractor()
    rep_a1 = run_streamed_full_input(
        ledger_path=tmp_path / "a1.db", source_store=store, extractor=ext_a1,
        writer=_PartialPrefixWriter(1), full_input_state=state_a, policy_version=policy,
        project=project, limit=1, batch_size=1, max_chunk_bytes=400,
    )
    assert rep_a1["status"] == "partial"
    assert rep_a1["projected"] == 1

    ext_a2 = _RecordingExtractor()
    rep_a2 = run_streamed_full_input(
        ledger_path=tmp_path / "a2.db", source_store=store, extractor=ext_a2,
        writer=_Writer(), full_input_state=state_a, policy_version=policy,
        project=project, limit=1, batch_size=1, max_chunk_bytes=800,
    )
    assert rep_a2["status"] == "ok"
    assert rep_a2["resumed_chunks"] == 0
    assert rep_a2["stream_coverage_complete"] is True
    assert rep_a2["hash_order_match"] is True
    assert "evidence-0" in ext_a2.seen_texts
    assert "".join(t for t in ext_a2.seen_texts if t != "evidence-0") == big_body

    # Sub-case B: B -> A (800 -> 400) after p0 completes under B=800.
    state_b = _State()
    ext_b1 = _RecordingExtractor()
    rep_b1 = run_streamed_full_input(
        ledger_path=tmp_path / "b1.db", source_store=store, extractor=ext_b1,
        writer=_PartialPrefixWriter(1), full_input_state=state_b, policy_version=policy,
        project=project, limit=1, batch_size=1, max_chunk_bytes=800,
    )
    assert rep_b1["status"] == "partial"
    assert rep_b1["projected"] == 1

    ext_b2 = _RecordingExtractor()
    rep_b2 = run_streamed_full_input(
        ledger_path=tmp_path / "b2.db", source_store=store, extractor=ext_b2,
        writer=_Writer(), full_input_state=state_b, policy_version=policy,
        project=project, limit=1, batch_size=1, max_chunk_bytes=400,
    )
    assert rep_b2["status"] == "ok"
    assert rep_b2["resumed_chunks"] == 0
    assert rep_b2["stream_coverage_complete"] is True
    assert rep_b2["hash_order_match"] is True
    assert "".join(t for t in ext_b2.seen_texts if t != "evidence-0") == big_body

    # Sub-case C: Partial restart with identical max_chunk_bytes=400 resumes cleanly without duplicates.
    state_c = _State()
    partial_writer = _PartialPrefixWriter(2)
    ext_c1 = _RecordingExtractor()
    rep_c1 = run_streamed_full_input(
        ledger_path=tmp_path / "c1.db", source_store=store, extractor=ext_c1,
        writer=partial_writer, full_input_state=state_c, policy_version=policy,
        project=project, limit=1, batch_size=1, max_chunk_bytes=400,
    )
    assert rep_c1["status"] == "partial"
    assert len(partial_writer.accepted_texts) == 2

    ext_c2 = _RecordingExtractor()
    rep_c2 = run_streamed_full_input(
        ledger_path=tmp_path / "c2.db", source_store=store, extractor=ext_c2,
        writer=_Writer(), full_input_state=state_c, policy_version=policy,
        project=project, limit=1, batch_size=1, max_chunk_bytes=400,
    )
    assert rep_c2["status"] == "ok"
    assert rep_c2["resumed_chunks"] == 2
    assert rep_c2["stream_coverage_complete"] is True
    assert rep_c2["hash_order_match"] is True
    for already in partial_writer.accepted_texts:
        assert already not in ext_c2.seen_texts
    assert "".join(partial_writer.accepted_texts) + "".join(
        t for t in ext_c2.seen_texts if t != "evidence-0"
    ) == big_body

    ext_c3 = _RecordingExtractor()
    rep_c3 = run_streamed_full_input(
        ledger_path=tmp_path / "c3.db", source_store=store, extractor=ext_c3,
        writer=_Writer(), full_input_state=state_c, policy_version=policy,
        project=project, limit=1, batch_size=1, max_chunk_bytes=400,
    )
    assert rep_c3["status"] == "ok"
    assert rep_c3["skipped_resumed"] == 1
    assert ext_c3.calls == 0


def test_policy_version_change_and_legacy_checkpoint_never_false_complete(tmp_path, monkeypatch):
    import hashlib

    store = InMemoryCouchDBSourceStore()
    project = PFX + "legacy"
    body = "".join(f"[seg:{idx:03d}]" + ("K" * 20) for idx in range(50))
    sid = _seed(store, project, [body], evidence=1)
    monkeypatch.setattr(runner_mod, "_pg_authority_by_hash", lambda *a, **k: {})

    class _RecExtractor:
        model = "free-model"
        provider = P
        endpoint_class = "approved_free"
        price_status = "free_confirmed"

        def __init__(self) -> None:
            self.seen_texts: list[str] = []

        def extract(self, batch):
            for item in batch:
                self.seen_texts.append(
                    item.text if isinstance(item.text, str) else "".join(item.text.iter_pages())
                )
            return BulkSemanticExtractionResult(tuple(
                BulkSemanticSessionResult(
                    session_key=item.session_key,
                    entities=(BulkSemanticEntity(name=PFX + "E", type="Concept", summary="s"),),
                )
                for item in batch
            ))

    # 1) Completing under v1 must NEVER false-complete or skip chunks under v2.
    shared_state = _State()
    ext_v1 = _RecExtractor()
    rep_v1 = run_streamed_full_input(
        ledger_path=tmp_path / "v1.db", source_store=store, extractor=ext_v1,
        writer=_Writer(), full_input_state=shared_state, policy_version=PFX + "v1",
        project=project, limit=1, batch_size=1, max_chunk_bytes=500,
    )
    assert rep_v1["status"] == "ok"
    assert rep_v1["stream_coverage_complete"] is True

    ext_v2 = _RecExtractor()
    rep_v2 = run_streamed_full_input(
        ledger_path=tmp_path / "v2.db", source_store=store, extractor=ext_v2,
        writer=_Writer(), full_input_state=shared_state, policy_version=PFX + "v2",
        project=project, limit=1, batch_size=1, max_chunk_bytes=500,
    )
    assert rep_v2["status"] == "ok"
    assert rep_v2["skipped_resumed"] == 0
    assert rep_v2["resumed_chunks"] == 0
    assert "".join(t for t in ext_v2.seen_texts if t != "evidence-0") == body

    # 2) Pre-fix legacy keys (no pol=, no :b...-:m...) must fail closed and trigger full reprocessing.
    source_hash = session_source_revision_from_couchdb_source(session_id_hash=sid, source_store=store)
    plan = plan_validated_source(store, session_id_hash=sid, expected_source_hash=source_hash)

    def _legacy_key(prefix: str, material: str) -> str:
        return "sha256:" + hashlib.sha256(f"{prefix}|{material}".encode("utf-8")).hexdigest()

    legacy_keys = {
        _legacy_key(f"{plan.source_hash}:{m.lane}:{m.ordinal}:{m.doc_id}", m.content_hash)
        for m in plan.members
    }
    legacy_keys.add(
        _legacy_key(
            f"{plan.source_hash}:{plan.members[0].lane}:{plan.members[0].ordinal}:{plan.members[0].doc_id}:p0",
            plan.members[0].content_hash,
        )
    )
    legacy_state = _State(complete=legacy_keys)
    ext_legacy = _RecExtractor()
    rep_legacy = run_streamed_full_input(
        ledger_path=tmp_path / "leg.db", source_store=store, extractor=ext_legacy,
        writer=_Writer(), full_input_state=legacy_state, policy_version=PFX + "v1",
        project=project, limit=1, batch_size=1, max_chunk_bytes=500,
    )
    assert rep_legacy["status"] == "ok"
    assert rep_legacy["skipped_resumed"] == 0
    assert rep_legacy["resumed_chunks"] == 0
    assert rep_legacy["stream_coverage_complete"] is True
    assert "".join(t for t in ext_legacy.seen_texts if t != "evidence-0") == body

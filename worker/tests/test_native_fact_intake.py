"""실제 native source intake를 독립 Git 검증과 공통 fact queue에 연결한다.

miner/model은 명시적 대역이며 Couch HTTP, PostgreSQL, Git과 정규 intake는 실제다.
"""
import json
import re

import pytest

from agent_knowledge.couchdb_source.observation_catalog import CouchObservationCatalog
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.tool_evidence_bundler import store_tool_evidence_bundles
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from agent_knowledge.session_memory.candidate_intake_cli import (
    CandidateIntakeSettings, load_chunks_from_pg, run_candidate_intake,
)
from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector

from test_fact_resolution import coordinator, facts, counts
from test_observation_provenance import actual_couch_source, record, _seed_unpinned_source, PROJECT
from test_pg_backfill import CountingEmbedProvider


@pytest.mark.parametrize("facts", [PROJECT], indirect=True)
@pytest.mark.parametrize("correction_first", [False, True])
def test_regular_native_intake_and_correction_converge_without_new_accepted_duplicate(
    coordinator, actual_couch_source, tmp_path, correction_first,
):
    c = coordinator
    worker = c.service.worker(project=PROJECT, worker_id="native-fact-worker")
    if correction_first:
        correction_task = c.service.submit(c.request)["changes"][0]["task_id"]
        assert worker.run_once(task_id=correction_task)["state"] == "resolved"
    _seed_unpinned_source(actual_couch_source)
    a = record(redacted_summary="기능 상태: missing")
    store_tool_evidence_bundles([a], store=actual_couch_source, full_session_generation=True)
    materialized = materialize_session_memory(session_id_hash=a.session_id_hash, store=actual_couch_source)
    embedder = CountingEmbedProvider()
    PgSessionMemoryProjector(c.f.store, embedder).project(target_profile=materialized.target_profile,
        document=materialized.to_projection_document())
    catalog = CouchObservationCatalog(actual_couch_source, extractor_versions={"codex": a.extractor_version})
    chunks = load_chunks_from_pg(c.f.store.dsn, project=PROJECT, limit=20, observation_catalog=catalog)
    calls = []
    def completion(messages):
        calls.append(messages)
        key = re.search(r"SOURCE_KEY: (\S+)", messages[-1]["content"])[1]
        assert a.evidence_id_hash in messages[-1]["content"]
        return json.dumps([{"source_key": key, "candidate": {"card_type": "status", "title": c.rule.target_title,
            "statement": "기능 상태: missing", "evidence_id_hashes": [a.evidence_id_hash],
            "typed_payload": {"status_value": "missing", "observed_at": a.observed_at,
                "expires_at": "", "current_authority": "native-test"}}}])
    steward = BrainStewardService(c.f.ledger, pgvector_store=c.f.store,
        allow_auto_accept=True, fact_coordinator=c.service)
    settings = CandidateIntakeSettings(project=PROJECT, enable_proposals=True, auto_accept_work_items=True,
        checkpoint_path=str(tmp_path / "native-checkpoint.json"))
    report, checkpoint = run_candidate_intake(settings=settings, chunks=chunks, completion_fn=completion, steward=steward)
    assert report.status == "completed" and report.fact_resolution_queued_count == 1
    assert report.auto_accepted_count == 0
    with c.f.store._scope() as db:
        task_id = db.execute("SELECT task_id FROM lbrain_recheck_tasks WHERE request->>'lane'='intake'").fetchone()["task_id"]
    task = c.service.queue.get(task_id)
    candidate = c.f.store.get_steward_card(task["request"]["candidate_memory_id"])
    assert candidate["authorization_status"] == "disabled"
    assert task["request"]["trigger"]["source_refs"] == candidate["source_refs"]
    [ref] = candidate["source_refs"][0]["native_observation_refs"]
    assert ref["observation_id_hash"] == a.observation_id_hash
    assert candidate["source_refs"][0]["source_revision_hash"] == materialized.source_hash
    assert worker.run_once(task_id=task_id)["state"] == "resolved"
    outcome = c.service.result(task_id)["resolution"]
    assert outcome["current"]["value"] == "ready" and outcome["requested_value"] == "missing"
    assert outcome["action"] == ("attach_evidence" if correction_first else "replace_current")
    if not correction_first:
        correction_task = c.service.submit(c.request)["changes"][0]["task_id"]
        assert worker.run_once(task_id=correction_task)["state"] == "resolved"
        assert c.service.result(correction_task)["resolution"]["action"] == "attach_evidence"
    assert counts(c.f)["steward_card_decisions"] == 2
    assert counts(c.f)["lbrain_fact_assertions"] == 1
    retried = steward.candidate_autopilot_accept(candidate_memory_id=candidate["memory_id"],
        decision_id=f"autopilot-{candidate['memory_id']}")
    assert retried["fact_resolution"]["results"][0] == {"policy_id": c.rule.policy_id,
        "status": "resolved", "task_id": task_id, "duplicate": True}
    with c.f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM lbrain_recheck_tasks WHERE request->>'lane'='intake'").fetchone()["n"] == 1
    repeat, _ = run_candidate_intake(settings=settings, chunks=chunks, completion_fn=completion,
        steward=steward, checkpoint=checkpoint)
    assert repeat.status == "completed" and len(calls) == 1 and repeat.fact_resolution_queued_count == 0
    assert embedder.calls == 1

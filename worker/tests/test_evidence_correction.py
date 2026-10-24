"""source 재조회, canonical authority, transport 권한과 실패 원자성을 검증한다."""
from __future__ import annotations

import copy
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_knowledge.evidence_correction import (
    CorrectionError, POLICY_SCHEMA, resolve_value, validate_policies,
)
from agent_knowledge.knowledge_search_service import DisabledRetiredIndexBridgeClient, KnowledgeSearchService
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.ledger_adapter import LedgerSourceRefCatalog
from agent_knowledge.llm_brain_core.models import SourceRefRecord
from agent_knowledge.llm_brain_core.objects.knowledge_objects import memory_card_to_knowledge_object
from agent_knowledge.mcp_jsonrpc import CORRECTION_AUTH_IDENTITY, handle_jsonrpc_message, run_stdio_server
from agent_knowledge.public_safe_util import sha256_text, short_hash, utc_now_iso
from agent_knowledge.session_memory.brain_steward import BrainStewardService


PROJECT = "correction-test"


def source(*, source_id=None, path="facts", value="ready", **overrides):
    content = json.dumps({"facts": {"status": value}}, sort_keys=True)
    data = {
        "root_id": "facts-root", "device_id_hash": sha256_text("device"),
        "relative_path_hash": sha256_text(path), "content_hash": sha256_text(content),
        "mtime": utc_now_iso(), "size": len(content.encode()), "sync_policy": "full_sync",
        "permission_scope": "project", "last_seen_at": utc_now_iso(), "redacted_content": content,
        **overrides,
    }
    data["source_ref_id"] = source_id or "src_" + short_hash([
        data["device_id_hash"], data["root_id"], data["relative_path_hash"], data["content_hash"],
    ])
    return SourceRefRecord(**data)


def binding(record):
    return {"adapter": "registered_json", "root_id": record.root_id,
            "device_id_hash": record.device_id_hash, "relative_path_hash": record.relative_path_hash,
            "json_pointer": "/facts/status"}


def policy(record, *, memory_id="old-card", **overrides):
    return {"policy_id": "feature-status", "project": PROJECT, "repository": "neurons",
            "memory_id": memory_id, "field_path": "typed_payload.status_value",
            "summary_template": "기능 상태: {value}", "sources": [binding(record)],
            "max_age_seconds": 300, "value_map": {}, **overrides}


def refs(*records):
    return [{"source_ref_id": record.source_ref_id, "source_content_hash": record.content_hash} for record in records]


def resolve(rule, records, supplied=None):
    def list_for_path(**locator):
        return [record for record in records if all(getattr(record, key) == value for key, value in locator.items())]
    return resolve_value(rule=rule, catalog=SimpleNamespace(list_for_path=list_for_path),
                         evidence_refs=supplied if supplied is not None else refs(*records))


def test_source_adapter_derives_value_from_data_instead_of_caller_claim():
    record = source()
    [rule] = validate_policies({"schema_version": POLICY_SCHEMA, "rules": [policy(record)]})
    value, proofs = resolve(rule, [record])
    assert value == "ready"
    assert proofs[0]["content_hash"] == sha256_text(record.redacted_content)
    assert "ready" not in json.dumps(proofs)


@pytest.mark.parametrize("change,reason", [
    ({"sync_policy": "local_only"}, "source_policy_denied"),
    ({"sync_policy": "derived_only"}, "source_policy_denied"),
    ({"revoked_at": "2026-01-01T00:00:00Z"}, "source_deleted_or_revoked"),
    ({"deleted_at": "2026-01-01T00:00:00Z"}, "source_deleted_or_revoked"),
    ({"content_hash": sha256_text("fabricated")}, "source_hash_unverifiable"),
    ({"last_seen_at": "2020-01-01T00:00:00Z"}, "source_stale"),
    ({"last_seen_at": "2099-01-01T00:00:00Z"}, "source_stale"),
    ({"last_seen_at": "2026-01-01T00:00:00"}, "source_observation_time_invalid"),
])
def test_source_adapter_rejects_unverifiable_or_stale_content(change, reason):
    record = source(**change)
    with pytest.raises(CorrectionError, match=reason):
        resolve(policy(record), [record])


def test_latest_source_uses_instants_and_requires_all_policy_sources():
    old = source(value="old", last_seen_at="2026-10-25T10:00:00+09:00")
    newer = source(last_seen_at="2026-10-25T02:00:00Z")
    rule = policy(old, max_age_seconds=86400)
    with pytest.raises(CorrectionError, match="source_revision_not_current"):
        resolve(rule, [old, newer], refs(old))
    foreign = source(path="other", value="missing")
    rule = policy(newer, sources=[binding(newer), binding(foreign)])
    with pytest.raises(CorrectionError, match="source_set_mismatch"):
        resolve(rule, [newer, foreign], refs(newer))
    newer = source()
    rule = policy(newer, sources=[binding(newer), binding(foreign)])
    with pytest.raises(CorrectionError, match="authoritative_sources_conflict"):
        resolve(rule, [newer, foreign])


@pytest.mark.parametrize("content", ['{"facts":{"status":"a","status":"b"}}', '{"facts":{}}', '{"facts":{"status":[]}}'])
def test_source_adapter_rejects_ambiguous_or_unsupported_json(content):
    record = source(redacted_content=content, content_hash=sha256_text(content))
    with pytest.raises(CorrectionError):
        resolve(policy(record), [record])


@pytest.mark.parametrize("change", [
    {"field_path": "typed_payload.preference"}, {"summary_template": "{value}{other}"},
    {"max_age_seconds": True}, {"max_age_seconds": 86401},
    {"sources": [{"adapter": "caller_claim"}]},
])
def test_policy_rejects_unsupported_authority_scope(change):
    with pytest.raises((ValueError, TypeError)):
        validate_policies({"schema_version": POLICY_SCHEMA, "rules": [policy(source(), **change)]})


def test_correction_cli_wires_only_canonical_store_and_policy(tmp_path, monkeypatch):
    import argparse
    from agent_knowledge import cli
    from agent_knowledge.rag_ingress import pg_recall, qdrant_recall
    from agent_knowledge.postgres_store import pgvector_store

    ledger_path = tmp_path / "ledger.sqlite"
    Ledger(ledger_path)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"schema_version": POLICY_SCHEMA, "rules": [policy(source())]}))
    parser = argparse.ArgumentParser()
    cli._add_recall_service_arguments(parser)
    args = parser.parse_args(["--ledger", str(ledger_path), "--allow-evidence-backed-object-corrections",
                              "--evidence-correction-policy-file", str(policy_path)])
    args.surface = "correction"
    monkeypatch.delenv("NEURON_LEDGER_PG_DSN", raising=False)
    monkeypatch.delenv("LLM_BRAIN_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.setenv("NEURON_LBRAIN_PGVECTOR_DSN", "postgresql://synthetic.invalid/correction")
    monkeypatch.setattr(cli, "build_graph_adapter_from_env", lambda **_: None)
    def forbidden(*args, **kwargs):
        raise AssertionError("교정 전용 surface가 archive provider에 의존하면 안 된다")
    monkeypatch.setattr(pg_recall, "build_pg_brain_query_search_from_env", forbidden)
    monkeypatch.setattr(qdrant_recall, "build_qdrant_brain_query_search_from_env", forbidden)
    store = SimpleNamespace()
    monkeypatch.setattr(pgvector_store, "PgVectorStore", lambda **_: store)
    service = cli._build_recall_service(args)
    assert service.pgvector_store is store
    assert service._mirror_search is None
    assert service.allow_evidence_backed_object_corrections is True
    assert service.ledger.read_only is False


def test_correction_cli_missing_policy_fails_before_writable_ledger_open(tmp_path, monkeypatch):
    import argparse
    from agent_knowledge import cli

    parser = argparse.ArgumentParser()
    cli._add_recall_service_arguments(parser)
    args = parser.parse_args(["--ledger", str(tmp_path / "unused.sqlite"), "--allow-evidence-backed-object-corrections"])
    def forbidden(*args, **kwargs):
        raise AssertionError("검증되지 않은 policy로 ledger를 열면 안 된다")
    monkeypatch.setattr(cli, "Ledger", forbidden)
    with pytest.raises(cli._ServiceWiringError, match="^invalid evidence correction policy configuration$"):
        cli._build_recall_service(args)


@pytest.fixture
def correction(tmp_path, isolated_pg_store):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    store = isolated_pg_store
    steward = BrainStewardService(ledger, pgvector_store=store, allow_restricted=True)
    span = {
        "card_type": "status", "project": PROJECT, "provider": "codex", "scope": "project",
        "title": "교정 기능 상태", "redacted_summary": "기능 상태: missing",
        "typed_payload": {"status_value": "missing", "observed_at": utc_now_iso(),
                          "expires_at": "", "current_authority": "structured-facts"},
        "source_ref": {"source_id": "source-old"}, "span_ref": {"span_id": "span-old"},
        "content_hash": sha256_text("old status"), "confidence": 0.95, "confidence_basis": "명시적 사실",
    }
    old_id = steward.candidate_create(source_span=span)["memory_id"]
    steward.candidate_approve(candidate_memory_id=old_id, approved_by="fixture", decision_id="seed-approval")
    old = store.get_steward_card(old_id)
    record = source()
    LedgerSourceRefCatalog(ledger).register(record)
    rule = policy(record, memory_id=old_id)
    service = KnowledgeSearchService(
        ledger=ledger, retired_index_bridge=DisabledRetiredIndexBridgeClient(), dataset_ids=[],
        pgvector_store=store, allow_evidence_backed_object_corrections=True,
        evidence_correction_policies=(rule,),
    )
    request = dict(project=PROJECT, repository="neurons",
                   target_object_id=memory_card_to_knowledge_object(old).object_id,
                   expected_content_hash=old["content_hash"], policy_id=rule["policy_id"],
                   idempotency_key="correct-status-1", evidence_refs=refs(record))
    return SimpleNamespace(ledger=ledger, store=store, service=service, old=old, record=record, request=request, rule=rule)


def authority_snapshot(fixture):
    return fixture.store.list_steward_cards(project=PROJECT, accepted_only=True, limit=100)


def call(fixture, request=None):
    return handle_jsonrpc_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "brain_object_correct", "arguments": request or fixture.request,
        }}, fixture.service, surface="correction", auth_token=CORRECTION_AUTH_IDENTITY,
    )["result"]["structuredContent"]


def test_actual_pg_correction_public_read_retry_audit_and_outboxes(correction):
    f = correction
    before = f.service.brain_memory_resolve(project=PROJECT, mode="list", limit=20)
    assert before["items"][0]["typed_payload"]["status_value"] == "missing"
    # 이전 cache를 채운 동일 service 인스턴스에서 교정한다.
    f.service._brain_card_cache.list_accepted_cards(project=PROJECT, limit=100)
    request = dict(f.request)
    request.pop("target_object_id")
    request["target_memory_id"] = before["items"][0]["id"]
    result = call(f, request)
    assert result["correction_status"] == "committed"
    assert result["read_after_write_verified"] is True
    assert result["authority_basis"] == "evidence_policy"
    new_id = result["successor_memory_id"]
    old = f.store.get_steward_card(f.old["memory_id"])
    new = f.store.get_steward_card(new_id)
    assert old["currentness"] == "superseded" and old["superseded_by"] == [new_id]
    assert new["supersedes"] == [old["memory_id"]]
    assert new["typed_payload"]["status_value"] == "ready"
    assert new["typed_payload"]["current_authority"] == f.old["typed_payload"]["current_authority"]
    assert "approved_by" not in new
    assert f.store.get_steward_decision(result["decision_id"])["action"] == "correct_fact"
    assert f.ledger.get_llm_brain_memory_card(new_id) is None
    after = f.service.brain_memory_resolve(project=PROJECT, mode="list", limit=20)
    assert [item["id"] for item in after["items"]] == [new_id]
    assert after["items"][0]["summary"] == "기능 상태: ready"
    snapshot = authority_snapshot(f)
    retry = call(f, request)
    assert retry["duplicate"] is True and retry["authority_write_performed"] is False
    assert authority_snapshot(f) == snapshot
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions WHERE action = 'correct_fact'").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM memory_edges WHERE src_id = %s AND dst_id = %s AND rel_type = 'supersedes'", (new_id, old["memory_id"])).fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM embedding_outbox WHERE target_id = %s", (new_id,)).fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM graph_projection_outbox WHERE source_id = ANY(%s)", ([new_id, old["memory_id"]],)).fetchone()["n"] >= 2


@pytest.mark.parametrize("case,reason", [
    ("cross_scope", "policy_scope_mismatch"), ("stale_target", "target_revision_conflict"),
    ("forged_source", "source_revision_not_current"), ("stale_source", "source_stale"),
    ("unverifiable", "source_hash_unverifiable"),
])
def test_negative_pg_paths_preserve_authority_and_persist_denial(correction, case, reason):
    f = correction
    request = copy.deepcopy(f.request)
    if case == "cross_scope":
        request["project"] = "other-project"
    elif case == "stale_target":
        request["expected_content_hash"] = sha256_text("stale")
    elif case == "forged_source":
        request["evidence_refs"][0]["source_content_hash"] = sha256_text("forged")
    else:
        changes = {"last_seen_at": "2020-01-01T00:00:00Z"} if case == "stale_source" else {
            "content_hash": sha256_text("forged"),
            "last_seen_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        }
        record = source(**changes)
        LedgerSourceRefCatalog(f.ledger).register(record)
        request["evidence_refs"] = refs(record)
    before = authority_snapshot(f)
    result = call(f, request)
    assert result["authority_write_performed"] is False
    assert result["reason_code"] == reason
    assert authority_snapshot(f) == before
    assert result["review_proposal"]["status"] == "needs_review"
    with f.store._scope() as db:
        row = db.execute("SELECT reason_code FROM steward_correction_denials WHERE audit_event_id = %s", (result["audit_event_id"],)).fetchone()
        assert row["reason_code"] == reason
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions WHERE action='correct_fact'").fetchone()["n"] == 0


def test_atomic_rollback_after_partial_sql_authority_update(correction, monkeypatch):
    f = correction
    before = authority_snapshot(f)
    def fail(*args, **kwargs):
        raise RuntimeError("injected projection failure")
    monkeypatch.setattr(f.store, "_enqueue_graph_outbox_on", fail)
    with pytest.raises(RuntimeError, match="injected"):
        f.service.brain_object_correct(**f.request)
    assert authority_snapshot(f) == before
    assert f.store.get_steward_card(f.old["memory_id"])["currentness"] == "current"
    with f.store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM steward_card_decisions WHERE action='correct_fact'").fetchone()["n"] == 0


def test_target_cas_rechecked_inside_authority_transaction(correction, monkeypatch):
    f = correction
    original = f.store.steward_decision
    concurrent_state = []
    def race(**kwargs):
        old = f.store.get_steward_card(f.old["memory_id"])
        old["currentness"] = "stale"
        with f.store.transaction() as db:
            db.execute("UPDATE memory_cards SET currentness='stale', steward_envelope=%s::jsonb WHERE memory_id=%s", (json.dumps(old), old["memory_id"]))
        concurrent_state.extend(authority_snapshot(f))
        return original(**kwargs)
    monkeypatch.setattr(f.store, "steward_decision", race)
    result = call(f)
    assert result["reason_code"] == "target_revision_conflict"
    assert authority_snapshot(f) == concurrent_state


def test_correction_transport_has_distinct_permission_and_stdio_visibility(correction):
    f = correction
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert "error" in handle_jsonrpc_message(message, f.service, surface="correction")
    visible = handle_jsonrpc_message(message, f.service, surface="correction", auth_token=CORRECTION_AUTH_IDENTITY)
    assert {tool["name"] for tool in visible["result"]["tools"]} == {"brain.resolve", "brain_object_correct"}
    public = handle_jsonrpc_message(message, f.service)
    assert "brain_object_correct" not in {tool["name"] for tool in public["result"]["tools"]}
    buffer = io.StringIO()
    run_stdio_server(f.service, stdin=io.StringIO(json.dumps(message) + "\n"), stdout=buffer)
    assert "brain_object_correct" in {tool["name"] for tool in json.loads(buffer.getvalue())["result"]["tools"]}
    f.service.allow_evidence_backed_object_corrections = False
    buffer = io.StringIO()
    run_stdio_server(f.service, stdin=io.StringIO(json.dumps(message) + "\n"), stdout=buffer)
    assert "brain_object_correct" not in {tool["name"] for tool in json.loads(buffer.getvalue())["result"]["tools"]}


def test_conflicting_sources_and_changed_idempotency_key_payload_do_not_write(correction):
    f = correction
    conflicting = source(path="second", value="missing")
    LedgerSourceRefCatalog(f.ledger).register(conflicting)
    f.service.evidence_correction_policies = validate_policies({"schema_version": POLICY_SCHEMA, "rules": [
        {**f.rule, "sources": [binding(f.record), binding(conflicting)]},
    ]})
    before = authority_snapshot(f)
    result = call(f, {**f.request, "evidence_refs": refs(f.record, conflicting)})
    assert result["reason_code"] == "authoritative_sources_conflict"
    assert authority_snapshot(f) == before
    f.service.evidence_correction_policies = (f.rule,)
    assert call(f)["correction_status"] == "committed"
    before = authority_snapshot(f)
    altered = copy.deepcopy(f.request)
    altered["evidence_refs"][0]["source_content_hash"] = sha256_text("different")
    assert call(f, altered)["reason_code"] == "idempotency_conflict"
    assert authority_snapshot(f) == before


def test_same_request_concurrent_retries_have_one_authority_decision(correction, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    f = correction
    original = f.store.get_steward_decision
    barrier, lock, seen = Barrier(2), Lock(), [0]
    def first_read(decision_id):
        prior = original(decision_id)
        with lock:
            seen[0] += 1
            first = seen[0] <= 2
        if first:
            barrier.wait(timeout=10)
        return prior
    monkeypatch.setattr(f.store, "get_steward_decision", first_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: f.service.brain_object_correct(**f.request), range(2)))
    assert all(result["correction_status"] in {"committed", "conflict"} for result in results)
    assert sum(result["authority_write_performed"] for result in results) == 1
    assert len({result["successor_memory_id"] for result in results if "successor_memory_id" in result}) == 1
    retry = f.service.brain_object_correct(**f.request)
    assert retry["correction_status"] == "committed" and retry["duplicate"] is True


def test_policy_follows_successor_lineage_with_fresh_source_revision(correction):
    f = correction
    first = call(f)
    record = source(value="verified", last_seen_at=(datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat())
    LedgerSourceRefCatalog(f.ledger).register(record)
    current = f.store.get_steward_card(first["successor_memory_id"])
    request = {key: value for key, value in f.request.items() if key != "target_object_id"}
    request.update(target_memory_id=current["memory_id"], expected_content_hash=current["content_hash"],
                   idempotency_key="second-correction", evidence_refs=refs(record))
    second = call(f, request)
    assert second["correction_status"] == "committed"
    assert f.store.get_steward_card(current["memory_id"])["currentness"] == "superseded"
    assert f.store.get_steward_card(second["successor_memory_id"])["typed_payload"]["status_value"] == "verified"

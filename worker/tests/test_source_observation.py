"""서버 독립 관측과 지연 native 수집의 실제 PG/Couch 합류 계약."""

import copy
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation

from agent_knowledge.source_observation import (
    SourceObservationService,
    NativeSourceObservationBridge,
    _referenced_results,
)
from agent_knowledge.public_safe_util import hash_payload, sha256_text
import test_fact_resolution as fact_fixtures
import test_observation_provenance as observation_fixtures

facts = fact_fixtures.facts
coordinator = fact_fixtures.coordinator
actual_couch_source = observation_fixtures.actual_couch_source


def observe(c, key="first"):
    return SourceObservationService(c.service).observe(
        project=c.rule.scope.project,
        repository=c.rule.scope.repository,
        policy_id=c.rule.policy_id,
        idempotency_key=key,
    )


def correct(c, read, key="correction"):
    request = copy.deepcopy(c.request)
    request["idempotency_key"] = key
    request["changes"][0]["evidence"] = read["evidence"]
    return c.service.submit(request)


def run(c, task_id):
    return c.service.worker(
        project=c.rule.scope.project, worker_id="observation-worker", retry_seconds=0.1
    ).run_once(task_id=task_id)


def scalar(c, table):
    from psycopg import sql

    with c.f.store._scope() as db:
        return db.execute(
            sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
        ).fetchone()["n"]


def capture(
    c,
    source,
    tmp_path,
    payload,
    *,
    call_id="read-1",
    provider="codex",
    project=None,
    activate=True,
    call_ids=None,
):
    from agent_knowledge.session_memory import native_tool_receipts as native
    from agent_knowledge.couchdb_source import document_model as dm
    from agent_knowledge.couchdb_source.observation_catalog import (
        CouchObservationCatalog,
    )
    from agent_knowledge.couchdb_source.tool_evidence_bundler import (
        store_tool_evidence_bundles,
        build_tool_evidence_bundle_documents,
    )
    from agent_knowledge.session_memory.transcript_model import (
        TranscriptSession,
        TranscriptChunk,
    )
    from test_native_tool_receipts import rows

    project = project or c.rule.scope.project
    path = tmp_path / (call_id + ".jsonl")
    path.write_text(
        "\n".join(
            json.dumps(row)
            for native_call in (call_ids or [call_id])
            for row in rows(provider, call_id=native_call, result=json.dumps(payload))
        )
    )
    records = native.evidence_records(provider, path, project=project)
    session_id = records[0].session_id_hash
    source.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=session_id,
                provider=provider,
                project=project,
                started_at="2026-10-27T00:00:00Z",
                ended_at="2026-10-27T00:01:00Z",
            )
        )
    )
    source.put(
        dm.build_conversation_chunk_document(
            chunk=TranscriptChunk.from_text(
                chunk_id="public-fixture",
                session_id_hash=session_id,
                provider=provider,
                project=project,
                turn_start_index=0,
                turn_end_index=0,
                text="합성 세션",
            )
        )
    )
    if activate:
        store_tool_evidence_bundles(records, store=source, full_session_generation=True)
    else:
        for bundle in build_tool_evidence_bundle_documents(records):
            source.put(bundle)
    catalog = CouchObservationCatalog(
        source, extractor_versions={provider: native.EXTRACTOR}
    )
    c.service.native_catalog = catalog
    return NativeSourceObservationBridge(c.service, catalog)


def native_tasks(c):
    with c.f.store._scope() as db:
        return db.execute(
            "SELECT * FROM lbrain_recheck_tasks WHERE request ? 'native_route_key' ORDER BY created_at"
        ).fetchall()


def test_observe_does_not_write_authority_and_reference_is_replayable(coordinator):
    c = coordinator
    read = observe(c)
    assert read["canonical_write_performed"] is False
    assert scalar(c, "steward_card_decisions") == 1
    assert scalar(c, "lbrain_recheck_tasks") == 0
    assert scalar(c, "lbrain_fact_source_reads") == 1
    assert observe(c) == read  # 시각 갱신으로 현재 관측을 새로 만들지 않는다.
    with c.f.store._scope() as db:
        before = db.execute(
            "SELECT observation FROM lbrain_fact_observations"
        ).fetchone()["observation"]
    task = correct(c, read)["changes"][0]["task_id"]
    assert run(c, task)["state"] == "resolved"
    receipt = c.service.result(task)["resolution"]
    assert (
        receipt["applied_value"] == "ready"
        and receipt["observation_version"] == read["observation_version"]
    )
    assert scalar(c, "lbrain_fact_observations") == 1
    assert before["proof"]["source_ref_id"]
    assert scalar(c, "steward_card_decisions") == 2


def test_concurrent_read_key_has_one_immutable_reference(coordinator):
    c = coordinator
    with ThreadPoolExecutor(max_workers=3) as pool:
        reads = list(pool.map(lambda _: observe(c), range(3)))
    assert all(item == reads[0] for item in reads)
    assert scalar(c, "lbrain_fact_source_reads") == 1
    assert scalar(c, "steward_card_decisions") == 1


@pytest.mark.parametrize("change", ["ref", "hash", "revision"])
def test_bad_reference_does_not_create_task(coordinator, change):
    c = coordinator
    read = observe(c)
    if change == "ref":
        read["evidence"]["observation_ref"] = "obsread_" + "0" * 48
    if change == "hash":
        read["evidence"]["content_hash"] = sha256_text("different")
    if change == "revision":
        read["evidence"]["revision"] = c.previous
    result = correct(c, read)
    assert result["changes"][0]["status"] == "rejected"
    assert scalar(c, "lbrain_recheck_tasks") == 0
    assert scalar(c, "steward_card_decisions") == 1


def test_legacy_v2_serialization_is_unchanged(coordinator):
    from agent_knowledge.fact_correction_coordinator import FactBatch

    expected = copy.deepcopy(coordinator.request)
    expected["changes"][0]["target_description"] = None
    assert FactBatch.model_validate(coordinator.request).model_dump() == expected


@pytest.mark.parametrize(
    "provider", ["codex", "claude", "gemini", "grok", "antigravity"]
)
def test_correction_before_session_joins_original_observation_without_second_write(
    coordinator, actual_couch_source, tmp_path, provider
):
    c = coordinator
    read = observe(c)
    original = correct(c, read)["changes"][0]["task_id"]
    assert run(c, original)["state"] == "resolved"
    resolution = c.service.result(original)["resolution"]
    bridge = capture(c, actual_couch_source, tmp_path, read, provider=provider)
    dispatched = bridge.dispatch_once(project=c.rule.scope.project, batch_size=100)
    assert dispatched["queued"] == 1, dispatched
    [task] = native_tasks(c)
    assert run(c, task["task_id"])["state"] == "resolved"
    joined = c.service.result(task["task_id"])["resolution"]
    assert joined["resolution_id"] == resolution["resolution_id"]
    assert joined["native_source_read"]["join_only"] is True
    assert joined["authority_write_performed"] is False
    assert scalar(c, "lbrain_fact_observations") == 1
    assert scalar(c, "lbrain_fact_assertions") == 1
    assert scalar(c, "steward_card_decisions") == 2
    assert (
        bridge.dispatch_once(project=c.rule.scope.project, batch_size=100)["queued"]
        == 0
    )
    assert len(native_tasks(c)) == 1
    with c.f.store._scope() as db:
        row = db.execute("""SELECT r.redacted_content,r.content_hash FROM lbrain_fact_native_read_links n
            JOIN lbrain_fact_source_receipts r ON r.source_ref_id=n.native_source_ref_id""").fetchone()
    assert read["observation_ref"] in row["redacted_content"]
    assert sha256_text(row["redacted_content"]) == row["content_hash"]


def test_session_arrives_first_and_automatically_corrects_same_existing_fact(
    coordinator, actual_couch_source, tmp_path
):
    c = coordinator
    read = observe(c)
    bridge = capture(
        c,
        actual_couch_source,
        tmp_path,
        {"content": [{"type": "text", "text": json.dumps(read)}]},
    )
    dispatched = bridge.dispatch_once(project=c.rule.scope.project, batch_size=100)
    assert dispatched["queued"] == 1, dispatched
    [task] = native_tasks(c)
    result = run(c, task["task_id"])
    assert result["state"] == "resolved", result
    resolved = c.service.result(task["task_id"])["resolution"]
    assert resolved["applied_value"] == "ready"
    assert resolved["native_source_read"]["join_only"] is False
    assert resolved["authority_write_performed"] is True
    assert scalar(c, "lbrain_fact_observations") == 1
    assert scalar(c, "steward_card_decisions") == 2
    second = correct(c, read)["changes"][0]["task_id"]
    assert run(c, second)["state"] == "resolved"
    assert c.service.result(second)["resolution"]["authority_write_performed"] is False
    assert scalar(c, "steward_card_decisions") == 2


@pytest.mark.parametrize("tamper", ["unknown", "changed", "other_project"])
def test_native_reference_rejects_forgery_and_foreign_project(
    coordinator, actual_couch_source, tmp_path, tamper
):
    c = coordinator
    read = observe(c)
    project = c.rule.scope.project
    if tamper == "unknown":
        read["observation_ref"] = "obsread_" + "0" * 48
    if tamper == "changed":
        read["canonical_write_performed"] = True
    if tamper == "other_project":
        project = "foreign-project"
    bridge = capture(c, actual_couch_source, tmp_path, read, project=project)
    result = bridge.dispatch_once(project=project, batch_size=100)
    assert result["queued"] == 0 and result["rejected"] >= 1, result
    assert native_tasks(c) == []
    assert scalar(c, "steward_card_decisions") == 1


def test_committed_native_link_recovers_without_couch_or_git(
    coordinator, actual_couch_source, tmp_path, monkeypatch
):
    c = coordinator
    bridge = capture(c, actual_couch_source, tmp_path, observe(c))
    assert (
        bridge.dispatch_once(project=c.rule.scope.project, batch_size=100)["queued"]
        == 1
    )
    [task] = native_tasks(c)
    original = c.service.queue.finish

    def crash(*args, **kwargs):
        raise SystemExit(91)

    monkeypatch.setattr(c.service.queue, "finish", crash)
    with pytest.raises(SystemExit):
        run(c, task["task_id"])
    assert scalar(c, "lbrain_fact_native_read_links") == 1
    assert scalar(c, "steward_card_decisions") == 2
    monkeypatch.setattr(c.service.queue, "finish", original)
    with c.f.store.transaction() as db:
        db.execute(
            "UPDATE lbrain_recheck_tasks SET lease_until=clock_timestamp()-interval '1 second' WHERE task_id=%s",
            (task["task_id"],),
        )
    c.service.native_catalog = None
    hidden = c.root.with_name("unavailable-source")
    c.root.rename(hidden)
    try:
        assert run(c, task["task_id"])["state"] == "resolved"
    finally:
        hidden.rename(c.root)
    assert scalar(c, "steward_card_decisions") == 2


def test_source_outage_is_visible_before_any_queue_and_recovers(
    coordinator, actual_couch_source, monkeypatch
):
    from agent_knowledge.fact_source_lifecycle import FactSourceLifecycle
    from agent_knowledge.couchdb_source.observation_catalog import (
        CouchObservationCatalog,
    )

    c = coordinator
    bridge = NativeSourceObservationBridge(
        c.service,
        CouchObservationCatalog(
            actual_couch_source, extractor_versions={"codex": "fixture.v1"}
        ),
    )
    original = actual_couch_source.changes_since

    def unavailable(**kwargs):
        raise RuntimeError("credential must never appear in result")

    monkeypatch.setattr(actual_couch_source, "changes_since", unavailable)
    result = bridge.dispatch_once(project=c.rule.scope.project)
    assert result == {
        "status": "unavailable",
        "reason_code": "native_discovery_unavailable",
        "attention_required": True,
    }
    health = FactSourceLifecycle(c.f.store).health(project=c.rule.scope.project)
    assert health["native_sources"] == {
        "configured": 1,
        "unhealthy": 1,
        "missing_required": False,
    }
    assert health["attention_required"]
    assert scalar(c, "lbrain_recheck_tasks") == 0
    monkeypatch.setattr(actual_couch_source, "changes_since", original)
    assert bridge.dispatch_once(project=c.rule.scope.project)["status"] == "processed"
    assert (
        FactSourceLifecycle(c.f.store).health(project=c.rule.scope.project)[
            "native_sources"
        ]["unhealthy"]
        == 0
    )


def test_monitor_detects_configured_native_source_that_never_started(coordinator):
    from agent_knowledge.fact_source_lifecycle import FactSourceLifecycle

    c = coordinator
    health = FactSourceLifecycle(c.f.store).health(
        project=c.rule.scope.project,
        native_source_key=hash_payload("configured-source"),
    )
    assert health["native_sources"] == {
        "configured": 0,
        "unhealthy": 0,
        "missing_required": True,
    }
    assert health["attention_required"]


def test_read_and_native_link_cannot_be_overwritten_or_removed_while_referenced(
    coordinator, actual_couch_source, tmp_path
):
    c = coordinator
    read = observe(c)
    bridge = capture(c, actual_couch_source, tmp_path, read)
    assert (
        bridge.dispatch_once(project=c.rule.scope.project, batch_size=100)["queued"]
        == 1
    )
    [task] = native_tasks(c)
    assert run(c, task["task_id"])["state"] == "resolved"
    with pytest.raises(CheckViolation), c.f.store.transaction() as db:
        db.execute("UPDATE lbrain_fact_source_reads SET request_key='changed'")
    with pytest.raises(ForeignKeyViolation), c.f.store.transaction() as db:
        db.execute("DELETE FROM lbrain_fact_source_reads")
    with pytest.raises(CheckViolation), c.f.store.transaction() as db:
        db.execute("DELETE FROM lbrain_fact_native_read_links")
    with pytest.raises(ForeignKeyViolation), c.f.store.transaction() as db:
        db.execute(
            "DELETE FROM lbrain_fact_source_receipts WHERE source_ref_id LIKE 'src_native_%'"
        )


def test_reference_extraction_requires_complete_json_and_does_not_parse_prose():
    payload = {
        "schema_version": "brain_source_observation.v1",
        "observation_ref": "obsread_" + "a" * 48,
    }
    assert _referenced_results(
        json.dumps({"content": [{"text": json.dumps(payload)}]})
    ) == [payload]
    assert _referenced_results("내가 본 결과: " + json.dumps(payload)) == []
    assert _referenced_results(json.dumps(payload)[:-1]) == []
    assert _referenced_results('{"x":1,"x":2}') == []
    with pytest.raises(ValueError, match="native_source_reference_collision"):
        _referenced_results(json.dumps([payload, {**payload, "project": "forged"}]))


def test_late_native_reference_keeps_newer_current_and_old_decision(
    coordinator, actual_couch_source, tmp_path
):
    from test_correction_sources import git

    c = coordinator
    read = observe(c)
    first = correct(c, read)["changes"][0]["task_id"]
    assert run(c, first)["state"] == "resolved"
    (c.root / "facts.json").write_text('{"status":"failed"}')
    git(c.root, "add", "facts.json")
    git(c.root, "commit", "-m", "지연 관측보다 새로운 상태")
    later = observe(c, "new-state")
    with c.f.store._scope() as db:
        current = db.execute("SELECT memory_id FROM lbrain_fact_scopes").fetchone()
    card = c.service.resolver.project_cards(
        [c.f.store.get_steward_card(current["memory_id"])]
    )[0]
    c.request["changes"][0].update(
        target_memory_id=card["memory_id"],
        expected_content_hash=card["content_hash"],
        old_value="ready",
    )
    next_task = correct(c, later, "next")["changes"][0]["task_id"]
    assert run(c, next_task)["state"] == "resolved"
    bridge = capture(c, actual_couch_source, tmp_path, read)
    assert (
        bridge.dispatch_once(project=c.rule.scope.project, batch_size=100)["queued"]
        == 1
    )
    [task] = native_tasks(c)
    assert run(c, task["task_id"])["state"] == "resolved"
    result = c.service.result(task["task_id"])["resolution"]
    assert result["decision_time_current"]["value"] == "ready"
    assert result["current"]["value"] == "failed"
    assert result["authority_write_performed"] is False
    assert c.service.result(task["task_id"])["consistency_token"][
        "min_fact_generation"
    ] == c.f.store.fact_project_generation(c.rule.scope.project)
    assert scalar(c, "steward_card_decisions") == 3


def test_unactivated_native_bundle_waits_then_restart_resolves_without_resubmission(
    coordinator, actual_couch_source, tmp_path
):
    import time
    from agent_knowledge.couchdb_source.source_revision import activate_source_revision

    c = coordinator
    bridge = capture(c, actual_couch_source, tmp_path, observe(c), activate=False)
    assert (
        bridge.dispatch_once(project=c.rule.scope.project, batch_size=100)["queued"]
        == 1
    )
    [task] = native_tasks(c)
    blocked = run(c, task["task_id"])
    assert (
        blocked["state"] == "blocked"
        and blocked["reason_code"] == "native_source_not_ready"
    ), blocked
    assert scalar(c, "steward_card_decisions") == 1
    activate_source_revision(
        store=actual_couch_source,
        session_id_hash=task["request"]["native_source_read"]["session_id_hash"],
    )
    time.sleep(0.12)
    assert run(c, task["task_id"])["state"] == "resolved"
    assert c.service.queue.get(task["task_id"])["attempt_count"] == 2
    assert scalar(c, "steward_card_decisions") == 2


def test_couch_cursor_concurrent_restart_enqueues_one_route(
    coordinator, actual_couch_source, tmp_path
):
    c = coordinator
    bridge = capture(c, actual_couch_source, tmp_path, observe(c))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: bridge.dispatch_once(
                    project=c.rule.scope.project, batch_size=100
                ),
                range(2),
            )
        )
    assert sum(item.get("queued", 0) for item in results) == 1, results
    assert len(native_tasks(c)) == 1
    restarted = NativeSourceObservationBridge(c.service, c.service.native_catalog)
    assert (
        restarted.dispatch_once(project=c.rule.scope.project, batch_size=100)["queued"]
        == 0
    )


def test_multiple_native_calls_reference_one_server_observation_without_extra_support(
    coordinator, actual_couch_source, tmp_path
):
    c = coordinator
    read = observe(c)
    bridge = capture(
        c, actual_couch_source, tmp_path, read, call_ids=["call-a", "call-b"]
    )
    result = bridge.dispatch_once(project=c.rule.scope.project, batch_size=100)
    assert result["queued"] == 2, result
    tasks = native_tasks(c)
    for task in tasks:
        assert run(c, task["task_id"])["state"] == "resolved"
    assert scalar(c, "lbrain_fact_native_read_links") == 2
    assert scalar(c, "lbrain_fact_assertions") == 1
    assert scalar(c, "lbrain_fact_observations") == 1
    assert scalar(c, "steward_card_decisions") == 2
    receipts = [c.service.result(task["task_id"])["resolution"] for task in tasks]
    assert sum(receipt["authority_write_performed"] for receipt in receipts) == 1
    assert len({receipt["resolution_id"] for receipt in receipts}) == 1


def test_source_changed_before_observation_reference_issue_is_rejected(
    coordinator, monkeypatch
):
    from test_correction_sources import git
    from agent_knowledge.recheck_scheduler import RecheckBlocked

    c = coordinator
    original = c.service.git_reader.read_current
    changed = False

    def change_after_read(**kwargs):
        nonlocal changed
        result = original(**kwargs)
        if not changed:
            changed = True
            (c.root / "facts.json").write_text('{"status":"failed"}')
            git(c.root, "add", "facts.json")
            git(c.root, "commit", "-m", "관찰 참조 발급 전 실제 원천 변경")
        return result

    monkeypatch.setattr(c.service.git_reader, "read_current", change_after_read)
    with pytest.raises(RecheckBlocked, match="source_read_changed"):
        observe(c)
    assert scalar(c, "lbrain_fact_source_reads") == 0
    assert scalar(c, "steward_card_decisions") == 1


def test_old_read_first_arrival_retains_history_then_automatic_current_check_repairs(
    coordinator, actual_couch_source, tmp_path
):
    from test_correction_sources import git
    from agent_knowledge.fact_verification import FactVerificationService

    c = coordinator
    read = observe(c)
    (c.root / "facts.json").write_text('{"status":"failed"}')
    git(c.root, "add", "facts.json")
    git(c.root, "commit", "-m", "수집 전 관찰 이후 실제 상태 변경")
    bridge = capture(c, actual_couch_source, tmp_path, read)
    assert (
        bridge.dispatch_once(project=c.rule.scope.project, batch_size=100)["queued"]
        == 1
    )
    [task] = native_tasks(c)
    assert run(c, task["task_id"])["state"] == "resolved"
    result = c.service.result(task["task_id"])["resolution"]
    assert (
        result["action"] == "retain_history"
        and result["authority_write_performed"] is False
    )
    assert scalar(c, "steward_card_decisions") == 1
    scheduled = FactVerificationService(c.f.store).dispatch_once(
        project=c.rule.scope.project, retry_seconds=0.1
    )
    assert scheduled["scheduled"] == 1
    assert (
        c.service.worker(
            project=c.rule.scope.project, worker_id="current-check", retry_seconds=0.1
        ).run_once()["state"]
        == "resolved"
    )
    with c.f.store._scope() as db:
        head = db.execute("SELECT value FROM lbrain_fact_scopes").fetchone()
    assert head["value"] == "failed"
    assert scalar(c, "steward_card_decisions") == 2


@pytest.mark.parametrize("binding", ["read_project", "task_evidence"])
def test_database_rejects_cross_scope_or_changed_read_evidence(coordinator, binding):
    c = coordinator
    read = observe(c)
    if binding == "read_project":
        with (
            pytest.raises(CheckViolation, match="source_read_binding_mismatch"),
            c.f.store.transaction() as db,
        ):
            db.execute(
                """INSERT INTO lbrain_fact_source_reads
                SELECT %s,'foreign-project','forged',request_hash,policy_id,policy_version,fact_key,target_memory_id,
                    observation_version,result,result_hash,created_at FROM lbrain_fact_source_reads""",
                ("obsread_" + "1" * 48,),
            )
    else:
        task = correct(c, read)["changes"][0]["task_id"]
        with c.f.store.transaction() as db:
            original = db.execute(
                "SELECT * FROM lbrain_recheck_tasks WHERE task_id=%s", (task,)
            ).fetchone()
        forged = copy.deepcopy(original["request"])
        forged["change"]["evidence"]["revision"] = c.previous
        task = c.service.queue.register(
            project=c.rule.scope.project,
            memory_id=original["memory_id"],
            request=forged,
            request_key="forged",
        )["task_id"]
        with (
            pytest.raises(CheckViolation, match="source_read_task_binding_mismatch"),
            c.f.store.transaction() as db,
        ):
            db.execute(
                "INSERT INTO lbrain_fact_source_read_tasks(task_id,observation_ref) VALUES (%s,%s)",
                (task, read["observation_ref"]),
            )


def test_actual_http_observe_correct_then_cli_worker_discovers_late_session(
    coordinator,
    actual_couch_source,
    tmp_path,
    policy_descriptor=None,
    additional_env=None,
):
    import os
    import subprocess
    import time
    from dataclasses import asdict
    from pathlib import Path

    c = coordinator
    read = fact_fixtures.test_real_cli_http_configuration_source_read_and_current_query(
        c,
        tmp_path,
        return_read=True,
        policy_descriptor=policy_descriptor,
        additional_env=additional_env,
    )
    capture(c, actual_couch_source, tmp_path, read, activate=False)
    config = tmp_path / "native-worker-source-policy.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": "fact_correction_config.v1",
                "repositories": [
                    {
                        "repository": c.rule.scope.repository,
                        "root": str(c.root),
                        "branch_ref": c.rule.scope.branch,
                    }
                ],
                "rules": [
                    policy_descriptor
                    if policy_descriptor is not None
                    else asdict(c.rule)
                ],
            },
            ensure_ascii=False,
        )
    )
    config.chmod(0o600)
    native_policy = tmp_path / "native-source-policy.json"
    native_policy.write_text(
        json.dumps(
            {
                "schema_version": "native_source_policy.v1",
                "extractor_versions": {"codex": "native-tool-receipt-extractor.v1"},
                "redaction_version": "redaction.v1",
            }
        )
    )
    # policy가 실제 receipt redaction 계약과 동일해야 한다.
    from agent_knowledge.couchdb_source.observation_catalog import REDACTION_VERSION

    policy = json.loads(native_policy.read_text())
    policy["redaction_version"] = REDACTION_VERSION
    native_policy.write_text(json.dumps(policy))
    native_policy.chmod(0o600)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "lib"),
        "NEURON_LBRAIN_PGVECTOR_DSN": c.f.store.dsn,
        "COUCHDB_URL": actual_couch_source.base_url,
        "COUCHDB_DB": actual_couch_source.db,
        "COUCHDB_USER": os.environ["COUCHDB_USER"],
        "COUCHDB_PASSWORD": os.environ["COUCHDB_PASSWORD"],
    }
    env.update(additional_env or {})
    argv = [
        "uv",
        "run",
        "--no-project",
        "-m",
        "agent_knowledge.fact_source_lifecycle",
        "worker",
        "--project",
        c.rule.scope.project,
        "--config",
        str(config),
        "--native-source-policy-file",
        str(native_policy),
        "--iterations",
        "1",
        "--poll-seconds",
        ".05",
        "--retry-seconds",
        ".1",
    ]
    result = subprocess.run(argv, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, "native worker process failed (raw output withheld)"
    [task] = native_tasks(c)
    assert (
        task["state"] == "blocked" and task["reason_code"] == "native_source_not_ready"
    ), task["reason_code"]
    monitor = subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "-m",
            "agent_knowledge.fact_source_lifecycle",
            "monitor",
            "--project",
            c.rule.scope.project,
            "--heartbeat-grace",
            ".01",
        ],
        env=env,
        capture_output=True,
        timeout=20,
    )
    assert monitor.returncode == 0
    assert json.loads(monitor.stdout)["attention_required"]
    from agent_knowledge.couchdb_source.source_revision import activate_source_revision

    activate_source_revision(
        store=actual_couch_source,
        session_id_hash=task["request"]["native_source_read"]["session_id_hash"],
    )
    time.sleep(0.12)
    argv[argv.index("--iterations") + 1] = "3"
    restarted = subprocess.run(argv, env=env, capture_output=True, timeout=30)
    assert restarted.returncode == 0, (
        "restarted native worker failed (raw output withheld)"
    )
    [task] = native_tasks(c)
    assert task["state"] == "resolved"
    assert task["attempt_count"] == 2
    assert scalar(c, "steward_card_decisions") == 2
    assert scalar(c, "lbrain_fact_native_read_links") == 1
    joined = c.service.result(task["task_id"])["resolution"]
    assert (
        joined["authority_write_performed"] is False
        and joined["native_source_read"]["join_only"] is True
    )

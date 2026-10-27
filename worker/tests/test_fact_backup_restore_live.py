"""작업 전용 PG의 실제 백업 복원 뒤 옛 교정과 수집의 재생을 검증한다.

백업 실행기는 별도 프로세스다. 이 테스트 컨테이너에는 Docker 권한을 주지 않는다.
실제 운영 데이터가 아니라 합성 Git source와 전용 schema/database만 사용한다.
"""
import copy
import json
import os
from pathlib import Path
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from agent_knowledge.fact_correction_coordinator import FactCorrectionCoordinator
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.public_safe_util import sha256_text
from test_correction_sources import git
from test_fact_resolution import coordinator, counts, facts  # noqa: F401


pytestmark = pytest.mark.skipif(
    not (os.environ.get("LBRAIN_TEST_BACKUP_CONTROL_DIR")
         and os.environ.get("LBRAIN_TEST_RESTORE_ALLOW_DB_CREATION") == "1"),
    reason="전용 PG와 외부 백업·복원 제어 필요",
)


@pytest.mark.parametrize("facts", ["fact-restore-" + uuid.uuid4().hex[:12]], indirect=True)
def test_actual_backup_restore_preserves_correction_and_fences_old_intake(coordinator):  # noqa: F811
    c = coordinator
    control = Path(os.environ["LBRAIN_TEST_BACKUP_CONTROL_DIR"])
    assert control.is_dir() and not (control / "request.json").exists()
    source_dsn = os.environ["LBRAIN_TEST_PG_DSN"]
    config = conninfo_to_dict(source_dsn)
    assert config.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert config.get("dbname") == "neurons_correction_goal_canonical"
    target_db = "lb_restore_" + uuid.uuid4().hex
    with c.f.store._scope() as db:
        source_schema = db.execute("SELECT current_schema() AS name").fetchone()["name"]
    assert source_schema.startswith("c1_test_") and len(source_schema) == 40

    first = c.service.submit(c.request)["changes"][0]["task_id"]
    worker = c.service.worker(project=c.f.scope.project, worker_id="restore-before")
    assert worker.run_once(task_id=first)["state"] == "resolved"
    assert c.service.result(first)["resolution"]["current"]["value"] == "ready"
    text = '{"status":"failed"}'
    (c.root / "facts.json").write_text(text)
    git(c.root, "add", "facts.json")
    git(c.root, "commit", "-m", "백업 전 합성 후속 상태")
    second_request = copy.deepcopy(c.request)
    second_request["idempotency_key"] = "restore-newer"
    change = second_request["changes"][0]
    change.pop("target_memory_id")
    change.pop("expected_content_hash")
    change.update(target_description=c.rule.target_title, old_value="ready", proposed_value="failed",
        evidence={"revision": git(c.root, "rev-parse", "HEAD"), "content_hash": sha256_text(text)})
    second = c.service.submit(second_request)["changes"][0]["task_id"]
    assert worker.run_once(task_id=second)["state"] == "resolved"
    assert c.service.result(second)["resolution"]["current"]["value"] == "failed"
    expected_counts = counts(c.f)

    created = False
    try:
        with psycopg.connect(source_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_db)))
            created = True
        target_dsn = make_conninfo(source_dsn, dbname=target_db, connect_timeout=5)
        with psycopg.connect(target_dsn, autocommit=True) as target:
            target.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
            target.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
        (control / "request.json").write_text(json.dumps({"schema": source_schema,
            "source_database": config["dbname"], "target_database": target_db}))
        deadline = time.monotonic() + 120
        while not (control / "restored").exists():
            assert not (control / "failed").exists(), "작업 전용 백업 또는 복원 실패"
            assert time.monotonic() < deadline, "백업·복원 제어 기한 초과"
            time.sleep(.1)
        scoped_dsn = make_conninfo(target_dsn, options=f"-csearch_path={source_schema},public "
            "-cstatement_timeout=15000 -clock_timeout=10000")
        restored_store = PgVectorStore(dsn=scoped_dsn)
        restored = FactCorrectionCoordinator(store=restored_store, git_reader=c.service.git_reader, policies=[c.rule])
        # 원래 결과의 결정 당시 값과 복원된 현재 값을 별도로 반환한다.
        old = restored.result(first)["resolution"]
        assert old["decision_time_current"]["value"] == "ready"
        assert old["current"]["value"] == "failed"
        assert restored.result(second)["resolution"]["current"]["value"] == "failed"
        assert restored.submit(c.request)["duplicate"]
        current_id = old["current"]["memory_id"]
        with pytest.raises(psycopg.errors.CheckViolation, match="managed_fact_resolution_required"):
            with restored_store.transaction() as db:
                db.execute("UPDATE memory_cards SET summary=%s WHERE memory_id=%s", ("우회한 내용", current_id))
        view = GraphFirstResolver(store=restored_store).resolve(project=c.f.scope.project, mode="list")
        assert [item["typed_payload"]["status_value"] for item in view["items"]] == ["failed"]
        late = copy.deepcopy(c.request)
        late["idempotency_key"] = "restore-late-backfill"
        late_change = late["changes"][0]
        late_change.pop("target_memory_id")
        late_change.pop("expected_content_hash")
        late_change.update(target_description=c.rule.target_title, old_value="failed", proposed_value="ready")
        late_id = restored.submit(late, lane="intake")["changes"][0]["task_id"]
        outcome = restored.worker(project=c.f.scope.project, worker_id="restore-late").run_once(task_id=late_id)
        assert outcome["state"] == "resolved", outcome
        late_result = restored.result(late_id)["resolution"]
        assert late_result["action"] == "retain_history" and late_result["current"]["value"] == "failed"
        with restored_store._scope() as db:
            decision_count = db.execute("SELECT count(*) AS n FROM steward_card_decisions").fetchone()["n"]
        assert decision_count == expected_counts["steward_card_decisions"]
    finally:
        if created:
            with psycopg.connect(source_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(target_db)))

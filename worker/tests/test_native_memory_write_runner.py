from __future__ import annotations

import json

from agent_knowledge.session_memory.curation import CurationService
from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.memory_card import build_memory_candidate
from agent_knowledge.session_memory.native_memory_mirror import (
    NativeMemoryMirrorStore,
    session_tag_for,
)
from agent_knowledge.session_memory.native_memory_writer import NativeMemoryMirrorWriter
from agent_knowledge.session_memory.native_memory_write_runner import (
    NativeMemoryMirrorWriteRunner,
    NativeMemoryWriteConfig,
    adapt_card_to_statement,
    main,
    run_native_memory_sync,
)


PROJECT = "workspace-ragflow-advisor"


class _FakeRagflow:
    def __init__(self):
        self.add_calls: list[dict] = []
        self.disable_calls: list[dict] = []
        self.add_envelope = {"code": 0, "data": None}
        self.search_result = {"status_code": 200, "json": {"code": 0, "data": []}}

    def add_message(self, **kwargs):
        self.add_calls.append(kwargs)
        return {"status_code": 200, "json": self.add_envelope}

    def search_messages(self, **kwargs):
        return self.search_result

    def disable_message(self, **kwargs):
        self.disable_calls.append(kwargs)
        return {"status_code": 200, "json": {"code": 0}}


def _setup(tmp_path, config=None):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    store = NativeMemoryMirrorStore(ledger)
    ragflow = _FakeRagflow()
    writer = NativeMemoryMirrorWriter(ragflow=ragflow, store=store, memory_id="mem_main", agent_id="a")
    runner = NativeMemoryMirrorWriteRunner(ledger=ledger, store=store, writer=writer, config=config)
    service = CurationService(ledger)
    return ledger, store, ragflow, runner, service


def _approve(service, statement, ctype="procedural_rule"):
    cand = service.add_candidate(
        build_memory_candidate(
            candidate_type=ctype,
            statement=statement,
            project=PROJECT,
            provider="claude",
            evidence_refs=[{"knowledge_id": "kn", "content_hash": "sha256:c"}],
        )
    )
    return service.approve(cand["candidate_id"], approved_by="ddalkak")


# --- adapter ---


def test_adapt_card_to_statement_maps_fields():
    card = {
        "memory_id": "mem_abc",
        "project": "p",
        "summary": "s",
        "content_hash": "h",
        "card_type": "user_preference",
    }
    stmt = adapt_card_to_statement(card)
    assert stmt.statement_id == "mem_abc"
    assert stmt.brain_id == "/project/p"
    assert stmt.text == "s"
    assert stmt.original_content_hash == "h"
    assert stmt.card_type == "user_preference"
    assert stmt.approved is True


# --- _is_pending (forward 선별 로직) ---


def test_is_pending_logic():
    card = {"memory_id": "m1", "content_hash": "h2"}
    assert NativeMemoryMirrorWriteRunner._is_pending(None, card) is True
    assert NativeMemoryMirrorWriteRunner._is_pending(
        {"status": "active", "original_content_hash": "h1"}, card
    ) is True
    assert NativeMemoryMirrorWriteRunner._is_pending(
        {"status": "active", "original_content_hash": "h2"}, card
    ) is False
    # superseded 행은 재활성화 대상 아님(major #2 수정)
    assert NativeMemoryMirrorWriteRunner._is_pending(
        {"status": "superseded", "original_content_hash": "h1"}, card
    ) is False


# --- forward write ---


def test_forward_writes_unmirrored_card(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    card = _approve(service, "run lint before deploy")

    report = runner.run()

    assert report["scanned"] == 1
    assert report["pending"] == 1
    assert report["written"] == 1
    assert len(ragflow.add_calls) == 1
    tag = session_tag_for(card["memory_id"])
    assert store.get_by_session_tags([tag])[tag]["status"] == "active"


def test_forward_skips_already_mirrored_same_hash(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    _approve(service, "run lint before deploy")
    runner.run()

    report = runner.run()  # 2회차: 이미 mirror active + 동일 hash

    assert len(ragflow.add_calls) == 1  # writer.write 재호출 안 함
    assert report["pending"] == 0
    assert report["written"] == 0


def test_forward_skips_superseded_mirror_row(tmp_path):
    # ledger active 인데 mirror 가 superseded 면 forward 가 재활성화하지 않는다.
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    card = _approve(service, "run lint before deploy")
    runner.run()
    store.mark_superseded(card["memory_id"], superseded_by="x")
    calls_before = len(ragflow.add_calls)

    report = runner.run()

    assert report["pending"] == 0
    assert len(ragflow.add_calls) == calls_before


# --- supersede-sync (reverse) ---


def test_supersede_sync_marks_ledger_disabled_orphan(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    card = _approve(service, "run lint before deploy")
    runner.run()  # mirror active

    service.disable(card["memory_id"], reviewed_by="ddalkak", reason="obsolete")
    report = runner.run()

    assert report["superseded_synced"] == 1
    tag = session_tag_for(card["memory_id"])
    assert store.get_by_session_tags([tag])[tag]["status"] == "superseded"


def test_supersede_sync_ignores_still_active(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    _approve(service, "run lint before deploy")
    runner.run()

    report = runner.run()  # ledger 여전히 active

    assert report["superseded_synced"] == 0


def test_supersede_sync_marks_deleted_card_orphan(tmp_path):
    # ledger card 행 자체가 삭제되면 get_memory_card_state→None → orphan 으로 superseded.
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    card = _approve(service, "run lint before deploy")
    runner.run()  # mirror active
    with ledger._connect() as conn:
        conn.execute("DELETE FROM memory_cards WHERE memory_id = ?", (card["memory_id"],))

    report = runner.run()

    assert report["superseded_synced"] == 1
    tag = session_tag_for(card["memory_id"])
    assert store.get_by_session_tags([tag])[tag]["status"] == "superseded"


def test_forward_counts_add_message_rejected(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    _approve(service, "run lint before deploy")
    ragflow.add_envelope = {"code": 102, "message": "boom"}  # envelope 실패

    report = runner.run()

    assert report["add_message_rejected"] == 1
    assert report["written"] == 0


# --- edge ---


def test_empty_drain(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    report = runner.run()
    assert report == {
        "superseded_synced": 0,
        "scanned": 0,
        "pending": 0,
        "written": 0,
        "duplicate_active": 0,
        "governance_blocked": 0,
        "add_message_rejected": 0,
        "batch_limit_hit": False,
    }


def test_batch_limit_hit(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path, config=NativeMemoryWriteConfig(batch_limit=1))
    _approve(service, "fact one")
    _approve(service, "fact two")

    report = runner.run()

    assert report["batch_limit_hit"] is True
    assert report["scanned"] == 1


# --- dry-run (mutation 0) ---


def test_run_dry_run_skips_forward_mutation(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    _approve(service, "run lint before deploy")

    report = runner.run(dry_run=True)

    assert report["pending"] == 1  # would-write
    assert report["written"] == 0
    assert len(ragflow.add_calls) == 0


def test_run_dry_run_counts_supersede_without_marking(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    card = _approve(service, "run lint before deploy")
    runner.run()  # mirror active
    service.disable(card["memory_id"], reviewed_by="d", reason="r")

    report = runner.run(dry_run=True)

    assert report["superseded_synced"] == 1  # would-supersede
    tag = session_tag_for(card["memory_id"])
    assert store.get_by_session_tags([tag])[tag]["status"] == "active"  # 실제 전이 없음


# --- run_native_memory_sync (write + reconcile 오케스트레이션) ---


def test_run_native_memory_sync_writes_then_reconciles(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    _approve(service, "run lint before deploy")

    report = run_native_memory_sync(ledger=ledger, ragflow=ragflow, memory_id="mem_main")

    assert report["status"] == "ok"
    assert report["dry_run"] is False
    assert report["write"]["written"] == 1
    assert report["reconcile"]["processed"] == 0  # 첫 sync: pending_reconcile 비어있음


def test_run_native_memory_sync_dry_run_skips_reconcile_and_mutation(tmp_path):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    _approve(service, "run lint before deploy")

    report = run_native_memory_sync(ledger=ledger, ragflow=ragflow, memory_id="mem_main", dry_run=True)

    assert report["dry_run"] is True
    assert report["write"]["pending"] == 1
    assert report["write"]["written"] == 0
    assert len(ragflow.add_calls) == 0
    assert report["reconcile"]["status"] == "skipped_dry_run"


def test_native_memory_sync_cli_no_memory_binding_is_noop(tmp_path, capsys):
    rc = main(["--ledger", str(tmp_path / "ledger.sqlite3")])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"status": "not_executed_no_memory_binding"}


def test_native_memory_sync_cli_dry_run_runs_without_network(tmp_path, capsys):
    ledger, store, ragflow, runner, service = _setup(tmp_path)
    _approve(service, "run lint before deploy")

    rc = main([
        "--ledger",
        str(ledger.path),
        "--native-memory-id",
        "mem_main",
        "--dry-run",
    ])

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["dry_run"] is True
    assert report["write"]["pending"] == 1
    assert report["write"]["written"] == 0
    assert report["reconcile"]["status"] == "skipped_dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert len(ragflow.add_calls) == 0


def test_native_memory_sync_cli_live_run_is_fail_closed(tmp_path, capsys):
    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)

    rc = main([
        "--ledger",
        str(ledger_path),
        "--native-memory-id",
        "mem_main",
    ])

    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False

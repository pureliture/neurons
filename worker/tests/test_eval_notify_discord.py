from __future__ import annotations

import json

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory import eval_notify_discord
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService
from agent_knowledge.session_memory.memory_miner import build_memory_card_candidate_from_source_span


PROJECT = "neurons"
PROVIDER = "hermes"


def _candidate(**overrides):
    span = {
        "source_ref": {"source_id": "src_notify"},
        "span_ref": {"span_id": "span_notify"},
        "content_hash": "sha256:notify-card",
        "brain_id": f"/project/{PROJECT}",
        "card_type": "task",
        "scope": "project",
        "project": PROJECT,
        "provider": PROVIDER,
        "title": "notify fixture",
        "redacted_summary": "Notifier fixture summary.",
        "typed_payload": {
            "task_state": "active",
            "next_action": "send aggregate notifier digest",
            "blocker": None,
            "owner_hint": PROVIDER,
            "status": "active",
        },
        "confidence": 0.91,
        "confidence_basis": "operator-approved notify fixture",
    }
    span.update(overrides)
    return build_memory_card_candidate_from_source_span(span, refresh_watermark="notify")


def _insert_eval_run(ledger: Ledger, *, status: str = "pass", run_id: str = "eval_run_notify"):
    return ledger.insert_eval_run(
        {
            "run_id": run_id,
            "status": status,
            "project": PROJECT,
            "provider": PROVIDER,
            "k": 5,
            "query_count": 12,
            "metrics": {
                "query_count": 12,
                "passed_count": 12 if status == "pass" else 11,
                "failed_count": 0 if status == "pass" else 1,
                "avg_recall": 1.0 if status == "pass" else 0.92,
                "avg_precision": 1.0 if status == "pass" else 0.91,
                "expected_count": 13,
                "matched_count": 13 if status == "pass" else 12,
                "retrieved_count": 13,
                "per_query": [{"query_id": "raw-must-not-leak"}],
            },
            "failures": ["raw-must-not-leak"] if status != "pass" else [],
            "network_used": True,
            "mutation_performed": True,
        }
    )


def test_build_discord_digest_uses_aggregate_counts_and_no_raw_fields(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    _insert_eval_run(ledger)
    ledger.upsert_eval_query(
        {
            "query_id": "eval_notify",
            "query_hash": "sha256:eval-notify",
            "query_terms": ["notify"],
            "project": PROJECT,
            "provider": PROVIDER,
            "expected_memory_ids": [],
            "k": 5,
            "min_recall": 1.0,
            "min_precision": 1.0,
            "enabled": True,
        }
    )
    LLMBrainMemoryService(ledger).accept_human_approved_candidate(
        _candidate(), approved_by="ddalkak", decision_id="notify-decision"
    )

    snapshot = eval_notify_discord.collect_notification_snapshot(
        ledger=ledger,
        project=PROJECT,
        provider=PROVIDER,
    )
    payload = eval_notify_discord.build_discord_payload(snapshot)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert snapshot["latest_eval"]["status"] == "pass"
    assert snapshot["enabled_eval_queries"] == 1
    assert snapshot["review_queue_count"] == 0
    assert snapshot["attention_required"] is False
    assert payload["username"] == "neurons-eval-notifier"
    assert "✅ LBrain 운영 점검 — 정상" in payload["content"]
    assert "검색 품질: 통과" in payload["content"]
    assert "평가셋: 12개 중 12개 통과" in payload["content"]
    assert "정확도: recall 1.00 / precision 1.00" in payload["content"]
    assert "사람 검토 대기: 없음" in payload["content"]
    assert "지금 할 일: 없음" in payload["content"]
    assert "raw-must-not-leak" not in text
    assert "memory_id" not in text
    assert "summary" not in text.lower()


def test_notifier_main_send_posts_webhook_and_prints_safe_status(tmp_path, capsys, monkeypatch):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    _insert_eval_run(ledger, status="fail")
    sent = []

    def fake_post(url: str, payload: dict, timeout_seconds: int) -> dict:
        sent.append((url, payload, timeout_seconds))
        return {"status": "sent", "http_status": 204}

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/redacted")
    monkeypatch.setattr(eval_notify_discord, "post_discord_webhook", fake_post)

    rc = eval_notify_discord.main(
        [
            "--ledger",
            str(ledger_path),
            "--project",
            PROJECT,
            "--provider",
            PROVIDER,
            "--send",
        ]
    )

    stdout = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert stdout == {
        "attention_required": True,
        "discord_status": "sent",
        "enabled_eval_queries": 0,
        "latest_eval_status": "fail",
        "message_sent": True,
        "review_queue_count": 0,
        "schema_version": "llm_brain_eval_discord_notifier.v1",
    }
    assert sent and sent[0][0] == "https://discord.example/redacted"
    assert "🚨 LBrain 운영 점검 — 확인 필요" in sent[0][1]["content"]
    assert "검색 품질: 실패" in sent[0][1]["content"]
    assert "평가셋: 12개 중 11개 통과, 1개 실패" in sent[0][1]["content"]
    assert "의미: 자동 검색 평가가 기대 Memory를 일부 못 찾았거나 불필요한 결과를 섞었습니다." in sent[0][1]["content"]
    assert "다음 행동: eval 실패 원인을 확인하세요" in sent[0][1]["content"]
    assert "raw-must-not-leak" not in json.dumps(sent[0][1], sort_keys=True)


def test_notifier_main_send_without_webhook_fails_closed(tmp_path, capsys, monkeypatch):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    _insert_eval_run(ledger)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    rc = eval_notify_discord.main(
        [
            "--ledger",
            str(ledger_path),
            "--project",
            PROJECT,
            "--provider",
            PROVIDER,
            "--send",
        ]
    )

    stdout = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert stdout["status"] == "blocked_missing_webhook"
    assert stdout["message_sent"] is False
    assert "webhook" not in json.dumps(stdout).lower() or "url" not in json.dumps(stdout).lower()

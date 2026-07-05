"""Discord digest notifier for LLM-brain eval/review state.

The notifier is read-only with respect to the neurons ledger. It emits only
aggregate status/counts to stdout and Discord; no raw query text, MemoryCard IDs,
summaries, source refs, hostnames, or webhook URLs are printed.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping

from agent_knowledge.ledger import Ledger

SCHEMA_VERSION = "llm_brain_eval_discord_notifier.v1"
DEFAULT_TIMEOUT_SECONDS = 10


def _json_loads(value: object, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _count_enabled_eval_queries(ledger: Ledger, *, project: str, provider: str) -> int:
    if hasattr(ledger, "list_eval_queries"):
        return len(ledger.list_eval_queries(project=project, provider=provider, enabled_only=True))
    with ledger._connect() as connection:
        row = connection.execute(
            "SELECT count(*) AS n FROM eval_queries WHERE project = ? AND provider = ? AND enabled = 1",
            (project, provider),
        ).fetchone()
    return int(row["n"])


def _latest_eval_run(ledger: Ledger, *, project: str, provider: str) -> dict | None:
    with ledger._connect() as connection:
        row = connection.execute(
            """
            SELECT run_id, status, project, provider, query_count, metrics_json,
                   network_used, mutation_performed, created_at
            FROM eval_runs
            WHERE project = ? AND provider = ?
            ORDER BY created_at DESC, run_id DESC
            LIMIT 1
            """,
            (project, provider),
        ).fetchone()
    if not row:
        return None
    record = dict(row)
    metrics = _json_loads(record.pop("metrics_json", ""), {})
    return {
        "status": str(record.get("status") or "unknown"),
        "project": str(record.get("project") or ""),
        "provider": str(record.get("provider") or ""),
        "query_count": int(record.get("query_count") or 0),
        "metrics": _safe_metrics(metrics),
        "network_used": bool(record.get("network_used")),
        "mutation_performed": bool(record.get("mutation_performed")),
        "created_at_present": bool(record.get("created_at")),
    }


def _safe_metrics(metrics: Mapping[str, Any]) -> dict:
    allowed = (
        "query_count",
        "passed_count",
        "failed_count",
        "avg_recall",
        "avg_precision",
        "expected_count",
        "matched_count",
        "retrieved_count",
    )
    return {key: metrics.get(key) for key in allowed if key in metrics}


def _review_queue_count(ledger: Ledger, *, project: str) -> int:
    if hasattr(ledger, "list_llm_brain_review_queue"):
        return len(ledger.list_llm_brain_review_queue(project=project, limit=100))
    return 0


def collect_notification_snapshot(*, ledger: Ledger, project: str, provider: str) -> dict:
    latest = _latest_eval_run(ledger, project=project, provider=provider)
    enabled_eval_queries = _count_enabled_eval_queries(ledger, project=project, provider=provider)
    review_queue_count = _review_queue_count(ledger, project=project)
    metrics = dict((latest or {}).get("metrics") or {})
    attention_required = (
        latest is None
        or str(latest.get("status") or "") != "pass"
        or int(metrics.get("failed_count") or 0) > 0
        or float(metrics.get("avg_recall") or 0.0) < 1.0
        or float(metrics.get("avg_precision") or 0.0) < 1.0
        or review_queue_count > 0
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "project": project,
        "provider": provider,
        "enabled_eval_queries": enabled_eval_queries,
        "latest_eval": latest,
        "review_queue_count": review_queue_count,
        "attention_required": attention_required,
    }


def build_discord_payload(snapshot: Mapping[str, Any]) -> dict:
    latest = snapshot.get("latest_eval") if isinstance(snapshot.get("latest_eval"), Mapping) else None
    metrics = dict((latest or {}).get("metrics") or {})
    status = str((latest or {}).get("status") or "missing")
    project = str(snapshot.get("project") or "")
    provider = str(snapshot.get("provider") or "")
    icon = "🚨" if snapshot.get("attention_required") else "✅"
    query_count = int(metrics.get("query_count") or (latest or {}).get("query_count") or 0)
    passed_count = int(metrics.get("passed_count") or 0)
    failed_count = int(metrics.get("failed_count") or 0)
    review_queue_count = int(snapshot.get("review_queue_count") or 0)
    enabled_eval_queries = int(snapshot.get("enabled_eval_queries") or 0)
    quality_label = "통과" if status == "pass" and failed_count == 0 else "실패"
    if latest is None:
        quality_label = "결과 없음"
    recall = _format_metric(metrics.get("avg_recall"))
    precision = _format_metric(metrics.get("avg_precision"))
    eval_summary = f"평가셋: {query_count}개 중 {passed_count}개 통과"
    if failed_count:
        eval_summary += f", {failed_count}개 실패"
    review_summary = f"사람 검토 대기: {review_queue_count}건" if review_queue_count else "사람 검토 대기: 없음"
    if not snapshot.get("attention_required"):
        action = "지금 할 일: 없음"
        meaning = "의미: eval 검색 품질과 사람 검토 대기열이 모두 정상입니다."
    elif review_queue_count and failed_count:
        action = "다음 행동: 사람 검토 대기를 먼저 승인/거절하고, 이어서 eval 실패 원인을 확인하세요."
        meaning = "의미: 사람이 판단할 Memory 후보가 있고, 자동 검색 평가도 기대 결과와 어긋났습니다."
    elif review_queue_count:
        action = "다음 행동: 사람 검토 대기 항목을 승인/거절하세요."
        meaning = "의미: accepted/current Memory로 올릴지 사람이 판단할 후보가 있습니다."
    elif failed_count or status != "pass":
        action = "다음 행동: eval 실패 원인을 확인하세요."
        meaning = "의미: 자동 검색 평가가 기대 Memory를 일부 못 찾았거나 불필요한 결과를 섞었습니다."
    else:
        action = "다음 행동: 상태를 확인하세요."
        meaning = "의미: 운영자가 확인해야 하는 신호가 있습니다."
    content = "\n".join(
        [
            f"{icon} LBrain eval 점검 — {'확인 필요' if snapshot.get('attention_required') else '정상'}",
            f"대상: {project}/{provider}",
            f"검색 품질: {quality_label}",
            eval_summary,
            f"정확도: recall {recall} / precision {precision}",
            review_summary,
            meaning,
            action,
            f"참고: 활성 평가 쿼리 {enabled_eval_queries}개, semantic/model lane {'사용' if bool((latest or {}).get('network_used')) else '미사용'}",
        ]
    )
    return {
        "username": "neurons-eval-notifier",
        "content": content[:1900],
        "allowed_mentions": {"parse": []},
    }


def _format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def post_discord_webhook(url: str, payload: dict, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "neurons-eval-notifier/1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 0) or 0)
    except urllib.error.HTTPError as exc:
        return {"status": "http_error", "http_status": int(exc.code)}
    except Exception as exc:  # noqa: BLE001 - sanitize to type only
        return {"status": "error", "error_type": type(exc).__name__}
    return {"status": "sent" if 200 <= status < 300 else "http_error", "http_status": status}


def _safe_stdout_payload(*, snapshot: Mapping[str, Any], message_sent: bool, discord_status: str) -> dict:
    latest = snapshot.get("latest_eval") if isinstance(snapshot.get("latest_eval"), Mapping) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "latest_eval_status": str((latest or {}).get("status") or "missing"),
        "enabled_eval_queries": int(snapshot.get("enabled_eval_queries") or 0),
        "review_queue_count": int(snapshot.get("review_queue_count") or 0),
        "attention_required": bool(snapshot.get("attention_required")),
        "message_sent": bool(message_sent),
        "discord_status": discord_status,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge eval-notify-discord")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--send", action="store_true", help="post to Discord; omit for read-only dry-run")
    parser.add_argument(
        "--only-on-attention",
        action="store_true",
        help="when sending, skip Discord post if latest eval/review state is healthy",
    )
    parser.add_argument("--webhook-env", default="DISCORD_WEBHOOK_URL")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    ledger = Ledger.open_read_only(args.ledger)
    snapshot = collect_notification_snapshot(ledger=ledger, project=args.project, provider=args.provider)
    should_send = bool(args.send) and (not args.only_on_attention or bool(snapshot["attention_required"]))
    if args.send:
        webhook_url = os.environ.get(args.webhook_env, "")
        if not webhook_url:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "blocked_missing_webhook", "message_sent": False}, sort_keys=True))
            return 2
    else:
        webhook_url = ""

    discord_status = "dry_run"
    message_sent = False
    if should_send:
        result = post_discord_webhook(
            webhook_url,
            build_discord_payload(snapshot),
            timeout_seconds=max(1, int(args.timeout_seconds)),
        )
        discord_status = str(result.get("status") or "unknown")
        message_sent = discord_status == "sent"
    elif args.send:
        discord_status = "skipped_no_attention"

    print(
        json.dumps(
            _safe_stdout_payload(snapshot=snapshot, message_sent=message_sent, discord_status=discord_status),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if discord_status in {"dry_run", "sent", "skipped_no_attention"} else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

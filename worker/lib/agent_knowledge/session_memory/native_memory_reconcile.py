"""Option C reconcile: superseded mirror row 를 RAGFlow 에서 실제 은퇴(disable).

ledger 가 정본(supersede = status 변경). reconcile 는 list_pending_reconcile 을 드레인해
각 superseded statement 의 session_tag 에 속한 모든 RAGFlow message(raw + 추출본)를
PUT disable 로 하드 제외시키고 ragflow_disabled_at 을 기록한다(멱등).

dirty_session_memory_sync 의 dirty-drain 선례(list_pending → per-row process → mark,
bounded per-run, fail-soft per row)를 따른다. probe(진단도구)와 별도 모듈(아키텍처 교훈 ②).
라이브 호출은 주입된 ragflow(search_messages/disable_message). 테스트=fake.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..ragflow_client import envelope_failed
from .native_memory_mirror import NativeMemoryMirrorStore

RECONCILE_NATIVE_MEMORY_SCHEMA_VERSION = "agent_knowledge_native_memory_reconcile.v1"


@dataclass(frozen=True)
class NativeMemoryReconcileConfig:
    memory_id: str               # 단일 RAGFlow Memory 바인딩
    batch_limit: int = 100       # list_pending_reconcile(limit)
    max_rows_per_run: int = 100  # 한 run 처리량 상한(dirty-drain bounded)
    # NOTE(라이브 미확인): RAGFlow search_messages 의 top_n 서버 hard cap 값은 검증 사실에 없다.
    # 기본 50 은 보수적이라 안전하나, 서버 cap 을 초과하면 서버가 조용히 cap 을 적용해 일부
    # 추출본이 누락될 수 있다(거짓음성). 누락분은 멱등 재실행 + recall ledger active 백스톱으로 봉인.
    reconcile_top_n: int = 50


def _search_items(search_result: dict) -> list:
    """envelope `json.data`(리스트 직접) 또는 `json.data.chunks` 정상 경로만 추출. 비정상=[]."""
    j = search_result.get("json")
    if not isinstance(j, dict):
        return []
    data = j.get("data", {})
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("chunks"), list):
        return data["chunks"]
    return []


class NativeMemoryReconcileRunner:
    def __init__(self, *, ragflow, store: NativeMemoryMirrorStore, config: NativeMemoryReconcileConfig, now_func=None, log=None):
        self.ragflow = ragflow
        self.store = store
        self.config = config
        self._now = now_func or (lambda: datetime.now(timezone.utc).isoformat())
        self._log = log

    def run(self) -> dict:
        rows = self.store.list_pending_reconcile(limit=self.config.batch_limit)[: self.config.max_rows_per_run]
        report = {
            "schema_version": RECONCILE_NATIVE_MEMORY_SCHEMA_VERSION,
            "status": "ok",
            "processed": 0,
            "disabled_total": 0,
            "rows_fully_disabled": 0,
            "rows_partial": 0,
            "rows_no_match": 0,
            "rows_search_failed": 0,
            "network_used": False,
            "mutation_performed": False,
        }
        for row in rows:
            report["processed"] += 1
            report["network_used"] = True
            outcome = self.reconcile_one(row)
            report["disabled_total"] += outcome.get("disabled", 0)
            if outcome.get("disabled", 0) > 0:
                report["mutation_performed"] = True
            if outcome["ok"]:
                report["rows_fully_disabled"] += 1
            else:
                reason = outcome.get("reason", "")
                if reason == "no_ragflow_match":
                    report["rows_no_match"] += 1
                elif reason == "search_failed":
                    report["rows_search_failed"] += 1
                elif reason == "partial":
                    report["rows_partial"] += 1
        return report

    def reconcile_one(self, row: dict) -> dict:
        session_tag = row["session_tag"]
        search_result = self.ragflow.search_messages(
            query=row.get("search_text", ""),
            memory_id=self.config.memory_id,
            top_n=self.config.reconcile_top_n,
        )
        if envelope_failed(search_result):
            return {"ok": False, "session_tag": session_tag, "matched": 0, "disabled": 0, "reason": "search_failed"}

        # 클라이언트측 session_tag 필터(서버측 session_id 필터 무효, 라이브 사실 #5).
        # 거짓양성 0: 다른 session_tag 의 message 는 절대 disable 하지 않는다.
        matched = [
            it for it in _search_items(search_result)
            if isinstance(it, dict) and it.get("session_id") == session_tag
        ]
        if not matched:
            # search_text 패러프레이즈 누락 가능 → row 미변경, 다음 패스 재시도.
            return {"ok": False, "session_tag": session_tag, "matched": 0, "disabled": 0, "reason": "no_ragflow_match"}

        # session_tag 의 모든 item(raw + 추출본 N) 을 각각 disable(게이트 사실).
        disabled = 0
        all_ok = True
        for it in matched:
            res = self.ragflow.disable_message(
                memory_id=self.config.memory_id,
                message_id=str(it.get("message_id")),
            )
            if envelope_failed(res):
                all_ok = False
            else:
                disabled += 1
        if not all_ok:
            # 일부 disable 실패 → ragflow_disabled_at 미기록(row 잔존, 다음 run 재시도).
            return {"ok": False, "session_tag": session_tag, "matched": len(matched), "disabled": disabled, "reason": "partial"}

        self.store.mark_ragflow_disabled(
            row["statement_id"],
            ragflow_disabled_at=self._now(),
            ragflow_memory_id=self.config.memory_id,
        )
        return {"ok": True, "session_tag": session_tag, "matched": len(matched), "disabled": disabled}

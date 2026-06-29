"""brain.query read-model 어댑터 — ledger 결합 격리 지점.

ledger phase-out 제약(2026-06-11): brain_query.py는 Ledger를 모른다.
이 파일이 유일한 결합 지점이며, F/O 시 이 파일만 신 backend 어댑터로
교체한다(M8.1 recall_read_model.py의 LegacyLedgerRecallReadModel 선례).
"""

from __future__ import annotations

from .native_memory_recall import recall_active_native_memory


class LegacyLedgerBrainReadModel:
    """behavior-preserving ledger 어댑터. BrainReadModel protocol 구현."""

    def __init__(self, ledger):
        self._ledger = ledger

    def get_card_meta(self, card_id: str) -> dict | None:
        return self._ledger.get_memory_card(card_id)

    def list_recent_cards(self, *, project: str, limit: int) -> list[dict]:
        return self._ledger.list_approved_memory_cards(project=project, limit=limit)

    def list_accepted_cards(self, *, project: str, limit: int) -> list[dict]:
        # accepted lane 전체(현재+과거)를 반환한다. drift_explain 같은 history 소비자는 superseded/
        # stale 카드도 필요하다. 현재-권위(current authority) 소비자(persona/context pack)는 자체적으로
        # currentness=current 로 거른다(over-restrict 방지).
        if hasattr(self._ledger, "list_llm_brain_memory_cards"):
            return self._ledger.list_llm_brain_memory_cards(
                project=project, accepted_only=True, limit=limit
            )
        return []

    def list_project_card_counts(self) -> list[tuple[str, int]]:
        # F/O 예정인 Ledger 클래스에 메서드를 추가하지 않기 위해 어댑터에서
        # 직접 질의한다(_connect 공유는 NativeMemoryMirrorStore 선례).
        with self._ledger._connect() as connection:
            rows = connection.execute(
                """
                WITH project_counts AS (
                    SELECT project, COUNT(*) AS n FROM memory_cards
                    WHERE state = 'active' GROUP BY project
                    UNION ALL
                    SELECT project, COUNT(*) AS n FROM llm_brain_memory_cards
                    WHERE lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                      AND approval_state IN ('approved', 'auto_accepted')
                    GROUP BY project
                )
                SELECT project, SUM(n) AS n FROM project_counts
                GROUP BY project ORDER BY project
                """
            ).fetchall()
        return [(str(row["project"] or ""), int(row["n"])) for row in rows]


def build_semantic_recall(*, ledger, ragflow, memory_id: str):
    """(query, brain_id) -> hits 클로저. store는 lazy 생성 —
    read-only ledger 등 구성 예외가 run 시점 fallback으로 흡수되게 한다."""

    def semantic_recall(query: str, brain_id: str) -> list[dict]:
        from .native_memory_mirror import NativeMemoryMirrorStore

        store = NativeMemoryMirrorStore(ledger)
        return recall_active_native_memory(
            ragflow=ragflow, store=store, memory_id=memory_id, query=query, brain_id=brain_id
        )

    return semantic_recall

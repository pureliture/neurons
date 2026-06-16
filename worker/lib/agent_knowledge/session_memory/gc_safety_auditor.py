"""GC Safety Lane seam (Phase A).

비가역 GC(RAGFlow hard delete + tombstone + audit)를 ``IGCSafetyAuditor`` 인터페이스
뒤로 격리한다. GC 스크립트가 audit/tombstone을 이 seam에 위임하면, "audit 없는 삭제"가
구조적으로 어려워지고(인터페이스 한 점에 co-locate), 단위 테스트가 실제 RAGFlow/ledger
없이 seam 상태 전이만 빠르게 검증할 수 있다.

Phase A에서는 seam을 *정의*하고 tombstone/audit 소유권을 이 모듈로 가져온다. 실제 비가역
delete 호출을 seam 경유로 라우팅하는 것(co-location)은 Phase A2(사람 승인 게이트)다.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class AuditContext:
    """비가역 GC 한 건의 전체 audit payload.

    ``Ledger.record_memory_gc_audit``의 14개 입력을 손실 없이 운반한다(typed carrier).
    seam을 통해 ``IGCSafetyAuditor.record_gc_audit``로 전달된다.
    """

    gc_kind: str
    operation: str
    schema_version: str
    mode: str
    knowledge_id: str
    ragflow_document_id: str
    dataset_id: str
    replacement_knowledge_id: str = ""
    dirty_at: str = ""
    snapshot_updated_at: str = ""
    approval_operation: str = ""
    age_gate_seconds: int = 0
    mutated: bool = True


class IGCSafetyAuditor(ABC):
    """비가역 GC의 audit/tombstone을 격리하는 seam."""

    @abstractmethod
    def record_gc_audit(self, ctx: AuditContext) -> dict:
        """비가역 삭제 성공 후 durable append-only audit row를 남긴다."""

    @abstractmethod
    def mark_session_memory_deleted(
        self, knowledge_id: str, *, now_iso: str, operation: str
    ) -> None:
        """session_memory hard delete의 tombstone(knowledge_items metadata)을 기록한다."""


class LedgerGCSafetyAuditor(IGCSafetyAuditor):
    """Ledger-backed 구현. 기존 ``record_memory_gc_audit``와 session_memory tombstone
    UPDATE의 canonical 소유자다(S2 소유권 이동). ``now_iso``는 호출부(runner)가 결정적
    clock으로 넘겨, 특성화 게이트가 byte-identical하게 비교할 수 있다.
    """

    def __init__(self, ledger):
        self._ledger = ledger

    def record_gc_audit(self, ctx: AuditContext) -> dict:
        return self._ledger.record_memory_gc_audit(
            gc_kind=ctx.gc_kind,
            operation=ctx.operation,
            schema_version=ctx.schema_version,
            mode=ctx.mode,
            knowledge_id=ctx.knowledge_id,
            ragflow_document_id=ctx.ragflow_document_id,
            dataset_id=ctx.dataset_id,
            replacement_knowledge_id=ctx.replacement_knowledge_id,
            dirty_at=ctx.dirty_at,
            snapshot_updated_at=ctx.snapshot_updated_at,
            approval_operation=ctx.approval_operation,
            age_gate_seconds=ctx.age_gate_seconds,
            mutated=ctx.mutated,
        )

    def mark_session_memory_deleted(
        self, knowledge_id: str, *, now_iso: str, operation: str
    ) -> None:
        row = self._ledger.get_by_knowledge_id(knowledge_id)
        if not row:
            return
        try:
            metadata = json.loads(str(row.get("metadata_json") or "{}"))
            if not isinstance(metadata, dict):
                metadata = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        metadata["session_memory_gc"] = {
            "status": "deleted",
            "deleted_at": now_iso,
            "operation": operation,
        }
        with self._ledger._connect() as connection:
            connection.execute(
                "UPDATE knowledge_items SET metadata_json = ?, updated_at = ? WHERE knowledge_id = ?",
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now_iso,
                    knowledge_id,
                ),
            )

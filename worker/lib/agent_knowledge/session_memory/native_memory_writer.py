from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..ragflow_client import envelope_code, envelope_failed
from .memory_card import MAX_MEMORY_STATEMENT_CHARS
from .native_memory_governance import governance_tier, mirror_prerequisite_block_reason
from .native_memory_mirror import NativeMemoryMirrorStore, session_tag_for
from .transcript_model import redact_and_bound_text


@dataclass(frozen=True)
class ApprovedStatement:
    statement_id: str
    brain_id: str
    text: str
    original_content_hash: str
    card_type: str = "semantic_fact"   # 기본 low-risk (기존 테스트 호환)
    approved: bool = False
    provenance_status: str = "missing"
    eval_status: str = "missing"


class NativeMemoryMirrorWriter:
    def __init__(
        self,
        *,
        ragflow,                       # RagflowHttpClient (또는 add_message 덕타입). 테스트=fake.
        store: NativeMemoryMirrorStore,
        memory_id: str,                # 단일 str. write() 내부에서 [self.memory_id] 로 감싸 호출.
        agent_id: str,
        user_id: str = "",
    ):
        # ragflow_client.add_message 시그니처는 memory_id: list[str] 이지만 self.memory_id 는
        # 단일 str 로 보관하고, add_message 호출 시점에만 [self.memory_id] 로 감싼다(이중 wrapping 방지).
        self.ragflow = ragflow
        self.store = store
        self.memory_id = memory_id
        self.agent_id = agent_id
        self.user_id = user_id

    def write(self, statement: ApprovedStatement, *, now: datetime | None = None) -> dict:
        tier = governance_tier(statement.card_type)
        tag = session_tag_for(statement.statement_id)
        existing = self.store.get_by_session_tags([tag])
        row = existing.get(tag)
        if (
            row is not None
            and row["status"] == "active"
            and row["original_content_hash"] == statement.original_content_hash
        ):
            return {"written": False, "reason": "duplicate_active"}

        # governance 게이트(dedup 직후·add_message 직전): Memory API mirror 는
        # approved memory card + provenance/eval pass 의 bounded mirror 로만 허용한다.
        # RAGFlow Memory 가 canonical authority 가 되는 경로를 원천 차단한다.
        block_reason = mirror_prerequisite_block_reason(
            approved=statement.approved,
            provenance_status=statement.provenance_status,
            eval_status=statement.eval_status,
        )
        if block_reason:
            return {
                "written": False,
                "reason": block_reason,
                "tier": tier,
                "card_type": statement.card_type,
                "provenance_status": statement.provenance_status,
                "eval_status": statement.eval_status,
            }

        result = self.ragflow.add_message(
            memory_id=[self.memory_id],
            agent_id=self.agent_id,
            session_id=tag,
            user_input=statement.text,
            agent_response="",
            user_id=self.user_id,
        )
        if envelope_failed(result):
            return {
                "written": False,
                "reason": "add_message_rejected",
                "envelope_code": envelope_code(result),
            }

        # reconcile 용 bounded·redacted 원문(결정 #1 A). 이중 redaction 은 멱등(fail-closed).
        search_text = redact_and_bound_text(statement.text, MAX_MEMORY_STATEMENT_CHARS)
        self.store.upsert_statement(
            statement_id=statement.statement_id,
            brain_id=statement.brain_id,
            original_content_hash=statement.original_content_hash,
            search_text=search_text,
            card_type=statement.card_type,
            ragflow_memory_id="",
            now=now,
        )
        return {"written": True, "session_tag": tag, "tier": tier}

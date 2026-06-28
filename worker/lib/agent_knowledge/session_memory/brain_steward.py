"""Brain Steward — agent-facing, proposal-only memory management surface.

Hermes 같은 agent가 brain의 authoritative memory를 직접 바꾸지 않고, 안전하게
관리 후보(candidate)와 proposal(stale / supersede)을 남길 수 있게 하는 얇은 표면이다.

설계 경계(이 파일이 보장하는 invariant):
- neurons ledger가 authority다. accepted + current 인 MemoryCard 만 authoritative memory다.
- proposal tool은 accepted/current MemoryCard 를 만들거나, 덮어쓰거나, 삭제하지 않는다.
  proposal 은 항상 non-accepted lifecycle(candidate / needs_review)로만 ledger 에 남는다.
- read tool(authority pack, review queue)은 raw transcript, raw dataset_id,
  raw document_id, source 원문, secret 을 반환하지 않는다. 안전한 필드만 projection 하고,
  마지막에 forbidden-content scan 으로 fail-closed 검증한다.
- approve / reject / auto_accept 는 restricted 다. 기본 권한에서는 어떤 write 도 하지 않고
  거부한다. human/manual gate 또는 명시적 test-only flag 에서만 위임이 열린다.

이 모듈은 기존 MemoryCard envelope/promotion 모델을 재사용한다. 새 lifecycle 이나 새
validation 규칙을 만들지 않는다.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .llm_brain_service import LLMBrainMemoryService
from .memory_card import (
    _ensure_no_forbidden_content,  # 기존 validator 와 동일한 forbidden-content 규칙 재사용
    validate_memory_card_envelope,
)
from .memory_miner import build_memory_card_candidate_from_source_span
from .memory_promotion import mark_candidate_needs_review

# 승인/현행 lane 정의는 ledger.list_llm_brain_memory_cards 와 동일하게 유지한다.
ACCEPTED_LIFECYCLE_STATES = frozenset({"accepted", "human_accepted", "auto_accepted"})
ACCEPTED_APPROVAL_STATES = frozenset({"approved", "auto_accepted"})
# review queue 에 노출되는 pending proposal lifecycle.
REVIEW_LIFECYCLE_STATES = frozenset({"candidate", "suggested_accept", "needs_review"})

STEWARD_PROPOSAL_PREFIX = "mem_steward_"

# read tool 응답에 절대 들어가면 안 되는 키. 안전한 projection 이면 애초에 등장하지 않지만,
# 회귀 방지를 위해 명시적으로 거른다.
_FORBIDDEN_OUTPUT_KEYS = frozenset(
    {
        "envelope_json",
        "render_text",
        "typed_payload",
        "source_refs",
        "evidence_refs",
        "span_refs",
        "judgment_basis_bundle",
        "dataset_id",
        "dataset_ids",
        "document_id",
        "document_ids",
        "raw",
        "raw_text",
        "raw_transcript",
        "body",
        "content",
        "uri",
        "url",
        "path",
        "token",
        "access_token",
        "secret",
        "api_key",
        "apikey",
        "password",
        "passwd",
        "bearer",
        "cookie",
        "authorization",
        "credential",
    }
)


class StewardPermissionError(PermissionError):
    """restricted tool 이 기본 권한에서 호출됐을 때. write 는 절대 수행되지 않은 상태."""


def _sha16(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _is_accepted(card: Mapping[str, Any]) -> bool:
    return (
        str(card.get("lifecycle_state") or "") in ACCEPTED_LIFECYCLE_STATES
        and str(card.get("approval_state") or "") in ACCEPTED_APPROVAL_STATES
    )


def _text(card: Mapping[str, Any], key: str) -> str:
    """card 필드를 빈 문자열 fallback 으로 안전하게 문자열화한다."""

    return str(card.get(key) or "")


def _str_list(card: Mapping[str, Any], key: str) -> list[str]:
    """card 의 list 필드를 문자열 리스트로 정규화한다(누락 시 빈 리스트)."""

    return [str(item) for item in (card.get(key) or [])]


def _count(card: Mapping[str, Any], key: str) -> int:
    """card 의 list 필드 길이를 반환한다(원소 자체는 출력에 노출하지 않는다)."""

    return len(card.get(key) or [])


def assert_public_safe(payload: Any, field_name: str = "steward_response") -> Any:
    """projection 결과가 raw/private/secret 을 담고 있지 않은지 fail-closed 로 검증한다.

    cards 는 write 시점에 이미 forbidden-content 를 통과했으므로 정상 경로에서는
    절대 raise 되지 않는다. 향후 projection 회귀나 손상된 card 에 대한 안전망이다.
    """

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key) in _FORBIDDEN_OUTPUT_KEYS:
                raise ValueError(f"{field_name}.{key} is not allowed in steward output")
            assert_public_safe(value, f"{field_name}.{key}")
        return payload
    if isinstance(payload, list):
        for index, item in enumerate(payload):
            assert_public_safe(item, f"{field_name}[{index}]")
        return payload
    _ensure_no_forbidden_content(payload, field_name)
    return payload


class BrainStewardService:
    """proposal-only Brain Steward 서비스.

    ledger 는 authority store. allow_restricted 가 True 일 때만 approve/reject/auto_accept
    위임이 열린다(기본 False).
    """

    def __init__(self, ledger, *, allow_restricted: bool = False) -> None:
        self.ledger = ledger
        self.allow_restricted = bool(allow_restricted)

    # ------------------------------------------------------------------ read

    def authority_pack_read(self, *, project: str, limit: int = 8) -> dict:
        """현재 따라야 할 accepted + current authoritative memory pack 만 반환한다."""

        if not project:
            raise ValueError("authority pack requires a project scope")
        cards = self.ledger.list_llm_brain_memory_cards(
            project=project,
            accepted_only=True,
            current_only=True,
            limit=max(int(limit), 1),
        )
        items = [self._authority_item(card) for card in cards if _is_accepted(card)]
        response = {
            "schema_version": "brain_steward_authority_pack.v1",
            "project": project,
            "authority": "ledger_accepted_current",
            "count": len(items),
            "items": items,
        }
        return assert_public_safe(response, "authority_pack")

    def review_queue_list(self, *, project: str = "", limit: int = 20) -> dict:
        """사람이 검토해야 할 candidate / stale / supersede proposal 목록(redacted)."""

        cards = self.ledger.list_llm_brain_review_queue(
            project=project or None,
            limit=max(int(limit), 1),
        )
        items = [self._review_item(card) for card in cards]
        response = {
            "schema_version": "brain_steward_review_queue.v1",
            "project": project or "",
            "count": len(items),
            "items": items,
        }
        return assert_public_safe(response, "review_queue")

    # -------------------------------------------------------------- proposal

    def candidate_create(
        self,
        *,
        source_span: Mapping[str, Any],
        refresh_watermark: str = "steward_candidate",
        mark_needs_review: bool = False,
        review_reason: str = "",
        decision_id: str = "steward_candidate",
    ) -> dict:
        """새 MemoryCard 후보를 만든다. accepted 가 아니라 candidate / needs_review 로만 남는다."""

        card = build_memory_card_candidate_from_source_span(
            source_span,
            refresh_watermark=refresh_watermark,
            mining_reason="steward_candidate",
        )
        kind = "candidate"
        if mark_needs_review:
            review = mark_candidate_needs_review(
                card,
                reason=review_reason or "steward marked candidate for human review",
                decision_id=decision_id,
            )
            card = review["review_card"]
            kind = "needs_review"
        card = self._stamp_proposal(card, kind=kind, target_memory_id="")
        stored = self._persist_proposal(card)
        return self._proposal_result(kind="candidate_create", card=stored)

    def stale_mark(self, *, memory_id: str, reason: str, decision_id: str = "steward_stale") -> dict:
        """특정 MemoryCard 가 stale 하다는 proposal 을 남긴다. 원본은 삭제/수정하지 않는다."""

        if not memory_id:
            raise ValueError("stale mark requires a memory_id")
        if not reason or not reason.strip():
            raise ValueError("stale mark requires a reason")
        target = self.ledger.get_llm_brain_memory_card(memory_id)
        if target is None:
            raise ValueError("unknown target memory card")
        review = mark_candidate_needs_review(target, reason=reason, decision_id=decision_id)
        proposal = review["review_card"]
        proposal["freshness"] = "historical"
        proposal["currentness"] = "stale"
        proposal["derived_from"] = [memory_id]
        proposal = self._stamp_proposal(proposal, kind="stale", target_memory_id=memory_id)
        stored = self._persist_proposal(proposal)
        return self._proposal_result(kind="stale_mark", card=stored, target_memory_id=memory_id)

    def supersede_propose(
        self,
        *,
        old_memory_id: str,
        source_span: Mapping[str, Any],
        refresh_watermark: str = "steward_supersede",
    ) -> dict:
        """기존 MemoryCard 를 새 후보로 대체하자는 proposal. 기존 card 를 즉시 교체하지 않는다."""

        if not old_memory_id:
            raise ValueError("supersede proposal requires an old_memory_id")
        old = self.ledger.get_llm_brain_memory_card(old_memory_id)
        if old is None:
            raise ValueError("unknown target memory card")
        card = build_memory_card_candidate_from_source_span(
            source_span,
            refresh_watermark=refresh_watermark,
            mining_reason="steward_supersede",
        )
        card["supersedes"] = [old_memory_id]
        card = self._stamp_proposal(card, kind="supersede", target_memory_id=old_memory_id)
        stored = self._persist_proposal(card)
        return self._proposal_result(
            kind="supersede_propose", card=stored, target_memory_id=old_memory_id
        )

    # ------------------------------------------------------------ restricted

    def candidate_approve(
        self, *, candidate_memory_id: str, approved_by: str, decision_id: str
    ) -> dict:
        self._guard_restricted("memory_candidate_approve")
        self._guard_writable()
        candidate = self._load_pending_candidate(candidate_memory_id)
        return LLMBrainMemoryService(self.ledger).accept_human_approved_candidate(
            candidate, approved_by=approved_by, decision_id=decision_id
        )

    def candidate_reject(
        self, *, candidate_memory_id: str, rejected_by: str, decision_id: str, reason: str
    ) -> dict:
        self._guard_restricted("memory_candidate_reject")
        self._guard_writable()
        from .memory_promotion import human_reject_memory_card_candidate

        candidate = self._load_pending_candidate(candidate_memory_id)
        rejection = human_reject_memory_card_candidate(
            candidate, rejected_by=rejected_by, decision_id=decision_id, reason=reason
        )
        stored = self.ledger.upsert_llm_brain_memory_card(rejection["rejected_card"])
        return {
            "schema_version": "brain_steward_candidate_rejection.v1",
            "canonical_write_performed": True,
            "rejected_card": stored,
        }

    def candidate_auto_accept(
        self,
        *,
        candidate_memory_id: str,
        evaluation: Mapping[str, Any],
        operator_approval_ref: str,
    ) -> dict:
        self._guard_restricted("memory_candidate_auto_accept")
        self._guard_writable()
        candidate = self._load_pending_candidate(candidate_memory_id)
        return LLMBrainMemoryService(self.ledger).accept_auto_policy_candidate(
            candidate, evaluation, operator_approval_ref=operator_approval_ref
        )

    # -------------------------------------------------------------- internals

    def _guard_restricted(self, tool_name: str) -> None:
        if not self.allow_restricted:
            raise StewardPermissionError(
                f"{tool_name} is restricted and requires a human/manual gate"
            )

    def _guard_writable(self) -> None:
        # 라이브 recall MCP transport 는 read-only ledger 로 서비스를 만든다. 그 위에서
        # proposal/restricted write 를 시도하면 sqlite 가 깨지므로, write 이전에
        # 명확한 메시지로 fail-closed 한다(라이브 enablement 는 writable transport 필요).
        if getattr(self.ledger, "read_only", False):
            raise ValueError(
                "brain steward writes require a writable ledger; this transport is read-only"
            )

    def _load_pending_candidate(self, candidate_memory_id: str) -> dict:
        card = self.ledger.get_llm_brain_memory_card(candidate_memory_id)
        if card is None:
            raise ValueError("unknown candidate memory card")
        if str(card.get("lifecycle_state") or "") not in REVIEW_LIFECYCLE_STATES:
            # accepted / human_rejected / rejected 등은 승인·거부 대상이 아니다.
            raise ValueError("only pending review-queue candidates are eligible")
        kind = str(card.get("steward_proposal_kind") or "candidate")
        if kind in {"stale", "supersede"}:
            # stale 주장과 supersede 제안은 plain approve/reject 대상이 아니다(전용 경로 필요).
            raise ValueError(f"{kind} proposal is not an approvable candidate")
        return card

    def _stamp_proposal(
        self, card: dict, *, kind: str, target_memory_id: str
    ) -> dict:
        """proposal 전용 memory_id 로 재발급해 accepted/miner candidate id 와 분리한다."""

        idempotency_key = str(card.get("idempotency_key") or card.get("memory_id") or "")
        card["memory_id"] = STEWARD_PROPOSAL_PREFIX + _sha16(
            idempotency_key, kind, target_memory_id
        )
        card["steward_proposal_kind"] = kind
        if target_memory_id:
            card["steward_target_memory_id"] = target_memory_id
        return card

    def _persist_proposal(self, card: dict) -> dict:
        self._guard_writable()
        validate_memory_card_envelope(card)
        if _is_accepted(card):
            # 안전망: proposal 은 절대 accepted lane 으로 들어가지 않는다.
            raise ValueError("steward proposal must not be in an accepted lifecycle state")
        # write 전에 review-queue projection 을 fail-closed 로 검증한다. 안전하지 않은
        # proposal 은 ledger 에 한 줄도 남기지 않으며, 따라서 review queue 읽기를
        # 망가뜨리지(DoS) 않는다.
        assert_public_safe(self._review_item(card), "proposal_persist")
        memory_id = str(card["memory_id"])
        existing = self.ledger.get_llm_brain_memory_card(memory_id)
        if existing is not None and _is_accepted(existing):
            # 안전망: accepted card 를 proposal 로 덮어쓰지 않는다.
            raise ValueError("proposal memory_id collides with an accepted card")
        return self.ledger.upsert_llm_brain_memory_card(card)

    def _proposal_result(
        self, *, kind: str, card: Mapping[str, Any], target_memory_id: str = ""
    ) -> dict:
        result = {
            "schema_version": "brain_steward_proposal.v1",
            "proposal_kind": kind,
            "accepted": False,
            "write_performed": True,
            "authoritative_memory_changed": False,
            "proposal": self._review_item(card),
        }
        if target_memory_id:
            result["target_memory_id"] = target_memory_id
        return assert_public_safe(result, "proposal_result")

    def _authority_item(self, card: Mapping[str, Any]) -> dict:
        return {
            "memory_id": _text(card, "memory_id"),
            "card_type": _text(card, "card_type"),
            "scope": _text(card, "scope"),
            "project": _text(card, "project"),
            "provider": _text(card, "provider"),
            "title": _text(card, "title"),
            "summary": _text(card, "summary"),
            "lifecycle_state": _text(card, "lifecycle_state"),
            "approval_state": _text(card, "approval_state"),
            "freshness": _text(card, "freshness"),
            "currentness": _text(card, "currentness"),
            "governance_tier": _text(card, "governance_tier"),
            "confidence": card.get("confidence"),
            "confidence_basis": _text(card, "confidence_basis"),
            "supersedes": _str_list(card, "supersedes"),
            "superseded_by": _str_list(card, "superseded_by"),
            "source_ref_count": _count(card, "source_refs"),
            "evidence_hash_count": _count(card, "evidence_hashes"),
        }

    def _review_item(self, card: Mapping[str, Any]) -> dict:
        capsule = card.get("reason_capsule")
        reason = ""
        if isinstance(capsule, Mapping):
            reason = str(capsule.get("model_reason") or "")
        return {
            "memory_id": _text(card, "memory_id"),
            "proposal_kind": str(card.get("steward_proposal_kind") or "candidate"),
            "target_memory_id": _text(card, "steward_target_memory_id"),
            "card_type": _text(card, "card_type"),
            "scope": _text(card, "scope"),
            "project": _text(card, "project"),
            "provider": _text(card, "provider"),
            "title": _text(card, "title"),
            "summary": _text(card, "summary"),
            "lifecycle_state": _text(card, "lifecycle_state"),
            "judgment_state": _text(card, "judgment_state"),
            "approval_state": _text(card, "approval_state"),
            "currentness": _text(card, "currentness"),
            "freshness": _text(card, "freshness"),
            "governance_tier": _text(card, "governance_tier"),
            "confidence": card.get("confidence"),
            "reason": reason,
            "supersedes": _str_list(card, "supersedes"),
            "source_ref_count": _count(card, "source_refs"),
            "evidence_hash_count": _count(card, "evidence_hashes"),
        }

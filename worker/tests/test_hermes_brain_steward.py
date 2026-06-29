"""Hermes-facing Brain Steward behavior: proposer attribution (M2), Hermes-role
restricted denial (M4), and read no-leak guards (M3).

기존 invariant은 test_brain_steward.py가 다룬다. 이 파일은 Hermes provider 관점의
귀속/권한/누설 회귀를 고정한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_server import (
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
    MEMORY_CANDIDATE_REJECT_TOOL_NAME,
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
    MEMORY_STALE_COMMIT_TOOL_NAME,
    MEMORY_SUPERSEDE_COMMIT_TOOL_NAME,
    STEWARD_RESTRICTED_TOOL_NAMES,
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
    dispatch_tool_call,
)
from agent_knowledge.session_memory.brain_steward import BrainStewardService

PROJECT = "workspace-steward"


def _service(tmp_path: Path, *, allow_restricted: bool = False) -> KnowledgeSearchService:
    return KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_restricted_steward=allow_restricted,
    )


def _text(tool_result: dict) -> dict:
    return json.loads(tool_result["content"][0]["text"])


def _ledger(tmp_path: Path) -> Ledger:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return Ledger(private / "ledger.sqlite")


def _span(**overrides) -> dict:
    span = {
        "source_ref": {"source_id": "src_steward"},
        "span_ref": {"span_id": "span_steward"},
        "content_hash": "sha256:steward-card",
        "card_type": "preference",
        "scope": "project",
        "project": PROJECT,
        "provider": "hermes",
        "title": "Korean response preference",
        "redacted_summary": "한국어로 응답한다",
        "typed_payload": {
            "preference": "한국어로 응답한다",
            "explicitness": "explicit",
            "repeated_count": 1,
            "confirmation_status": "confirmed",
            "applies_to": "natural_language_response",
        },
        "confidence": 0.9,
        "confidence_basis": "human-approved preference",
    }
    span.update(overrides)
    return span


# ------------------------------------------------------------------- M2 proposer


def test_proposer_is_recorded_on_candidate_and_surfaced_in_queue(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)

    result = steward.candidate_create(source_span=_span(), proposer="hermes")

    assert result["proposal"]["proposed_by"] == "hermes"
    queue = steward.review_queue_list()
    item = next(i for i in queue["items"] if i["memory_id"] == result["proposal"]["memory_id"])
    assert item["proposed_by"] == "hermes"


def test_proposer_defaults_to_unspecified_when_omitted(tmp_path):
    steward = BrainStewardService(_ledger(tmp_path))
    result = steward.candidate_create(source_span=_span())
    assert result["proposal"]["proposed_by"] == "unspecified"


def test_proposer_is_normalized(tmp_path):
    steward = BrainStewardService(_ledger(tmp_path))
    result = steward.candidate_create(source_span=_span(), proposer="  Hermes ")
    assert result["proposal"]["proposed_by"] == "hermes"


def test_stale_and_supersede_record_proposer(tmp_path):
    from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService
    from agent_knowledge.session_memory.memory_miner import (
        build_memory_card_candidate_from_source_span,
    )

    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    candidate = build_memory_card_candidate_from_source_span(_span(), refresh_watermark="test")
    accepted = LLMBrainMemoryService(ledger).accept_human_approved_candidate(
        candidate, approved_by="ddalkak", decision_id="d0"
    )["accepted_card"]

    stale = steward.stale_mark(
        memory_id=accepted["memory_id"], reason="근거 교체로 stale", proposer="hermes"
    )
    assert stale["proposal"]["proposed_by"] == "hermes"

    superseded = steward.supersede_propose(
        old_memory_id=accepted["memory_id"],
        source_span=_span(content_hash="sha256:replacement", redacted_summary="영어로 응답한다"),
        proposer="hermes",
    )
    assert superseded["proposal"]["proposed_by"] == "hermes"


def test_proposer_flows_through_mcp_dispatch(tmp_path):
    service = _service(tmp_path)
    created = _text(
        dispatch_tool_call(
            {
                "name": MEMORY_CANDIDATE_CREATE_TOOL_NAME,
                "arguments": {**_span(), "proposer": "hermes"},
            },
            service,
        )
    )
    assert created["proposal"]["proposed_by"] == "hermes"
    queue = _text(
        dispatch_tool_call(
            {"name": MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME, "arguments": {"project": PROJECT}},
            service,
        )
    )
    item = next(i for i in queue["items"] if i["memory_id"] == created["proposal"]["memory_id"])
    assert item["proposed_by"] == "hermes"


def test_proposed_by_does_not_leak_raw_or_private(tmp_path):
    # 안전망: proposer 라벨도 forbidden-content fail-closed 를 통과해야 한다.
    steward = BrainStewardService(_ledger(tmp_path))
    result = steward.candidate_create(source_span=_span(), proposer="hermes")
    serialized = json.dumps(steward.review_queue_list(), ensure_ascii=False)
    assert "/Users/" not in serialized
    assert "steward_proposed_by" not in serialized  # 내부 키는 노출 금지, proposed_by 만 노출
    assert result["proposal"]["proposed_by"] == "hermes"


# ----------------------------------------------------- M4 restricted Hermes role


def test_hermes_default_role_denies_all_restricted_tools(tmp_path):
    # Hermes 가 연결하는 기본 transport(allow_restricted_steward=False)에서 restricted
    # 도구(approve/reject/auto_accept/supersede_commit/stale_commit)는 모두 거부되고
    # 어떤 write 도 일어나지 않는다.
    service = _service(tmp_path)  # default: allow_restricted=False (Hermes role)
    created = _text(
        dispatch_tool_call(
            {"name": MEMORY_CANDIDATE_CREATE_TOOL_NAME, "arguments": {**_span(), "proposer": "hermes"}},
            service,
        )
    )
    candidate_id = created["proposal"]["memory_id"]

    restricted_args = {
        MEMORY_CANDIDATE_APPROVE_TOOL_NAME: {
            "candidate_memory_id": candidate_id,
            "approved_by": "hermes",
            "decision_id": "d1",
        },
        MEMORY_CANDIDATE_REJECT_TOOL_NAME: {
            "candidate_memory_id": candidate_id,
            "rejected_by": "hermes",
            "decision_id": "d1",
            "reason": "no",
        },
        MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME: {
            "candidate_memory_id": candidate_id,
            "operator_approval_ref": "op",
            "evaluation": {"ok": True},
        },
        MEMORY_SUPERSEDE_COMMIT_TOOL_NAME: {
            "proposal_memory_id": candidate_id,
            "approved_by": "hermes",
            "decision_id": "d1",
        },
        MEMORY_STALE_COMMIT_TOOL_NAME: {
            "proposal_memory_id": candidate_id,
            "approved_by": "hermes",
            "decision_id": "d1",
        },
    }
    for name in STEWARD_RESTRICTED_TOOL_NAMES:
        denied = _text(dispatch_tool_call({"name": name, "arguments": restricted_args[name]}, service))
        assert denied["permission"] == "denied"
        assert denied["write_performed"] is False
        assert denied["authoritative_memory_changed"] is False

    # candidate 는 여전히 non-accepted 다(어떤 restricted write 도 없었다).
    card = service.ledger.get_llm_brain_memory_card(candidate_id)
    assert card["lifecycle_state"] not in {"accepted", "human_accepted", "auto_accepted"}


def test_restricted_write_response_is_public_safe(tmp_path):
    # human-gate 가 열린(allow_restricted=True) 경우에도 restricted write 응답은 raw/private
    # 필드를 노출하지 않고 안전 projection 으로 반환된다(C4).
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    created = steward.candidate_create(source_span=_span(), proposer="hermes")
    promoted = steward.candidate_approve(
        candidate_memory_id=created["proposal"]["memory_id"],
        approved_by="ddalkak",
        decision_id="d_ok",
    )
    assert promoted["canonical_write_performed"] is True
    assert promoted["accepted_card"]["memory_id"]
    serialized = json.dumps(promoted, ensure_ascii=False)
    for forbidden in ("source_refs", "typed_payload", "render_text", "envelope_json", "evidence_refs"):
        assert forbidden not in serialized


def test_safe_restricted_result_drops_nested_application_card(tmp_path):
    # auto_accept 성공 경로는 top-level accepted_card 외에 application.accepted_card(full
    # MemoryCard)도 담는다. nested full card 가 projection 누락되면 assert_public_safe 가
    # raise 해 write 는 됐는데 호출은 실패처럼 보인다 — application 을 떨궈 fail-closed 회피.
    steward = BrainStewardService(_ledger(tmp_path), allow_restricted=True)
    raw = {
        "schema_version": "llm_brain_auto_acceptance_commit.v1",
        "canonical_write_performed": True,
        "accepted_card": {"memory_id": "m1", "lifecycle_state": "auto_accepted"},
        "application": {
            "status": "auto_accepted",
            "accepted_card": {
                "memory_id": "m1",
                "typed_payload": {"k": "v"},
                "source_refs": [{"source_id": "s"}],
            },
        },
    }
    safe = steward._safe_restricted_result(raw)
    serialized = json.dumps(safe, ensure_ascii=False)
    for forbidden in ("source_refs", "typed_payload"):
        assert forbidden not in serialized
    assert "application" not in safe
    assert safe["accepted_card"]["memory_id"] == "m1"


# ------------------------------------------------------------ M3 Korean round-trip


def test_korean_free_text_round_trips_through_proposal(tmp_path):
    # 프로젝트 기본이 한국어이므로 redaction 이 한글 Unicode 를 훼손하지 않아야 한다.
    steward = BrainStewardService(_ledger(tmp_path))
    korean_title = "한국어 응답 선호 규칙"
    korean_summary = "사용자는 모든 자연어 응답을 한국어로 받기를 원한다"
    result = steward.candidate_create(
        source_span=_span(
            title=korean_title,
            redacted_summary=korean_summary,
            content_hash="sha256:korean-roundtrip",
            typed_payload={
                "preference": korean_summary,
                "explicitness": "explicit",
                "repeated_count": 1,
                "confirmation_status": "confirmed",
                "applies_to": "natural_language_response",
            },
        ),
        proposer="hermes",
    )
    proposal = result["proposal"]
    assert proposal["title"] == korean_title
    assert proposal["summary"] == korean_summary
    # review queue projection 도 동일하게 안전 통과 + 한글 보존.
    queue = steward.review_queue_list()
    qitem = next(i for i in queue["items"] if i["memory_id"] == proposal["memory_id"])
    assert qitem["title"] == korean_title
    assert qitem["summary"] == korean_summary

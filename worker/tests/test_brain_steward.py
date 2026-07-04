from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_server import (
    MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
    MEMORY_CANDIDATE_CREATE_TOOL_NAME,
    MEMORY_CANDIDATE_REJECT_TOOL_NAME,
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
    MEMORY_STALE_COMMIT_TOOL_NAME,
    MEMORY_STALE_MARK_TOOL_NAME,
    MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
    STEWARD_RESTRICTED_TOOL_NAMES,
    BrainStewardService,
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
    StewardPermissionError,
    dispatch_tool_call,
    list_tools,
)
from agent_knowledge.session_memory.brain_steward import assert_public_safe
from agent_knowledge.session_memory.llm_brain_service import LLMBrainMemoryService
from agent_knowledge.session_memory.memory_miner import (
    build_memory_card_candidate_from_source_span,
)

PROJECT = "workspace-steward"


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


def _accept_card(ledger: Ledger, **span_overrides) -> dict:
    candidate = build_memory_card_candidate_from_source_span(
        _span(**span_overrides), refresh_watermark="test"
    )
    result = LLMBrainMemoryService(ledger).accept_human_approved_candidate(
        candidate, approved_by="ddalkak", decision_id="decision_steward"
    )
    return result["accepted_card"]


def _service(tmp_path: Path, *, allow_restricted: bool = False) -> KnowledgeSearchService:
    return KnowledgeSearchService(
        ledger=_ledger(tmp_path),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_restricted_steward=allow_restricted,
    )


def _text(tool_result: dict) -> dict:
    return json.loads(tool_result["content"][0]["text"])


# --------------------------------------------------------------- tool surface


def test_review_lifecycle_states_single_source():
    from agent_knowledge.session_memory import brain_steward, memory_card

    assert memory_card.REVIEW_LIFECYCLE_STATES == frozenset(
        {"candidate", "suggested_accept", "needs_review"}
    )
    # service 와 model 이 정의를 공유한다(ledger 필터와 드리프트 없음).
    assert brain_steward.REVIEW_LIFECYCLE_STATES is memory_card.REVIEW_LIFECYCLE_STATES


def test_review_queue_lists_only_review_lifecycles(tmp_path):
    from agent_knowledge.session_memory.memory_card import REVIEW_LIFECYCLE_STATES

    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    _accept_card(ledger)  # accepted card 는 큐에 나오면 안 된다
    steward.candidate_create(source_span=_span(content_hash="sha256:q"))
    rows = ledger.list_llm_brain_review_queue(project=PROJECT, limit=50)
    assert rows  # candidate 가 존재한다
    for card in rows:
        assert card["lifecycle_state"] in REVIEW_LIFECYCLE_STATES


def test_list_tools_exposes_steward_surface():
    tools = {tool["name"]: tool for tool in list_tools()}
    for name in (
        MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
        MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
        MEMORY_CANDIDATE_CREATE_TOOL_NAME,
        MEMORY_STALE_MARK_TOOL_NAME,
        MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
        *STEWARD_RESTRICTED_TOOL_NAMES,
    ):
        assert name in tools
        assert tools[name]["inputSchema"]["type"] == "object"
    # restricted tool 은 명확히 표시된다.
    for name in STEWARD_RESTRICTED_TOOL_NAMES:
        assert "restricted" in tools[name]["description"]


# -------------------------------------------------------------- proposal-only


def test_candidate_create_does_not_create_accepted_memory(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)

    result = steward.candidate_create(source_span=_span())

    assert result["accepted"] is False
    assert result["authoritative_memory_changed"] is False
    proposal_id = result["proposal"]["memory_id"]
    assert proposal_id.startswith("mem_steward_")

    # accepted/current authoritative lane 은 비어 있어야 한다.
    assert ledger.list_llm_brain_memory_cards(
        project=PROJECT, accepted_only=True, current_only=True, limit=50
    ) == []
    # candidate 는 review queue 에서만 보인다.
    queue_ids = [item["memory_id"] for item in steward.review_queue_list()["items"]]
    assert proposal_id in queue_ids


def test_candidate_create_is_idempotent(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    first = steward.candidate_create(source_span=_span())
    second = steward.candidate_create(source_span=_span())
    assert first["proposal"]["memory_id"] == second["proposal"]["memory_id"]
    assert len(steward.review_queue_list()["items"]) == 1


def test_stale_mark_does_not_delete_or_mutate_target(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    target = _accept_card(ledger)
    target_id = target["memory_id"]

    result = steward.stale_mark(memory_id=target_id, reason="근거 문서가 교체되어 stale")

    # target 은 그대로 살아남는다.
    reloaded = ledger.get_llm_brain_memory_card(target_id)
    assert reloaded is not None
    assert reloaded["lifecycle_state"] == target["lifecycle_state"]
    assert reloaded["currentness"] == "current"
    # proposal 은 별도의 non-authoritative record 다.
    proposal = result["proposal"]
    assert proposal["memory_id"] != target_id
    assert proposal["proposal_kind"] == "stale"
    assert proposal["target_memory_id"] == target_id
    assert proposal["currentness"] == "stale"
    # authority pack 은 손대지 않은 target 을 그대로 반환한다.
    pack_ids = [item["memory_id"] for item in steward.authority_pack_read(project=PROJECT)["items"]]
    assert target_id in pack_ids
    assert proposal["memory_id"] not in pack_ids


def test_stale_mark_unknown_target_is_rejected(tmp_path):
    steward = BrainStewardService(_ledger(tmp_path))
    with pytest.raises(ValueError):
        steward.stale_mark(memory_id="mem_missing", reason="x")


def test_stale_proposal_is_reference_only_not_a_target_copy(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    target = _accept_card(ledger)
    target_id = target["memory_id"]

    result = steward.stale_mark(memory_id=target_id, reason="근거 문서 교체로 stale")
    stored = ledger.get_llm_brain_memory_card(result["proposal"]["memory_id"])

    # proposal 은 target 의 raw ref / typed_payload 를 복제하지 않는다.
    assert stored["card_type"] == "status"
    assert stored["source_refs"] == []
    assert stored["evidence_refs"] == []
    assert stored["evidence_hashes"] == []
    assert "preference" not in stored["typed_payload"]  # target 의 preference payload 미복제
    assert stored["typed_payload"]["status_value"] == "stale"
    assert stored["typed_payload"]["current_authority"] == target_id
    assert stored["derived_from"] == [target_id]
    assert stored["currentness"] == "stale"


def test_stale_proposal_id_is_idempotent_per_target_and_reason(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    target_id = _accept_card(ledger)["memory_id"]

    a1 = steward.stale_mark(memory_id=target_id, reason="reason one")["proposal"]["memory_id"]
    a2 = steward.stale_mark(memory_id=target_id, reason="reason one")["proposal"]["memory_id"]
    b = steward.stale_mark(memory_id=target_id, reason="a different reason")["proposal"]["memory_id"]
    assert a1 == a2  # 같은 (target, reason) → 같은 proposal
    assert a1 != b  # 다른 reason → 별개 proposal(reason 이 조용히 덮어써지지 않음)


def test_supersede_propose_does_not_replace_target(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    target = _accept_card(ledger)
    target_id = target["memory_id"]

    result = steward.supersede_propose(
        old_memory_id=target_id,
        source_span=_span(
            content_hash="sha256:steward-replacement",
            redacted_summary="이제는 영어로 응답한다",
            typed_payload={
                "preference": "영어로 응답한다",
                "explicitness": "explicit",
                "repeated_count": 1,
                "confirmation_status": "confirmed",
                "applies_to": "natural_language_response",
            },
        ),
    )

    # old card 는 그대로이며 여전히 current/accepted 다.
    reloaded = ledger.get_llm_brain_memory_card(target_id)
    assert reloaded["currentness"] == "current"
    assert reloaded["lifecycle_state"] == target["lifecycle_state"]
    # 교체 후보는 supersede 의도를 기록한 non-accepted proposal 이다.
    proposal = result["proposal"]
    assert proposal["proposal_kind"] == "supersede"
    assert proposal["target_memory_id"] == target_id
    assert target_id in proposal["supersedes"]
    accepted_ids = [
        card["memory_id"]
        for card in ledger.list_llm_brain_memory_cards(accepted_only=True, current_only=True, limit=50)
    ]
    assert proposal["memory_id"] not in accepted_ids


# ------------------------------------------------------------------- read safety


def test_authority_pack_contains_only_accepted_current(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    accepted = _accept_card(ledger)

    # candidate(non-accepted)와 superseded(accepted-but-not-current) 카드.
    steward.candidate_create(source_span=_span(content_hash="sha256:cand"))
    from agent_knowledge.session_memory.memory_promotion import commit_supersession

    demoted = commit_supersession(_accept_card(ledger, content_hash="sha256:old"), superseded_by=accepted["memory_id"])
    ledger.upsert_llm_brain_memory_card(demoted)

    pack = steward.authority_pack_read(project=PROJECT)
    ids = [item["memory_id"] for item in pack["items"]]
    assert accepted["memory_id"] in ids
    assert demoted["memory_id"] not in ids
    for item in pack["items"]:
        assert item["lifecycle_state"] in {"accepted", "human_accepted", "auto_accepted"}
        assert item["currentness"] == "current"


def test_review_queue_returns_no_raw_or_private(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    steward.candidate_create(source_span=_span())
    queue = steward.review_queue_list()
    serialized = json.dumps(queue, ensure_ascii=False)

    assert "/Users/" not in serialized
    assert "raw_transcript" not in serialized
    assert "envelope_json" not in serialized
    for item in queue["items"]:
        # raw locator/payload 가 아니라 안전한 reference metadata 만.
        assert "source_refs" not in item
        assert "typed_payload" not in item
        assert "render_text" not in item
        assert isinstance(item["source_ref_count"], int)


def test_assert_public_safe_fails_closed_on_forbidden_content():
    assert_public_safe({"summary": "정상적인 redacted summary"})
    with pytest.raises(ValueError):
        assert_public_safe({"summary": "secret token=live-abc123"})
    with pytest.raises(ValueError):
        assert_public_safe({"note": "/Users/example/.claude/private/x"})
    with pytest.raises(ValueError):
        assert_public_safe({"note": "/Volumes/usb/private/x"})
    with pytest.raises(ValueError):
        assert_public_safe({"dataset_id": "abc"})


def test_poisoned_title_or_basis_is_rejected_at_envelope_validation():
    from agent_knowledge.session_memory.memory_card import validate_memory_card_envelope

    base = build_memory_card_candidate_from_source_span(_span(), refresh_watermark="t")
    for field, value in (
        ("title", "/Users/example/.ssh/id_rsa"),
        ("title", "/Volumes/usb/secret"),
        ("confidence_basis", "see /Users/example/.ssh/id_rsa for proof"),
        ("confidence_basis", "Bearer abc123def456ghi789"),
    ):
        poisoned = dict(base)
        poisoned[field] = value
        with pytest.raises(ValueError):
            validate_memory_card_envelope(poisoned)


def test_candidate_create_with_poisoned_field_writes_nothing_and_does_not_dos_queue(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)

    with pytest.raises(ValueError):
        steward.candidate_create(source_span=_span(title="/Volumes/usb/private/x"))

    # row 가 안 써져서 project review queue 는 깨끗하게 읽힌다.
    assert ledger.list_llm_brain_review_queue(project=PROJECT, limit=50) == []
    clean = steward.candidate_create(source_span=_span(content_hash="sha256:clean"))
    queue = steward.review_queue_list(project=PROJECT)
    assert [item["memory_id"] for item in queue["items"]] == [clean["proposal"]["memory_id"]]


# --------------------------------------------------------------- restricted gate


def test_mcp_cli_review_commit_flag_enables_only_review_commit(tmp_path, monkeypatch):
    from agent_knowledge import cli as cli_module

    ledger = _ledger(tmp_path)
    ledger_path = ledger.path
    parser = argparse.ArgumentParser()
    cli_module._add_recall_service_arguments(parser)

    args = parser.parse_args([
        "--ledger",
        str(ledger_path),
        "--allow-steward-proposals",
        "--allow-steward-review-commit",
    ])
    monkeypatch.setattr(cli_module, "build_index_client", lambda: DisabledRetiredIndexBridgeClient())
    monkeypatch.setattr(cli_module, "build_graph_adapter_from_env", lambda **_: None)

    service = cli_module._build_recall_service(args)

    assert service.allow_restricted_steward is True
    assert service.allow_steward_auto_accept is False


def test_restricted_tools_blocked_by_default_service(tmp_path):
    service = _service(tmp_path)  # allow_restricted_steward 기본값 False
    created = _text(
        dispatch_tool_call(
            {"name": MEMORY_CANDIDATE_CREATE_TOOL_NAME, "arguments": _span()},
            service,
        )
    )
    candidate_id = created["proposal"]["memory_id"]

    denied = _text(
        dispatch_tool_call(
            {
                "name": MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
                "arguments": {
                    "candidate_memory_id": candidate_id,
                    "approved_by": "hermes",
                    "decision_id": "d1",
                },
            },
            service,
        )
    )
    assert denied["permission"] == "denied"
    assert denied["write_performed"] is False
    # write 가 없었다: candidate 는 여전히 non-accepted 다.
    card = service.ledger.get_llm_brain_memory_card(candidate_id)
    assert card["lifecycle_state"] not in {"accepted", "human_accepted", "auto_accepted"}


def test_restricted_methods_raise_without_flag(tmp_path):
    steward = BrainStewardService(_ledger(tmp_path), allow_restricted=False)
    with pytest.raises(StewardPermissionError):
        steward.candidate_approve(candidate_memory_id="x", approved_by="a", decision_id="d")
    with pytest.raises(StewardPermissionError):
        steward.candidate_reject(candidate_memory_id="x", rejected_by="a", decision_id="d", reason="r")
    with pytest.raises(StewardPermissionError):
        steward.candidate_auto_accept(candidate_memory_id="x", evaluation={}, operator_approval_ref="op")


def test_restricted_approve_promotes_only_when_explicitly_enabled(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    created = steward.candidate_create(source_span=_span())
    candidate_id = created["proposal"]["memory_id"]

    promoted = steward.candidate_approve(
        candidate_memory_id=candidate_id, approved_by="ddalkak", decision_id="d_ok"
    )
    assert promoted["canonical_write_performed"] is True
    # 이제 accepted authoritative memory 이며 authority pack 에 나타난다.
    pack_ids = [item["memory_id"] for item in steward.authority_pack_read(project=PROJECT)["items"]]
    assert promoted["accepted_card"]["memory_id"] in pack_ids


def test_auto_accept_needs_its_own_capability_not_review_commit(tmp_path):
    ledger = _ledger(tmp_path)
    # review_commit 켜고 auto_accept 끔 → auto_accept 는 계속 막힌다.
    review_only = BrainStewardService(ledger, allow_restricted=True, allow_auto_accept=False)
    cand_id = review_only.candidate_create(source_span=_span())["proposal"]["memory_id"]
    with pytest.raises(StewardPermissionError):
        review_only.candidate_auto_accept(
            candidate_memory_id=cand_id, evaluation={}, operator_approval_ref="op"
        )
    # approve(review_commit capability)는 같은 flag 에서 허용된다.
    review_only.candidate_approve(candidate_memory_id=cand_id, approved_by="op", decision_id="d")

    # auto_accept 를 명시적으로 켜면 gate 를 통과한다(permission error 없음).
    full = BrainStewardService(ledger, allow_restricted=True, allow_auto_accept=True)
    cand2 = full.candidate_create(source_span=_span(content_hash="sha256:aa"))["proposal"]["memory_id"]
    result = full.candidate_auto_accept(
        candidate_memory_id=cand2, evaluation={}, operator_approval_ref="op"
    )
    assert isinstance(result, dict)  # gate 통과(차단 정책 결과일 수 있고 raise 아님)


def test_commits_write_audit_feedback_records(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)

    # stale commit 은 audit feedback record 를 남긴다.
    stale_target = _accept_card(ledger)["memory_id"]
    stale_prop = steward.stale_mark(memory_id=stale_target, reason="stale 사유")["proposal"]["memory_id"]
    steward.stale_commit(proposal_memory_id=stale_prop, approved_by="op", decision_id="d_stale")
    assert ledger.list_llm_brain_feedback_records(limit=100)

    # reject 도 feedback record 를 남긴다.
    cand_id = steward.candidate_create(source_span=_span(content_hash="sha256:rj"))["proposal"]["memory_id"]
    steward.candidate_reject(candidate_memory_id=cand_id, rejected_by="op", decision_id="d_rej", reason="no")
    rej_records = [r for r in ledger.list_llm_brain_feedback_records(limit=100) if r["final_status"] == "rejected"]
    assert rej_records


def test_knowledge_service_auto_accept_flag_defaults_closed(tmp_path):
    service = _service(tmp_path)  # auto_accept flag 없음
    steward = service.brain_steward()
    assert steward.allow_auto_accept is False
    assert steward.allow_review_commit is False


def test_proposal_persist_guard_refuses_to_overwrite_accepted(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    accepted = _accept_card(ledger)
    # accepted card 와 memory_id 가 충돌하는 proposal 을 위조한다.
    forged = build_memory_card_candidate_from_source_span(_span(content_hash="sha256:forge"), refresh_watermark="t")
    forged["memory_id"] = accepted["memory_id"]
    with pytest.raises(ValueError):
        steward._persist_proposal(forged)
    # accepted card 는 그대로다.
    assert ledger.get_llm_brain_memory_card(accepted["memory_id"])["lifecycle_state"] == accepted["lifecycle_state"]


def test_proposal_write_fails_closed_on_read_only_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.read_only = True  # 라이브 recall MCP transport 를 흉내
    steward = BrainStewardService(ledger, allow_restricted=True)
    with pytest.raises(ValueError):
        steward.candidate_create(source_span=_span())
    with pytest.raises(ValueError):
        steward.candidate_approve(candidate_memory_id="x", approved_by="a", decision_id="d")


def test_assert_public_safe_blocks_credential_output_keys():
    for key in ("password", "passwd", "authorization", "bearer", "cookie", "api_key"):
        with pytest.raises(ValueError):
            assert_public_safe({key: "anything"})


def test_stale_and_rejected_proposals_are_not_approvable(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    target = _accept_card(ledger)

    stale = steward.stale_mark(memory_id=target["memory_id"], reason="근거 교체로 stale")
    with pytest.raises(ValueError):
        steward.candidate_approve(
            candidate_memory_id=stale["proposal"]["memory_id"], approved_by="a", decision_id="d"
        )
    # stale proposal 은 그대로이고 target 은 current 를 유지한다.
    assert ledger.get_llm_brain_memory_card(target["memory_id"])["currentness"] == "current"

    created = steward.candidate_create(source_span=_span(content_hash="sha256:rej"))
    cand_id = created["proposal"]["memory_id"]
    steward.candidate_reject(candidate_memory_id=cand_id, rejected_by="a", decision_id="d", reason="no")
    with pytest.raises(ValueError):
        steward.candidate_approve(candidate_memory_id=cand_id, approved_by="a", decision_id="d2")


def _supersede_span():
    return _span(
        content_hash="sha256:replacement",
        redacted_summary="이제는 영어로 응답한다",
        typed_payload={
            "preference": "영어로 응답한다",
            "explicitness": "explicit",
            "repeated_count": 1,
            "confirmation_status": "confirmed",
            "applies_to": "natural_language_response",
        },
    )


def test_stale_commit_blocked_by_default(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)  # allow_restricted 기본값 False
    target_id = _accept_card(ledger)["memory_id"]
    prop = steward.stale_mark(memory_id=target_id, reason="stale 사유")["proposal"]["memory_id"]
    with pytest.raises(StewardPermissionError):
        steward.stale_commit(proposal_memory_id=prop, approved_by="op", decision_id="d")
    # target 은 그대로.
    assert ledger.get_llm_brain_memory_card(target_id)["currentness"] == "current"


def test_stale_commit_demotes_target_and_clears_queue(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    target_id = _accept_card(ledger)["memory_id"]
    prop_id = steward.stale_mark(memory_id=target_id, reason="stale 사유")["proposal"]["memory_id"]

    steward.stale_commit(proposal_memory_id=prop_id, approved_by="op", decision_id="d")

    # target accepted card 가 stale 로 demote → authority pack 에서 빠진다.
    assert ledger.get_llm_brain_memory_card(target_id)["currentness"] == "stale"
    assert target_id not in [i["memory_id"] for i in steward.authority_pack_read(project=PROJECT)["items"]]
    # proposal 은 review queue 를 떠난다.
    assert prop_id not in [i["memory_id"] for i in steward.review_queue_list(project=PROJECT)["items"]]


def test_supersede_commit_accepts_new_and_demotes_old(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    old_id = _accept_card(ledger)["memory_id"]
    prop_id = steward.supersede_propose(old_memory_id=old_id, source_span=_supersede_span())["proposal"]["memory_id"]

    steward.supersede_commit(proposal_memory_id=prop_id, approved_by="op", decision_id="d")

    old = ledger.get_llm_brain_memory_card(old_id)
    assert old["currentness"] == "superseded"
    assert prop_id in old["superseded_by"]
    # 새(교체) card 가 이제 accepted+current authority 이고 old 는 아니다.
    pack_ids = [i["memory_id"] for i in steward.authority_pack_read(project=PROJECT)["items"]]
    assert prop_id in pack_ids
    assert old_id not in pack_ids
    # proposal 은 더 이상 review queue 에 pending 이 아니다.
    assert prop_id not in [i["memory_id"] for i in steward.review_queue_list(project=PROJECT)["items"]]


def test_stale_committed_card_excluded_from_brain_query_recall(tmp_path):
    from agent_knowledge.session_memory.brain_query import build_brain_query_response_v2

    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    target_id = _accept_card(ledger)["memory_id"]
    prop = steward.stale_mark(memory_id=target_id, reason="stale 사유")["proposal"]["memory_id"]
    steward.stale_commit(proposal_memory_id=prop, approved_by="op", decision_id="d")

    demoted = ledger.get_llm_brain_memory_card(target_id)
    resp = build_brain_query_response_v2(
        brain_id=f"/project/{PROJECT}", query_intent="x", ledger_cards=[demoted]
    )
    # stale 로 확정된 card 는 recall 근거로 다시 떠오르면 안 된다.
    assert target_id not in [c["memory_id"] for c in resp["accepted"]]
    assert target_id not in [c["memory_id"] for c in resp["current"]]


def test_stale_commit_records_approver_and_timestamp(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    target_id = _accept_card(ledger)["memory_id"]
    prop = steward.stale_mark(memory_id=target_id, reason="stale 사유")["proposal"]["memory_id"]
    steward.stale_commit(proposal_memory_id=prop, approved_by="op", decision_id="d")
    committed = ledger.get_llm_brain_memory_card(prop)
    assert committed["approved_by"] == "op"
    assert committed["approved_at"]


def test_commit_rejects_non_current_target(tmp_path):
    from agent_knowledge.session_memory.memory_promotion import commit_stale

    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    # supersede: proposal 생성 후 target 이 stale 로 demote → commit 은 거부해야 한다.
    old_id = _accept_card(ledger)["memory_id"]
    sup = steward.supersede_propose(old_memory_id=old_id, source_span=_supersede_span())["proposal"]["memory_id"]
    ledger.upsert_llm_brain_memory_card(commit_stale(ledger.get_llm_brain_memory_card(old_id)))
    with pytest.raises(ValueError):
        steward.supersede_commit(proposal_memory_id=sup, approved_by="op", decision_id="d")

    # stale: target 이 이미 stale → commit 은 거부해야 한다(non-current 재-stale 금지).
    other_id = _accept_card(ledger, content_hash="sha256:other")["memory_id"]
    st = steward.stale_mark(memory_id=other_id, reason="r")["proposal"]["memory_id"]
    ledger.upsert_llm_brain_memory_card(commit_stale(ledger.get_llm_brain_memory_card(other_id)))
    with pytest.raises(ValueError):
        steward.stale_commit(proposal_memory_id=st, approved_by="op", decision_id="d")


def test_stale_card_excluded_from_persona_but_kept_for_history(tmp_path):
    from agent_knowledge.session_memory.memory_promotion import commit_stale

    service = _service(tmp_path)
    target_id = _accept_card(service.ledger)["memory_id"]  # a preference (persona) card
    # 현재-권위일 때 persona fact 로 노출된다(캐시 prime).
    assert len(service.core_brain(project=PROJECT).brain_persona_get(project=PROJECT)["facts"]) == 1
    # stale 로 demote → persona fact 에서 빠져야 한다(컨텍스트 read leak 방지).
    service.ledger.upsert_llm_brain_memory_card(
        commit_stale(service.ledger.get_llm_brain_memory_card(target_id))
    )
    service.invalidate_brain_card_cache()
    assert service.core_brain(project=PROJECT).brain_persona_get(project=PROJECT)["facts"] == []
    # history 소비자(read model accepted lane)는 여전히 stale 카드를 본다(drift_explain 용).
    accepted = service.ledger.list_llm_brain_memory_cards(project=PROJECT, accepted_only=True, limit=50)
    assert target_id in [c["memory_id"] for c in accepted]


def test_restricted_commit_invalidates_session_card_cache(tmp_path):
    service = _service(tmp_path, allow_restricted=True)
    calls: list[int] = []
    service.invalidate_brain_card_cache = lambda: calls.append(1)  # spy(호출 감시)
    target_id = _accept_card(service.ledger)["memory_id"]
    prop = service.brain_steward().stale_mark(memory_id=target_id, reason="r")["proposal"]["memory_id"]
    dispatch_tool_call(
        {"name": MEMORY_STALE_COMMIT_TOOL_NAME,
         "arguments": {"proposal_memory_id": prop, "approved_by": "op", "decision_id": "d"}},
        service,
    )
    assert calls  # 성공한 restricted commit 이 캐시를 무효화함(read-after-write)


def test_denied_restricted_call_does_not_invalidate_cache(tmp_path):
    service = _service(tmp_path)  # restricted 꺼짐 → denied, write 없음
    calls: list[int] = []
    service.invalidate_brain_card_cache = lambda: calls.append(1)
    dispatch_tool_call(
        {"name": MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
         "arguments": {"candidate_memory_id": "x", "approved_by": "op", "decision_id": "d"}},
        service,
    )
    assert not calls  # denied 경로는 write 도 invalidation 도 없다


def test_commit_rejects_mismatched_proposal_kind(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    cand_id = steward.candidate_create(source_span=_span(content_hash="sha256:mk"))["proposal"]["memory_id"]
    with pytest.raises(ValueError):
        steward.stale_commit(proposal_memory_id=cand_id, approved_by="op", decision_id="d")
    with pytest.raises(ValueError):
        steward.supersede_commit(proposal_memory_id=cand_id, approved_by="op", decision_id="d")


def test_stale_commit_is_atomic_rolls_back_on_mid_failure(tmp_path, monkeypatch):
    from agent_knowledge.ledger import _LedgerTransaction

    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    target_id = _accept_card(ledger)["memory_id"]
    prop = steward.stale_mark(memory_id=target_id, reason="stale 사유")["proposal"]["memory_id"]

    # fault injection: 트랜잭션 중간(audit write)에서 실패시킨다.
    def boom(self, record):
        raise RuntimeError("injected audit failure")

    # _accept_card 가 이미 자체 acceptance feedback 1건을 남겼으므로 baseline 을 잡는다.
    feedback_before = len(ledger.list_llm_brain_feedback_records(limit=100))
    monkeypatch.setattr(_LedgerTransaction, "upsert_llm_brain_feedback_record", boom)
    with pytest.raises(RuntimeError):
        steward.stale_commit(proposal_memory_id=prop, approved_by="op", decision_id="d")

    # 전부 rollback: target 미demote, proposal 그대로 pending, 새 audit 0 — 부분 커밋 없음.
    assert ledger.get_llm_brain_memory_card(target_id)["currentness"] == "current"
    assert prop in [i["memory_id"] for i in steward.review_queue_list(project=PROJECT)["items"]]
    assert len(ledger.list_llm_brain_feedback_records(limit=100)) == feedback_before


def test_supersede_commit_is_atomic_rolls_back_on_mid_failure(tmp_path, monkeypatch):
    from agent_knowledge.ledger import _LedgerTransaction

    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger, allow_restricted=True)
    old_id = _accept_card(ledger)["memory_id"]
    prop = steward.supersede_propose(old_memory_id=old_id, source_span=_supersede_span())["proposal"]["memory_id"]

    def boom(self, record):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(_LedgerTransaction, "upsert_llm_brain_feedback_record", boom)
    with pytest.raises(RuntimeError):
        steward.supersede_commit(proposal_memory_id=prop, approved_by="op", decision_id="d")

    # 전부 rollback: old 는 그대로 current, 교체 후보는 accept되지 않고 pending — 두 카드 동시 current 없음.
    assert ledger.get_llm_brain_memory_card(old_id)["currentness"] == "current"
    assert prop in [i["memory_id"] for i in steward.review_queue_list(project=PROJECT)["items"]]
    pack_ids = [i["memory_id"] for i in steward.authority_pack_read(project=PROJECT)["items"]]
    assert prop not in pack_ids


def test_projection_field_sets_are_stable(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    _accept_card(ledger)
    steward.candidate_create(source_span=_span(content_hash="sha256:proj"))

    auth_item = steward.authority_pack_read(project=PROJECT)["items"][0]
    assert set(auth_item) == {
        "memory_id", "card_type", "scope", "project", "provider", "title", "summary",
        "lifecycle_state", "approval_state", "freshness", "currentness", "governance_tier",
        "confidence", "confidence_basis", "supersedes", "superseded_by",
        "source_ref_count", "evidence_hash_count",
    }
    review_item = next(
        i for i in steward.review_queue_list(project=PROJECT)["items"]
        if i["proposal_kind"] == "candidate"
    )
    assert set(review_item) == {
        "memory_id", "proposal_kind", "proposed_by", "target_memory_id", "card_type", "scope",
        "project", "provider", "title", "summary", "lifecycle_state", "judgment_state",
        "approval_state", "currentness", "freshness", "governance_tier", "confidence", "reason",
        "supersedes", "source_ref_count", "evidence_hash_count",
    }


def test_service_owns_source_span_selection_and_denial(tmp_path):
    steward = BrainStewardService(_ledger(tmp_path))
    # source_span 필드 선택을 service 가 소유한다(dispatch 의 중복 튜플 제거).
    selected = steward.select_source_span(
        {"card_type": "status", "project": "p", "junk": 1, "limit": 5}
    )
    assert "junk" not in selected and "limit" not in selected
    assert selected["card_type"] == "status" and selected["project"] == "p"
    # denied 페이로드도 service 가 소유한다.
    denied = steward.restricted_denied_payload("memory_candidate_approve")
    assert denied["permission"] == "denied"
    assert denied["write_performed"] is False
    assert denied["tool"] == "memory_candidate_approve"


def test_dispatch_round_trip_read_and_proposal(tmp_path):
    service = _service(tmp_path)
    _accept_card(service.ledger)
    pack = _text(
        dispatch_tool_call(
            {"name": MEMORY_AUTHORITY_PACK_READ_TOOL_NAME, "arguments": {"project": PROJECT}},
            service,
        )
    )
    assert pack["count"] == 1
    proposal = _text(
        dispatch_tool_call(
            {"name": MEMORY_CANDIDATE_CREATE_TOOL_NAME, "arguments": _span(content_hash="sha256:rt")},
            service,
        )
    )
    assert proposal["accepted"] is False
    queue = _text(
        dispatch_tool_call(
            {"name": MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME, "arguments": {"project": PROJECT}},
            service,
        )
    )
    assert proposal["proposal"]["memory_id"] in [item["memory_id"] for item in queue["items"]]

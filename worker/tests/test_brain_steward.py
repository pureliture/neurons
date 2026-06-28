from __future__ import annotations

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
    MEMORY_STALE_MARK_TOOL_NAME,
    MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
    STEWARD_RESTRICTED_TOOL_NAMES,
    BrainStewardService,
    DisabledRagflowClient,
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
        ragflow=DisabledRagflowClient(),
        dataset_ids=[],
        allow_restricted_steward=allow_restricted,
    )


def _text(tool_result: dict) -> dict:
    return json.loads(tool_result["content"][0]["text"])


# --------------------------------------------------------------- tool surface


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
    # restricted tools are clearly labeled.
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

    # accepted/current authoritative lane stays empty.
    assert ledger.list_llm_brain_memory_cards(
        project=PROJECT, accepted_only=True, current_only=True, limit=50
    ) == []
    # the candidate is visible only in the review queue.
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

    # target survives unchanged.
    reloaded = ledger.get_llm_brain_memory_card(target_id)
    assert reloaded is not None
    assert reloaded["lifecycle_state"] == target["lifecycle_state"]
    assert reloaded["currentness"] == "current"
    # the proposal is a separate, non-authoritative record.
    proposal = result["proposal"]
    assert proposal["memory_id"] != target_id
    assert proposal["proposal_kind"] == "stale"
    assert proposal["target_memory_id"] == target_id
    assert proposal["currentness"] == "stale"
    # authority pack still returns the untouched target.
    pack_ids = [item["memory_id"] for item in steward.authority_pack_read(project=PROJECT)["items"]]
    assert target_id in pack_ids
    assert proposal["memory_id"] not in pack_ids


def test_stale_mark_unknown_target_is_rejected(tmp_path):
    steward = BrainStewardService(_ledger(tmp_path))
    with pytest.raises(ValueError):
        steward.stale_mark(memory_id="mem_missing", reason="x")


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

    # old card is untouched and still current/accepted.
    reloaded = ledger.get_llm_brain_memory_card(target_id)
    assert reloaded["currentness"] == "current"
    assert reloaded["lifecycle_state"] == target["lifecycle_state"]
    # the replacement is a non-accepted proposal that records the supersede intent.
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

    # a candidate (non-accepted) and a superseded (accepted-but-not-current) card.
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
        # only safe reference metadata, never raw locators/payload.
        assert "source_refs" not in item
        assert "typed_payload" not in item
        assert "render_text" not in item
        assert isinstance(item["source_ref_count"], int)


def test_assert_public_safe_fails_closed_on_forbidden_content():
    assert_public_safe({"summary": "정상적인 redacted summary"})
    with pytest.raises(ValueError):
        assert_public_safe({"summary": "secret token=live-abc123"})
    with pytest.raises(ValueError):
        assert_public_safe({"note": "/Users/ddalkak/.claude/private/x"})
    with pytest.raises(ValueError):
        assert_public_safe({"note": "/Volumes/usb/private/x"})
    with pytest.raises(ValueError):
        assert_public_safe({"dataset_id": "abc"})


def test_poisoned_title_or_basis_is_rejected_at_envelope_validation():
    from agent_knowledge.session_memory.memory_card import validate_memory_card_envelope

    base = build_memory_card_candidate_from_source_span(_span(), refresh_watermark="t")
    for field, value in (
        ("title", "/Users/ddalkak/.ssh/id_rsa"),
        ("title", "/Volumes/usb/secret"),
        ("confidence_basis", "see /Users/ddalkak/.ssh/id_rsa for proof"),
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

    # no row was written, so the project review queue still reads cleanly.
    assert ledger.list_llm_brain_review_queue(project=PROJECT, limit=50) == []
    clean = steward.candidate_create(source_span=_span(content_hash="sha256:clean"))
    queue = steward.review_queue_list(project=PROJECT)
    assert [item["memory_id"] for item in queue["items"]] == [clean["proposal"]["memory_id"]]


# --------------------------------------------------------------- restricted gate


def test_restricted_tools_blocked_by_default_service(tmp_path):
    service = _service(tmp_path)  # allow_restricted_steward defaults to False
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
    # no write happened: candidate is still non-accepted.
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
    # now it is accepted authoritative memory and appears in the authority pack.
    pack_ids = [item["memory_id"] for item in steward.authority_pack_read(project=PROJECT)["items"]]
    assert promoted["accepted_card"]["memory_id"] in pack_ids


def test_proposal_persist_guard_refuses_to_overwrite_accepted(tmp_path):
    ledger = _ledger(tmp_path)
    steward = BrainStewardService(ledger)
    accepted = _accept_card(ledger)
    # forge a proposal whose memory_id collides with an accepted card.
    forged = build_memory_card_candidate_from_source_span(_span(content_hash="sha256:forge"), refresh_watermark="t")
    forged["memory_id"] = accepted["memory_id"]
    with pytest.raises(ValueError):
        steward._persist_proposal(forged)
    # accepted card is untouched.
    assert ledger.get_llm_brain_memory_card(accepted["memory_id"])["lifecycle_state"] == accepted["lifecycle_state"]


def test_proposal_write_fails_closed_on_read_only_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.read_only = True  # mirror the live recall MCP transport
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
    # the stale proposal is untouched and the target stays current.
    assert ledger.get_llm_brain_memory_card(target["memory_id"])["currentness"] == "current"

    created = steward.candidate_create(source_span=_span(content_hash="sha256:rej"))
    cand_id = created["proposal"]["memory_id"]
    steward.candidate_reject(candidate_memory_id=cand_id, rejected_by="a", decision_id="d", reason="no")
    with pytest.raises(ValueError):
        steward.candidate_approve(candidate_memory_id=cand_id, approved_by="a", decision_id="d2")


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

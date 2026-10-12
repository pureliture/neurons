"""Startup context must stay readable inside the MCP tool-result wire budget.

The measured production shape of this pack is large enough that the old
per-field halving drove every section value to an empty string. These tests use
that shape so the budget ladder keeps subject/state/next-action/decision text
instead of returning only ids and hashes.
"""
from __future__ import annotations

from agent_knowledge.llm_brain_core.graph_first_resolver import (
    GraphFirstResolver,
    _task_subject,
)
from agent_knowledge.mcp_payload import tool_result_bytes

HASH = "sha256:" + "c" * 64


def _card(memory_id: str, card_type: str, *, title: str, summary: str, typed: dict) -> dict:
    return {
        "memory_id": memory_id,
        "project": "neurons",
        "card_type": card_type,
        "title": title,
        "summary": summary,
        "typed_payload": typed,
        "currentness": "current",
        "content_hash": HASH,
        "lifecycle_state": "human_accepted",
        "authorization_status": "active",
    }


def _fill(length: int, char: str = "한") -> str:
    return char * length


class Store:
    """Minimal read model: identity list plus the preselected context page."""

    def __init__(self, context_cards: list[dict]) -> None:
        self.context_cards = context_cards

    def list_context_cards(self, **_kwargs):
        return [dict(card) for card in self.context_cards]

    def graph_projection_health(self, *_args, **_kwargs):
        return {"unprojected": False, "projection_lag_ms": None}

    def list_authorized_cards(self, **_kwargs):
        return []


def _measured_production_shape() -> list[dict]:
    """Field lengths as read back from the running production PostgreSQL."""
    task = _card(
        "mem_task",
        "task",
        title=_fill(32),
        summary=_fill(104),
        typed={
            "task_state": "in_progress",
            "next_action": _fill(68),
            "blocker": _fill(28),
            "status": _fill(4, "z"),
        },
    )
    decision = _card(
        "mem_decision",
        "decision",
        title=_fill(67),
        summary=_fill(385),
        typed={"decision": _fill(170), "rationale": _fill(212)},
    )
    status = _card(
        "mem_status",
        "status",
        title=_fill(53),
        summary=_fill(221),
        typed={"status_value": _fill(9, "k")},
    )
    preference = _card(
        "mem_preference",
        "preference",
        title=_fill(36),
        summary=_fill(108),
        typed={"preference": _fill(160), "scope": "general"},
    )
    evidence = _card(
        "mem_evidence",
        "evidence",
        title=_fill(35),
        summary=_fill(452),
        typed={"evidence_kind": "runtime", "result_status": "pass", "count_refs": "1"},
    )
    return [task, decision, status, preference, evidence]


def _sections(result: dict) -> tuple[bool, list[str]]:
    empty: list[str] = []
    for index, item in enumerate(result.get("decisions", [])):
        if not str(item.get("decision") or "").strip():
            empty.append(f"decision[{index}]")
        if not str(item.get("rationale") or "").strip():
            empty.append(f"rationale[{index}]")
    for index, item in enumerate(result.get("tasks", [])):
        for field in ("task", "task_state", "next_action"):
            if field in item and not str(item.get(field) or "").strip():
                empty.append(f"task[{index}].{field}")
    for index, item in enumerate(result.get("statuses", [])):
        if not str(item.get("status") or "").strip():
            empty.append(f"status[{index}]")
    for index, item in enumerate(result.get("preferences", [])):
        if not str(item.get("rule") or "").strip():
            empty.append(f"rule[{index}]")
    return not empty, empty


def test_measured_production_context_shape_fits_wire_budget_with_readable_text():
    result = GraphFirstResolver(Store(_measured_production_shape())).resolve(
        project="neurons", mode="context", limit=5
    )

    assert result.get("error_code") is None, result
    assert tool_result_bytes(result) <= 3072
    # The work subject must be the task itself, not its status token.
    assert result["tasks"], result
    assert result["tasks"][0]["task"] == _fill(32)
    assert result["tasks"][0]["task_state"] == "in_progress"
    assert str(result["tasks"][0]["next_action"]).strip()
    # A surviving section must never be reduced to an empty string.
    readable, empty = _sections(result)
    assert readable, empty
    # Work, latest decision and current status are the startup pack's core, so
    # none of them may be sacrificed while a card remains to drop.
    assert result["decisions"], result
    assert str(result["decisions"][0]["decision"]).strip()
    assert result["statuses"], result
    assert str(result["statuses"][0]["status"]).strip()
    assert len(result["items"]) >= 3, result["items"]


def test_context_keeps_task_and_decision_when_even_trimmed_text_exceeds_budget():
    oversized = [
        _card(
            "mem_task", "task",
            title="Latest recovery work",
            summary="work summary " * 400,
            typed={"task_state": "in_progress", "next_action": "Add durable retry " * 400},
        ),
        _card(
            "mem_decision", "decision",
            title="Keep approval gate",
            summary="rationale " * 400,
            typed={"decision": "Never auto-approve " * 400, "rationale": "human review " * 400},
        ),
        _card(
            "mem_status", "status",
            title="stale status",
            summary="status summary " * 400,
            typed={"status_value": "blocked " * 400},
        ),
        _card(
            "mem_preference", "preference",
            title="private management",
            summary="preference summary " * 400,
            typed={"preference": "prefer private paths " * 400, "scope": "general"},
        ),
    ]
    result = GraphFirstResolver(Store(oversized)).resolve(project="neurons", mode="context", limit=4)

    assert result.get("error_code") is None, result
    assert tool_result_bytes(result) <= 3072
    assert result["tasks"], "the task card must survive an oversized pack"
    assert result["decisions"], "the decision card must survive an oversized pack"
    readable, empty = _sections(result)
    assert readable, empty
    assert str(result["tasks"][0]["next_action"]).strip()


def test_task_state_token_is_not_used_as_the_task_name():
    token_card = _card(
        "mem_task", "task",
        title="Repair the selector",
        summary="summary",
        typed={"task_state": "in_progress", "next_action": "Run the regression"},
    )
    assert _task_subject(token_card, token_card["typed_payload"]) == "Repair the selector"

    free_text_card = _card(
        "mem_task", "task",
        title="unused title",
        summary="unused summary",
        typed={"task_state": "Finish latest work", "next_action": "Run tests"},
    )
    assert _task_subject(free_text_card, free_text_card["typed_payload"]) == "Finish latest work"

    explicit_card = _card(
        "mem_task", "task",
        title="unused title",
        summary="unused summary",
        typed={"task": "Explicit task name", "task_state": "blocked"},
    )
    assert _task_subject(explicit_card, explicit_card["typed_payload"]) == "Explicit task name"

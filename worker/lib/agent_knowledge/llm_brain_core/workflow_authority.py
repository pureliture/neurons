from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ._util import ensure_public_safe, public_safe_text


@dataclass(frozen=True)
class WorkflowContractCard:
    memory_id: str
    rule: str
    scope: str
    reason: str
    confidence: float
    evidence_refs: tuple[str, ...]
    exceptions: tuple[str, ...] = ()
    auto_update_allowed: bool = False

    def __post_init__(self) -> None:
        ensure_public_safe(self.to_dict(), "WorkflowContractCard")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        data["exceptions"] = list(self.exceptions)
        return data


@dataclass(frozen=True)
class WorkflowDefaultCard:
    memory_id: str
    default: str
    scope: str
    reason: str
    confidence: float
    evidence_refs: tuple[str, ...]
    exceptions: tuple[str, ...] = ()
    auto_update_allowed: bool = False

    def __post_init__(self) -> None:
        ensure_public_safe(self.to_dict(), "WorkflowDefaultCard")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        data["exceptions"] = list(self.exceptions)
        return data


@dataclass(frozen=True)
class SkillEvolutionCard:
    memory_id: str
    skill_name: str
    change_summary: str
    reason: str
    confidence: float
    evidence_refs: tuple[str, ...]
    auto_update_allowed: bool = False

    def __post_init__(self) -> None:
        ensure_public_safe(self.to_dict(), "SkillEvolutionCard")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        return data


def workflow_contract_cards_from_memory_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for card in cards:
        payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
        if str(card.get("currentness") or "unknown") not in {"current", "unknown", ""}:
            continue
        if not _is_workflow_card(card, payload):
            continue
        rule = public_safe_text(
            str(payload.get("rule") or payload.get("workflow_contract") or payload.get("default") or card.get("summary") or ""),
            max_chars=360,
        )
        if not rule:
            continue
        contracts.append(
            WorkflowContractCard(
                memory_id=str(card.get("memory_id") or ""),
                rule=rule,
                scope=public_safe_text(str(payload.get("applies_to") or card.get("scope") or "project"), max_chars=180),
                reason=public_safe_text(str(payload.get("reason") or card.get("summary") or ""), max_chars=360),
                confidence=float(card.get("confidence") or 0),
                evidence_refs=tuple(_evidence_refs(card)),
                exceptions=tuple(public_safe_text(str(item), max_chars=180) for item in payload.get("exceptions") or []),
                auto_update_allowed=False,
            ).to_dict()
        )
    return contracts


def workflow_default_cards_from_memory_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    defaults: list[dict[str, Any]] = []
    for card in cards:
        payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
        if not _is_workflow_default_card(card, payload):
            continue
        default = public_safe_text(
            str(payload.get("default") or payload.get("workflow_default") or payload.get("rule") or card.get("summary") or ""),
            max_chars=360,
        )
        if not default:
            continue
        defaults.append(
            WorkflowDefaultCard(
                memory_id=str(card.get("memory_id") or ""),
                default=default,
                scope=public_safe_text(str(payload.get("applies_to") or card.get("scope") or "project"), max_chars=180),
                reason=public_safe_text(str(payload.get("reason") or card.get("summary") or ""), max_chars=360),
                confidence=float(card.get("confidence") or 0),
                evidence_refs=tuple(_evidence_refs(card)),
                exceptions=tuple(public_safe_text(str(item), max_chars=180) for item in payload.get("exceptions") or []),
                auto_update_allowed=False,
            ).to_dict()
        )
    return defaults


def skill_evolution_cards_from_memory_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evolutions: list[dict[str, Any]] = []
    for card in cards:
        payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
        if str(card.get("card_type") or "") != "skill_evolution":
            continue
        skill_name = public_safe_text(str(payload.get("skill_name") or payload.get("skill") or ""), max_chars=120)
        change_summary = public_safe_text(
            str(payload.get("change_summary") or payload.get("summary") or card.get("summary") or ""),
            max_chars=360,
        )
        if not skill_name or not change_summary:
            continue
        evolutions.append(
            SkillEvolutionCard(
                memory_id=str(card.get("memory_id") or ""),
                skill_name=skill_name,
                change_summary=change_summary,
                reason=public_safe_text(str(payload.get("reason") or card.get("summary") or ""), max_chars=360),
                confidence=float(card.get("confidence") or 0),
                evidence_refs=tuple(_evidence_refs(card)),
                auto_update_allowed=False,
            ).to_dict()
        )
    return evolutions


def _is_workflow_card(card: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    card_type = str(card.get("card_type") or "")
    return card_type in {"workflow", "workflow_contract"} or bool(payload.get("workflow_contract"))


def _is_workflow_default_card(card: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    card_type = str(card.get("card_type") or "")
    return card_type == "workflow_default" or bool(payload.get("workflow_default"))


def _evidence_refs(card: Mapping[str, Any]) -> list[str]:
    memory_id = str(card.get("memory_id") or "")
    evidence_refs = [memory_id] if memory_id else []
    for ref in card.get("source_refs") or []:
        ref_id = _source_ref_id(ref)
        if ref_id and ref_id not in evidence_refs:
            evidence_refs.append(ref_id)
    return evidence_refs


def _source_ref_id(ref: Any) -> str:
    if isinstance(ref, Mapping):
        return public_safe_text(str(ref.get("source_ref_id") or ref.get("id") or ref.get("knowledge_id") or ""), max_chars=180)
    return public_safe_text(str(ref or ""), max_chars=180)

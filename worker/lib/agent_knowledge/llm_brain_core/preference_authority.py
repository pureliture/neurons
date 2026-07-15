from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ._util import ensure_public_safe, public_safe_text


@dataclass(frozen=True)
class PreferenceRuleCard:
    memory_id: str
    rule: str
    scope: str
    reason: str
    confidence: float
    currentness: str
    evidence_refs: tuple[str, ...]
    exceptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ensure_public_safe(self.to_dict(), "PreferenceRuleCard")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence_refs"] = list(self.evidence_refs)
        data["exceptions"] = list(self.exceptions)
        return data


ALWAYS_APPLIED_SCOPES = {
    "global",
    "project",
    "natural language response",
    "communication",
    "writing style",
}


def preference_rule_cards_from_memory_cards(
    cards: list[dict[str, Any]],
    *,
    current_request: str = "",
    current_files: list[str] | tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    preferences: list[dict[str, Any]] = []
    for card in cards:
        payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
        if str(card.get("card_type") or "") != "preference":
            continue
        rule = public_safe_text(str(payload.get("preference") or card.get("summary") or ""), max_chars=360)
        if not rule:
            continue
        scope = public_safe_text(str(payload.get("applies_to") or card.get("scope") or "global"), max_chars=180)
        if not _scope_applies(scope, current_request=current_request, current_files=current_files):
            continue
        preference = PreferenceRuleCard(
            memory_id=str(card.get("memory_id") or ""),
            rule=rule,
            scope=scope,
            reason=public_safe_text(str(payload.get("reason") or card.get("summary") or ""), max_chars=360),
            confidence=float(card.get("confidence") or 0),
            currentness=public_safe_text(str(card.get("currentness") or "unknown"), max_chars=80),
            evidence_refs=tuple(_evidence_refs(card)),
            exceptions=tuple(
                public_safe_text(str(item), max_chars=180) for item in payload.get("exceptions") or []
            ),
        ).to_dict()
        target_object_id = public_safe_text(str(payload.get("target_object_id") or ""), max_chars=180)
        if target_object_id:
            preference["target_object_id"] = target_object_id
            preference["project"] = public_safe_text(str(card.get("project") or ""), max_chars=120)
            preference["source_content_hash"] = public_safe_text(
                str(payload.get("source_content_hash") or ""),
                max_chars=80,
            )
            preference["authority_decision_id"] = public_safe_text(
                str(payload.get("authority_decision_id") or ""),
                max_chars=180,
            )
        preferences.append(preference)
    return preferences


def _scope_applies(
    scope: str,
    *,
    current_request: str,
    current_files: list[str] | tuple[str, ...],
) -> bool:
    normalized_scope = _normalize(scope)
    if normalized_scope in ALWAYS_APPLIED_SCOPES:
        return True
    scope_terms = _terms(normalized_scope)
    if not scope_terms:
        return True
    context = _normalize(" ".join([current_request, *current_files]))
    return any(term in context for term in scope_terms)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_가-힣]+", " ", value.replace("_", " ")).strip().casefold()


def _terms(value: str) -> list[str]:
    return [term for term in re.split(r"[^a-zA-Z0-9_가-힣]+", value) if len(term) >= 3]


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

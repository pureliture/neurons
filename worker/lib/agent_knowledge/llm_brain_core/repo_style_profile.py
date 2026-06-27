from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe, public_safe_text


REPO_STYLE_PROFILE_SCHEMA = "repo_style_profile.v1"


def repo_style_profile_from_memory_cards(
    cards: list[Mapping[str, Any]],
    *,
    repository: str,
) -> dict[str, Any]:
    claims: list[dict[str, Any]] = []
    ignored: list[dict[str, str]] = []
    for card in cards:
        card_type = str(card.get("card_type") or "")
        if card_type != "repo_style":
            ignored.append(
                {
                    "memory_id": str(card.get("memory_id") or ""),
                    "reason": "user_preference_not_repo_style"
                    if card_type == "preference"
                    else "insufficient_style_authority",
                }
            )
            continue
        payload = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
        claim = public_safe_text(str(payload.get("claim") or card.get("summary") or ""), max_chars=360)
        if not claim:
            ignored.append({"memory_id": str(card.get("memory_id") or ""), "reason": "insufficient_style_authority"})
            continue
        files = _safe_list(payload.get("files"), max_chars=240)
        commits = _safe_list(payload.get("commits"), max_chars=160)
        sessions = _safe_list(payload.get("sessions"), max_chars=160)
        memory_id = str(card.get("memory_id") or "")
        evidence_refs = [memory_id, *files, *commits, *sessions]
        claims.append(
            {
                "memory_id": memory_id,
                "claim": claim,
                "repo_scope": public_safe_text(str(payload.get("repo_scope") or repository), max_chars=180),
                "reason": public_safe_text(str(payload.get("reason") or card.get("summary") or ""), max_chars=360),
                "confidence": float(card.get("confidence") or 0),
                "files": files,
                "commits": commits,
                "sessions": sessions,
                "evidence_refs": evidence_refs,
            }
        )
    result = {
        "schema_version": REPO_STYLE_PROFILE_SCHEMA,
        "repository": public_safe_text(repository, max_chars=180),
        "claims": claims,
        "ignored_inputs": ignored,
    }
    ensure_public_safe(result, "RepoStyleProfile")
    return result


def _safe_list(value: Any, *, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [public_safe_text(str(item), max_chars=max_chars) for item in value if str(item or "")]

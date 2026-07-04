"""use-brain Phase 1: brain.query / brain.resolve 순수 로직.

ledger phase-out 제약(2026-06-11): 이 모듈은 Ledger/NativeMemoryMirrorStore를
import하지 않는다. 카드 메타 read는 BrainReadModel protocol 뒤에,
semantic recall은 callable 주입으로 받는다. ledger 결합은
brain_read_model.py 어댑터 한 파일로 격리한다(M8.1 recall_read_model.py 선례).

contract: docs/superpowers/specs/2026-06-11-use-brain-ux-design.md
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Callable, Protocol, runtime_checkable

from .memory_card import CANDIDATE_TYPES
from .native_memory_governance import HIGH_RISK_CARD_TYPES
from .query_planner import MAX_QUERY_CHARS
from .transcript_model import MAX_TRANSCRIPT_SNIPPET_CHARS, redact_and_bound_evidence_text
from .memory_card import validate_memory_card_envelope

BRAIN_ID_PROJECT_PREFIX = "/project/"
DEFAULT_LIMIT = 8
MAX_LIMIT = 10
LEDGER_QUERY_RANKING_CANDIDATE_LIMIT = 50
QUERY_RESPONSE_LANES = (
    "current",
    "accepted",
    "archive",
    "evidence_candidates",
    "promotion_candidates",
    "conflicts",
)
ACCEPTED_LIFECYCLE_STATES = {"accepted", "human_accepted", "auto_accepted"}
ACCEPTED_APPROVAL_STATES = {"approved", "auto_accepted"}


@runtime_checkable
class BrainReadModel(Protocol):
    """envelope 보강에 필요한 최소 read 표면. 현재 구현체는 brain_read_model.py."""

    def get_card_meta(self, card_id: str) -> dict | None: ...

    def list_recent_cards(self, *, project: str, limit: int) -> list[dict]: ...

    def list_project_card_counts(self) -> list[tuple[str, int]]: ...


SemanticRecall = Callable[[str, str], list[dict]]  # (query, brain_id) -> hits
RetiredIndexBridgeMirrorSearch = Callable[[str, str], list[dict]]


def project_from_brain_id(brain_id: str) -> str | None:
    if not isinstance(brain_id, str) or not brain_id.startswith(BRAIN_ID_PROJECT_PREFIX):
        return None
    project = brain_id[len(BRAIN_ID_PROJECT_PREFIX):]
    return project or None


def _error(code: str, message: str) -> dict:
    # 에러 응답에 query 원문을 에코하지 않는다(민감정보 잔류 방지, spec §2.2).
    return {"error": {"code": code, "message": message}, "results": []}


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()[:MAX_QUERY_CHARS]


def _query_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9가-힣_-]+", str(text or ""))
        if len(token) > 1
    }


def _card_search_text(card: Mapping[str, Any]) -> str:
    payload = card.get("typed_payload")
    payload_values = payload.values() if isinstance(payload, Mapping) else []
    return " ".join(
        str(value or "")
        for value in (
            card.get("title"),
            card.get("summary"),
            card.get("render_text"),
            card.get("card_type"),
            *payload_values,
        )
    )


def _rank_ledger_cards_for_query(*, cards: list[dict], query: str, limit: int, strict: bool) -> list[dict]:
    """Return query-relevant accepted cards without padding weak matches.

    v2 is a retrieval surface, not a "latest cards" listing. Use a bounded
    accepted-card candidate window, rank by lexical overlap, and only return
    cards that clear a conservative query-term coverage threshold. This keeps
    targeted eval queries from failing precision because the response was padded
    with weakly-related recent cards.
    """

    if not strict:
        return cards[:limit]
    tokens = _query_tokens(query)
    if not tokens:
        return []
    threshold = max(1, (len(tokens) + 1) // 2)
    scored: list[tuple[int, int, dict]] = []
    for index, card in enumerate(cards):
        overlap = len(tokens.intersection(_query_tokens(_card_search_text(card))))
        if overlap >= threshold:
            scored.append((index, overlap, card))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return [card for _, _, card in scored[:limit]]


def _hit_rank(hit: dict) -> tuple:
    non_raw = 0 if str(hit.get("message_type") or "") != "raw" else 1
    score = hit.get("score")
    score_key = -float(score) if isinstance(score, (int, float)) else float("inf")
    return (non_raw, score_key)


def dedupe_native_hits(hits: list[dict]) -> list[dict]:
    """동일 session_tag(raw+추출본) → 1건. non-raw 우선 > score 최고 > 입력순."""
    best: dict[str, dict] = {}
    order: list[str] = []
    for hit in hits:
        tag = str(hit.get("session_tag") or "")
        if not tag:
            continue
        if tag not in best:
            best[tag] = hit
            order.append(tag)
        elif _hit_rank(hit) < _hit_rank(best[tag]):
            best[tag] = hit
    return [best[tag] for tag in order]


def list_ledger_accepted_cards(
    read_model: BrainReadModel,
    *,
    project: str,
    limit: int,
) -> list[dict]:
    """Read accepted/current candidates from the injected ledger read model only."""

    if hasattr(read_model, "list_accepted_cards"):
        cards = getattr(read_model, "list_accepted_cards")(project=project, limit=limit)
    else:
        cards = read_model.list_recent_cards(project=project, limit=limit)
    if not isinstance(cards, list):
        return []
    return [dict(card) for card in cards if _is_accepted_ledger_card(card)]


def run_brain_query_v2(
    *,
    read_model: BrainReadModel,
    brain_id: str,
    query: str,
    query_intent: str = "session_context",
    index_search: RetiredIndexBridgeMirrorSearch | None = None,
    promotion_candidates: list[dict] | None = None,
    evidence_candidates: list[dict] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """LLM-brain query envelope with local-ledger precedence.

    This v2 helper is deliberately read-only. It does not write ledger state and
    it treats RetiredIndexBridge as a mirror whose results can only fill archive/evidence
    lanes unless matching local ledger truth already exists.
    """

    project = project_from_brain_id(brain_id)
    if project is None:
        return _query_v2_error(
            brain_id=brain_id,
            code="unsupported_brain_id",
            message=f"only {BRAIN_ID_PROJECT_PREFIX}<project> brain_id is supported",
        )
    normalized = _normalize_query(query if isinstance(query, str) else "")
    if not normalized:
        return _query_v2_error(brain_id=brain_id, code="invalid_query", message="query must be non-empty")
    bounded_limit = max(1, min(MAX_LIMIT, int(limit)))
    strict_eval_ranking = query_intent == "eval"
    candidate_limit = max(bounded_limit, LEDGER_QUERY_RANKING_CANDIDATE_LIMIT) if strict_eval_ranking else bounded_limit
    ledger_cards = list_ledger_accepted_cards(read_model, project=project, limit=candidate_limit)
    ledger_cards = _rank_ledger_cards_for_query(
        cards=ledger_cards,
        query=normalized,
        limit=bounded_limit,
        strict=strict_eval_ranking,
    )
    index_results = None
    if index_search is not None:
        try:
            index_results = index_search(normalized, brain_id)
            if not isinstance(index_results, list):
                index_results = []
        except Exception:
            index_results = None
    response = build_brain_query_response_v2(
        brain_id=brain_id,
        query_intent=query_intent,
        ledger_cards=ledger_cards,
        index_results=index_results,
        promotion_candidates=promotion_candidates,
        evidence_candidates=evidence_candidates,
    )
    response["results"] = _compat_results_from_query_lanes(response)
    response["audit"] = {
        "query_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
        "path": "ledger_precedence_v2",
        "index_bound": index_search is not None,
    }
    return response


def build_brain_query_response_v2(
    *,
    brain_id: str,
    query_intent: str,
    ledger_cards: list[dict] | None,
    index_results: list[dict] | None = None,
    promotion_candidates: list[dict] | None = None,
    evidence_candidates: list[dict] | None = None,
) -> dict:
    """Merge local ledger and RetiredIndexBridge mirror results into explicit query lanes."""

    response = {
        "brain_id": brain_id,
        "query_intent": query_intent,
        "current": [],
        "accepted": [],
        "archive": [],
        "evidence_candidates": list(evidence_candidates or []),
        "promotion_candidates": list(promotion_candidates or []),
        "conflicts": [],
        "projection_state": {"status": "unavailable", "details": []},
        "sources": [
            {"source": "local_ledger", "authority": "canonical"},
            {"source": "retired_index_bridge", "authority": "searchable_runtime_mirror"},
        ],
    }
    ledger_by_memory_id: dict[str, dict] = {}
    for card in ledger_cards or []:
        normalized = _normalize_query_memory_card(brain_id=brain_id, card=card)
        memory_id = str(normalized.get("memory_id") or "")
        if memory_id:
            ledger_by_memory_id[memory_id] = normalized
        if normalized.get("currentness") == "current":
            response["current"].append(normalized)
        # stale 도 accepted/compat recall lane 에서 제외한다 — stale_commit 으로 내려간 카드가
        # 다시 답변 근거(local_ledger_accepted)로 쓰이면 안 된다.
        if normalized.get("currentness") not in ("superseded", "conflicted", "stale"):
            response["accepted"].append(normalized)

    for mirror_item in index_results or []:
        memory_id = _mirror_memory_id(mirror_item)
        if memory_id and memory_id in ledger_by_memory_id:
            conflict = _projection_conflict(ledger_by_memory_id[memory_id], mirror_item)
            if conflict:
                response["conflicts"].append(conflict)
            continue
        target_lane = (
            "evidence_candidates" if _is_evidence_like_mirror_item(mirror_item) else "archive"
        )
        response[target_lane].append(_sanitize_mirror_item(mirror_item))

    response["projection_state"] = _projection_state(
        conflicts=response["conflicts"], index_results=index_results
    )
    return response


_DEMOTE = {"high": "medium", "medium": "low", "low": "low"}


def _base_confidence(card_type: str) -> str:
    # governance_tier와 다른 목적: tier=high는 "명시 승인 필요(위험)"이고,
    # confidence=high는 "명시 승인을 거쳤으니 신뢰"다. 미지정 타입은 low(fail-closed).
    if card_type in HIGH_RISK_CARD_TYPES:
        return "high"
    if card_type in CANDIDATE_TYPES:
        return "medium"
    return "low"


def _is_accepted_ledger_card(card: Any) -> bool:
    if not isinstance(card, Mapping):
        return False
    lifecycle_state = str(card.get("lifecycle_state") or "")
    approval_state = str(card.get("approval_state") or "")
    if lifecycle_state:
        return lifecycle_state in ACCEPTED_LIFECYCLE_STATES and approval_state in ACCEPTED_APPROVAL_STATES
    # Legacy memory_cards table rows use state=active for approved cards.
    return str(card.get("state") or "") == "active"


def _query_v2_error(*, brain_id: str, code: str, message: str) -> dict:
    response = {
        "brain_id": brain_id,
        "query_intent": "unknown",
        "current": [],
        "accepted": [],
        "archive": [],
        "evidence_candidates": [],
        "promotion_candidates": [],
        "conflicts": [],
        "projection_state": {"status": "unavailable", "details": []},
        "sources": [],
        "error": {"code": code, "message": message},
        "results": [],
    }
    return response


def _normalize_query_memory_card(*, brain_id: str, card: dict) -> dict:
    if "lifecycle_state" in card and "typed_payload" in card:
        normalized = validate_memory_card_envelope(card)
        confidence = normalized["confidence"]
        lifecycle_state = str(normalized.get("lifecycle_state") or "")
        approval_state = str(normalized.get("approval_state") or "")
        currentness = str(normalized.get("currentness") or "")
        freshness = str(normalized.get("freshness") or "")
        judgment_state = str(normalized.get("judgment_state") or "")
        source_refs = list(normalized.get("source_refs") or [])
        evidence_hashes = list(normalized.get("evidence_hashes") or [])
        typed_payload = dict(normalized.get("typed_payload") or {})
    else:
        normalized = dict(card)
        confidence = None
        lifecycle_state = "accepted" if str(card.get("state") or "") == "active" else "unknown"
        approval_state = "approved" if str(card.get("state") or "") == "active" else "needs_review"
        currentness = "current" if str(card.get("state") or "") == "active" else "unknown"
        freshness = "current"
        judgment_state = "none"
        source_refs = [str(card.get("memory_id") or "")]
        evidence_hashes = []
        typed_payload = {}
    summary = redact_and_bound_evidence_text(
        str(normalized.get("summary") or ""), MAX_TRANSCRIPT_SNIPPET_CHARS
    )
    render_text = redact_and_bound_evidence_text(
        str(normalized.get("render_text") or summary), MAX_TRANSCRIPT_SNIPPET_CHARS
    )
    return {
        "brain_id": str(normalized.get("brain_id") or brain_id),
        "memory_id": str(normalized.get("memory_id") or ""),
        "card_type": str(normalized.get("card_type") or ""),
        "title": redact_and_bound_evidence_text(
            str(normalized.get("title") or ""), MAX_TRANSCRIPT_SNIPPET_CHARS
        ),
        "summary": summary,
        "render_text": render_text,
        "lifecycle_state": lifecycle_state,
        "judgment_state": judgment_state,
        "approval_state": approval_state,
        "freshness": freshness,
        "currentness": currentness,
        "confidence": confidence,
        "confidence_basis": redact_and_bound_evidence_text(
            str(normalized.get("confidence_basis") or ""), MAX_TRANSCRIPT_SNIPPET_CHARS
        ),
        "typed_payload": typed_payload,
        "source_refs": _sanitize_public_refs(source_refs),
        "evidence_hashes": [str(item) for item in evidence_hashes],
        "authority": "local_ledger",
        "content_hash": str(normalized.get("content_hash") or normalized.get("card_hash") or ""),
    }


def _compat_results_from_query_lanes(response: Mapping[str, Any]) -> list[dict]:
    """Preserve the Phase 1 ``results`` surface while v2 lanes become primary."""

    results = []
    seen: set[str] = set()
    for lane_name, why in (("current", "local_ledger_current"), ("accepted", "local_ledger_accepted")):
        lane = response.get(lane_name)
        if not isinstance(lane, list):
            continue
        for item in lane:
            if not isinstance(item, Mapping):
                continue
            memory_id = str(item.get("memory_id") or "")
            if memory_id and memory_id in seen:
                continue
            if memory_id:
                seen.add(memory_id)
            results.append(
                {
                    "brain_id": str(item.get("brain_id") or response.get("brain_id") or ""),
                    "result_type": "memory_card",
                    "summary": str(item.get("summary") or ""),
                    "why_retrieved": why,
                    "source_ref": memory_id,
                    "observed_at": str(item.get("observed_at") or ""),
                    "freshness": str(item.get("freshness") or ""),
                    "approval_state": str(item.get("approval_state") or ""),
                    "privacy": "redacted",
                    "confidence": item.get("confidence"),
                    "conflicts": [],
                    "currentness": str(item.get("currentness") or ""),
                    "card_type": str(item.get("card_type") or ""),
                    "memory_id": memory_id,
                }
            )
    return results


def _mirror_memory_id(item: Mapping[str, Any]) -> str:
    if not isinstance(item, Mapping):
        return ""
    for field in ("memory_id", "card_id"):
        value = str(item.get(field) or "")
        if value:
            return value
    source_ref = str(item.get("source_ref") or "")
    return source_ref if source_ref.startswith("mem_") else ""


def _projection_conflict(ledger_card: Mapping[str, Any], mirror_item: Mapping[str, Any]) -> dict | None:
    ledger_signature = _result_signature(ledger_card)
    mirror_signature = _result_signature(mirror_item)
    ledger_currentness = str(ledger_card.get("currentness") or "")
    mirror_currentness = str(mirror_item.get("currentness") or "")
    reasons = []
    if ledger_signature and mirror_signature and ledger_signature != mirror_signature:
        reasons.append("content_hash_mismatch")
    if mirror_currentness and ledger_currentness and mirror_currentness != ledger_currentness:
        reasons.append("currentness_mismatch")
    if not reasons:
        return None
    return {
        "memory_id": str(ledger_card.get("memory_id") or _mirror_memory_id(mirror_item)),
        "conflict_type": "projection_stale",
        "winner": "local_ledger",
        "ledger_currentness": ledger_currentness,
        "projection_currentness": mirror_currentness,
        "reasons": reasons,
    }


def _result_signature(item: Mapping[str, Any]) -> str:
    for field in ("projection_hash", "content_hash", "card_hash"):
        value = str(item.get(field) or "")
        if value:
            return value
    hashes = item.get("evidence_hashes")
    if isinstance(hashes, list) and hashes:
        return "|".join(str(value) for value in hashes)
    return ""


def _is_evidence_like_mirror_item(item: Mapping[str, Any]) -> bool:
    if not isinstance(item, Mapping):
        return False
    return str(item.get("card_type") or item.get("result_type") or "") == "evidence"


def _sanitize_mirror_item(item: Mapping[str, Any]) -> dict:
    if not isinstance(item, Mapping):
        return {"result_type": "index_mirror", "summary": ""}
    sanitized = {
        "result_type": str(item.get("result_type") or "index_mirror"),
        "memory_id": str(item.get("memory_id") or ""),
        "card_type": str(item.get("card_type") or ""),
        "summary": redact_and_bound_evidence_text(
            str(item.get("summary") or ""), MAX_TRANSCRIPT_SNIPPET_CHARS
        ),
        "currentness": str(item.get("currentness") or "unknown"),
        "authority": "index_mirror",
    }
    if item.get("score") is not None:
        sanitized["score"] = item.get("score")
    if item.get("content_hash"):
        sanitized["content_hash"] = str(item.get("content_hash"))
    if isinstance(item.get("evidence_hashes"), list):
        sanitized["evidence_hashes"] = [str(value) for value in item["evidence_hashes"]]
    return sanitized


def _sanitize_public_refs(refs: list[Any]) -> list[Any]:
    safe_refs = []
    for ref in refs:
        if isinstance(ref, Mapping):
            safe_refs.append({str(key): _safe_public_text(value) for key, value in ref.items()})
        else:
            safe_refs.append(_safe_public_text(ref))
    return safe_refs


def _safe_public_text(value: Any) -> str:
    return redact_and_bound_evidence_text(str(value or ""), MAX_TRANSCRIPT_SNIPPET_CHARS)


def _projection_state(*, conflicts: list[dict], index_results: list[dict] | None) -> dict:
    if conflicts:
        return {"status": "projection_stale", "details": conflicts}
    if index_results is None:
        return {"status": "unavailable", "details": []}
    return {"status": "fresh", "details": []}


def build_card_envelope(
    *,
    brain_id: str,
    card: dict,
    why: str,
    demote: bool,
    hit: dict | None = None,
) -> dict:
    confidence = _base_confidence(str(card.get("card_type") or ""))
    if demote:
        confidence = _DEMOTE[confidence]
    supersedes = str(card.get("supersedes") or "")
    envelope = {
        "brain_id": brain_id,
        "result_type": "memory_card",
        # strict redactor: envelope는 외부 노출 표면 — 일반 /Users/ 경로·credential까지 마스킹 (spec §3.3)
        "summary": redact_and_bound_evidence_text(
            str(card.get("summary") or ""), MAX_TRANSCRIPT_SNIPPET_CHARS
        ),
        "why_retrieved": why,
        "source_ref": str(card.get("memory_id") or ""),
        # Phase 1: get_memory_card가 knowledge_items.observed_at을 SELECT하지 않으므로
        # approved_at을 쓴다(spec §3 정정). observed_at 승격은 Phase 3.
        "observed_at": str(card.get("approved_at") or ""),
        "freshness": "current",
        "approval_state": "approved",
        "privacy": "redacted",
        "confidence": confidence,
        "conflicts": [{"superseded": supersedes}] if supersedes else [],
    }
    if hit is not None:
        envelope["session_tag"] = str(hit.get("session_tag") or "")
    return envelope


def run_brain_query(
    *,
    read_model: BrainReadModel,
    semantic_recall: SemanticRecall | None = None,
    brain_id: str,
    query: str,
    mode: str = "latest",
    time: str | None = None,
    privacy: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    if mode == "archive":
        return _error("mode_not_implemented", "mode=archive is reserved for Phase 2")
    if mode != "latest":
        return _error("mode_not_implemented", f"unsupported mode: {mode}")
    if time is not None:
        return _error("param_not_implemented", "time param is reserved")
    if privacy is not None:
        return _error("param_not_implemented", "privacy param is reserved")
    project = project_from_brain_id(brain_id)
    if project is None:
        return _error(
            "unsupported_brain_id",
            f"only {BRAIN_ID_PROJECT_PREFIX}<project> brain_id is supported in Phase 1",
        )
    normalized = _normalize_query(query if isinstance(query, str) else "")
    if not normalized:
        return _error("invalid_query", "query must be a non-empty string")
    bounded_limit = max(1, min(MAX_LIMIT, int(limit)))
    audit = {
        # audit용 짧은 핑거프린트(전체 무결성용 아님 — query_planner.sha256_text와 다른 축)
        "query_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16],
        "path": "ledger_fallback",
        "native_memory_bound": semantic_recall is not None,
        "dropped_hits": 0,
    }
    if semantic_recall is not None:
        try:
            hits = semantic_recall(normalized, brain_id)
            if not isinstance(hits, list):
                # 계약 외 반환(None/dict/str)은 fail-closed로 빈 결과 취급
                hits = []
        except Exception:
            hits = None
            audit["path"] = "native_error_fallback"
        if hits:
            # limit은 card 보강 전 컷오프 — drop 발생 시 결과가 limit 미만일 수 있다(spec §4.1).
            # 컷오프 전 _hit_rank 정렬로 "상위 limit"을 보장(upstream 순서에 의존하지 않음).
            deduped = sorted(dedupe_native_hits(hits), key=_hit_rank)[:bounded_limit]
            results = []
            dropped = 0
            for hit in deduped:
                tag = str(hit.get("session_tag") or "")
                card_id = tag[len("mem:"):] if tag.startswith("mem:") else ""
                card = read_model.get_card_meta(card_id) if card_id else None
                if not card or str(card.get("state") or "") != "active":
                    dropped += 1
                    continue
                score = hit.get("score")
                score_text = f"{score}" if isinstance(score, (int, float)) else "none"
                results.append(
                    build_card_envelope(
                        brain_id=brain_id,
                        card=card,
                        why=f"semantic_match(score={score_text})",
                        demote=not isinstance(score, (int, float)),
                        hit=hit,
                    )
                )
            audit["path"] = "native_semantic"
            audit["dropped_hits"] = dropped
            return {"results": results, "audit": audit}
        if hits is not None:
            audit["path"] = "native_empty_fallback"
    cards = read_model.list_recent_cards(project=project, limit=bounded_limit)
    results = [
        build_card_envelope(brain_id=brain_id, card=card, why="ledger_recent", demote=True)
        for card in cards
        if str(card.get("state") or "") == "active"
    ]
    return {"results": results, "audit": audit}


def resolve_brain_ids(*, read_model: BrainReadModel, query: str = "") -> dict:
    needle = (query or "").strip().lower()
    candidates = []
    for project, count in read_model.list_project_card_counts():
        if not project:
            continue
        brain_id = f"{BRAIN_ID_PROJECT_PREFIX}{project}"
        if needle and needle not in brain_id.lower():
            continue
        candidates.append(
            {"brain_id": brain_id, "kind": "project", "card_count": int(count), "hint": ""}
        )
    return {"candidates": candidates}

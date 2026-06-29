from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .memory_card import (
    CANDIDATE_TYPES,
    MEMORY_CARD_TYPES,
    build_memory_candidate,
    validate_judgment_basis_bundle,
    validate_memory_card_envelope,
    validate_source_locator,
)


# Prompt hardened against live deepseek-v4-flash:cloud (2026-06-07): the weak form
# produced conversational prose; this strict form + few-shot yields a clean JSON
# array. Type-label drift (short labels) is absorbed downstream by _normalize_type.
_EXTRACTION_SYSTEM_PROMPT = (
    "You are a strict JSON extraction function, not a chat assistant. "
    "Output a JSON array and NOTHING else: start with '[', end with ']', no markdown, no prose, no greeting.\n"
    "Every element MUST be an object with EXACTLY two string keys: \"type\" and \"statement\". "
    "Do NOT use the category name as a key; the category goes in the \"type\" value.\n"
    '"type" must be one of: ' + ", ".join(CANDIDATE_TYPES) + ".\n"
    '"statement" is one concise self-contained sentence.\n'
    "Extract only durable cross-session facts, decisions, preferences, rules, skills, open tasks, or risks. "
    "Omit transient chatter.\n"
    'CORRECT: [{"type":"project_decision","statement":"Runtime store lives in the server container."},'
    '{"type":"risk_or_constraint","statement":"Splitting the ledger god class is too costly."}]\n'
    'WRONG: [{"decision":"..."}]  (never use the category as a key)'
)
LLM_BRAIN_CANDIDATE_SCHEMA_VERSION = "llm_brain_memory_card_candidate.v1"
HIGH_SIGNAL_EVENT_KINDS = (
    "user_approval",
    "commit",
    "merge",
    "runtime_verification",
    "high_severity_drift",
)
_RAW_TRANSCRIPT_KEYS = {
    "raw",
    "raw_text",
    "raw_body",
    "raw_transcript",
    "transcript_body",
    "transcript_text",
}


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_candidate_items(raw) -> list[dict]:
    if not isinstance(raw, str):
        return []
    try:
        data = json.loads(_strip_code_fence(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


# LLMs (e.g. deepseek-v4-flash) tend to emit short category labels instead of the
# canonical enum. Normalize known synonyms; drop truly-unknown types fail-closed.
_TYPE_SYNONYMS = {
    "fact": "semantic_fact",
    "semantic": "semantic_fact",
    "preference": "user_preference",
    "profile": "user_preference",
    "decision": "project_decision",
    "rule": "procedural_rule",
    "procedure": "procedural_rule",
    "skill": "tool_skill",
    "tool": "tool_skill",
    "task": "unresolved_task",
    "todo": "unresolved_task",
    "risk": "risk_or_constraint",
    "constraint": "risk_or_constraint",
}


def _normalize_type(raw_type) -> str | None:
    candidate_type = str(raw_type or "").strip().lower()
    if candidate_type in CANDIDATE_TYPES:
        return candidate_type
    return _TYPE_SYNONYMS.get(candidate_type)


def build_index_completion_fn(client, *, llm_id: str = ""):
    """Adapt a RetiredIndexBridge client into a miner ``completion_fn``.

    The miner stays backend-neutral: it only knows ``completion_fn(messages) ->
    str``. This adapter binds the concrete RetiredIndexBridge ``chat_completion`` (stateless
    tenant-default chat model; ``llm_id`` optional override) behind that seam.
    """

    def completion_fn(messages):
        return client.chat_completion(messages, llm_id=llm_id)

    return completion_fn


class LlmMemoryMiner:
    """Real miner: an injected ``completion_fn`` (RetiredIndexBridge chat completion) extracts
    typed candidates. The LLM only yields ``(type, statement)`` pairs; assembly
    reuses :func:`build_memory_candidate` so redaction, bounding, evidence_refs,
    sensitivity, and approval_state stay in one place. Live calls are isolated
    behind ``completion_fn`` for dry-run/fixture testing.
    """

    def __init__(self, *, completion_fn, max_candidates: int = 5):
        self.completion_fn = completion_fn
        self.max_candidates = max_candidates

    def mine_chunk(self, chunk: dict) -> list[dict]:
        body = str(chunk.get("redacted_text") or "")
        user_content = (
            'Extract durable memory candidates from the SESSION below. '
            'Output ONLY a JSON array; each element {"type","statement"}. '
            'Begin your reply with "[" and output no other text.\n\nSESSION:\n' + body
        )
        messages = [
            {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        items = _parse_candidate_items(self.completion_fn(messages))
        evidence_refs = [{"knowledge_id": chunk["knowledge_id"], "content_hash": chunk["content_hash"]}]
        candidates = []
        for item in items:
            if len(candidates) >= self.max_candidates:
                break
            candidate_type = _normalize_type(item.get("type"))
            statement = str(item.get("statement") or "").strip()
            if candidate_type is None or not statement:
                continue
            candidates.append(
                build_memory_candidate(
                    candidate_type=candidate_type,
                    statement=statement,
                    project=chunk["project"],
                    provider=chunk["provider"],
                    evidence_refs=evidence_refs,
                )
            )
        return candidates


def memory_card_candidate_idempotency_key(source_span: Mapping[str, Any]) -> str:
    """Stable candidate identity from source/span/content, independent of cycle end."""

    normalized = _normalize_candidate_source_span(source_span)
    seed = _stable_json(
        {
            "schema_version": LLM_BRAIN_CANDIDATE_SCHEMA_VERSION,
            "source_ref": normalized["source_ref"],
            "span_ref": normalized["span_ref"],
            "content_hash": normalized["content_hash"],
            "card_type": normalized["card_type"],
            "typed_payload": normalized["typed_payload"],
        }
    )
    return "llm_brain_candidate:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def build_memory_card_candidate_from_source_span(
    source_span: Mapping[str, Any],
    *,
    refresh_watermark: str,
    mining_reason: str = "refresh_cycle",
) -> dict:
    """Build a candidate MemoryCard without copying raw transcript or writing state."""

    normalized = _normalize_candidate_source_span(source_span)
    idempotency_key = memory_card_candidate_idempotency_key(normalized)
    candidate_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    source_ref = normalized["source_ref"]
    span_ref = normalized["span_ref"]
    evidence_hashes = [normalized["content_hash"]]
    basis = {
        "judgment_id": "judgment_" + candidate_hash[:16],
        "memory_id": "mem_candidate_" + candidate_hash[:16],
        "source_refs": [source_ref],
        "span_refs": [span_ref],
        "redacted_summary": normalized["summary"],
        "evidence_hashes": evidence_hashes,
        "deterministic_signals": [{"kind": mining_reason, "refresh_watermark": refresh_watermark}],
        "model_reason": str(normalized.get("model_reason") or ""),
        "confidence": float(normalized["confidence"]),
        "policy_version": str(normalized.get("policy_version") or "policy.v0"),
        "evaluator_version": str(normalized.get("evaluator_version") or "eval.v0"),
    }
    validate_judgment_basis_bundle(basis)
    card = {
        "memory_id": basis["memory_id"],
        "brain_id": normalized["brain_id"],
        "card_type": normalized["card_type"],
        "scope": normalized["scope"],
        "project": normalized["project"],
        "provider": normalized["provider"],
        "title": normalized["title"],
        "summary": normalized["summary"],
        "render_text": normalized["render_text"],
        "lifecycle_state": "candidate",
        "judgment_state": "none",
        "status": "candidate",
        "approval_state": "suggested",
        "governance_tier": normalized["governance_tier"],
        "freshness": "unknown",
        "currentness": "unknown",
        "confidence": float(normalized["confidence"]),
        "confidence_basis": normalized["confidence_basis"],
        "source_refs": [source_ref],
        "evidence_refs": [source_ref],
        "evidence_hashes": evidence_hashes,
        "derived_from": [str(source_ref.get("source_id") or source_ref.get("ref_id") or "")],
        "supersedes": [],
        "superseded_by": [],
        "conflicts": [],
        "active_until": None,
        "typed_payload": normalized["typed_payload"],
        "candidate_id": "cand_" + candidate_hash[:16],
        "idempotency_key": idempotency_key,
        "refresh_watermark": refresh_watermark,
        "span_refs": [span_ref],
        "judgment_basis_bundle": basis,
        "mining_reason": mining_reason,
    }
    validate_memory_card_envelope(card)
    return card


def mine_refresh_cycle_candidates(
    source_spans: list[Mapping[str, Any]],
    *,
    refresh_watermark: str,
    max_candidates: int = 50,
) -> dict:
    """Batch candidate mining for a session-memory refresh cycle.

    The input records must already contain redacted summaries and opaque source
    locators from transcript-memory. This function intentionally performs no
    ledger write, queue write, RetiredIndexBridge write, or raw transcript lookup.
    """

    candidates = []
    skipped = []
    seen_keys: set[str] = set()
    for index, source_span in enumerate(source_spans):
        if len(candidates) >= max(max_candidates, 1):
            break
        try:
            candidate = build_memory_card_candidate_from_source_span(
                source_span,
                refresh_watermark=refresh_watermark,
                mining_reason="refresh_cycle",
            )
        except ValueError as exc:
            skipped.append({"index": index, "reason": str(exc)})
            continue
        key = candidate["idempotency_key"]
        if key in seen_keys:
            skipped.append({"index": index, "reason": "duplicate_idempotency_key"})
            continue
        seen_keys.add(key)
        candidates.append(candidate)
    return {
        "schema_version": "llm_brain_refresh_cycle_mining_report.v1",
        "refresh_watermark": refresh_watermark,
        "examined_count": len(source_spans),
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "candidates": candidates,
        "skipped": skipped,
    }


def build_immediate_candidate_enqueue(event: Mapping[str, Any]) -> dict:
    """Build a high-signal enqueue record without persisting the queue item."""

    if not isinstance(event, Mapping):
        raise ValueError("high-signal event must be an object")
    event_kind = str(event.get("event_kind") or "")
    if event_kind not in HIGH_SIGNAL_EVENT_KINDS:
        raise ValueError("unsupported high-signal event kind")
    source_span = dict(event)
    if event_kind == "high_severity_drift":
        payload = source_span.get("typed_payload") or {}
        if source_span.get("card_type") != "drift" or payload.get("severity") != "high":
            raise ValueError("high_severity_drift requires card_type=drift and severity=high")
    refresh_watermark = str(event.get("refresh_watermark") or "immediate")
    candidate = build_memory_card_candidate_from_source_span(
        source_span,
        refresh_watermark=refresh_watermark,
        mining_reason=event_kind,
    )
    return {
        "schema_version": "llm_brain_immediate_candidate_enqueue.v1",
        "event_kind": event_kind,
        "enqueue_mode": "fast_path",
        "write_performed": False,
        "candidate": candidate,
    }


def _normalize_candidate_source_span(source_span: Mapping[str, Any]) -> dict:
    if not isinstance(source_span, Mapping):
        raise ValueError("source span must be an object")
    _assert_no_raw_transcript_input(source_span)
    source_ref = _normalize_locator(_required(source_span, "source_ref"), "source_ref")
    span_ref = _normalize_locator(_required(source_span, "span_ref"), "span_ref")
    source_ref.setdefault("source_owner", "transcript_memory_canonical_store")
    source_ref.setdefault("source_kind", "session_memory_refresh")
    source_ref.setdefault("access_mode", "source_ref_only")
    span_ref.setdefault("source_owner", source_ref["source_owner"])
    span_ref.setdefault("source_kind", "session_memory_span")
    span_ref.setdefault("access_mode", "span_ref_only")
    content_hash = str(_required(source_span, "content_hash"))
    if not content_hash.startswith("sha256:"):
        raise ValueError("source span content_hash must be sha256")
    card_type = str(_required(source_span, "card_type"))
    if card_type not in MEMORY_CARD_TYPES:
        raise ValueError("source span card_type must be a MemoryCard card_type")
    project = str(_required(source_span, "project"))
    provider = str(_required(source_span, "provider"))
    summary = str(source_span.get("redacted_summary") or source_span.get("summary") or "").strip()
    if not summary:
        raise ValueError("source span requires redacted_summary")
    confidence = source_span.get("confidence", 0.5)
    return {
        "source_ref": source_ref,
        "span_ref": span_ref,
        "content_hash": content_hash,
        "card_type": card_type,
        "typed_payload": dict(_required(source_span, "typed_payload")),
        "brain_id": str(source_span.get("brain_id") or f"/project/{project}"),
        "scope": str(source_span.get("scope") or "project"),
        "project": project,
        "provider": provider,
        "title": str(source_span.get("title") or _default_candidate_title(card_type)),
        "summary": summary,
        "render_text": str(source_span.get("render_text") or summary),
        "governance_tier": str(source_span.get("governance_tier") or "medium"),
        "confidence": confidence,
        "confidence_basis": str(source_span.get("confidence_basis") or "refresh mining signal"),
        "policy_version": str(source_span.get("policy_version") or "policy.v0"),
        "evaluator_version": str(source_span.get("evaluator_version") or "eval.v0"),
        "model_reason": str(source_span.get("model_reason") or ""),
    }


def _normalize_locator(value: Any, field_name: str) -> dict:
    locator = validate_source_locator(value, field_name=field_name)
    if isinstance(locator, str):
        return {"ref_id": locator}
    return dict(locator)


def _required(value: Mapping[str, Any], field_name: str) -> Any:
    if field_name not in value:
        raise ValueError(f"source span missing required field: {field_name}")
    return value[field_name]


def _assert_no_raw_transcript_input(value: Any, path: str = "source_span") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if key_text in _RAW_TRANSCRIPT_KEYS:
                raise ValueError(f"{path}.{key} would copy raw transcript content")
            _assert_no_raw_transcript_input(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_raw_transcript_input(child, f"{path}[{index}]")


def _default_candidate_title(card_type: str) -> str:
    return card_type.replace("_", " ").capitalize() + " candidate"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


_PREFIX_TO_TYPE = {
    "fact": "semantic_fact",
    "preference": "user_preference",
    "profile": "user_preference",
    "decision": "project_decision",
    "rule": "procedural_rule",
    "skill": "tool_skill",
    "task": "unresolved_task",
    "risk": "risk_or_constraint",
    "constraint": "risk_or_constraint",
}


class FakeMemoryMiner:
    def __init__(self, *, max_candidates: int = 5):
        self.max_candidates = max_candidates

    def mine_chunk(self, chunk: dict) -> list[dict]:
        candidates = []
        evidence_refs = [{"knowledge_id": chunk["knowledge_id"], "content_hash": chunk["content_hash"]}]
        for raw_line in str(chunk.get("redacted_text") or "").splitlines():
            if len(candidates) >= self.max_candidates:
                break
            label, statement = _split_labeled_line(raw_line)
            if not label:
                continue
            candidate_type = _PREFIX_TO_TYPE.get(label)
            if candidate_type is None:
                continue
            sensitivity = "profile_changing" if label == "profile" else None
            candidates.append(
                build_memory_candidate(
                    candidate_type=candidate_type,
                    statement=statement,
                    project=chunk["project"],
                    provider=chunk["provider"],
                    evidence_refs=evidence_refs,
                    sensitivity=sensitivity,
                )
            )
        return candidates


def _split_labeled_line(raw_line: str) -> tuple[str, str]:
    if ":" not in raw_line:
        return "", ""
    prefix, statement = raw_line.split(":", 1)
    label = prefix.strip().lower()
    statement = statement.strip()
    if not statement:
        return "", ""
    return label, statement

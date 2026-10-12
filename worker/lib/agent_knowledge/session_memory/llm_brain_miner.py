"""LLM-brain envelope miner — emits MemoryCard envelopes directly (Option B).

Instead of the legacy candidate_type miner (7 CANDIDATE_TYPES) plus a lossy 7->6 bridge,
this miner prompts the LLM to emit the 6 MEMORY_CARD_TYPES with their typed_payload, then
assembles each item through build_memory_card_candidate_from_source_span so redaction,
idempotency, evidence_refs, and envelope validation stay in one place. Output is directly
consumable by run_autopilot_cycle. Malformed items are skipped, never crash the cycle.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .memory_card import (MEMORY_CARD_TYPES, validate_content_hash, validate_observed_at,
                          validate_source_locator, _ensure_no_forbidden_content)
from .memory_miner import _parse_candidate_items, build_memory_card_candidate_from_source_span


_TYPED_PAYLOAD_SCHEMAS = (
    "decision={decision,rationale,alternatives,consequence,authority_ref}; "
    "task={task_state,next_action,blocker,owner_hint,status}; "
    "preference={preference,explicitness,repeated_count,confirmation_status,applies_to}; "
    "status={status_value,observed_at,expires_at,current_authority}; "
    "evidence={evidence_kind,result_status,hash_refs,count_refs}; "
    "drift={subject,expected_state,observed_state,drift_kind,severity,authority_lane,"
    "source_precedence_rank,resolution_action,suggested_action,basis_refs}"
)

_ENVELOPE_EXTRACTION_PROMPT = (
    "You extract durable project memory from a redacted work session. "
    "Output ONLY a JSON array; each candidate is an object with keys: "
    '"card_type" (one of: decision, task, drift, preference, status, evidence), '
    '"title" (short), "statement" (one redacted sentence), and '
    '"typed_payload" (an object whose fields match the card_type schema: '
    + _TYPED_PAYLOAD_SCHEMAS + "). "
    "Emit nothing but durable, decision-grade memory. Begin your reply with '[' and output no other text."
)


def _chunk_source_provenance(
    chunk: Mapping[str, Any], *, scope: str, span_index: int = 0
) -> dict[str, Any]:
    """Preserve canonical locators; fallback spans are local to this chunk."""
    metadata_value = chunk.get("metadata")
    metadata: Mapping[str, Any] = metadata_value if isinstance(metadata_value, Mapping) else {}

    def field(key: str, default: Any = None) -> Any:
        value = chunk.get(key)
        if value is None:
            value = metadata.get(key)
        return default if value is None else value

    project = field("project", "")
    knowledge_id = field("knowledge_id") or field("chunk_id") or field("content_hash", "")
    provenance = {
        "source_ref": field("source_ref", {"source_id": knowledge_id}),
        "span_ref": field("span_ref", {"span_id": f"{knowledge_id}#{span_index}"}),
        "content_hash": field("content_hash", ""),
        "brain_id": field("brain_id", f"/project/{project}"),
        "scope": field("scope", scope),
        "project": project,
        "provider": field("provider", ""),
    }
    observed_at = field("observed_at")
    if observed_at is not None:
        provenance["observed_at"] = observed_at
    return provenance


def _validate_chunk_source(chunk: Mapping[str, Any], *, scope: str) -> dict[str, Any]:
    """One source-admission rule shared by intake and batched mining."""
    if not isinstance(chunk, Mapping):
        raise ValueError("source chunk must be an object")
    provenance = _chunk_source_provenance(chunk, scope=scope)
    body = chunk.get("redacted_text")
    if body is None:
        body = chunk.get("content")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("source chunk requires redacted content")
    for field in ("project", "brain_id", "scope"):
        value = provenance[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("source chunk requires nonempty scope fields")
        _ensure_no_forbidden_content(value, "source_scope")
    validate_content_hash(provenance["content_hash"])
    validate_source_locator(provenance["source_ref"], field_name="source_ref")
    validate_source_locator(provenance["span_ref"], field_name="span_ref")
    if "observed_at" in provenance:
        validate_observed_at(provenance["observed_at"])
    return provenance


class LlmBrainEnvelopeMiner:
    def __init__(self, *, completion_fn: Callable[[list[dict]], Any], scope: str = "project", max_candidates: int = 5):
        self.completion_fn = completion_fn
        self.scope = scope
        self.max_candidates = max_candidates

    def mine_chunk(self, chunk: Mapping[str, Any], *, refresh_watermark: str) -> list[dict]:
        # Normalize both the flat fixture shape and the real list_transcript_memory_chunks
        # shape {content, content_hash, metadata{knowledge_id, project, provider, ...}}.
        provenance = _chunk_source_provenance(chunk, scope=self.scope)
        body = str(chunk.get("redacted_text") or chunk.get("content") or "")
        if not (body and provenance["project"] and provenance["content_hash"]):
            return []

        messages = [
            {"role": "system", "content": _ENVELOPE_EXTRACTION_PROMPT},
            {"role": "user", "content": "SESSION:\n" + body},
        ]
        items = _parse_candidate_items(self.completion_fn(messages))

        candidates: list[dict] = []
        for index, item in enumerate(items):
            if len(candidates) >= self.max_candidates:
                break
            if not isinstance(item, Mapping):
                continue
            card_type = str(item.get("card_type") or "")
            statement = str(item.get("statement") or "").strip()
            typed_payload = item.get("typed_payload")
            if card_type not in MEMORY_CARD_TYPES or not statement or not isinstance(typed_payload, Mapping):
                continue
            source_span = {
                **_chunk_source_provenance(chunk, scope=self.scope, span_index=index),
                "card_type": card_type,
                "title": str(item.get("title") or card_type),
                "redacted_summary": statement,
                "typed_payload": dict(typed_payload),
                "confidence": float(item.get("confidence") or 0.8),
                "confidence_basis": "llm-mined from transcript-memory",
            }
            try:
                candidate = build_memory_card_candidate_from_source_span(
                    source_span, refresh_watermark=refresh_watermark
                )
            except (ValueError, KeyError, TypeError):
                continue
            candidates.append(candidate)
        return candidates

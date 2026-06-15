"""LLM-brain envelope miner — emits MemoryCard envelopes directly (Option B).

Instead of the legacy candidate_type miner (7 CANDIDATE_TYPES) plus a lossy 7->6 bridge,
this miner prompts the LLM to emit the 6 MEMORY_CARD_TYPES with their typed_payload, then
assembles each item through build_memory_card_candidate_from_source_span so redaction,
idempotency, evidence_refs, and envelope validation stay in one place. Output is directly
consumable by run_autopilot_cycle. Malformed items are skipped, never crash the cycle.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..memory_card import MEMORY_CARD_TYPES
from .memory_miner import _parse_candidate_items, build_memory_card_candidate_from_source_span


_ENVELOPE_EXTRACTION_PROMPT = (
    "You extract durable project memory from a redacted work session. "
    "Output ONLY a JSON array; each element is an object with keys: "
    '"card_type" (one of: decision, task, drift, preference, status, evidence), '
    '"title" (short), "statement" (one redacted sentence), and '
    '"typed_payload" (an object whose fields match the card_type schema: '
    "decision={decision,rationale,alternatives,consequence,authority_ref}; "
    "task={task_state,next_action,blocker,owner_hint,status}; "
    "preference={preference,explicitness,repeated_count,confirmation_status,applies_to}; "
    "status={status_value,observed_at,expires_at,current_authority}; "
    "evidence={evidence_kind,result_status,hash_refs,count_refs}; "
    "drift={subject,expected_state,observed_state,drift_kind,severity,authority_lane,"
    "source_precedence_rank,resolution_action,suggested_action,basis_refs}). "
    "Emit nothing but durable, decision-grade memory. Begin your reply with '[' and output no other text."
)


class LlmBrainEnvelopeMiner:
    def __init__(self, *, completion_fn: Callable[[list[dict]], Any], scope: str = "project", max_candidates: int = 5):
        self.completion_fn = completion_fn
        self.scope = scope
        self.max_candidates = max_candidates

    def mine_chunk(self, chunk: Mapping[str, Any], *, refresh_watermark: str) -> list[dict]:
        # Normalize both the flat fixture shape and the real list_transcript_memory_chunks
        # shape {content, content_hash, metadata{knowledge_id, project, provider, ...}}.
        metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), Mapping) else {}

        def _field(key: str) -> str:
            return str(chunk.get(key) or metadata.get(key) or "")

        body = str(chunk.get("redacted_text") or chunk.get("content") or "")
        project = _field("project")
        provider = _field("provider")
        knowledge_id = _field("knowledge_id") or _field("chunk_id") or _field("content_hash")
        content_hash = _field("content_hash")
        if not (body and project and content_hash):
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
                "source_ref": {"source_id": knowledge_id},
                "span_ref": {"span_id": f"{knowledge_id}#{index}"},
                "content_hash": content_hash,
                "brain_id": f"/project/{project}",
                "card_type": card_type,
                "scope": self.scope,
                "project": project,
                "provider": provider,
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

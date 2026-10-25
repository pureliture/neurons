"""LLM-brain envelope miner — emits MemoryCard envelopes directly (Option B).

Instead of the legacy candidate_type miner (7 CANDIDATE_TYPES) plus a lossy 7->6 bridge,
this miner prompts the LLM to emit the 6 MEMORY_CARD_TYPES with their typed_payload, then
assembles each item through build_memory_card_candidate_from_source_span so redaction,
idempotency, evidence_refs, and envelope validation stay in one place. Output is directly
consumable by run_autopilot_cycle. Malformed items are skipped, never crash the cycle.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping
import json

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
    "Emit nothing but durable, decision-grade memory. Begin your reply with '[' and output no other text. "
    "When AVAILABLE_EVIDENCE_REFS exists, optionally include evidence_id_hashes with up to 20 listed IDs "
    "whose source spans support this candidate. Use [] for conversation-only claims. Never invent IDs."
)


def _chunk_source_provenance(
    chunk: Mapping[str, Any], *, scope: str, span_index: int = 0, evidence_id_hashes=None,
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
    source_hash = field("source_hash", "")
    if source_hash:
        from .observation_refs import HASH
        if not isinstance(source_hash, str) or not HASH.fullmatch(source_hash):
            raise ValueError("exact source revision required")
        ref = provenance["source_ref"]
        provenance["source_ref"] = {**(dict(ref) if isinstance(ref, Mapping) else {"ref_id": ref}),
            "source_revision_hash": source_hash}
    if evidence_id_hashes is not None:
        references = _chunk_observations(chunk)
        by_id = {ref["evidence_id_hash"]: ref for ref in references}
        if (not isinstance(evidence_id_hashes, list) or len(evidence_id_hashes) > 20
                or any(not isinstance(key, str) for key in evidence_id_hashes)
                or len(set(evidence_id_hashes)) != len(evidence_id_hashes)
                or not set(evidence_id_hashes) <= set(by_id)):
            raise ValueError("candidate observation references not grounded in source")
        if evidence_id_hashes:
            ref = provenance["source_ref"]
            provenance["source_ref"] = {**(dict(ref) if isinstance(ref, Mapping) else {"ref_id": ref}),
                "native_observation_refs": [by_id[key] for key in evidence_id_hashes]}
    return provenance


def _chunk_body(chunk):
    return str(chunk.get("redacted_text") or chunk.get("content") or chunk.get("body") or "")


def _chunk_observations(chunk):
    from .observation_refs import validate, HASH, digest
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), Mapping) else {}
    references = chunk.get("observation_refs", metadata.get("observation_refs", []))
    if not references:
        return []
    def field(key):
        return chunk.get(key, metadata.get(key, ""))
    source_hash = field("source_hash")
    if not isinstance(source_hash, str) or not HASH.fullmatch(source_hash) or digest(_chunk_body(chunk)) != field("content_hash"):
        raise ValueError("native observation source revision/body mismatch")
    return validate(references, body=_chunk_body(chunk), session_id_hash=field("session_id_hash"),
        project=field("project"), provider=field("provider"))


def _observation_prompt(chunk, visible_body, *, ranges=None, max_chars=4000):
    ranges = ranges if ranges is not None else [(0, len(visible_body))]
    refs = [ref for ref in _chunk_observations(chunk)
        if any(start <= ref["span_start"] <= ref["span_end"] <= end for start, end in ranges)][:100]
    if not refs:
        return ""
    entries = [{
        "evidence_id_hash": ref["evidence_id_hash"], "span_start": ref["span_start"], "span_end": ref["span_end"]}
        for ref in refs]
    while entries:
        text = "\nAVAILABLE_EVIDENCE_REFS:\n" + json.dumps(entries, separators=(",", ":"))
        if len(text) <= max_chars:
            return text
        entries.pop()
    return ""


def _prompt_reference_ids(prompt):
    if not prompt:
        return set()
    return {item["evidence_id_hash"] for item in json.loads(prompt[len("\nAVAILABLE_EVIDENCE_REFS:\n"):])}


def _bounded_chunk_prompt(chunk, *, max_chars, include_ids=False):
    body = _chunk_body(chunk)
    body_budget = max_chars * 2 // 3
    if len(body) <= body_budget:
        text, ranges = body, [(0, len(body))]
    else:
        marker = "\n...[truncated]...\n"
        half = max(1, (body_budget - len(marker)) // 2)
        text, ranges = body[:half] + marker + body[-half:], [(0, half), (len(body) - half, len(body))]
    prompt = _observation_prompt(chunk, body, ranges=ranges, max_chars=max_chars // 3)
    result = text + prompt
    return (result, _prompt_reference_ids(prompt)) if include_ids else result


def _validate_chunk_source(chunk: Mapping[str, Any], *, scope: str) -> dict[str, Any]:
    """One source-admission rule shared by intake and batched mining."""
    if not isinstance(chunk, Mapping):
        raise ValueError("source chunk must be an object")
    provenance = _chunk_source_provenance(chunk, scope=scope)
    body = _chunk_body(chunk)
    if not isinstance(body, str) or not body.strip():
        raise ValueError("source chunk requires redacted content")
    _chunk_observations(chunk)
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
        body = _chunk_body(chunk)
        if not (body and provenance["project"] and provenance["content_hash"]):
            return []
        evidence_prompt = _observation_prompt(chunk, body)
        visible_ids = _prompt_reference_ids(evidence_prompt)

        messages = [
            {"role": "system", "content": _ENVELOPE_EXTRACTION_PROMPT},
            {"role": "user", "content": "SESSION:\n" + body + evidence_prompt},
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
            try:
                selected = item.get("evidence_id_hashes")
                if selected is not None and (not isinstance(selected, list)
                        or any(not isinstance(key, str) or key not in visible_ids for key in selected)):
                    continue
                grounded = _chunk_source_provenance(chunk, scope=self.scope, span_index=index,
                    evidence_id_hashes=item.get("evidence_id_hashes"))
            except (ValueError, KeyError, TypeError):
                continue
            source_span = {
                **grounded,
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

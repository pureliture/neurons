"""Bounded batch mining for LBrain candidate intake.

``LlmBrainEnvelopeMiner.mine_chunk`` takes one chunk per model call. Calling it
once per session chunk is the expensive shape this module replaces: it groups
several chunks into a single model call and then splits the reply back per
chunk, so cost scales with batches instead of chunks.

The miner itself is deliberately unchanged. This is an additive adapter: it
reuses the same model call builder and the same candidate assembly, so a
single chunk still produces exactly what ``mine_chunk`` produced before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .memory_card import MEMORY_CARD_TYPES

# A model call that carries more than this many characters is split into a
# second call. This keeps one oversized session from flooding the batch.
DEFAULT_MAX_CALL_CHARS = 12_000
# Chunks per call. Five is deliberately small: a larger batch lowers cost but
# buries the decisions that matter most, so quality sets the ceiling, not cost.
DEFAULT_BATCH_SIZE = 5
DEFAULT_MAX_CALLS_PER_RUN = 20


@dataclass(frozen=True)
class BatchMiningReport:
    """Redaction-safe accounting for one batch mining run."""

    chunk_count: int = 0
    batch_count: int = 0
    model_calls: int = 0
    call_char_sizes: tuple[int, ...] = ()
    candidate_count: int = 0
    lower_bound_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "llm_brain_batch_mining.v1",
            "chunk_count": self.chunk_count,
            "batch_count": self.batch_count,
            "model_calls": self.model_calls,
            "call_char_sizes": list(self.call_char_sizes),
            "candidate_count": self.candidate_count,
            "lower_bound_calls": self.lower_bound_calls,
            "calls_saved": self.lower_bound_calls - self.model_calls,
        }


def plan_batches(
    chunks: Sequence[Mapping[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_call_chars: int = DEFAULT_MAX_CALL_CHARS,
) -> list[list[Mapping[str, Any]]]:
    """Group chunks into bounded batches without ever dropping a chunk.

    A batch grows until it reaches ``batch_size`` or its accumulated text
    reaches ``max_call_chars``. The char limit never shrinks a batch below one
    chunk, because a single chunk must still be sent on its own.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if max_call_chars < 1:
        raise ValueError("max_call_chars must be positive")
    batches: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    current_chars = 0
    for chunk in chunks:
        chunk_chars = _chunk_text_length(chunk)
        if current and (len(current) >= batch_size or current_chars + chunk_chars > max_call_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(chunk)
        current_chars += chunk_chars
    if current:
        batches.append(current)
    return batches


def _chunk_text_length(chunk: Mapping[str, Any]) -> int:
    if not isinstance(chunk, Mapping):
        return 0
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), Mapping) else {}
    body = str(chunk.get("redacted_text") or chunk.get("content") or "")
    return len(body)


class BatchedEnvelopeMiner:
    """Adapter that mines several chunks per model call.

    ``mine_batch`` sends one session block per batch and parses one item per
    chunk. Each item keeps the source identity of its own chunk, so a batch of
    five chunks yields candidates whose evidence still points at the chunk the
    model actually read.
    """

    def __init__(
        self,
        *,
        completion_fn: Callable[[list[dict]], Any],
        scope: str = "project",
        max_candidates: int = 5,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_call_chars: int = DEFAULT_MAX_CALL_CHARS,
    ) -> None:
        if not 1 <= int(max_candidates) <= 100:
            raise ValueError("max_candidates must be within 1..100")
        self._completion_fn = completion_fn
        self._scope = scope
        self._max_candidates = int(max_candidates)
        self._batch_size = int(batch_size)
        self._max_call_chars = int(max_call_chars)

    def mine_batches(
        self, chunks: Sequence[Mapping[str, Any]], *, refresh_watermark: str
    ) -> tuple[list[dict], BatchMiningReport]:
        """Mine every chunk through bounded batches.

        Returns per-chunk candidates plus a report. The chunk list order is
        preserved, and a chunk that yields nothing contributes no candidates
        rather than failing the whole batch.
        """
        bounded = [chunk for chunk in chunks if isinstance(chunk, Mapping)]
        if not bounded:
            return [], BatchMiningReport()

        batches = plan_batches(
            bounded, batch_size=self._batch_size, max_call_chars=self._max_call_chars
        )
        candidates: list[dict] = []
        call_sizes: list[int] = []
        for batch in batches:
            items = self._call_model(batch)
            call_sizes.append(sum(_chunk_text_length(chunk) for chunk in batch))
            for span_index, (chunk, item) in enumerate(zip(batch, items)):
                mined = self._candidate_for(
                    chunk, item, span_index=span_index, refresh_watermark=refresh_watermark
                )
                if mined is not None:
                    candidates.append(mined)

        report = BatchMiningReport(
            chunk_count=len(bounded),
            batch_count=len(batches),
            model_calls=len(batches),
            call_char_sizes=tuple(call_sizes),
            candidate_count=len(candidates),
            lower_bound_calls=len(bounded),
        )
        return candidates, report

    # ------------------------------------------------------------------ internals

    def _call_model(self, batch: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any] | None]:
        from .llm_brain_miner import _ENVELOPE_EXTRACTION_PROMPT
        from .memory_miner import _parse_candidate_items

        # One request asks for exactly as many items as there are chunks, in
        # the same order, so the reply can be mapped back deterministically.
        blocks = []
        for index, chunk in enumerate(batch):
            metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), Mapping) else {}
            body = str(chunk.get("redacted_text") or chunk.get("content") or "")
            knowledge_id = str(chunk.get("knowledge_id") or metadata.get("knowledge_id") or "")
            blocks.append(f"### SESSION {index} ({knowledge_id or 'unknown'})\n{body}")
        user_content = (
            f"Below are {len(batch)} redacted work sessions. Return a JSON array of "
            f"exactly {len(batch)} elements, one element per session, in the same order. "
            "Each element is an object with keys card_type, title, statement, typed_payload. "
            "Begin with '[' and output no other text.\n\n" + "\n\n".join(blocks)
        )
        messages = [
            {"role": "system", "content": _ENVELOPE_EXTRACTION_PROMPT},
            {"role": "user", "content": user_content},
        ]
        raw = self._completion_fn(messages)
        parsed = _parse_candidate_items(raw)
        # A short reply is padded rather than mis-aligned: the first chunks
        # keep their items and the remaining chunks are reported as skipped.
        padded: list[Mapping[str, Any] | None] = list(parsed[: len(batch)])
        while len(padded) < len(batch):
            padded.append(None)
        return padded

    def _candidate_for(
        self,
        chunk: Mapping[str, Any],
        item: Mapping[str, Any] | None,
        *,
        span_index: int,
        refresh_watermark: str,
    ) -> dict | None:
        if item is None:
            return None
        from .memory_miner import build_memory_card_candidate_from_source_span

        metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), Mapping) else {}
        chunk_map: Mapping[str, Any] = chunk

        def _field(key: str) -> str:
            return str(chunk_map.get(key) or metadata.get(key) or "")

        body = str(chunk.get("redacted_text") or chunk.get("content") or "")
        project = _field("project")
        provider = _field("provider")
        knowledge_id = _field("knowledge_id") or _field("chunk_id") or _field("content_hash")
        content_hash = _field("content_hash")
        if not (body and project and content_hash):
            return None
        card_type = str(item.get("card_type") or "")
        item_map: Mapping[str, Any] = item
        statement = str(item.get("statement") or "").strip()
        typed_payload = item_map.get("typed_payload")
        if card_type not in MEMORY_CARD_TYPES or not statement or not isinstance(typed_payload, Mapping):
            return None
        source_span = {
            "source_ref": {"source_id": knowledge_id},
            # The span index matches ``LlmBrainEnvelopeMiner`` exactly, so
            # batching never rewrites an existing candidate identity.
            "span_ref": {"span_id": f"{knowledge_id}#{span_index}"},
            "content_hash": content_hash,
            "brain_id": f"/project/{project}",
            "card_type": card_type,
            "scope": self._scope,
            "project": project,
            "provider": provider,
            "title": str(item_map.get("title") or card_type),
            "redacted_summary": statement,
            "typed_payload": dict(typed_payload),
            "confidence": float(item_map.get("confidence") or 0.8),
            "confidence_basis": "llm-mined from transcript-memory",
        }
        try:
            return build_memory_card_candidate_from_source_span(
                source_span, refresh_watermark=refresh_watermark
            )
        except (ValueError, KeyError, TypeError):
            return None


__all__ = [
    "BatchedEnvelopeMiner",
    "BatchMiningReport",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_CALL_CHARS",
    "DEFAULT_MAX_CALLS_PER_RUN",
    "plan_batches",
]

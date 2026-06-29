"""Shared conversation_chunk overlap canonicalization policy.

Single source of truth for how same-session conversation chunks are de-overlapped
before they become session-memory: collapse exact duplicates, then drop a shorter
chunk that is *subsumed* by a longer one — i.e. the longer chunk strictly contains
the shorter's turn window AND its sanitized text contains the shorter's text.

Both the canonical M3 materializer (`couchdb_source.session_memory_materializer`)
and the regeneration path (`memory_regeneration`) use this, so the overlap policy
is identical on every path. The functions are pure (no IO, no mutation of inputs).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..redaction import redact_public_ingress_text

_SESSION_MEMORY_CHUNK_HEADER_LABELS = (
    "session_id_hash",
    "source_locator_hash",
    "turn_start_index",
    "turn_end_index",
    "turn_part_index",
    "turn_part_count",
    "part_index",
    "part_count",
    "char_start",
    "char_end",
    "content_hash",
    "knowledge_id",
    "chunk_id",
    "dataset_id",
    "dataset_ref",
    "datasetId",
    "dataset_ids",
    "document_id",
    "document_ref",
    "documentId",
    "document_ids",
    "token",
    "access_token",
    "api_key",
)
_SESSION_MEMORY_HEADER_LABEL_ALTERNATION = "|".join(
    re.escape(label) for label in _SESSION_MEMORY_CHUNK_HEADER_LABELS
)
_SESSION_MEMORY_HEADER_LINE_RE = re.compile(
    rf"^\s*(?:{_SESSION_MEMORY_HEADER_LABEL_ALTERNATION})\s*[:=]\s*.*$",
    flags=re.IGNORECASE,
)
# Same labels as the line matcher, but stripping inline ``label: value`` spans.
_SESSION_MEMORY_HEADER_FIELD_RE = re.compile(
    rf"\b(?:{_SESSION_MEMORY_HEADER_LABEL_ALTERNATION})\s*[:=]\s*[^\s,;\]\)\n]+",
    flags=re.IGNORECASE,
)


def sanitize_session_memory_chunk_text(raw_text: str) -> str:
    text = str(raw_text)
    text = "\n".join(line for line in text.splitlines() if not _SESSION_MEMORY_HEADER_LINE_RE.match(line))
    text = _SESSION_MEMORY_HEADER_FIELD_RE.sub("<redacted:private-field>", text)
    text = re.sub(
        r"\b(?:ds_[A-Za-z0-9_-]+|doc_[A-Za-z0-9_-]+|kn_[A-Za-z0-9_-]+|chunk_[A-Za-z0-9_-]+)\b",
        "<redacted:private-field>",
        text,
    )
    return redact_public_ingress_text(text)


@dataclass(frozen=True)
class ChunkView:
    """Minimal view of a conversation chunk needed for overlap canonicalization.

    `text` is the chunk's body (already public-safe/redacted for stored docs); the
    overlap policy re-sanitizes it for the containment comparison.
    """

    content_hash: str
    turn_start_index: int
    turn_end_index: int
    part_index: int
    part_count: int
    char_start: int
    char_end: int
    redaction_version: str
    text: str


def chunk_turn_window_strictly_contains(container, candidate) -> bool:
    """True when ``container``'s turn window strictly contains ``candidate``'s.

    Duck-typed on turn_start_index/turn_end_index so it works for ChunkView and any
    record carrying those fields.
    """
    container_start = int(container.turn_start_index)
    container_end = int(container.turn_end_index)
    candidate_start = int(candidate.turn_start_index)
    candidate_end = int(candidate.turn_end_index)
    return (
        (container_start, container_end) != (candidate_start, candidate_end)
        and container_start <= candidate_start
        and container_end >= candidate_end
    )


def canonicalize_chunk_views(views: Iterable[ChunkView]) -> tuple[list[ChunkView], dict]:
    """Drop exact duplicates and subsumed (shorter, contained) chunks.

    Returns (kept_views_in_input_order, report). Input order of survivors is
    preserved so a pre-sorted sequence stays sorted. ``report["kept_indexes"]``
    holds each survivor's index into the input sequence, so callers can map back to
    their own records by index (no object-identity dependency).
    """
    views = list(views)
    # Carry each survivor's original index so callers can map back by index
    # (report["kept_indexes"]) rather than by object identity.
    deduped: list[tuple[int, ChunkView]] = []
    seen_exact: set[tuple[object, ...]] = set()
    exact_duplicate_count = 0
    for original_index, view in enumerate(views):
        source_key = (
            view.content_hash,
            view.turn_start_index,
            view.turn_end_index,
            int(view.part_index or 1),
            int(view.part_count or 1),
            int(view.char_start or 0),
            int(view.char_end or 0),
            view.redaction_version,
        )
        if source_key in seen_exact:
            exact_duplicate_count += 1
            continue
        seen_exact.add(source_key)
        deduped.append((original_index, view))

    sanitized_by_position = {
        position: sanitize_session_memory_chunk_text(view.text).strip()
        for position, (_, view) in enumerate(deduped)
    }
    subsumed_positions: set[int] = set()
    for container_position, (_, container) in enumerate(deduped):
        container_text = sanitized_by_position[container_position]
        if not container_text:
            continue
        for candidate_position, (_, candidate) in enumerate(deduped):
            if container_position == candidate_position or candidate_position in subsumed_positions:
                continue
            if not chunk_turn_window_strictly_contains(container, candidate):
                continue
            candidate_text = sanitized_by_position[candidate_position]
            if candidate_text and candidate_text in container_text:
                subsumed_positions.add(candidate_position)

    kept_pairs = [pair for position, pair in enumerate(deduped) if position not in subsumed_positions]
    kept = [view for _, view in kept_pairs]
    kept_indexes = [original_index for original_index, _ in kept_pairs]
    return kept, {
        "input_count": len(views),
        "kept_count": len(kept),
        "kept_indexes": kept_indexes,
        "exact_duplicate_count": exact_duplicate_count,
        "subsumed_overlap_count": len(subsumed_positions),
        "dropped_count": len(views) - len(kept),
    }

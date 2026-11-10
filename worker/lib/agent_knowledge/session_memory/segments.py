from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..couchdb_source.document_model import sha256_hash
from .chunk_overlap import ChunkView

if TYPE_CHECKING:
    from ..couchdb_source.session_memory_materializer import MaterializedSessionMemory


@dataclass(frozen=True)
class SessionSegment:
    segment_index: int
    turn_start: int
    turn_end: int
    member_hashes: tuple[str, ...]
    text: str
    content_hash: str
    segment_id: str


def _render_segment(
    *,
    provider: str,
    project: str,
    session_id_hash: str,
    segment_index: int,
    views: tuple[ChunkView, ...],
    tool_evidence_bodies: tuple[str, ...] = (),
) -> str:
    turn_start = min(view.turn_start_index for view in views)
    turn_end = max(view.turn_end_index for view in views)
    lines = [
        f"# session-memory {provider} {project}",
        f"session_id_hash: {session_id_hash}",
        f"segment: {segment_index} turns {turn_start}-{turn_end}",
        "",
    ]
    for view in views:
        lines.append(view.text.rstrip())
        lines.append("")
    if tool_evidence_bodies:
        lines.append("## tool_evidence_summary")
        lines.append("")
        for body in tool_evidence_bodies:
            lines.append(body)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _segment_identity(
    *,
    project: str,
    provider: str,
    session_id_hash: str,
    member_hashes: tuple[str, ...],
    max_bytes: int,
) -> str:
    identity = json.dumps(
        (
            "segment_v1",
            project,
            provider,
            session_id_hash,
            member_hashes,
            max_bytes,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return "seg_" + digest[:60]


def build_segments(
    materialized: MaterializedSessionMemory,
    *,
    max_bytes: int,
) -> tuple[SessionSegment, ...]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    views = tuple(materialized.conversation_views)
    if not views:
        raise ValueError("session-memory segmentation requires conversation views")
    evidence = tuple(
        str(body).rstrip()
        for body in getattr(materialized, "tool_evidence_bodies", ())
        if str(body).rstrip()
    )

    grouped: list[tuple[ChunkView, ...]] = []
    current: list[ChunkView] = []
    for view_index, view in enumerate(views):
        candidate = (*current, view)
        segment_index = len(grouped)
        is_final_candidate = view_index == len(views) - 1
        candidate_text = _render_segment(
            provider=materialized.provider,
            project=materialized.project,
            session_id_hash=materialized.session_id_hash,
            segment_index=segment_index,
            views=candidate,
            tool_evidence_bodies=evidence if is_final_candidate else (),
        )
        if current and len(candidate_text.encode("utf-8")) > max_bytes:
            grouped.append(tuple(current))
            current = [view]
        else:
            current.append(view)
    if current:
        grouped.append(tuple(current))

    segments: list[SessionSegment] = []
    for segment_index, members in enumerate(grouped):
        is_final_segment = segment_index == len(grouped) - 1
        text = _render_segment(
            provider=materialized.provider,
            project=materialized.project,
            session_id_hash=materialized.session_id_hash,
            segment_index=segment_index,
            views=members,
            tool_evidence_bodies=evidence if is_final_segment else (),
        )
        member_hashes = tuple(view.content_hash for view in members)
        segments.append(
            SessionSegment(
                segment_index=segment_index,
                turn_start=min(view.turn_start_index for view in members),
                turn_end=max(view.turn_end_index for view in members),
                member_hashes=member_hashes,
                text=text,
                content_hash=sha256_hash(text),
                segment_id=_segment_identity(
                    project=materialized.project,
                    provider=materialized.provider,
                    session_id_hash=materialized.session_id_hash,
                    member_hashes=member_hashes,
                    max_bytes=max_bytes,
                ),
            )
        )
    return tuple(segments)

"""Pure transcript chunk builder for server-owned transcript ingest."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .transcript_model import MAX_TRANSCRIPT_CHUNK_TEXT_CHARS, TranscriptChunk, TranscriptTurn
from .transcript_parsers import ParsedTranscript


@dataclass(frozen=True)
class _TurnTextPart:
    text: str
    part_index: int
    part_count: int
    char_start: int
    char_end: int


def build_transcript_chunks(parsed: ParsedTranscript) -> list[TranscriptChunk]:
    ordered_turns = sorted(parsed.turns, key=lambda item: item.turn_index)
    if not ordered_turns:
        raise ValueError("transcript requires at least one turn")
    chunks: list[TranscriptChunk] = []
    current_turns: list[TranscriptTurn] = []
    for turn in ordered_turns:
        if len(_chunk_text(parsed, [turn])) > MAX_TRANSCRIPT_CHUNK_TEXT_CHARS:
            if current_turns:
                chunks.append(_build_chunk_for_turns(parsed, current_turns))
            chunks.extend(_build_chunks_for_long_turn(parsed, turn))
            current_turns = []
            continue
        candidate = [*current_turns, turn]
        if current_turns and len(_chunk_text(parsed, candidate)) > MAX_TRANSCRIPT_CHUNK_TEXT_CHARS:
            chunks.append(_build_chunk_for_turns(parsed, current_turns))
            current_turns = [turn]
        else:
            current_turns = candidate
    if current_turns:
        chunks.append(_build_chunk_for_turns(parsed, current_turns))
    return chunks


def _build_chunk_for_turns(parsed: ParsedTranscript, turns: list[TranscriptTurn]) -> TranscriptChunk:
    turn_start = min(turn.turn_index for turn in turns)
    turn_end = max(turn.turn_index for turn in turns)
    text = _chunk_text(parsed, sorted(turns, key=lambda item: item.turn_index))
    chunk_seed = f"{parsed.session.session_id_hash}:{turn_start}:{turn_end}:{text}"
    chunk_id = "chunk_" + hashlib.sha256(chunk_seed.encode("utf-8")).hexdigest()[:16]
    return TranscriptChunk.from_text(
        chunk_id=chunk_id,
        session_id_hash=parsed.session.session_id_hash,
        provider=parsed.session.provider,
        project=parsed.session.project,
        turn_start_index=turn_start,
        turn_end_index=turn_end,
        text=text,
        source_status=parsed.source_status,
    )


def _build_chunks_for_long_turn(parsed: ParsedTranscript, turn: TranscriptTurn) -> list[TranscriptChunk]:
    chunks: list[TranscriptChunk] = []
    for part in _split_turn_text(parsed, turn):
        text = _chunk_part_text(parsed, turn, part)
        chunk_seed = (
            f"{parsed.session.session_id_hash}:{turn.turn_index}:{part.part_index}:"
            f"{part.part_count}:{part.char_start}:{part.char_end}:{text}"
        )
        chunk_id = "chunk_" + hashlib.sha256(chunk_seed.encode("utf-8")).hexdigest()[:16]
        chunks.append(
            TranscriptChunk(
                chunk_id=chunk_id,
                session_id_hash=parsed.session.session_id_hash,
                provider=parsed.session.provider,
                project=parsed.session.project,
                turn_start_index=turn.turn_index,
                turn_end_index=turn.turn_index,
                redacted_text=text,
                content_hash=_sha256_content(text),
                source_status=parsed.source_status,
                part_index=part.part_index,
                part_count=part.part_count,
                char_start=part.char_start,
                char_end=part.char_end,
            )
        )
    return chunks


def _split_turn_text(parsed: ParsedTranscript, turn: TranscriptTurn) -> list[_TurnTextPart]:
    text = turn.redacted_text
    part_count = 1
    while True:
        parts: list[_TurnTextPart] = []
        char_start = 0
        while char_start < len(text):
            part_index = len(parts) + 1
            char_end = _fit_turn_part_end(
                parsed,
                turn,
                text,
                char_start=char_start,
                part_index=part_index,
                part_count=part_count,
            )
            parts.append(
                _TurnTextPart(
                    text=text[char_start:char_end],
                    part_index=part_index,
                    part_count=part_count,
                    char_start=char_start,
                    char_end=char_end,
                )
            )
            char_start = char_end
        if len(parts) == part_count:
            return parts
        part_count = len(parts)


def _fit_turn_part_end(
    parsed: ParsedTranscript,
    turn: TranscriptTurn,
    text: str,
    *,
    char_start: int,
    part_index: int,
    part_count: int,
) -> int:
    char_end = min(len(text), char_start + MAX_TRANSCRIPT_CHUNK_TEXT_CHARS)
    while True:
        prefix = _chunk_part_prefix(
            parsed,
            turn,
            part_index=part_index,
            part_count=part_count,
            char_start=char_start,
            char_end=char_end,
        )
        available = MAX_TRANSCRIPT_CHUNK_TEXT_CHARS - len(prefix)
        if available <= 0:
            raise ValueError("transcript chunk metadata exceeds chunk text limit")
        candidate_end = min(len(text), char_start + available)
        if candidate_end <= char_start:
            raise ValueError("transcript chunk split made no progress")
        if candidate_end == char_end:
            return char_end
        char_end = candidate_end


def _chunk_part_text(parsed: ParsedTranscript, turn: TranscriptTurn, part: _TurnTextPart) -> str:
    return _chunk_part_prefix(
        parsed,
        turn,
        part_index=part.part_index,
        part_count=part.part_count,
        char_start=part.char_start,
        char_end=part.char_end,
    ) + part.text


def _chunk_part_prefix(
    parsed: ParsedTranscript,
    turn: TranscriptTurn,
    *,
    part_index: int,
    part_count: int,
    char_start: int,
    char_end: int,
) -> str:
    return "\n".join(
        [
            f"session_id_hash: {parsed.session.session_id_hash}",
            f"turn_start_index: {turn.turn_index}",
            f"turn_part_index: {part_index}",
            f"turn_part_count: {part_count}",
            f"char_start: {char_start}",
            f"char_end: {char_end}",
            "",
            f"{turn.role}: ",
        ]
    )


def _chunk_text(parsed: ParsedTranscript, turns: list[TranscriptTurn]) -> str:
    lines = [f"session_id_hash: {parsed.session.session_id_hash}"]
    if turns:
        turn_start = min(turn.turn_index for turn in turns)
        lines = [f"session_id_hash: {parsed.session.session_id_hash}", f"turn_start_index: {turn_start}"]
    lines.extend(f"{turn.role}: {turn.redacted_text}" for turn in sorted(turns, key=lambda item: item.turn_index))
    return "\n\n".join(lines)


def _sha256_content(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def knowledge_id_for_chunk(chunk: TranscriptChunk) -> str:
    return "kn_" + chunk.content_hash.split("sha256:", 1)[1][:16]


_knowledge_id_for_chunk = knowledge_id_for_chunk

from __future__ import annotations

from ....redaction import redact_text_v2
from ...transcript_model import TranscriptSession, TranscriptToolEvent, TranscriptTurn
from ..common import ParsedTranscript, _normalize_role, _sha256

def _parse_provider_fixture(
    provider: str,
    payload: dict,
    *,
    project: str,
    source_locator_hash: str,
) -> ParsedTranscript:
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")
    session = TranscriptSession(
        session_id_hash=_sha256(f"{provider}:{session_id}"),
        provider=provider,
        project=project,
        started_at=str(payload.get("started_at") or ""),
        ended_at=str(payload.get("ended_at") or ""),
        source_status="source_locator_private_spool_only",
        source_locator_hash=source_locator_hash,
    )
    raw_turns = payload.get("messages") if provider == "claude" else payload.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ValueError("source_parse_failed: missing transcript turns")

    turns: list[TranscriptTurn] = []
    tool_events: list[TranscriptToolEvent] = []
    for index, raw_turn in enumerate(raw_turns, start=1):
        if not isinstance(raw_turn, dict):
            raise ValueError("source_parse_failed: turn must be an object")
        role = _normalize_role(raw_turn.get("role"))
        raw_text = raw_turn.get("content") if provider == "claude" else raw_turn.get("text")
        text = str(raw_text or "")
        if not text:
            raise ValueError("source_parse_failed: turn text missing")
        turn_hash = _sha256(f"{session.session_id_hash}:{index}:{role}:{redact_text_v2(text)}")
        turns.append(
            TranscriptTurn(
                turn_id_hash=turn_hash,
                session_id_hash=session.session_id_hash,
                turn_index=index,
                role=role,
                observed_at=str(raw_turn.get("timestamp") or ""),
                redacted_text=text,
            )
        )
        raw_tool_events = raw_turn.get("tool_events") if provider == "claude" else raw_turn.get("tool_calls")
        if raw_tool_events is None:
            continue
        if not isinstance(raw_tool_events, list):
            raise ValueError("source_parse_failed: tool events must be a list")
        for event_index, raw_event in enumerate(raw_tool_events, start=1):
            if not isinstance(raw_event, dict):
                raise ValueError("source_parse_failed: tool event must be an object")
            tool_name = str(raw_event.get("tool_name") or raw_event.get("name") or "unknown")
            event_type = str(raw_event.get("event_type") or raw_event.get("type") or "tool_summary")
            summary = str(raw_event.get("summary") or "")
            tool_events.append(
                TranscriptToolEvent(
                    tool_event_id_hash=_sha256(f"{turn_hash}:{event_index}:{tool_name}:{redact_text_v2(summary)}"),
                    turn_id_hash=turn_hash,
                    event_index=event_index,
                    tool_name=tool_name,
                    event_type=event_type,
                    redacted_summary=summary,
                )
            )

    return ParsedTranscript(
        session=session,
        turns=turns,
        tool_events=tool_events,
        parser_warnings=[],
        source_status=session.source_status,
    )

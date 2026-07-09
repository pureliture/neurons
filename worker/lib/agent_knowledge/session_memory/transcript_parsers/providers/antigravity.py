from __future__ import annotations

import json
from pathlib import Path

from ....redaction import redact_text_v2
from ...transcript_model import TranscriptSession, TranscriptToolEvent, TranscriptTurn, ToolEvidenceSummaryRecord
from ..common import ParsedTranscript, _load_jsonl_source, _sha256
from ..evidence import _build_evidence_records, _coerce_result_text

def _parse_antigravity_native_jsonl(
    path: Path,
    *,
    project: str,
    source_locator_hash: str,
) -> ParsedTranscript:
    records = _load_jsonl_source(path)
    session_id = _antigravity_session_id_from_path(path)
    if not session_id:
        # Resolve session identity before any turn hashes so early turns are not
        # orphaned under sha256("antigravity:") when the id appears mid-stream.
        for record in records:
            if not isinstance(record, dict):
                continue
            session_id = str(
                record.get("conversationId")
                or record.get("conversation_id")
                or record.get("session_id")
                or ""
            )
            if session_id:
                break
    session_hash = _sha256(f"antigravity:{session_id}")
    turns: list[TranscriptTurn] = []
    tool_events: list[TranscriptToolEvent] = []
    started_at = ""
    ended_at = ""

    for record in records:
        text = str(record.get("content") or "")
        raw_tool_calls = record.get("tool_calls")
        tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else []
        if not text and not tool_calls:
            continue
        role = _normalize_antigravity_role(record)
        turn_index = _antigravity_turn_index(record, fallback=len(turns) + 1)
        observed_at = str(record.get("timestamp") or record.get("observed_at") or record.get("created_at") or "")
        if observed_at and not started_at:
            started_at = observed_at
        ended_at = observed_at or ended_at
        turn_hash = _sha256(f"{session_hash}:{turn_index}:{role}:{redact_text_v2(text)}")
        turns.append(
            TranscriptTurn(
                turn_id_hash=turn_hash,
                session_id_hash=session_hash,
                turn_index=turn_index,
                role=role,
                observed_at=observed_at,
                redacted_text=text,
            )
        )
        for event_index, raw_event in enumerate(tool_calls, start=1):
            if not isinstance(raw_event, dict):
                continue
            tool_name = str(raw_event.get("name") or raw_event.get("tool_name") or "unknown")
            event_type = str(raw_event.get("type") or record.get("type") or "tool_summary")
            summary = _antigravity_tool_summary(raw_event)
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

    if not turns:
        raise ValueError("source_parse_failed: missing transcript turns")
    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")

    session = TranscriptSession(
        session_id_hash=session_hash,
        provider="antigravity",
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        source_status="source_locator_private_spool_only",
        source_locator_hash=source_locator_hash,
    )
    return ParsedTranscript(
        session=session,
        turns=turns,
        tool_events=tool_events,
        parser_warnings=[],
        source_status=session.source_status,
    )


def _antigravity_session_id_from_path(path: Path) -> str:
    parts = list(path.parts)
    if ".system_generated" in parts:
        index = parts.index(".system_generated")
        if index > 0:
            return parts[index - 1]
    return ""


def _normalize_antigravity_role(record: dict) -> str:
    source = str(record.get("source") or "").upper()
    record_type = str(record.get("type") or "").upper()
    if source.startswith("USER") or record_type == "USER_INPUT":
        return "user"
    if source in {"MODEL", "ASSISTANT"} or "RESPONSE" in record_type:
        return "assistant"
    return "system_observed"


def _antigravity_turn_index(record: dict, *, fallback: int) -> int:
    value = record.get("step_index")
    if isinstance(value, int) and value > 0:
        return value
    return fallback


def _antigravity_tool_summary(raw_event: dict) -> str:
    for key in ("summary", "arguments", "args"):
        value = raw_event.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict) and value:
            return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return ""


def extract_antigravity_tool_evidence(
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> list[ToolEvidenceSummaryRecord]:
    """Extract tool evidence from a raw Antigravity transcript.

    Antigravity steps are typed (RUN_COMMAND / CODE_ACTION / VIEW_FILE / ...);
    the shell command lives in ``tool_calls[].args.CommandLine`` and the result in
    the step ``content`` / ``error`` with a step ``status``.
    """
    path = Path(source_path)
    records = _load_jsonl_source(path)
    session_id = _antigravity_session_id_from_path(path)
    raw_items: list[dict] = []

    for record in records:
        if not session_id:
            session_id = str(record.get("conversationId") or record.get("conversation_id") or record.get("session_id") or "")
        step_type = str(record.get("type") or "").upper()
        status = str(record.get("status") or "").upper()
        error = record.get("error")
        is_error = status == "ERROR" or bool(error)
        output = str(record.get("content") or "") or (str(error) if error else "")
        observed_at = str(record.get("created_at") or record.get("timestamp") or "")
        command = ""
        tool_calls = record.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if isinstance(call, dict) and isinstance(call.get("args"), dict):
                    command = str(call["args"].get("CommandLine") or call["args"].get("command") or "")
                    if command:
                        break
        if step_type == "RUN_COMMAND" or command:
            raw_items.append({"tool_name": "run_command", "command": command, "output": output, "is_error": is_error, "observed_at": observed_at})
        elif step_type == "CODE_ACTION":
            raw_items.append({"tool_name": "code_action", "command": "", "output": output, "is_error": is_error, "observed_at": observed_at})
        # VIEW_FILE / LIST_DIRECTORY / GREP_SEARCH / PLANNER_RESPONSE / etc. -> dropped

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")

    return _build_evidence_records(raw_items, session_hash=_sha256(f"antigravity:{session_id}"), provider="antigravity", project=project)

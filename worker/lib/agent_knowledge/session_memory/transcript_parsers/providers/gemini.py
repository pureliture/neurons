from __future__ import annotations

from pathlib import Path

from ....redaction import redact_text_v2
from ...transcript_model import TranscriptSession, TranscriptTurn, ToolEvidenceSummaryRecord
from ..common import ParsedTranscript, _extract_message_text, _load_jsonl_source, _sha256
from ..evidence import _build_evidence_records, _coerce_result_text, _SHELL_TOOL_NAMES

def _parse_gemini_native_jsonl(
    path: Path,
    *,
    project: str,
    source_locator_hash: str,
) -> ParsedTranscript:
    records = _load_jsonl_source(path)
    turns: list[TranscriptTurn] = []
    session_id = ""
    started_at = ""
    ended_at = ""

    for record in records:
        if not session_id:
            session_id = str(record.get("sessionId") or record.get("session_id") or "")
        record_type = str(record.get("type") or "")
        if record_type == "user":
            role = "user"
        elif record_type in {"gemini", "model", "assistant"}:
            role = "assistant"
        else:
            continue
        text = _extract_message_text(record.get("content"))
        if not text:
            continue
        observed_at = str(record.get("timestamp") or record.get("lastUpdated") or "")
        if not started_at:
            started_at = observed_at
        ended_at = observed_at or ended_at
        index = len(turns) + 1
        session_hash = _sha256(f"gemini:{session_id}")
        turn_hash = _sha256(f"{session_hash}:{index}:{role}:{redact_text_v2(text)}")
        turns.append(
            TranscriptTurn(
                turn_id_hash=turn_hash,
                session_id_hash=session_hash,
                turn_index=index,
                role=role,
                observed_at=observed_at,
                redacted_text=text,
            )
        )

    if not turns:
        raise ValueError("source_parse_failed: missing transcript turns")
    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")

    session = TranscriptSession(
        session_id_hash=_sha256(f"gemini:{session_id}"),
        provider="gemini",
        project=project,
        started_at=started_at,
        ended_at=ended_at,
        source_status="source_locator_private_spool_only",
        source_locator_hash=source_locator_hash,
    )
    return ParsedTranscript(
        session=session,
        turns=turns,
        tool_events=[],
        parser_warnings=[],
        source_status=session.source_status,
    )


def extract_gemini_tool_evidence(
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> list[ToolEvidenceSummaryRecord]:
    """Extract tool evidence from a raw Gemini CLI JSONL chat session.

    Gemini records carry a ``toolCalls`` list with name/args/result/status.
    """
    records = _load_jsonl_source(Path(source_path))
    session_id = ""
    raw_items: list[dict] = []

    for record in records:
        if not session_id:
            session_id = str(record.get("sessionId") or record.get("session_id") or "")
        observed_at = str(record.get("timestamp") or "")
        tool_calls = record.get("toolCalls")
        calls = list(tool_calls) if isinstance(tool_calls, list) else []
        for key in ("functionResponse", "function_response"):
            function_response = record.get(key)
            if isinstance(function_response, dict):
                calls.append(function_response)
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "unknown")
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            command = ""
            if name.strip().lower() in _SHELL_TOOL_NAMES:
                command = str(args.get("command") or args.get("cmd") or "")
            output = _coerce_result_text(call.get("resultDisplay")) or _coerce_result_text(call.get("result"))
            is_error = str(call.get("status") or "").lower() in {"error", "failed", "cancelled"}
            raw_items.append({"tool_name": name, "command": command, "output": output, "is_error": is_error, "observed_at": observed_at})

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")

    return _build_evidence_records(raw_items, session_hash=_sha256(f"gemini:{session_id}"), provider="gemini", project=project)

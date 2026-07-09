from __future__ import annotations

from pathlib import Path

from ....redaction import redact_text_v2
from ...transcript_model import TranscriptSession, TranscriptTurn, ToolEvidenceSummaryRecord
from ..common import (
    ParsedTranscript,
    _extract_claude_message_text,
    _extract_message_text,
    _load_jsonl_source,
    _normalize_role,
    _sha256,
)
from ..evidence import _build_evidence_records, _coerce_result_text, _SHELL_TOOL_NAMES

def _parse_claude_native_jsonl(
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
        if record_type not in {"user", "assistant"}:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = _normalize_role(message.get("role"))
        if role not in {"user", "assistant"}:
            continue
        text = _extract_claude_message_text(message.get("content"))
        if not text:
            continue
        observed_at = str(record.get("timestamp") or "")
        if not started_at:
            started_at = observed_at
        ended_at = observed_at or ended_at
        index = len(turns) + 1
        turn_hash = _sha256(f"{session_id}:{index}:{role}:{redact_text_v2(text)}")
        turns.append(
            TranscriptTurn(
                turn_id_hash=turn_hash,
                session_id_hash=_sha256(f"claude:{session_id}"),
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
        session_id_hash=_sha256(f"claude:{session_id}"),
        provider="claude",
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


def extract_claude_tool_evidence(
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> list[ToolEvidenceSummaryRecord]:
    """Extract tool evidence from a raw Claude Code JSONL transcript.

    Claude pairs ``tool_use`` (assistant) with ``tool_result`` (user) by id;
    ``is_error`` on the result flags failures.
    """
    records = _load_jsonl_source(Path(source_path))
    session_id = ""
    results_by_id: dict[str, dict] = {}
    uses: list[dict] = []

    for record in records:
        if not session_id:
            session_id = str(record.get("sessionId") or record.get("session_id") or "")
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        observed_at = str(record.get("timestamp") or "")
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses.append({"block": block, "observed_at": observed_at})
            elif block.get("type") == "tool_result":
                tool_use_id = str(block.get("tool_use_id") or "")
                if tool_use_id:
                    results_by_id[tool_use_id] = block

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")

    raw_items: list[dict] = []
    for entry in uses:
        block = entry["block"]
        name = str(block.get("name") or "unknown")
        result = results_by_id.get(str(block.get("id") or ""), {})
        output = _coerce_result_text(result.get("content"))
        is_error = bool(result.get("is_error"))
        command = ""
        if name.strip().lower() in _SHELL_TOOL_NAMES:
            tool_input = block.get("input")
            if isinstance(tool_input, dict):
                command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        raw_items.append({"tool_name": name, "command": command, "output": output, "is_error": is_error, "observed_at": entry["observed_at"]})

    return _build_evidence_records(raw_items, session_hash=_sha256(f"claude:{session_id}"), provider="claude", project=project)

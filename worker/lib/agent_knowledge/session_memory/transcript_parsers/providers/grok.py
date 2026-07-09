from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ....redaction import redact_text_v2
from ...transcript_model import TranscriptSession, TranscriptTurn, ToolEvidenceSummaryRecord
from ..common import ParsedTranscript, _load_jsonl_source, _sha256
from ..evidence import _build_evidence_records, _coerce_result_text, _extract_output_text

_GROK_CHUNK_ROLES = {
    "user_message_chunk": "user",
    "agent_message_chunk": "assistant",
}


def _parse_grok_native_jsonl(
    path: Path,
    *,
    project: str,
    source_locator_hash: str,
) -> ParsedTranscript:
    """Parse Grok Build ``updates.jsonl`` into user/assistant turns only.

    Codex/Claude parity: ``agent_thought_chunk``, ``hook_execution``, tool events,
    and unknown ``sessionUpdate`` values are silently skipped. Tool high-signal
    evidence is extracted separately via ``extract_grok_tool_evidence``.
    """
    records = _load_jsonl_source(path)
    session_id = ""
    turns: list[TranscriptTurn] = []
    started_at = ""
    ended_at = ""
    buffer_role = ""
    buffer_parts: list[str] = []
    buffer_observed_at = ""

    def clear_buffer() -> None:
        nonlocal buffer_role, buffer_parts, buffer_observed_at
        buffer_role = ""
        buffer_parts = []
        buffer_observed_at = ""

    def flush() -> None:
        if not buffer_role or not buffer_parts:
            clear_buffer()
            return
        text = "".join(buffer_parts)
        if not text:
            clear_buffer()
            return
        index = len(turns) + 1
        session_hash = _sha256(f"grok:{session_id}")
        turn_hash = _sha256(f"{session_hash}:{index}:{buffer_role}:{redact_text_v2(text)}")
        turns.append(
            TranscriptTurn(
                turn_id_hash=turn_hash,
                session_id_hash=session_hash,
                turn_index=index,
                role=buffer_role,
                observed_at=buffer_observed_at,
                redacted_text=text,
            )
        )
        clear_buffer()

    for record in records:
        if not isinstance(record, dict):
            continue
        params = record.get("params")
        if not isinstance(params, dict):
            continue
        if not session_id:
            session_id = str(params.get("sessionId") or params.get("session_id") or "")
        update = params.get("update")
        if not isinstance(update, dict):
            continue

        session_update = str(update.get("sessionUpdate") or "")
        observed_at = _grok_observed_at(record, params)
        if observed_at and not started_at:
            started_at = observed_at
        if observed_at:
            ended_at = observed_at

        chunk_role = _GROK_CHUNK_ROLES.get(session_update)
        if chunk_role is not None:
            text = _grok_content_text(update)
            if not text:
                continue
            if buffer_role and buffer_role != chunk_role:
                flush()
            buffer_role = chunk_role
            if not buffer_observed_at:
                buffer_observed_at = observed_at
            buffer_parts.append(text)
            continue

        if session_update == "turn_completed":
            flush()
            continue

        # thought / hook / tool / unknown → silent skip (Codex/Claude parity)

    flush()

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")
    if not turns:
        raise ValueError("source_parse_failed: missing transcript turns")

    session = TranscriptSession(
        session_id_hash=_sha256(f"grok:{session_id}"),
        provider="grok",
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


def _grok_content_text(update: dict) -> str:
    content = update.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""


def _grok_observed_at(record: dict, params: dict) -> str:
    meta = params.get("_meta")
    if isinstance(meta, dict):
        agent_ms = meta.get("agentTimestampMs")
        if isinstance(agent_ms, (int, float)) and agent_ms > 0:
            return _unix_ms_to_iso(int(agent_ms))
    raw_ts = record.get("timestamp")
    if isinstance(raw_ts, (int, float)) and raw_ts > 0:
        # Grok top-level timestamp is usually unix seconds; treat large values as ms.
        # Use raw_ts * 1000 before int() so fractional seconds keep ms precision.
        if raw_ts > 10_000_000_000:
            return _unix_ms_to_iso(int(raw_ts))
        return _unix_ms_to_iso(int(raw_ts * 1000))
    if isinstance(raw_ts, str) and raw_ts:
        return raw_ts
    return ""


def _unix_ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_grok_tool_evidence(
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> list[ToolEvidenceSummaryRecord]:
    """Extract high-signal tool evidence from Grok Build ``updates.jsonl``.

    Pairs ``tool_call`` with ``tool_call_update`` by ``toolCallId``, then reuses
    the Codex/Claude shared classifier (``_build_evidence_records``). Turn parser
    never materializes tool timelines.
    """
    records = _load_jsonl_source(Path(source_path))
    session_id = ""
    calls: dict[str, dict] = {}
    call_order: list[str] = []

    for record in records:
        if not isinstance(record, dict):
            continue
        params = record.get("params")
        if not isinstance(params, dict):
            continue
        if not session_id:
            session_id = str(params.get("sessionId") or params.get("session_id") or "")
        update = params.get("update")
        if not isinstance(update, dict):
            continue
        session_update = str(update.get("sessionUpdate") or "")
        if session_update not in {"tool_call", "tool_call_update"}:
            continue
        call_id = str(update.get("toolCallId") or update.get("tool_call_id") or "")
        if not call_id:
            continue

        observed_at = _grok_observed_at(record, params)
        entry = calls.get(call_id)
        if entry is None:
            entry = {
                "tool_name": "",
                "command": "",
                "output": "",
                "is_error": False,
                "observed_at": observed_at,
            }
            calls[call_id] = entry
            call_order.append(call_id)

        _merge_grok_tool_update(entry, update, observed_at=observed_at)

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")

    raw_items = [calls[call_id] for call_id in call_order]
    return _build_evidence_records(
        raw_items,
        session_hash=_sha256(f"grok:{session_id}"),
        provider="grok",
        project=project,
    )


def _merge_grok_tool_update(entry: dict, update: dict, *, observed_at: str) -> None:
    """Fold one ``tool_call`` / ``tool_call_update`` payload into a paired entry."""
    tool_meta = _grok_tool_meta(update)
    meta_name = str(tool_meta.get("name") or "").strip()
    title = str(update.get("title") or "").strip()
    existing = str(entry.get("tool_name") or "").strip()
    # Prefer stable x.ai/tool name. Never let a later human title overwrite it
    # (title may be "run tests" while meta is run_terminal_command).
    if meta_name:
        entry["tool_name"] = meta_name
    elif not existing or existing == "unknown":
        entry["tool_name"] = title or existing or "unknown"

    command = _grok_command_from_raw_input(update.get("rawInput"))
    if command:
        entry["command"] = command

    raw_output = update.get("rawOutput")
    if isinstance(raw_output, dict):
        out = _grok_text_from_raw_output(raw_output)
        if out:
            entry["output"] = out
        if not entry["command"]:
            cmd = raw_output.get("command") or raw_output.get("cmd") or ""
            if cmd:
                entry["command"] = str(cmd)
        exit_code = _coerce_exit_code(raw_output.get("exit_code"))
        if exit_code is not None and exit_code != 0:
            entry["is_error"] = True
        if bool(raw_output.get("timed_out")):
            entry["is_error"] = True

    status = str(update.get("status") or "").lower()
    if status in {"error", "failed", "cancelled"}:
        entry["is_error"] = True

    if not entry["output"]:
        fallback = _grok_text_from_content_blocks(update.get("content"))
        if fallback:
            entry["output"] = fallback

    if observed_at:
        entry["observed_at"] = observed_at


def _coerce_exit_code(value) -> int | None:
    """Best-effort numeric exit code; None when absent/unparseable.

    ``bool`` is excluded because it subclasses ``int`` but is not an exit code.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            # Accept "1", "1.0" (some serializers emit float-shaped strings).
            return int(float(text))
        except ValueError:
            return None
    return None


def _grok_command_from_raw_input(raw_input) -> str:
    if not isinstance(raw_input, dict):
        return ""
    cmd = raw_input.get("command") or raw_input.get("cmd") or ""
    if isinstance(cmd, list):
        cmd = " ".join(str(part) for part in cmd)
    return str(cmd) if cmd else ""


def _grok_text_from_raw_output(raw_output: dict) -> str:
    out = raw_output.get("output_for_prompt")
    if isinstance(out, str) and out:
        return out
    out = _extract_output_text(raw_output.get("output"))
    if out:
        return out
    return _coerce_result_text(raw_output.get("content")) or ""


def _grok_text_from_content_blocks(content) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        nested = block.get("content")
        if isinstance(nested, dict) and isinstance(nested.get("text"), str):
            parts.append(nested["text"])
        elif isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts) if parts else ""


def _grok_tool_meta(update: dict) -> dict:
    meta = update.get("_meta")
    if not isinstance(meta, dict):
        return {}
    tool = meta.get("x.ai/tool")
    return tool if isinstance(tool, dict) else {}

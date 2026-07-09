from __future__ import annotations

import json
from pathlib import Path

from ....redaction import redact_text_v2
from ...transcript_model import TranscriptSession, TranscriptTurn, ToolEvidenceSummaryRecord
from ..common import ParsedTranscript, _extract_message_text, _load_jsonl_source, _normalize_role, _sha256
from ..evidence import _build_evidence_records, _extract_output_text

def _parse_codex_native_jsonl(
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
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if not session_id and record.get("type") == "session_meta":
            session_id = str(payload.get("id") or "")
            continue
        if record.get("type") != "response_item":
            continue
        if payload.get("type") != "message":
            continue
        role = _normalize_role(payload.get("role"))
        if role not in {"user", "assistant"}:
            continue
        text = _extract_message_text(payload.get("content"))
        if not text:
            continue
        observed_at = str(record.get("timestamp") or payload.get("timestamp") or "")
        if not started_at:
            started_at = observed_at
        ended_at = observed_at or ended_at
        index = len(turns) + 1
        session_hash = _sha256(f"codex:{session_id}")
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

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")
    if not turns:
        raise ValueError("source_parse_failed: missing transcript turns")

    session = TranscriptSession(
        session_id_hash=_sha256(f"codex:{session_id}"),
        provider="codex",
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


def extract_codex_tool_evidence(
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> list[ToolEvidenceSummaryRecord]:
    """Extract redacted high-signal tool evidence from a raw Codex JSONL session.

    Append-only and non-destructive: this only reads the source file and never
    touches existing conversation_chunk output. Records are linked to the same
    ``session_id_hash`` the conversation_chunk parser uses
    (``sha256:codex:<session_id>``).
    """
    records = _load_jsonl_source(Path(source_path))
    session_id = ""
    outputs_by_call: dict[str, str] = {}
    patch_success_by_call: dict[str, bool] = {}
    calls: list[dict] = []

    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        if not session_id and record.get("type") == "session_meta":
            session_id = str(payload.get("id") or "")
            continue
        if payload_type in {"function_call", "custom_tool_call"}:
            calls.append({"record": record, "payload": payload})
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "")
            if call_id:
                outputs_by_call[call_id] = _extract_output_text(payload.get("output"))
        elif payload_type == "patch_apply_end":
            call_id = str(payload.get("call_id") or "")
            if call_id:
                patch_success_by_call[call_id] = bool(payload.get("success"))

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")

    raw_items: list[dict] = []
    for entry in calls:
        payload = entry["payload"]
        call_id = str(payload.get("call_id") or "")
        tool_name = str(payload.get("name") or payload.get("type") or "unknown")
        out = outputs_by_call.get(call_id, "")
        observed_at = str(entry["record"].get("timestamp") or payload.get("timestamp") or "")
        if payload.get("type") == "custom_tool_call" and tool_name == "apply_patch":
            succeeded = patch_success_by_call.get(call_id, "Success" in out or not out)
            raw_items.append({"tool_name": "apply_patch", "command": "", "output": out, "is_error": not succeeded, "observed_at": observed_at})
            continue
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}
        cmd = args.get("cmd") or args.get("command") or args.get("input") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(part) for part in cmd)
        raw_items.append({"tool_name": tool_name, "command": str(cmd), "output": out, "is_error": False, "observed_at": observed_at})

    return _build_evidence_records(raw_items, session_hash=_sha256(f"codex:{session_id}"), provider="codex", project=project)

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
        # A rendered shell result is tool output, not conversation prose: drop it
        # here so the turn text stays prose-only. The outcome itself is kept on
        # the high-signal evidence lane (see ``extract_antigravity_tool_evidence``).
        text = _strip_antigravity_tool_result(text)
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
    """Summarize one Antigravity tool call, preferring the provider's own label.

    Antigravity already writes a high-signal one-line description of what a
    call did into ``toolSummary`` (e.g. "Check running ollama and chatgpt
    processes", "Append to ORIGINAL_REQUEST.md"). That label is the intended
    summary of the call. Falling back to dumping the whole argument object
    instead would put file bodies (``CodeContent``), replacement text, and
    subagent prompts verbatim into every downstream consumer.

    When no label is present, fall back to the identifying argument only --
    the command line for shell calls, the target path for file calls -- and
    never the full argument payload.
    """

    args = raw_event.get("args")
    args = args if isinstance(args, dict) else {}

    for source in (raw_event, args):
        for key in ("toolSummary", "toolAction", "summary"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    # No label: keep the identifying argument, drop everything else.
    for key in ("CommandLine", "command", "TargetFile", "AbsolutePath", "Path", "DirectoryPath"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# Antigravity renders a shell step's raw stdout/stderr into the step ``content``
# itself, so the "text only" block filter the other providers rely on does NOT
# apply here. The literal markers below delimit that rendered output; everything
# from the marker onward is tool result, not conversation prose.
_ANTIGRAVITY_COMMAND_OUTPUT_MARKER = "Command output:"
_ANTIGRAVITY_COMMAND_EXIT_MARKER = "Command exited with code"


def _strip_antigravity_tool_result(text: str) -> str:
    """Drop the rendered tool result from a step's ``content``.

    Antigravity has no ``tool_result``/``tool_use`` content blocks: a shell step
    is delivered as a plain string whose body is the command's stdout/stderr,
    preceded by an exit-code line. Keeping that body in the turn text would put
    raw command output into every downstream consumer -- conversation chunks,
    session memory, and the Graphiti entity-extraction input -- even though the
    tool outcome is already summarised on the high-signal evidence lane
    (``extract_antigravity_tool_evidence``).

    Only the tool result is removed. The timestamp header, the exit-code line,
    and any prose before the marker stay, so the turn still records that a
    command ran and how it ended.
    """

    if not text:
        return text
    marker_index = text.find(_ANTIGRAVITY_COMMAND_OUTPUT_MARKER)
    if marker_index < 0:
        return text
    head = text[:marker_index].rstrip()
    # Keep the exit-code line: it is the tool outcome signal, and it carries no
    # command output bytes.
    exit_index = head.find(_ANTIGRAVITY_COMMAND_EXIT_MARKER)
    if exit_index >= 0:
        head = head[exit_index:].rstrip()
    else:
        head = ""
    if not head:
        return ""
    return head


def _antigravity_result_body(record: Mapping[str, Any]) -> str:
    """Return the rendered result body of an Antigravity step record.

    A result step is the ``GENERIC`` record that follows a ``PLANNER_RESPONSE``
    record: it carries the ``Created At:`` / ``Completed At:`` header, the exit
    code, and the command output. The planner record itself has an empty
    ``content`` -- the tool call lives only in its ``tool_calls``.

    The timestamp header is dropped here: the evidence summarizers treat every
    line as signal, so keeping it would push the actual outcome (the exit code
    and the output) out of the bounded summary.
    """

    content = str(record.get("content") or "")
    if not content:
        return ""
    if not content.startswith("Created At:"):
        return ""
    lines = content.splitlines()
    # The header is exactly the leading "Created At:" / "Completed At:" lines.
    start = 0
    while start < len(lines) and (
        lines[start].startswith("Created At:") or lines[start].startswith("Completed At:")
    ):
        start += 1
    return "\n".join(lines[start:]).strip()


def extract_antigravity_tool_evidence(
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> list[ToolEvidenceSummaryRecord]:
    """Extract tool evidence from a raw Antigravity transcript.

    Antigravity splits a shell step across two records: the ``PLANNER_RESPONSE``
    record carries the call in ``tool_calls[].args.CommandLine`` (its ``content``
    is empty), and the following ``GENERIC`` record carries the result -- the
    exit code and the command output. Reading the result off the planner record
    therefore always yields an empty output, which loses the outcome. This
    pairs the two records so the evidence carries "which command ran, and how
    did it turn out".
    """
    path = Path(source_path)
    records = _load_jsonl_source(path)
    session_id = _antigravity_session_id_from_path(path)
    raw_items: list[dict] = []

    for index, record in enumerate(records):
        if not session_id:
            session_id = str(record.get("conversationId") or record.get("conversation_id") or record.get("session_id") or "")
        step_type = str(record.get("type") or "").upper()
        status = str(record.get("status") or "").upper()
        error = record.get("error")
        is_error = status == "ERROR" or bool(error)
        own_content = str(record.get("content") or "")
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
            # The result of a planner-issued call is the next record.
            output = ""
            if index + 1 < len(records):
                output = _antigravity_result_body(records[index + 1])
            if not output:
                output = own_content or (str(error) if error else "")
            raw_items.append({"tool_name": "run_command", "command": command, "output": output, "is_error": is_error, "observed_at": observed_at})
        elif step_type == "CODE_ACTION":
            raw_items.append({"tool_name": "code_action", "command": "", "output": own_content or (str(error) if error else ""), "is_error": is_error, "observed_at": observed_at})
        # VIEW_FILE / LIST_DIRECTORY / GREP_SEARCH / PLANNER_RESPONSE / etc. -> dropped

    if not session_id:
        raise ValueError("source_parse_failed: missing session_id")

    return _build_evidence_records(raw_items, session_hash=_sha256(f"antigravity:{session_id}"), provider="antigravity", project=project)

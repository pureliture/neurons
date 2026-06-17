from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..redaction import redact_text_v2
from .transcript_model import (
    ToolEvidenceSummaryRecord,
    TranscriptSession,
    TranscriptToolEvent,
    TranscriptTurn,
)

PARSER_VERSION = "provider-transcript-parser.v1"
TOOL_EVIDENCE_EXTRACTOR_VERSION = "codex-tool-evidence-extractor.v1"


@dataclass(frozen=True)
class ParsedTranscript:
    session: TranscriptSession
    turns: list[TranscriptTurn]
    tool_events: list[TranscriptToolEvent] = field(default_factory=list)
    parser_warnings: list[str] = field(default_factory=list)
    source_status: str = "source_locator_private_spool_only"


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_transcript_source(
    provider: str,
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> ParsedTranscript:
    if provider not in {"claude", "gemini", "codex", "antigravity"}:
        raise ValueError(f"unsupported provider: {provider}")
    path = Path(source_path)
    if provider == "claude" and path.suffix.lower() == ".jsonl":
        return _parse_claude_native_jsonl(path, project=project, source_locator_hash=source_locator_hash)
    if provider == "gemini" and path.suffix.lower() == ".jsonl":
        return _parse_gemini_native_jsonl(path, project=project, source_locator_hash=source_locator_hash)
    if provider == "codex" and path.suffix.lower() == ".jsonl":
        return _parse_codex_native_jsonl(path, project=project, source_locator_hash=source_locator_hash)
    if provider == "antigravity" and path.suffix.lower() == ".jsonl":
        return _parse_antigravity_native_jsonl(path, project=project, source_locator_hash=source_locator_hash)
    payload = _load_json_source(path)
    if payload.get("provider") != provider:
        raise ValueError("source_parse_failed: provider mismatch")
    if payload.get("schema_version") != "provider_transcript_fixture.v1":
        raise ValueError("source_parse_failed: unsupported fixture schema")
    return _parse_provider_fixture(provider, payload, project=project, source_locator_hash=source_locator_hash)


def _load_json_source(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError("source_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("source_parse_failed: invalid json") from exc
    if not isinstance(payload, dict):
        raise ValueError("source_parse_failed: source root must be an object")
    return payload


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


def _parse_antigravity_native_jsonl(
    path: Path,
    *,
    project: str,
    source_locator_hash: str,
) -> ParsedTranscript:
    records = _load_jsonl_source(path)
    session_id = _antigravity_session_id_from_path(path)
    session_hash = _sha256(f"antigravity:{session_id}")
    turns: list[TranscriptTurn] = []
    tool_events: list[TranscriptToolEvent] = []
    started_at = ""
    ended_at = ""

    for record in records:
        if not session_id:
            session_id = str(record.get("conversationId") or record.get("conversation_id") or record.get("session_id") or "")
            session_hash = _sha256(f"antigravity:{session_id}")
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


def _load_jsonl_source(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("source_parse_failed: jsonl record must be an object")
                records.append(record)
    except FileNotFoundError as exc:
        raise ValueError("source_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("source_parse_failed: invalid jsonl") from exc
    if not records:
        raise ValueError("source_parse_failed: empty jsonl")
    return records


def _extract_claude_message_text(content) -> str:
    return _extract_message_text(content)


def _extract_message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _normalize_role(role) -> str:
    role_text = str(role or "").lower()
    if role_text in {"assistant", "model"}:
        return "assistant"
    if role_text == "user":
        return "user"
    if role_text.startswith("tool"):
        return "tool_summary"
    return "system_observed"


# --- Codex non-message tool-evidence extraction -----------------------------
#
# parse_transcript_source keeps only user/assistant messages and drops every
# function_call / function_call_output / patch record. extract_codex_tool_evidence
# re-reads the same JSONL and keeps only durable, high-signal evidence as
# redacted ToolEvidenceSummaryRecord objects, linked by the same session_id_hash.

_FILE_VIEW_VERBS = {"nl", "sed", "cat", "head", "tail", "less", "more", "bat", "tac"}
_SEARCH_VERBS = {"rg", "grep", "ag", "ack", "ripgrep", "fgrep", "egrep"}
_TRIVIAL_VERBS = {
    "ls", "find", "mkdir", "pwd", "date", "printf", "echo", "touch", "cd",
    "export", "which", "true", "false", "cp", "mv", "chmod", "chown", "tree",
    "stat", "du", "df", "basename", "dirname", "realpath", "readlink", "env",
}
_CMD_WRAPPERS = {"rtk", "sudo", "time", "nohup", "command", "stdbuf", "exec"}
_GIT_STATE_SUBCOMMANDS = {
    "status", "diff", "commit", "log", "add", "branch", "worktree", "stash",
    "merge", "reset", "checkout", "rebase", "switch", "pull", "fetch", "push", "tag",
}

_GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}


def _git_subcommand(tokens: list[str]) -> str:
    """Return the git subcommand, skipping global flags like ``-C <path>``/``--no-pager``."""
    index = 1  # tokens[0] == "git"
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            if token in _GIT_VALUE_FLAGS and "=" not in token:
                index += 2  # flag consumes its separate value
            else:
                index += 1
            continue
        return token
    return ""


_PYTEST_SUMMARY_RE = re.compile(r"\b\d+\s+(passed|failed|error|errors|skipped|xfailed|deselected)\b", re.IGNORECASE)
_PYTEST_FAIL_RE = re.compile(r"\b\d+\s+(failed|error|errors)\b", re.IGNORECASE)
_PYTEST_PASS_RE = re.compile(r"\b\d+\s+passed\b", re.IGNORECASE)
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)|^\s*[\w.]*(Error|Exception)\b\s*:?", re.MULTILINE)
_EXCEPTION_LINE_RE = re.compile(r"^\s*[\w.]*(Error|Exception|Warning)\b.*$", re.MULTILINE)
_GIT_FATAL_RE = re.compile(r"^(fatal|error):", re.IGNORECASE | re.MULTILINE)
_LIVE_PROOF_RE = re.compile(r"healthz|\bsync\b|rollback|live[_-]?proof|postcheck|smoke|/api/v1/", re.IGNORECASE)
_LIVE_FAIL_RE = re.compile(r"connection refused|could not connect|timed? ?out|\b5\d\d\b|\b4\d\d\b|refused|unreachable", re.IGNORECASE)
_APPROVAL_RE = re.compile(r"approv|not approved|\btimeout\b|timed out|\bretry\b|\bretries\b|rate.?limit", re.IGNORECASE)
_AUDIT_VERBS = {"sqlite3", "wc"}

# Provider-neutral tool-name classes. The command->category classifier is shared;
# these decide how each provider's tool maps onto it.
_SHELL_TOOL_NAMES = {
    "exec_command", "shell", "bash", "run_shell_command", "run_command",
    "local_shell", "container.exec",
}
_EDIT_TOOL_NAMES = {
    "apply_patch", "edit", "write", "write_file", "replace", "multiedit",
    "notebookedit", "create_file", "str_replace", "code_action",
}
_EXPLORATION_TOOL_NAMES = {
    "read", "read_file", "view_file", "cat", "grep", "grep_search", "glob",
    "ls", "list_directory", "search", "web_search", "google_web_search",
    "web_fetch", "notebookread", "fetch", "codebase_search",
}
_ORCH_TOOL_NAMES = {
    "task", "todowrite", "update_plan", "enter_plan_mode", "exit_plan_mode",
    "activate_skill", "invoke_agent", "invoke_subagent", "spawn_agent",
    "wait_agent", "close_agent", "send_input", "write_stdin", "tool_search_call",
}


def _normalize_command(cmd: str) -> str:
    text = str(cmd or "").strip()
    text = re.sub(r"^(bash|zsh|sh)\s+-l?c\s+", "", text).strip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        text = text[1:-1].strip()
    return text


def _command_tokens(cmd: str) -> list[str]:
    tokens = _normalize_command(cmd).split()
    index = 0
    while index < len(tokens) and tokens[index] in _CMD_WRAPPERS:
        index += 1
    # `uv run [flags] <verb>` -> treat the underlying verb as primary
    return tokens[index:]


def _extract_output_text(output) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("content", "output", "stdout", "text"):
            value = output.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _signal_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text).splitlines() if line.strip()]


def _test_summary(out: str) -> str:
    for line in reversed(_signal_lines(out)):
        if _PYTEST_SUMMARY_RE.search(line) and " in " in line:
            return line
    lines = _signal_lines(out)
    return lines[-1] if lines else "test run produced no summary"


def _error_summary(out: str) -> str:
    lines = _signal_lines(out)
    for line in reversed(lines):
        if _EXCEPTION_LINE_RE.match(line):
            return line
    return lines[-1] if lines else "command failed"


def _git_summary(subcommand: str, out: str) -> str:
    lines = _signal_lines(out)
    if subcommand == "status":
        branch = next((line for line in lines if line.startswith("##")), "")
        changed = [line for line in lines if not line.startswith("##")]
        state = "clean" if not changed else f"{len(changed)} file(s) changed"
        return f"git status: {state} {branch}".strip()
    if subcommand == "commit":
        return next((line for line in lines if line.startswith("[")), (lines[0] if lines else "git commit"))
    if subcommand == "diff":
        stat = next((line for line in lines if re.search(r"\b\d+ files? changed\b", line)), "")
        return stat or f"git diff captured ({len(lines)} output line(s))"
    return lines[0] if lines else f"git {subcommand}"


def _short_summary(out: str, *, max_lines: int = 6) -> str:
    lines = _signal_lines(out)
    return " ".join(lines[:max_lines]) if lines else ""


def _classify_exec_evidence(cmd: str, out: str) -> tuple[str, str, str] | None:
    """Return (category, outcome, summary) for a shell command, or None to drop."""
    norm = _normalize_command(cmd)
    tokens = _command_tokens(cmd)
    verb = tokens[0] if tokens else ""

    if "pytest" in norm:
        if _PYTEST_FAIL_RE.search(out) or "FAILED" in out or _TRACEBACK_RE.search(out):
            outcome = "fail"
        elif _PYTEST_PASS_RE.search(out):
            outcome = "pass"
        else:
            outcome = "info"
        return "test_result", outcome, _test_summary(out)

    if verb in _FILE_VIEW_VERBS or verb in _SEARCH_VERBS or verb in _TRIVIAL_VERBS:
        return None

    if verb == "git" and len(tokens) > 1:
        subcommand = _git_subcommand(tokens)
        if subcommand in _GIT_STATE_SUBCOMMANDS:
            if _GIT_FATAL_RE.search(out) or _TRACEBACK_RE.search(out):
                return "command_error", "error", _error_summary(out)
            return "git_state", "info", _git_summary(subcommand, out)
        return None

    if _TRACEBACK_RE.search(out):
        return "command_error", "error", _error_summary(out)

    if verb == "curl" or _LIVE_PROOF_RE.search(norm):
        outcome = "error" if _LIVE_FAIL_RE.search(out) else "pass"
        return "live_proof", outcome, _short_summary(out)

    if verb in _AUDIT_VERBS:
        return "local_audit", "info", _short_summary(out)

    if _APPROVAL_RE.search(out):
        return "approval_evidence", "info", _short_summary(out)

    return None


def _is_drop_command(cmd: str) -> bool:
    tokens = _command_tokens(cmd)
    verb = tokens[0] if tokens else ""
    return verb in _FILE_VIEW_VERBS or verb in _SEARCH_VERBS or verb in _TRIVIAL_VERBS


def _classify_tool_evidence(tool_name: str, command: str, output: str, is_error: bool) -> tuple[str, str, str] | None:
    """Provider-neutral mapping of one tool invocation to evidence, or None to drop."""
    name = (tool_name or "").strip().lower()
    if name in _SHELL_TOOL_NAMES:
        classified = _classify_exec_evidence(command, output)
        if classified is None and is_error and not _is_drop_command(command):
            classified = ("command_error", "error", _error_summary(output) or "command failed")
    elif name in _EDIT_TOOL_NAMES:
        classified = ("command_error", "error", _error_summary(output) or "edit failed") if is_error else None
    elif name in _EXPLORATION_TOOL_NAMES or name in _ORCH_TOOL_NAMES:
        classified = None
    elif command:
        classified = _classify_exec_evidence(command, output)
        if classified is None and is_error:
            classified = ("command_error", "error", _error_summary(output) or "command failed")
    else:
        classified = ("command_error", "error", _error_summary(output) or "command failed") if is_error else None

    if classified is None:
        return None
    category, outcome, summary = classified
    if is_error and category != "test_result":
        outcome = "error"
    return category, outcome, summary


def _build_evidence_records(
    raw_items: list[dict],
    *,
    session_hash: str,
    provider: str,
    project: str,
) -> list[ToolEvidenceSummaryRecord]:
    records: list[ToolEvidenceSummaryRecord] = []
    for item in raw_items:
        command = str(item.get("command") or "")
        classified = _classify_tool_evidence(item.get("tool_name", ""), command, str(item.get("output") or ""), bool(item.get("is_error")))
        if classified is None:
            continue
        category, outcome, summary = classified
        command_summary = _normalize_command(command) or str(item.get("tool_name") or "")
        records.append(
            ToolEvidenceSummaryRecord(
                session_id_hash=session_hash,
                provider=provider,
                project=project,
                category=category,
                outcome=outcome,
                tool_name=str(item.get("tool_name") or "unknown"),
                command_summary=command_summary,
                redacted_summary=summary,
                observed_at=str(item.get("observed_at") or ""),
                evidence_index=len(records),
                source_status="source_locator_private_spool_only",
            )
        )
    return records


def _coerce_result_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _extract_output_text(value) or str(value.get("text") or value.get("resultDisplay") or "")
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("output") or block.get("content") or block.get("stdout")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    return ""


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


_TOOL_EVIDENCE_EXTRACTORS = {
    "codex": extract_codex_tool_evidence,
    "claude": extract_claude_tool_evidence,
    "gemini": extract_gemini_tool_evidence,
    "antigravity": extract_antigravity_tool_evidence,
}


def extract_tool_evidence(
    provider: str,
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> list[ToolEvidenceSummaryRecord]:
    """Dispatch tool-evidence extraction by provider."""
    extractor = _TOOL_EVIDENCE_EXTRACTORS.get(provider)
    if extractor is None:
        raise ValueError(f"unsupported provider: {provider}")
    return extractor(source_path, project=project, source_locator_hash=source_locator_hash)

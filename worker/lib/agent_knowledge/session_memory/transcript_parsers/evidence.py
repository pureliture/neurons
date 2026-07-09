from __future__ import annotations

import json
import re

from ...redaction import redact_text_v2
from ..transcript_model import ToolEvidenceSummaryRecord
from .common import _sha256

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


_SHELL_TOOL_NAMES = {
    "exec_command", "shell", "bash", "run_shell_command", "run_command",
    "run_terminal_command", "local_shell", "container.exec",
}


_EDIT_TOOL_NAMES = {
    "apply_patch", "edit", "write", "write_file", "replace", "multiedit",
    "notebookedit", "create_file", "str_replace", "code_action", "search_replace",
}


_EXPLORATION_TOOL_NAMES = {
    "read", "read_file", "view_file", "cat", "grep", "grep_search", "glob",
    "ls", "list_directory", "list_dir", "search", "web_search", "google_web_search",
    "web_fetch", "notebookread", "fetch", "codebase_search",
}


_ORCH_TOOL_NAMES = {
    "task", "todowrite", "todo_write", "update_plan", "enter_plan_mode", "exit_plan_mode",
    "activate_skill", "invoke_agent", "invoke_subagent", "spawn_agent",
    "wait_agent", "close_agent", "send_input", "write_stdin", "tool_search_call",
    "update_goal", "search_tool",
}


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

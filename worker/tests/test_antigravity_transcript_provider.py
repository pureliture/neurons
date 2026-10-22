"""Antigravity transcript provider — native parser, tool-result suppression, evidence.

Antigravity delivers a shell step's raw stdout/stderr inside the step
``content`` string itself, and writes a high-signal ``toolSummary`` label on
every tool call. The parser therefore has two jobs the other providers do not:

- the rendered command output must not survive into the turn text, because that
  text reaches conversation chunks, session memory, and the Graphiti
  entity-extraction input;
- a tool call must be summarized by its own label, not by dumping the argument
  object (which carries file bodies and subagent prompts).

The tool outcome itself is not lost: it stays on the high-signal evidence lane
(``extract_tool_evidence``), mirroring the Grok provider's "tools via
high-signal evidence lane only" contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_knowledge.session_memory.transcript_parsers import (
    extract_tool_evidence,
    parse_transcript_source,
)
from agent_knowledge.session_memory.transcript_parsers.providers.antigravity import (
    _strip_antigravity_tool_result,
)


def _sha256(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _antigravity_dir(tmp_path: Path) -> Path:
    # The parser derives the session id from the parent of ``.system_generated``.
    root = tmp_path / "brain" / "conv-1" / ".system_generated" / "logs"
    root.mkdir(parents=True)
    return root


def _write_transcript(root: Path, records: list[dict]) -> Path:
    path = root / "transcript.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _parse(path: Path):
    return parse_transcript_source(
        "antigravity",
        path,
        project="antigravity",
        source_locator_hash=_sha256("locator"),
    )


def test_strip_tool_result_drops_stdout_but_keeps_exit_code():
    text = (
        "Created At: 2026-10-21T21:12:50+09:00\n"
        "Completed At: 2026-10-21T21:12:57+09:00\n"
        "Command exited with code 0.\n"
        "\n"
        "Command output:\n"
        "secret-token-value\n"
    )
    stripped = _strip_antigravity_tool_result(text)
    assert "secret-token-value" not in stripped
    assert "Command output:" not in stripped
    assert "Command exited with code 0." in stripped


def test_strip_tool_result_keeps_prose_without_marker():
    text = "The search returned a summary of the health check results."
    assert _strip_antigravity_tool_result(text) == text


def test_strip_tool_result_of_empty_text_is_empty():
    assert _strip_antigravity_tool_result("") == ""


def test_parser_drops_rendered_command_output_from_turn_text(tmp_path):
    root = _antigravity_dir(tmp_path)
    path = _write_transcript(
        root,
        [
            {
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\ncheck the process list\n</USER_REQUEST>",
                "step_index": 1,
            },
            {
                "type": "GENERIC",
                "source": "MODEL",
                "step_index": 2,
                "content": (
                    "Created At: 2026-10-21T21:12:50+09:00\n"
                    "Command exited with code 0.\n"
                    "\n"
                    "Command output:\n"
                    "raw-stdout-line\n"
                ),
            },
        ],
    )
    parsed = _parse(path)
    turn_text = "\n".join(turn.redacted_text for turn in parsed.turns)
    assert "raw-stdout-line" not in turn_text
    assert "Command output:" not in turn_text
    assert "check the process list" in turn_text


def test_parser_uses_provider_tool_summary_not_argument_dump(tmp_path):
    root = _antigravity_dir(tmp_path)
    path = _write_transcript(
        root,
        [
            {
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\nwrite it\n</USER_REQUEST>",
                "step_index": 1,
            },
            {
                "type": "GENERIC",
                "source": "MODEL",
                "step_index": 2,
                "content": "Wrote the file.",
                "tool_calls": [
                    {
                        "name": "write_to_file",
                        "args": {
                            "toolSummary": "Append to ORIGINAL_REQUEST.md",
                            "TargetFile": "/Users/example/project/ORIGINAL_REQUEST.md",
                            "CodeContent": "entire file body that must not leak",
                        },
                    }
                ],
            },
        ],
    )
    parsed = _parse(path)
    assert parsed.tool_events
    summary = parsed.tool_events[0].redacted_summary
    assert summary == "Append to ORIGINAL_REQUEST.md"
    assert "entire file body that must not leak" not in summary
    assert "CodeContent" not in summary


def test_parser_tool_event_falls_back_to_identifying_argument_only(tmp_path):
    root = _antigravity_dir(tmp_path)
    path = _write_transcript(
        root,
        [
            {
                "type": "GENERIC",
                "source": "MODEL",
                "step_index": 1,
                "content": "Ran the command.",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {
                            "CommandLine": "pytest -q",
                            "WaitMsBeforeAsync": "5000",
                        },
                    }
                ],
            },
        ],
    )
    parsed = _parse(path)
    assert parsed.tool_events
    summary = parsed.tool_events[0].redacted_summary
    assert summary == "pytest -q"
    assert "WaitMsBeforeAsync" not in summary


def test_tool_evidence_lane_still_reports_the_command_outcome(tmp_path):
    """Suppressing the rendered output must not lose the tool outcome."""
    root = _antigravity_dir(tmp_path)
    path = _write_transcript(
        root,
        [
            {
                "type": "GENERIC",
                "source": "MODEL",
                "status": "ERROR",
                "step_index": 1,
                "content": "boom",
                "error": "Traceback (most recent call last): boom",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {"CommandLine": "pytest -q", "toolSummary": "Run the suite"},
                    }
                ],
            },
        ],
    )
    records = extract_tool_evidence(
        "antigravity",
        path,
        project="antigravity",
        source_locator_hash=_sha256("locator"),
    )
    assert records
    assert records[0].tool_name == "run_command"
    assert "pytest" in records[0].command_summary

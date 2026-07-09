"""Grok Build transcript provider — identity, native parser, evidence, import.

Codex/Claude parity: turns = user/assistant message text only; thought/hook/unknown
silent drop; tools via high-signal evidence lane only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.historical_import import (
    ImportStatus,
    PROVIDER_LANES,
    SourceLocator,
    import_historical_source,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.redaction import redact_text_v2
from agent_knowledge.session_memory.transcript_model import (
    TranscriptSession,
    TranscriptTurn,
    canonicalize_provider,
)
from agent_knowledge.session_memory.transcript_packer import pack_conversation_chunk_document
from agent_knowledge.session_memory.transcript_parsers import (
    GROK_PARSER_VERSION,
    extract_tool_evidence,
    parse_transcript_source,
)


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _acp_line(
    *,
    session_id: str,
    session_update: str,
    timestamp: int = 1_700_000_000,
    content_text: str | None = None,
    tool: dict | None = None,
    method: str = "session/update",
) -> dict:
    update: dict = {"sessionUpdate": session_update}
    if content_text is not None:
        update["content"] = {"type": "text", "text": content_text}
    if tool is not None:
        update.update(tool)
    return {
        "timestamp": timestamp,
        "method": method,
        "params": {
            "sessionId": session_id,
            "update": update,
            "_meta": {"eventId": "e1", "agentTimestampMs": timestamp * 1000},
        },
    }


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    return path


def _grok_session_fixture(tmp_path: Path, *, session_id: str = "grok-session-1") -> Path:
    """Sanitized ACP stream covering turn assembly + silent-drop types + tools."""
    records = [
        _acp_line(session_id=session_id, session_update="agent_thought_chunk", content_text="secret plan"),
        _acp_line(session_id=session_id, session_update="user_message_chunk", content_text="Hello "),
        _acp_line(session_id=session_id, session_update="user_message_chunk", content_text="world"),
        _acp_line(session_id=session_id, session_update="turn_completed"),
        _acp_line(
            session_id=session_id,
            session_update="hook_execution",
            tool={"event_name": "SessionEnd", "runs": []},
        ),
        _acp_line(session_id=session_id, session_update="agent_message_chunk", content_text="Hi "),
        _acp_line(session_id=session_id, session_update="agent_message_chunk", content_text="there"),
        _acp_line(
            session_id=session_id,
            session_update="tool_call",
            tool={
                "toolCallId": "call-1",
                "title": "run tests",
                "rawInput": {"command": "uv run pytest -q", "description": "test"},
                "_meta": {
                    "x.ai/tool": {
                        "name": "run_terminal_command",
                        "kind": "execute",
                        "namespace": "default",
                        "label": "run_terminal_command",
                        "read_only": False,
                    }
                },
            },
        ),
        _acp_line(
            session_id=session_id,
            session_update="tool_call_update",
            tool={
                "toolCallId": "call-1",
                "status": "completed",
                "rawOutput": {
                    "type": "command_result",
                    "command": "uv run pytest -q",
                    "exit_code": 0,
                    "output_for_prompt": "12 passed in 0.1s",
                    "output": [],
                },
                "_meta": {
                    "x.ai/tool": {
                        "name": "run_terminal_command",
                        "kind": "execute",
                        "namespace": "default",
                        "label": "run_terminal_command",
                        "read_only": False,
                    }
                },
            },
        ),
        _acp_line(
            session_id=session_id,
            session_update="tool_call",
            tool={
                "toolCallId": "call-2",
                "title": "read file",
                "rawInput": {"path": "README.md"},
                "_meta": {
                    "x.ai/tool": {
                        "name": "read_file",
                        "kind": "read",
                        "namespace": "default",
                        "label": "read_file",
                        "read_only": True,
                    }
                },
            },
        ),
        _acp_line(
            session_id=session_id,
            session_update="tool_call_update",
            tool={
                "toolCallId": "call-2",
                "status": "completed",
                "rawOutput": {
                    "type": "read_result",
                    "output_for_prompt": "# title",
                },
                "_meta": {
                    "x.ai/tool": {
                        "name": "read_file",
                        "kind": "read",
                        "namespace": "default",
                        "label": "read_file",
                        "read_only": True,
                    }
                },
            },
        ),
        _acp_line(session_id=session_id, session_update="turn_completed"),
        _acp_line(session_id=session_id, session_update="unknown_future_type", content_text="ignore me"),
    ]
    return _write_jsonl(tmp_path / "updates.jsonl", records)


# --- M1: identity + turns ---


def test_canonicalize_provider_accepts_grok_casing():
    assert canonicalize_provider("Grok") == "grok"
    assert canonicalize_provider("  GROK  ") == "grok"
    assert canonicalize_provider("grok") == "grok"


def test_parse_transcript_source_accepts_grok(tmp_path):
    path = _grok_session_fixture(tmp_path)
    parsed = parse_transcript_source(
        "Grok",
        path,
        project="neurons",
        source_locator_hash="sha256:fixture",
    )
    assert parsed.session.provider == "grok"
    assert parsed.session.session_id_hash == _sha256("grok:grok-session-1")
    roles = [turn.role for turn in parsed.turns]
    texts = [turn.redacted_text for turn in parsed.turns]
    assert roles == ["user", "assistant"]
    assert texts == ["Hello world", "Hi there"]
    # Codex/Claude parity: tool_events empty on turn parse path
    assert parsed.tool_events == []
    # thought / hook / unknown must not appear as turns
    joined = " ".join(texts)
    assert "secret plan" not in joined
    assert "ignore me" not in joined


def test_parse_grok_fails_closed_without_message_turns(tmp_path):
    session_id = "empty-turns"
    path = _write_jsonl(
        tmp_path / "updates.jsonl",
        [
            _acp_line(session_id=session_id, session_update="agent_thought_chunk", content_text="only thought"),
            _acp_line(
                session_id=session_id,
                session_update="hook_execution",
                tool={"event_name": "SessionEnd", "runs": []},
            ),
        ],
    )
    with pytest.raises(ValueError, match="missing transcript turns"):
        parse_transcript_source("grok", path, project="neurons", source_locator_hash="h")


def test_parse_grok_rejects_unsupported_when_not_jsonl(tmp_path):
    path = tmp_path / "not-jsonl.txt"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_transcript_source("grok", path, project="neurons", source_locator_hash="h")


# --- M2: tool evidence ---


def test_extract_grok_tool_evidence_high_signal_only(tmp_path):
    path = _grok_session_fixture(tmp_path)
    records = extract_tool_evidence(
        "grok",
        path,
        project="neurons",
        source_locator_hash="sha256:fixture",
    )
    assert records
    assert all(r.session_id_hash == _sha256("grok:grok-session-1") for r in records)
    assert all(r.provider == "grok" for r in records)
    # shell pytest → test_result; pure read_file exploration → dropped
    categories = {r.category for r in records}
    assert "test_result" in categories
    assert all(r.tool_name != "read_file" for r in records)
    assert any("passed" in r.redacted_summary for r in records)
    # stable meta name kept (not human title "run tests")
    assert any(r.tool_name == "run_terminal_command" for r in records)


def test_grok_tool_name_not_overwritten_by_later_title(tmp_path):
    session_id = "title-overwrite"
    path = _write_jsonl(
        tmp_path / "updates.jsonl",
        [
            _acp_line(session_id=session_id, session_update="user_message_chunk", content_text="hi"),
            _acp_line(session_id=session_id, session_update="turn_completed"),
            _acp_line(
                session_id=session_id,
                session_update="tool_call",
                tool={
                    "toolCallId": "c1",
                    "title": "run tests",
                    "rawInput": {"command": "uv run pytest -q"},
                    "_meta": {"x.ai/tool": {"name": "run_terminal_command", "kind": "execute"}},
                },
            ),
            _acp_line(
                session_id=session_id,
                session_update="tool_call_update",
                tool={
                    "toolCallId": "c1",
                    "title": "run tests",  # human title only, no meta name
                    "status": "completed",
                    "rawOutput": {
                        "exit_code": "1",  # string code must count as error
                        "command": "uv run pytest -q",
                        "output_for_prompt": "1 failed in 0.2s",
                    },
                },
            ),
        ],
    )
    records = extract_tool_evidence("grok", path, project="neurons", source_locator_hash="h")
    assert records
    assert all(r.tool_name == "run_terminal_command" for r in records)
    assert any(r.outcome in {"fail", "error"} or r.category == "test_result" for r in records)


def test_grok_parser_version_wired_into_packed_metadata():
    assert GROK_PARSER_VERSION == "grok-updates-jsonl-parser.v1"
    session = TranscriptSession(
        session_id_hash=_sha256("grok:s1"),
        provider="grok",
        project="neurons",
        started_at="2026-07-09T00:00:00Z",
        ended_at="2026-07-09T00:00:01Z",
        source_status="source_locator_private_spool_only",
        source_locator_hash="sha256:fixture",
    )
    turn = TranscriptTurn(
        turn_id_hash=_sha256("t1"),
        session_id_hash=session.session_id_hash,
        turn_index=1,
        role="user",
        observed_at="2026-07-09T00:00:00Z",
        redacted_text="hello",
    )
    packed = pack_conversation_chunk_document(
        session=session,
        turns=[turn],
        tool_events=[],
        chunk_id="c1",
        knowledge_id="k1",
        capture_request_id="r1",
        chunk_redacted_text="hello",
        part_index=0,
        part_count=1,
        char_start=0,
        char_end=5,
    )
    assert packed.metadata["parser_version"] == GROK_PARSER_VERSION


# --- M3: lane / import / redaction ---


def test_provider_lanes_includes_grok():
    assert "grok" in PROVIDER_LANES
    assert PROVIDER_LANES["grok"].parser is parse_transcript_source


def test_import_historical_source_grok_round_trip(tmp_path):
    store = InMemoryCouchDBSourceStore()
    path = _grok_session_fixture(tmp_path)
    result = import_historical_source(
        locator=SourceLocator(
            provider="Grok",
            source_path=str(path),
            capture_metadata_project="neurons",
        ),
        store=store,
    )
    assert result.status == ImportStatus.IMPORTED
    assert result.provider == "grok"
    assert result.session_id_hash == _sha256("grok:grok-session-1")
    sessions = store.find_by_session(
        session_id_hash=result.session_id_hash,
        doc_type=dm.SourceDocType.TRANSCRIPT_SESSION,
    )
    assert len(sessions) == 1
    assert sessions[0]["provider"] == "grok"


def test_grok_session_identity_distinct_from_codex(tmp_path):
    store = InMemoryCouchDBSourceStore()
    sid = "shared-session-id"
    grok_dir = tmp_path / "g"
    grok_dir.mkdir()
    grok_path = _grok_session_fixture(grok_dir, session_id=sid)
    # Same raw session id must not collide across providers.
    assert _sha256(f"grok:{sid}") != _sha256(f"codex:{sid}")
    result = import_historical_source(
        locator=SourceLocator(provider="grok", source_path=str(grok_path), capture_metadata_project="neurons"),
        store=store,
    )
    assert result.session_id_hash == _sha256(f"grok:{sid}")


def test_redaction_covers_grok_home_path():
    text = "see /Users/example/.grok/sessions/abc/updates.jsonl for details"
    redacted = redact_text_v2(text)
    assert "/Users/example/.grok/" not in redacted
    assert "<redacted:private-path>" in redacted


def test_import_grok_without_capture_does_not_use_updates_jsonl_as_project(tmp_path):
    path = _grok_session_fixture(tmp_path)
    store = InMemoryCouchDBSourceStore()
    result = import_historical_source(
        locator=SourceLocator(
            provider="grok",
            source_path=str(path),
            capture_metadata_project="",
        ),
        store=store,
    )
    assert result.status == ImportStatus.IMPORTED
    assert result.project != "updates.jsonl"
    assert result.project_ambiguous is True
    assert result.eligible_for_retirement is False

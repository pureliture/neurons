"""Tool-evidence inclusion in canonical server-owned session_memory."""

from __future__ import annotations

import json

from agent_knowledge.session_memory.memory_regeneration import (
    SESSION_MEMORY_BODY_VERSION,
    SESSION_MEMORY_BODY_VERSION_WITH_EVIDENCE,
    FixtureTranscriptMemorySource,
    SessionChunkGroup,
    SessionMemoryRegenerationRunner,
    TranscriptMemoryChunkRecord,
    pack_session_memory_document,
)


SESSION_HASH = "sha256:" + "3" * 64
PROJECT = "workspace-ragflow-advisor"
SECRET_VALUE = "synthetic-" + "smem-token-value"
LOCAL_PATH = "/Users/example/Projects/app/run.py"


def _chunk(index_start=1, index_end=2):
    return TranscriptMemoryChunkRecord(
        knowledge_id="conv-know-1",
        chunk_id="conv-chunk-1",
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        turn_start_index=index_start,
        turn_end_index=index_end,
        observed_at_start="2026-05-27T23:20:47+00:00",
        observed_at_end="2026-05-27T23:59:00+00:00",
        content_hash="sha256:" + "c" * 64,
        redacted_text="goal: ship tool evidence\nstate: in progress",
        source_status="source_locator_private_spool_only",
    )


def _group():
    return SessionChunkGroup(
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        chunks=(_chunk(),),
    )


def _evidence_rows():
    return [
        {
            "category": "test_result",
            "outcome": "pass",
            "tool_name": "exec_command",
            "command_summary": "uv run pytest tests -q",
            "redacted_summary": "12 passed in 1.0s",
            "evidence_index": 0,
        },
        {
            "category": "git_state",
            "outcome": "info",
            "tool_name": "exec_command",
            "command_summary": "git status --short",
            "redacted_summary": "git status: 2 file(s) changed",
            "evidence_index": 1,
        },
        {
            "category": "command_error",
            "outcome": "error",
            "tool_name": "exec_command",
            "command_summary": "uv run python sync.py",
            "redacted_summary": f"ValueError boom EVIDENCE_TOKEN={SECRET_VALUE} at {LOCAL_PATH}",
            "evidence_index": 2,
        },
    ]


def test_session_memory_without_evidence_uses_baseline_body_version():
    packed = pack_session_memory_document(_group())
    assert "Tool Evidence" not in packed.body
    assert packed.metadata["body_version"] == SESSION_MEMORY_BODY_VERSION
    assert packed.metadata.get("tool_evidence_count", 0) == 0


def test_session_memory_with_evidence_renders_evidence_section():
    packed = pack_session_memory_document(_group(), evidence=_evidence_rows())
    assert "## Tool Evidence" in packed.body
    assert "test_result" in packed.body
    assert "git status: 2 file(s) changed" in packed.body
    assert packed.metadata["body_version"] == SESSION_MEMORY_BODY_VERSION_WITH_EVIDENCE
    assert packed.metadata["body_version"] != SESSION_MEMORY_BODY_VERSION
    assert packed.metadata["tool_evidence_count"] == 3


def test_session_memory_with_evidence_uses_distinct_knowledge_id_from_v2():
    without_evidence = pack_session_memory_document(_group())
    with_evidence = pack_session_memory_document(_group(), evidence=_evidence_rows())

    assert with_evidence.metadata["knowledge_id"] != without_evidence.metadata["knowledge_id"]


def test_session_memory_evidence_is_redacted_in_body():
    packed = pack_session_memory_document(_group(), evidence=_evidence_rows())
    blob = packed.body + json.dumps(packed.metadata, ensure_ascii=False)
    assert SECRET_VALUE not in blob
    assert LOCAL_PATH not in blob
    assert "/Users/" not in blob


def test_session_memory_runner_includes_evidence_for_session():
    source = FixtureTranscriptMemorySource([_chunk()], tool_evidence=_evidence_rows())
    runner = SessionMemoryRegenerationRunner(source=source, sync=False)
    report = runner.run(project=PROJECT, provider="codex", session_id_hash=SESSION_HASH)
    planned = report["would_write_session_memory"]
    assert planned, "expected a planned session_memory document"
    assert planned[0].get("tool_evidence_count", 0) == 3

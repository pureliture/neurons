from __future__ import annotations

import json

from agent_knowledge.session_memory.memory_regeneration import (
    ProjectChunkGroup,
    SessionChunkGroup,
    TranscriptMemoryChunkRecord,
    pack_project_memory_document,
    pack_session_memory_document,
    pack_session_recap_document,
)
from agent_knowledge.session_memory.transcript_model import (
    ToolEvidenceSummaryRecord,
    TranscriptSession,
    TranscriptToolEvent,
    TranscriptTurn,
)
from agent_knowledge.session_memory.transcript_packer import pack_conversation_chunk_document, pack_tool_evidence_summary_document


SESSION_HASH = "sha256:" + "4" * 64
SOURCE_LOCATOR_HASH = "sha256:" + "5" * 64
PROJECT = "neurons"


def _session() -> TranscriptSession:
    return TranscriptSession(
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        started_at="2026-06-15T12:00:00+00:00",
        ended_at="2026-06-15T12:05:00+00:00",
        source_status="source_locator_private_spool_only",
        source_locator_hash=SOURCE_LOCATOR_HASH,
    )


def _turn() -> TranscriptTurn:
    return TranscriptTurn(
        turn_id_hash="sha256:" + "6" * 64,
        session_id_hash=SESSION_HASH,
        turn_index=1,
        role="user",
        observed_at="2026-06-15T12:01:00+00:00",
        redacted_text="check transcript capture attribution",
    )


def _tool_event() -> TranscriptToolEvent:
    return TranscriptToolEvent(
        tool_event_id_hash="sha256:" + "7" * 64,
        turn_id_hash="sha256:" + "6" * 64,
        event_index=1,
        tool_name="exec_command",
        event_type="call",
        redacted_summary="uv run pytest -q passed",
    )


def _evidence_record() -> ToolEvidenceSummaryRecord:
    return ToolEvidenceSummaryRecord(
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        category="test_result",
        outcome="pass",
        tool_name="exec_command",
        command_summary="uv run pytest -q",
        redacted_summary="1 passed",
        observed_at="2026-06-15T12:03:00+00:00",
        evidence_index=1,
    )


def _chunk() -> TranscriptMemoryChunkRecord:
    return TranscriptMemoryChunkRecord(
        knowledge_id="kn-neurons",
        chunk_id="chunk-neurons",
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        turn_start_index=1,
        turn_end_index=1,
        observed_at_start="2026-06-15T12:01:00+00:00",
        observed_at_end="2026-06-15T12:01:00+00:00",
        content_hash="sha256:" + "8" * 64,
        redacted_text="user: check transcript capture attribution",
        source_status="indexed_transcript_memory",
    )


def _session_group() -> SessionChunkGroup:
    return SessionChunkGroup(session_id_hash=SESSION_HASH, provider="codex", project=PROJECT, chunks=(_chunk(),))


def test_conversation_chunk_uses_repo_project_and_capture_agent_id():
    packed = pack_conversation_chunk_document(session=_session(), turns=[_turn()], tool_events=[_tool_event()], chunk_id="chunk-1")
    serialized = json.dumps({"body": packed.body, "metadata": packed.metadata}, sort_keys=True)

    assert packed.metadata["project"] == PROJECT
    assert packed.metadata["agent_id"] == "codex-transcript-capture"
    assert "- project: neurons" in packed.body
    assert "ragflow-advisor" not in serialized


def test_tool_evidence_uses_repo_project_and_tool_agent_id():
    packed = pack_tool_evidence_summary_document(session=_session(), records=[_evidence_record()])
    serialized = json.dumps({"body": packed.body, "metadata": packed.metadata}, sort_keys=True)

    assert packed.metadata["project"] == PROJECT
    assert packed.metadata["agent_id"] == "codex-tool-evidence"
    assert "- project: neurons" in packed.body
    assert "ragflow-advisor" not in serialized


def test_derived_memory_uses_repo_project_and_producer_agent_ids():
    session_memory = pack_session_memory_document(_session_group())
    session_recap = pack_session_recap_document(_session_group())
    project_memory = pack_project_memory_document(ProjectChunkGroup(provider="codex", project=PROJECT, chunks=(_chunk(),)))

    assert session_memory.metadata["agent_id"] == "codex-memory-regeneration"
    assert session_recap.metadata["agent_id"] == "codex-session-recap"
    assert project_memory.metadata["agent_id"] == "codex-project-memory"
    assert {session_memory.metadata["project"], session_recap.metadata["project"], project_memory.metadata["project"]} == {
        PROJECT
    }

    serialized = json.dumps(
        {
            "session_memory": {"body": session_memory.body, "metadata": session_memory.metadata},
            "session_recap": {"body": session_recap.body, "metadata": session_recap.metadata},
            "project_memory": {"body": project_memory.body, "metadata": project_memory.metadata},
        },
        sort_keys=True,
    )
    assert "ragflow-advisor" not in serialized

"""TDD for packing tool_evidence_summary source documents.

A tool_evidence_summary document is a new append-only source-document kind in
the transcript-memory dataset. It mirrors the conversation_chunk packer: bounded
markdown body, flat string-only metadata for RAGFlow filters, deterministic
order, and the same session_id_hash linkage.
"""

from __future__ import annotations

import json

from agent_knowledge.transcript_model import REDACTION_VERSION, TranscriptSession, ToolEvidenceSummaryRecord
from agent_knowledge.transcript_packer import (
    PackedTranscriptDocument,
    chunk_tool_evidence_records,
    pack_tool_evidence_summary_document,
    pack_tool_evidence_summary_documents,
)


SESSION_HASH = "sha256:" + "3" * 64
SOURCE_LOCATOR_HASH = "sha256:" + "a" * 64
PROJECT = "workspace-ragflow-advisor"
SECRET_VALUE = "synthetic-" + "packer-token-value"
LOCAL_RUNTIME_PATH = "/Users/example/Projects/secret-app/run.py"


def _session() -> TranscriptSession:
    return TranscriptSession(
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        started_at="2026-05-27T23:20:47+00:00",
        ended_at="2026-05-27T23:59:00+00:00",
        source_status="source_locator_private_spool_only",
        source_locator_hash=SOURCE_LOCATOR_HASH,
    )


def _record(category: str, outcome: str, command: str, summary: str, index: int = 0) -> ToolEvidenceSummaryRecord:
    return ToolEvidenceSummaryRecord(
        session_id_hash=SESSION_HASH,
        provider="codex",
        project=PROJECT,
        category=category,
        outcome=outcome,
        tool_name="exec_command",
        command_summary=command,
        redacted_summary=summary,
        observed_at="2026-05-27T23:21:00+00:00",
        evidence_index=index,
    )


def _records() -> list[ToolEvidenceSummaryRecord]:
    return [
        _record("test_result", "pass", "uv run pytest tests -q", "12 passed in 1.23s", 0),
        _record("test_result", "fail", "uv run pytest tests/test_x.py", "1 failed, 4 passed in 2.00s", 1),
        _record("git_state", "info", "git status --short", "git status: 2 file(s) changed", 2),
        _record("command_error", "error", f"uv run python {LOCAL_RUNTIME_PATH}", f"ValueError: boom EVIDENCE_TOKEN={SECRET_VALUE}", 3),
    ]


def test_pack_tool_evidence_summary_document_kind_and_metadata():
    packed = pack_tool_evidence_summary_document(session=_session(), records=_records())
    assert isinstance(packed, PackedTranscriptDocument)
    assert packed.kind == "tool_evidence_summary"
    assert packed.metadata["result_type"] == "tool_evidence_summary"
    assert packed.metadata["type"] == "tool_evidence_summary"
    assert packed.metadata["session_id_hash"] == SESSION_HASH
    assert packed.metadata["provider"] == "codex"
    assert packed.metadata["project"] == PROJECT
    assert packed.metadata["redaction_version"] == REDACTION_VERSION
    assert packed.metadata["schema_version"] == "agent_knowledge_document.v2"
    assert int(packed.metadata["evidence_count"]) == 4


def test_pack_tool_evidence_summary_body_contains_evidence_sections():
    packed = pack_tool_evidence_summary_document(session=_session(), records=_records())
    assert "Tool Evidence" in packed.body
    assert "test_result" in packed.body
    assert "git_state" in packed.body
    assert "12 passed in 1.23s" in packed.body


def test_pack_tool_evidence_summary_metadata_is_flat_for_ragflow_filters():
    packed = pack_tool_evidence_summary_document(session=_session(), records=_records())
    for key, value in packed.metadata.items():
        assert not isinstance(value, (dict, list, tuple, set)), f"metadata {key} must be a flat scalar"


def test_pack_tool_evidence_summary_does_not_leak_secrets_or_paths():
    packed = pack_tool_evidence_summary_document(session=_session(), records=_records())
    blob = packed.body + json.dumps(packed.metadata, ensure_ascii=False)
    assert SECRET_VALUE not in blob
    assert LOCAL_RUNTIME_PATH not in blob
    assert "/Users/" not in blob


def test_pack_tool_evidence_summary_is_stable_across_runs():
    first = pack_tool_evidence_summary_document(session=_session(), records=_records())
    second = pack_tool_evidence_summary_document(session=_session(), records=_records())
    assert first.body == second.body
    assert first.metadata == second.metadata


def test_pack_tool_evidence_summary_requires_records():
    try:
        pack_tool_evidence_summary_document(session=_session(), records=[])
    except ValueError:
        return
    raise AssertionError("expected ValueError for empty evidence records")


def test_chunk_tool_evidence_records_splits_by_size():
    big_summary = "x" * 2000
    records = [_record("git_state", "info", "git status", big_summary, i) for i in range(10)]
    parts = chunk_tool_evidence_records(records, max_chars=4096)
    assert len(parts) > 1
    assert sum(len(part) for part in parts) == len(records)
    # order preserved, no record dropped or duplicated
    flat = [r.evidence_id_hash for part in parts for r in part]
    assert flat == [r.evidence_id_hash for r in records]


def test_pack_tool_evidence_summary_documents_sets_part_counts():
    big_summary = "y" * 2000
    records = [_record("git_state", "info", "git status", big_summary, i) for i in range(10)]
    docs = pack_tool_evidence_summary_documents(session=_session(), records=records)
    assert len(docs) > 1
    assert all(doc.metadata["part_count"] == len(docs) for doc in docs)
    assert sorted(int(doc.metadata["part_index"]) for doc in docs) == list(range(1, len(docs) + 1))
    assert all(doc.kind == "tool_evidence_summary" for doc in docs)

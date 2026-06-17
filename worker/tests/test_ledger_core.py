from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS


PROJECT = "workspace-ragflow-advisor"
SOURCE_MANIFEST_HASH = "sha256:" + ("a" * 64)


@dataclass(frozen=True)
class FakeTranscriptSession:
    session_id_hash: str = "sha256:session"

    def to_record(self) -> dict:
        return {
            "session_id_hash": self.session_id_hash,
            "provider": "codex",
            "project": PROJECT,
            "started_at": "2026-06-13T00:00:00+00:00",
            "ended_at": "2026-06-13T00:01:00+00:00",
            "source_status": "indexed_transcript_memory",
            "source_locator_hash": "sha256:locator",
        }


@dataclass(frozen=True)
class FakeTranscriptChunk:
    knowledge_id: str = "kn_chunk"
    chunk_id: str = "chunk_1"
    session_id_hash: str = "sha256:session"
    content_hash: str = "sha256:chunk"

    def to_record(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "session_id_hash": self.session_id_hash,
            "provider": "codex",
            "project": PROJECT,
            "turn_start_index": 0,
            "turn_end_index": 1,
            "part_index": 1,
            "part_count": 1,
            "char_start": 0,
            "char_end": 15,
            "content_hash": self.content_hash,
            "redacted_text": "redacted chunk",
            "source_status": "indexed_transcript_memory",
            "redaction_version": "redaction.v2",
        }

    def title(self) -> str:
        return "Conversation chunk"

    def summary(self) -> str:
        return "redacted chunk"


def _private_dir(tmp_path: Path) -> Path:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return private


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(_private_dir(tmp_path) / "ledger.sqlite")


def test_ledger_core_lifecycle_and_queue_selectors(tmp_path: Path):
    ledger = _ledger(tmp_path)

    item = ledger.upsert_prepared(
        knowledge_id="kn_1",
        content_hash="sha256:kn_1",
        provider="codex",
        project=PROJECT,
        domain="agent_memory",
        type="conversation_chunk",
        title="Chunk",
        summary="Chunk summary",
        privacy_level="private",
        metadata={"chunk_id": "chunk_1"},
    )
    assert item["status"] == "prepared"

    ledger.mark_enqueued("kn_1", target_profile="ragflow-transcript-memory", job_id="job_1")
    queued = ledger.list_queued_documents(
        document_type="conversation_chunk",
        target_profile="ragflow-transcript-memory",
    )

    assert [row["knowledge_id"] for row in queued] == ["kn_1"]
    assert queued[0]["ingress_job_id"] == "job_1"
    assert queued[0]["metadata"]["chunk_id"] == "chunk_1"

    ledger.mark_indexed("kn_1", run="DONE")
    assert ledger.list_queued_documents(
        document_type="conversation_chunk",
        target_profile="ragflow-transcript-memory",
    ) == []
    assert ledger.lifecycle_counts()["indexed"] == 1


def test_ledger_read_only_snapshot_rejects_writes_without_mutating_source(tmp_path: Path):
    path = _private_dir(tmp_path) / "ledger.sqlite"
    ledger = Ledger(path)
    ledger.upsert_prepared(
        knowledge_id="kn_ro",
        content_hash="sha256:ro",
        provider="codex",
        project=PROJECT,
        domain="agent_memory",
        type="runtime_evidence",
        title="Read only",
        summary="Read only",
    )

    read_only = Ledger.open_read_only(path)

    assert read_only.get_by_knowledge_id("kn_ro")["status"] == "prepared"
    with pytest.raises(sqlite3.OperationalError):
        read_only.mark_disabled("kn_ro")
    assert Ledger(path).get_by_knowledge_id("kn_ro")["authorization_status"] == "active"


def test_ledger_transcript_tables_store_sessions_and_chunks(tmp_path: Path):
    ledger = _ledger(tmp_path)
    session = FakeTranscriptSession()
    chunk = FakeTranscriptChunk()

    ledger.upsert_transcript_session(session)
    item = ledger.upsert_transcript_chunk(knowledge_id=chunk.knowledge_id, chunk=chunk)

    assert ledger.get_transcript_session(session.session_id_hash)["provider"] == "codex"
    assert ledger.list_transcript_sessions(project=PROJECT)[0]["session_id_hash"] == session.session_id_hash
    assert ledger.get_transcript_chunk_by_knowledge_id(item["knowledge_id"])["chunk_id"] == chunk.chunk_id
    assert ledger.get_by_knowledge_id(item["knowledge_id"])["type"] == "conversation_chunk"


def test_ledger_dirty_session_memory_state_machine(tmp_path: Path):
    ledger = _ledger(tmp_path)

    pending = ledger.mark_session_memory_dirty(
        session_id_hash="sha256:session",
        provider="codex",
        project=PROJECT,
        reason="indexed_transcript_chunk",
        source_knowledge_id="kn_chunk",
    )
    assert pending["status"] == "pending"
    assert ledger.list_dirty_session_memory(quiet_period_seconds=0)[0]["session_id_hash"] == "sha256:session"

    enqueued = ledger.mark_dirty_session_memory_enqueued(
        session_id_hash="sha256:session",
        summary_knowledge_id="kn_session_memory",
        ingress_job_id="job_session_memory",
    )
    assert enqueued["status"] == "enqueued"
    assert enqueued["last_ingress_job_id"] == "job_session_memory"

    promoted = ledger.mark_dirty_session_memory_promoted(
        session_id_hash="sha256:session",
        summary_knowledge_id="kn_session_memory",
    )
    assert promoted["status"] == "promoted"


def test_session_memory_requires_regeneration_evidence_before_promotion(tmp_path: Path):
    ledger = _ledger(tmp_path)
    item = ledger.upsert_session_memory(
        knowledge_id="kn_session_memory",
        content_hash="sha256:session-memory",
        provider="codex",
        project=PROJECT,
        session_id_hash="sha256:session",
        title="Session memory",
        summary="Session memory",
        evidence_status="historical",
        coverage_status="complete",
        source_manifest_hash=SOURCE_MANIFEST_HASH,
        source_chunk_count=1,
    )
    ledger.mark_uploaded(item["knowledge_id"], dataset_id="ds_session", document_id="doc_session", run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")

    with pytest.raises(ValueError, match="regenerated transcript provenance"):
        ledger.promote_session_memory(item["knowledge_id"])

    regenerated = ledger.upsert_session_memory(
        knowledge_id=item["knowledge_id"],
        content_hash=item["content_hash"],
        provider="codex",
        project=PROJECT,
        session_id_hash="sha256:session",
        title="Session memory",
        summary="Session memory",
        evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        coverage_status="complete",
        source_manifest_hash=SOURCE_MANIFEST_HASH,
        source_chunk_count=1,
    )
    assert regenerated["evidence_status"] == SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS

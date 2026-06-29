import hashlib
import inspect

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory import memory_regeneration as memory_regeneration_module
from agent_knowledge.session_memory.memory_regeneration import (
    DEFAULT_PROJECT_MEMORY_TARGET_PROFILE,
    DEFAULT_SESSION_MEMORY_TARGET_PROFILE,
    PROJECT_CONTEXT_SNAPSHOT_KIND,
    PROJECT_MEMORY_DATASET_ROLE,
    SESSION_MEMORY_DATASET_ROLE,
    FixtureTranscriptMemorySource,
    LedgerTranscriptMemorySource,
    ProjectChunkGroup,
    ProjectMemoryRegenerationRunner,
    SessionMemoryRegenerationRunner,
    TranscriptMemoryChunkRecord,
    pack_project_memory_document,
)


PROJECT = "workspace-index-advisor"
DATASET_ID = "ds_private_transcript_memory"
DOCUMENT_ID = "doc_private_transcript_chunk"


class FakeIngressQueueSink:
    def __init__(self):
        self.requests = []

    def enqueue_document(self, *, source, packed, content_hash, target_profile, kind, idempotency_key):
        self.requests.append(
            {
                "source": source,
                "body": packed.body,
                "metadata": packed.metadata,
                "content_hash": content_hash,
                "target_profile": target_profile,
                "kind": kind,
                "idempotency_key": idempotency_key,
            }
        )
        return {"job_id": "job_project_memory_001", "status": "queued"}


def _chunk(
    *,
    knowledge_id: str,
    chunk_id: str,
    session_id_hash: str = "sha256:sessionalpha",
    turn_start_index: int,
    turn_end_index: int,
    redacted_text: str,
    content_hash: str | None = None,
) -> TranscriptMemoryChunkRecord:
    return TranscriptMemoryChunkRecord(
        knowledge_id=knowledge_id,
        chunk_id=chunk_id,
        session_id_hash=session_id_hash,
        provider="codex",
        project=PROJECT,
        turn_start_index=turn_start_index,
        turn_end_index=turn_end_index,
        observed_at_start=f"2026-05-18T10:{turn_start_index:02d}:00+09:00",
        observed_at_end=f"2026-05-18T10:{turn_end_index:02d}:30+09:00",
        content_hash=content_hash or f"sha256:{knowledge_id}",
        redacted_text=redacted_text,
        source_status="indexed_transcript_memory",
        redaction_version="redaction.v2",
    )


def _body_without_front_matter(markdown: str) -> str:
    if markdown.startswith("---\n") and "\n---\n" in markdown:
        return markdown.split("\n---\n", 1)[1]
    return markdown


def _assert_minimal_front_matter(markdown: str, *, result_type: str) -> None:
    assert markdown.startswith(
        "---\n"
        "schema_version: agent_knowledge_document.v2\n"
        f"result_type: {result_type}\n"
        "---\n"
    )
    assert "knowledge_id:" not in markdown.split("---\n", 2)[1]
    assert "content_hash:" not in markdown.split("---\n", 2)[1]


def test_memory_regeneration_module_is_server_core_without_client_cli_wiring():
    source = inspect.getsource(memory_regeneration_module)

    assert "agent_knowledge.cli" not in source
    assert "IngressQueueClient" not in source
    assert "outbox_client" not in source
    assert DEFAULT_SESSION_MEMORY_TARGET_PROFILE == "index-session-memory"
    assert SESSION_MEMORY_DATASET_ROLE == "session-memory"


def test_fixture_transcript_chunks_group_into_project_memory_snapshot_document():
    runner = ProjectMemoryRegenerationRunner(
        source=FixtureTranscriptMemorySource(
            [
                _chunk(
                    knowledge_id="kn_project_a",
                    chunk_id="chunk_project_a",
                    session_id_hash="sha256:sessionalpha",
                    turn_start_index=1,
                    turn_end_index=2,
                    redacted_text="user: Work on session-memory routing.",
                ),
                _chunk(
                    knowledge_id="kn_project_b",
                    chunk_id="chunk_project_b",
                    session_id_hash="sha256:sessionbeta",
                    turn_start_index=3,
                    turn_end_index=4,
                    redacted_text="assistant: Keep project-memory as repo-level snapshot.",
                ),
            ]
        )
    )

    report = runner.run(project=PROJECT, provider="codex")

    assert report["mode"] == "dry_run"
    assert report["datasetRole"] == PROJECT_MEMORY_DATASET_ROLE
    assert report["targetProfile"] == DEFAULT_PROJECT_MEMORY_TARGET_PROFILE
    assert report["kind"] == PROJECT_CONTEXT_SNAPSHOT_KIND
    assert report["projects_seen"] == 1
    assert report["snapshots_planned"] == 1
    planned = report["would_enqueue"][0]
    assert planned["datasetRole"] == PROJECT_MEMORY_DATASET_ROLE
    assert planned["targetProfile"] == DEFAULT_PROJECT_MEMORY_TARGET_PROFILE
    assert planned["kind"] == PROJECT_CONTEXT_SNAPSHOT_KIND
    assert planned["provider"] == "codex"
    assert planned["project"] == PROJECT
    assert planned["source_session_count"] == 2
    assert planned["source_chunk_count"] == 2
    assert planned["contentHash"].startswith("sha256:")
    assert planned["idempotencyKey"] == f"project_context_snapshot:codex:{PROJECT}:{planned['contentHash']}"
    assert "body" not in planned


def test_project_memory_document_prioritizes_project_state_before_appendix():
    chunks = (
        _chunk(
            knowledge_id="kn_project_quality_1",
            chunk_id="chunk_project_quality_1",
            session_id_hash="sha256:project-quality-a",
            turn_start_index=1,
            turn_end_index=2,
            redacted_text=(
                "Current Runtime State: session-memory runtime gate complete. "
                "Dataset Shape: project-memory has one workspace-index-advisor.md document. "
                "Active Routes: project-memory writes through memory-regeneration --output project-memory."
            ),
            content_hash="sha256:project-quality-1",
        ),
        _chunk(
            knowledge_id="kn_project_quality_2",
            chunk_id="chunk_project_quality_2",
            session_id_hash="sha256:project-quality-b",
            turn_start_index=3,
            turn_end_index=4,
            redacted_text=(
                "Recent Decisions: keep rerank out of session-memory direct lookup. "
                "Open Risks / Follow-ups: regenerate session-memory in canary batches."
            ),
            content_hash="sha256:project-quality-2",
        ),
    )

    packed = pack_project_memory_document(ProjectChunkGroup(provider="codex", project=PROJECT, chunks=chunks))

    body = _body_without_front_matter(packed.body)
    first_window = body[:2600]
    expected_order = [
        "## Current Runtime State",
        "## Dataset Shape",
        "## Active Routes",
        "## Recent Decisions",
        "## Open Risks / Follow-ups",
        "## Appendix: Source Sessions",
    ]
    positions = [body.index(section) for section in expected_order]
    assert positions == sorted(positions)
    assert "workspace-index-advisor" in first_window
    assert "project_context_snapshot" in first_window
    assert "runtime_evidence" not in first_window
    assert first_window.count("sha256:") <= 2
    _assert_minimal_front_matter(packed.body, result_type=PROJECT_CONTEXT_SNAPSHOT_KIND)


def test_enqueue_mode_calls_queue_sink_with_project_memory_contract(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    sink = FakeIngressQueueSink()
    source = FixtureTranscriptMemorySource(
        [
            _chunk(
                knowledge_id="kn_project_source_1",
                chunk_id="chunk_project_source_1",
                turn_start_index=1,
                turn_end_index=2,
                redacted_text="user: Keep repo state in project-memory.",
            )
        ]
    )

    report = ProjectMemoryRegenerationRunner(source=source, ledger=ledger, enqueue_sink=sink, enqueue=True).run()

    assert report["mode"] == "enqueue"
    assert report["snapshots_planned"] == 1
    assert report["would_enqueue"] == []
    assert report["enqueued"][0]["job_id"] == "job_project_memory_001"
    assert len(sink.requests) == 1
    request = sink.requests[0]
    assert request["source"] == {
        "host": "mac_mini",
        "producer": "memory-regeneration-runner",
        "provider": "codex",
        "project": PROJECT,
    }
    assert request["target_profile"] == DEFAULT_PROJECT_MEMORY_TARGET_PROFILE
    assert request["kind"] == PROJECT_CONTEXT_SNAPSHOT_KIND
    assert request["metadata"]["dataset_role"] == PROJECT_MEMORY_DATASET_ROLE
    assert request["content_hash"] == "sha256:" + hashlib.sha256(request["body"].encode("utf-8")).hexdigest()
    assert request["idempotency_key"] == f"project_context_snapshot:codex:{PROJECT}:{request['content_hash']}"
    row = ledger.get_by_content_hash(request["content_hash"])
    assert row["type"] == PROJECT_CONTEXT_SNAPSHOT_KIND
    assert row["status"] == "queued"
    assert row["ingress_target_profile"] == DEFAULT_PROJECT_MEMORY_TARGET_PROFILE
    assert row["ingress_job_id"] == "job_project_memory_001"
    assert row["index_target_id"] == ""
    assert row["index_document_id"] == ""


def test_ledger_transcript_source_densifies_sparse_turn_indexes_for_session_memory(tmp_path):
    from agent_knowledge.session_memory.transcript_model import TranscriptSession, TranscriptTurn

    ledger = Ledger(tmp_path / "ledger.sqlite")
    session = TranscriptSession(
        session_id_hash="sha256:sparse-turn-session",
        provider="codex",
        project=PROJECT,
        started_at="2026-05-18T10:01:00+09:00",
        ended_at="2026-05-18T10:10:00+09:00",
        source_status="indexed_transcript_memory",
        source_locator_hash="sha256:sparse-turn-locator",
    )
    ledger.upsert_transcript_session(session)
    for turn_index, role, text in (
        (1, "user", "first sparse source turn"),
        (3, "assistant", "second sparse source turn"),
        (10, "user", "third sparse source turn"),
    ):
        ledger.upsert_transcript_turn(
            TranscriptTurn(
                turn_id_hash=f"sha256:sparse-turn-{turn_index}",
                session_id_hash=session.session_id_hash,
                turn_index=turn_index,
                role=role,
                observed_at=f"2026-05-18T10:{turn_index:02d}:00+09:00",
                redacted_text=text,
            )
        )

    source = LedgerTranscriptMemorySource(ledger)
    chunks = source.list_conversation_chunks(
        project=PROJECT,
        provider="codex",
        session_id_hash=session.session_id_hash,
    )
    report = memory_regeneration_module._coverage_report(tuple(chunks))
    runner_report = SessionMemoryRegenerationRunner(source=source).run(
        project=PROJECT,
        provider="codex",
        session_id_hash=session.session_id_hash,
    )

    assert [(chunk.turn_start_index, chunk.turn_end_index) for chunk in chunks] == [(1, 1), (2, 2), (3, 3)]
    assert report == {"duplicate_count": 0, "gap_count": 0}
    assert runner_report["memory_documents_planned"] == 1
    assert runner_report["would_write_session_memory"][0]["coverage_readiness_status"] == "ready_for_upload"


def test_ledger_transcript_source_densifies_chunk_fallback_windows(tmp_path):
    from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

    ledger = Ledger(tmp_path / "ledger.sqlite")
    session = TranscriptSession(
        session_id_hash="sha256:chunk-fallback-session",
        provider="codex",
        project=PROJECT,
        started_at="2026-05-18T10:01:00+09:00",
        ended_at="2026-05-18T10:03:00+09:00",
        source_status="indexed_transcript_memory",
        source_locator_hash="sha256:chunk-fallback-locator",
    )
    ledger.upsert_transcript_session(session)
    for index, text in enumerate(("first chunk for same provider window", "second chunk for same provider window"), start=1):
        chunk = TranscriptChunk(
            chunk_id=f"chunk_fallback_{index}",
            session_id_hash=session.session_id_hash,
            provider=session.provider,
            project=session.project,
            turn_start_index=1,
            turn_end_index=1,
            redacted_text=text,
            content_hash="",
            source_status=session.source_status,
            part_index=index,
            part_count=2,
            char_start=(index - 1) * 100,
            char_end=index * 100,
        )
        row = ledger.upsert_transcript_chunk(knowledge_id=f"kn_chunk_fallback_{index}", chunk=chunk)
        ledger.mark_uploaded(row["knowledge_id"], dataset_id=DATASET_ID, document_id=f"{DOCUMENT_ID}_{index}", run="DONE")
        ledger.mark_indexed(row["knowledge_id"], run="DONE")

    source = LedgerTranscriptMemorySource(ledger)
    chunks = source.list_conversation_chunks(
        project=PROJECT,
        provider="codex",
        session_id_hash=session.session_id_hash,
    )
    report = memory_regeneration_module._coverage_report(tuple(chunks))
    runner_report = SessionMemoryRegenerationRunner(source=source).run(
        project=PROJECT,
        provider="codex",
        session_id_hash=session.session_id_hash,
    )

    assert [(chunk.turn_start_index, chunk.turn_end_index) for chunk in chunks] == [(1, 1), (2, 2)]
    assert [(chunk.part_index, chunk.part_count) for chunk in chunks] == [(1, 1), (1, 1)]
    assert report == {"duplicate_count": 0, "gap_count": 0}
    assert runner_report["memory_documents_planned"] == 1
    assert runner_report["would_write_session_memory"][0]["coverage_readiness_status"] == "ready_for_upload"

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from agent_knowledge.session_memory import regeneration_index_sync
from agent_knowledge.session_memory.memory_regeneration import (
    FixtureTranscriptMemorySource,
    SessionMemoryRegenerationRunner,
    TranscriptMemoryChunkRecord,
)


PROJECT = "workspace-index-advisor"
DATASET_ID = "ds_session_memory"


class FakeLedger:
    def __init__(self, *, existing: dict | None = None):
        self.existing = existing
        self.events: list[tuple[str, dict]] = []
        self.coverage: list[dict] = []

    def get_by_knowledge_id(self, knowledge_id: str) -> dict | None:
        self.events.append(("get_by_knowledge_id", {"knowledge_id": knowledge_id}))
        return self.existing

    def get_by_content_hash(self, content_hash: str) -> dict | None:
        self.events.append(("get_by_content_hash", {"content_hash": content_hash}))
        return self.existing

    def upsert_session_memory(self, **kwargs) -> dict:
        self.events.append(("upsert_session_memory", kwargs))
        self.existing = {**kwargs, "knowledge_id": "kn_canonical", "status": "planned"}
        return self.existing

    def record_session_memory_coverage(self, **kwargs) -> None:
        self.events.append(("record_session_memory_coverage", kwargs))
        self.coverage.append(kwargs)

    def mark_uploaded(self, knowledge_id: str, **kwargs) -> None:
        self.events.append(("mark_uploaded", {"knowledge_id": knowledge_id, **kwargs}))
        self._set_status("uploaded_unparsed", index_document_id=kwargs["document_id"])

    def mark_metadata_applied(self, knowledge_id: str) -> None:
        self.events.append(("mark_metadata_applied", {"knowledge_id": knowledge_id}))
        self._set_status("metadata_applied")

    def mark_parse_requested(self, knowledge_id: str) -> None:
        self.events.append(("mark_parse_requested", {"knowledge_id": knowledge_id}))
        self._set_status("parse_requested")

    def mark_indexing(self, knowledge_id: str, **kwargs) -> None:
        self.events.append(("mark_indexing", {"knowledge_id": knowledge_id, **kwargs}))
        self._set_status("indexing")

    def mark_indexed(self, knowledge_id: str, **kwargs) -> None:
        self.events.append(("mark_indexed", {"knowledge_id": knowledge_id, **kwargs}))
        self._set_status("indexed")

    def mark_parse_failed(self, knowledge_id: str, **kwargs) -> None:
        self.events.append(("mark_parse_failed", {"knowledge_id": knowledge_id, **kwargs}))
        self._set_status("parse_failed")

    def mark_index_timeout(self, knowledge_id: str, **kwargs) -> None:
        self.events.append(("mark_index_timeout", {"knowledge_id": knowledge_id, **kwargs}))
        self._set_status("index_timeout")

    def _set_status(self, status: str, **updates) -> None:
        if self.existing is not None:
            self.existing.update(status=status, **updates)


class FakeRetiredIndexBridge:
    def __init__(self, statuses: list[dict]):
        self.statuses = list(statuses)
        self.events: list[tuple[str, dict]] = []

    def upload_document(self, dataset_id: str, content: str, *, filename: str) -> dict:
        self.events.append(
            ("upload_document", {"dataset_id": dataset_id, "content": content, "filename": filename})
        )
        return {"document_id": "doc_fresh", "run": "UNSTART"}

    def update_metadata(self, dataset_id: str, document_id: str, metadata: dict) -> None:
        self.events.append(
            ("update_metadata", {"dataset_id": dataset_id, "document_id": document_id, "metadata": dict(metadata)})
        )

    def request_parse(self, dataset_id: str, document_ids: list[str]) -> None:
        self.events.append(("request_parse", {"dataset_id": dataset_id, "document_ids": list(document_ids)}))

    def get_document_status(self, dataset_id: str, document_id: str) -> dict:
        self.events.append(("get_document_status", {"dataset_id": dataset_id, "document_id": document_id}))
        return self.statuses.pop(0)


class ParseRequestFailureBridge(FakeRetiredIndexBridge):
    def request_parse(self, dataset_id: str, document_ids: list[str]) -> None:
        super().request_parse(dataset_id, document_ids)
        raise RuntimeError("parse service unavailable")


def _source(*, session_id_hash: str = "sha256:session-sync") -> FixtureTranscriptMemorySource:
    return FixtureTranscriptMemorySource(
        [
            TranscriptMemoryChunkRecord(
                knowledge_id="kn_source_chunk",
                chunk_id="chunk_source",
                session_id_hash=session_id_hash,
                provider="codex",
                project=PROJECT,
                turn_start_index=1,
                turn_end_index=2,
                observed_at_start="2026-07-28T10:00:00+09:00",
                observed_at_end="2026-07-28T10:01:00+09:00",
                content_hash="sha256:source-content",
                redacted_text="user: regenerate the session memory.",
                source_status="indexed_transcript_memory",
                redaction_version="redaction.v2",
            )
        ]
    )


def _two_session_source() -> FixtureTranscriptMemorySource:
    return FixtureTranscriptMemorySource(
        _source().list_conversation_chunks()
        + _source(session_id_hash="sha256:session-sync-second").list_conversation_chunks()
    )


def _all_skipped_source() -> FixtureTranscriptMemorySource:
    source_chunk = _source().list_conversation_chunks()[0]
    return FixtureTranscriptMemorySource(
        [
            replace(
                source_chunk,
                session_id_hash="sha256:invalid-window",
                turn_start_index=0,
                turn_end_index=0,
            ),
            replace(
                source_chunk,
                session_id_hash="sha256:gapped-window",
                chunk_id="chunk_gap_first",
                content_hash="sha256:gap-first",
                turn_start_index=1,
                turn_end_index=1,
            ),
            replace(
                source_chunk,
                session_id_hash="sha256:gapped-window",
                chunk_id="chunk_gap_second",
                content_hash="sha256:gap-second",
                turn_start_index=3,
                turn_end_index=3,
            ),
        ]
    )


def _content_hash() -> str:
    report = SessionMemoryRegenerationRunner(source=_source()).run()
    return report["would_write_session_memory"][0]["contentHash"]


def _run_sync(*, ledger: FakeLedger, bridge: FakeRetiredIndexBridge, max_poll_attempts: int = 2) -> dict:
    return SessionMemoryRegenerationRunner(
        source=_source(),
        ledger=ledger,
        sync=True,
        retired_index_bridge=bridge,
        dataset_id=DATASET_ID,
        max_poll_attempts=max_poll_attempts,
        poll_interval_seconds=0,
    ).run()


def _event_names(events: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _details in events]


def test_sync_without_dependencies_returns_empty_source_report():
    report = SessionMemoryRegenerationRunner(
        source=FixtureTranscriptMemorySource([]),
        sync=True,
    ).run()

    assert report["mode"] == "sync"
    assert report["memory_documents_planned"] == 0
    assert report["skipped_sessions"] == []
    assert report["mutation_performed"] is False


def test_sync_without_dependencies_returns_all_skipped_groups_report():
    report = SessionMemoryRegenerationRunner(
        source=_all_skipped_source(),
        sync=True,
    ).run()

    assert report["mode"] == "sync"
    assert report["memory_documents_planned"] == 0
    assert {item["reason"] for item in report["skipped_sessions"]} == {
        "invalid_turn_window",
        "coverage_incomplete_before_upload",
    }
    assert report["mutation_performed"] is False


def test_sync_initializes_executor_once_and_keeps_document_request_minimal(monkeypatch):
    construction_args: list[dict] = []
    requests = []

    class RecordingExecutor:
        def __init__(self, **kwargs):
            construction_args.append(kwargs)

        def execute(self, request):
            requests.append(request)
            return regeneration_index_sync.SessionMemoryIndexSyncResult(
                planned=dict(request.planned),
                mutation_performed=False,
            )

    monkeypatch.setattr(regeneration_index_sync, "SessionMemoryIndexSyncExecutor", RecordingExecutor)
    ledger = FakeLedger()
    bridge = FakeRetiredIndexBridge([])
    runner = SessionMemoryRegenerationRunner(
        source=_two_session_source(),
        ledger=ledger,
        sync=True,
        retired_index_bridge=bridge,
        dataset_id=DATASET_ID,
        max_poll_attempts=3,
        poll_interval_seconds=0,
    )

    report = runner.run()

    assert len(construction_args) == 1
    assert construction_args[0]["ledger"] is ledger
    assert construction_args[0]["retired_index_bridge"] is bridge
    assert construction_args[0]["dataset_id"] == DATASET_ID
    assert construction_args[0]["max_poll_attempts"] == 3
    assert len(requests) == 2
    assert [field.name for field in fields(requests[0])] == ["packed", "planned", "coverage_records"]
    assert report["memory_documents_planned"] == 2


def test_sync_fresh_upload_metadata_parse_and_poll_success():
    ledger = FakeLedger()
    bridge = FakeRetiredIndexBridge([{"run": "RUNNING", "progress": 0.4}, {"run": "DONE"}])

    report = _run_sync(ledger=ledger, bridge=bridge)

    assert report["mode"] == "sync"
    assert report["network_used"] is True
    assert report["mutation_performed"] is True
    assert report["index_write_performed"] is True
    assert report["would_write_session_memory"][0]["document_id"] == "doc_fresh"
    assert _event_names(bridge.events) == [
        "upload_document",
        "update_metadata",
        "request_parse",
        "get_document_status",
        "get_document_status",
    ]
    assert _event_names(ledger.events) == [
        "get_by_knowledge_id",
        "get_by_content_hash",
        "upsert_session_memory",
        "record_session_memory_coverage",
        "mark_uploaded",
        "mark_metadata_applied",
        "mark_parse_requested",
        "mark_indexing",
        "mark_indexed",
    ]
    assert ledger.coverage[0]["active_knowledge_id"] == "kn_canonical"
    assert ledger.coverage[0]["derived_content_hash"] == _content_hash()


def test_sync_reuses_indexed_same_content_and_restores_coverage_without_bridge_calls():
    ledger = FakeLedger(
        existing={
            "knowledge_id": "kn_indexed",
            "content_hash": _content_hash(),
            "status": "indexed",
            "index_document_id": "doc_indexed",
        }
    )
    bridge = FakeRetiredIndexBridge([])

    report = _run_sync(ledger=ledger, bridge=bridge)

    assert report["mutation_performed"] is False
    assert report["index_write_performed"] is False
    assert report["would_write_session_memory"][0]["knowledge_id"] == "kn_indexed"
    assert bridge.events == []
    assert _event_names(ledger.events) == ["get_by_knowledge_id", "record_session_memory_coverage"]
    assert len(ledger.coverage) == 1
    coverage = ledger.coverage[0]
    assert coverage["active_knowledge_id"] == "kn_indexed"
    assert coverage["source_content_hash"] == "sha256:source-content"
    assert coverage["source_window_hash"].startswith("sha256:")
    assert coverage["derived_content_hash"] == _content_hash()
    assert coverage["redaction_version"] == "redaction.v2"
    assert coverage["turn_start_index"] == 1
    assert coverage["turn_end_index"] == 2


def test_sync_records_metadata_before_parse_failure_and_resumes_without_reapplying_metadata():
    ledger = FakeLedger()
    failing_bridge = ParseRequestFailureBridge([])

    with pytest.raises(RuntimeError, match="parse service unavailable"):
        _run_sync(ledger=ledger, bridge=failing_bridge)

    assert ledger.existing is not None
    assert ledger.existing["status"] == "metadata_applied"
    assert _event_names(ledger.events)[-1] == "mark_metadata_applied"

    resumed_bridge = FakeRetiredIndexBridge([{"run": "DONE"}])
    report = _run_sync(ledger=ledger, bridge=resumed_bridge)

    assert report["would_write_session_memory"][0]["document_id"] == "doc_fresh"
    assert _event_names(resumed_bridge.events) == ["request_parse", "get_document_status"]


@pytest.mark.parametrize(
    ("status", "expected_bridge_events", "expects_metadata_applied", "expects_parse_request"),
    [
        ("uploaded_unparsed", ["update_metadata", "request_parse", "get_document_status"], True, True),
        ("metadata_applied", ["request_parse", "get_document_status"], False, True),
        ("parse_requested", ["get_document_status"], False, False),
        ("indexing", ["get_document_status"], False, False),
        ("index_timeout", ["get_document_status"], False, False),
    ],
)
def test_sync_resumes_each_existing_index_state(
    status,
    expected_bridge_events,
    expects_metadata_applied,
    expects_parse_request,
):
    ledger = FakeLedger(
        existing={
            "knowledge_id": "kn_resume",
            "content_hash": _content_hash(),
            "status": status,
            "index_document_id": "doc_resume",
        }
    )
    bridge = FakeRetiredIndexBridge([{"run": "DONE"}])

    report = _run_sync(ledger=ledger, bridge=bridge)

    assert report["mutation_performed"] is True
    assert report["would_write_session_memory"][0]["document_id"] == "doc_resume"
    assert _event_names(bridge.events) == expected_bridge_events
    ledger_events = _event_names(ledger.events)
    assert "upsert_session_memory" not in ledger_events
    assert ("mark_metadata_applied" in ledger_events) is expects_metadata_applied
    assert ("mark_parse_requested" in ledger_events) is expects_parse_request
    assert ledger_events[-1] == "mark_indexed"


@pytest.mark.parametrize(
    ("statuses", "expected_ledger_event", "error"),
    [
        ([{"run": "FAIL"}], "mark_parse_failed", "parse failed"),
        ([{"run": "RUNNING", "progress": 0.25}], "mark_index_timeout", "index timeout"),
    ],
)
def test_sync_persists_parse_failure_or_timeout_status(statuses, expected_ledger_event, error):
    ledger = FakeLedger()
    bridge = FakeRetiredIndexBridge(statuses)

    with pytest.raises(RuntimeError, match=error):
        _run_sync(ledger=ledger, bridge=bridge, max_poll_attempts=1)

    assert expected_ledger_event in _event_names(ledger.events)

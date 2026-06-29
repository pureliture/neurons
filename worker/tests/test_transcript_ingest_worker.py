import json
import os
from pathlib import Path

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.transcript_ingest import (
    DEFAULT_TRANSCRIPT_TARGET_PROFILE,
    TranscriptIngestWorker,
)


PROJECT = "workspace-index-advisor"


class _FakeCaptureSpool:
    def __init__(self, root: Path):
        self.root = root
        for name in ("pending", "processing", "acked", "quarantine"):
            (root / name).mkdir(parents=True, exist_ok=True)

    def enqueue(self, request: dict) -> Path:
        path = self.root / "pending" / f"{request['request_id']}.json"
        path.write_text(json.dumps(request, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def claim_next(self) -> Path:
        source = sorted((self.root / "pending").glob("*.json"))[0]
        target = self.root / "processing" / source.name
        os.replace(source, target)
        return target

    def ack(self, processing_path: Path) -> Path:
        target = self.root / "acked" / processing_path.name
        os.replace(processing_path, target)
        return target

    def quarantine_with_failure(self, processing_path: Path, failure: dict | None = None) -> Path:
        payload = json.loads(processing_path.read_text(encoding="utf-8"))
        if failure:
            payload["last_failure"] = failure
        processing_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        target = self.root / "quarantine" / processing_path.name
        os.replace(processing_path, target)
        return target

    def depth_counts(self) -> dict[str, int]:
        return {name: len(list((self.root / name).glob("*.json"))) for name in ("pending", "processing", "acked", "quarantine")}


class _FakeIngressSink:
    def __init__(self, *, reject_after: int | None = None):
        self.reject_after = reject_after
        self.calls = []

    def enqueue_document(self, *, source, packed, content_hash, target_profile, kind, idempotency_key):
        if self.reject_after is not None and len(self.calls) >= self.reject_after:
            raise RuntimeError("synthetic enqueue rejection")
        self.calls.append(
            {
                "source": source,
                "packed": packed,
                "content_hash": content_hash,
                "target_profile": target_profile,
                "kind": kind,
                "idempotency_key": idempotency_key,
            }
        )
        return {"job_id": f"job_transcript_{len(self.calls):03d}", "status": "queued"}


def _write_source(path: Path, *, session_id: str = "transcript-ingest-session") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "provider_transcript_fixture.v1",
                "provider": "claude",
                "session_id": session_id,
                "started_at": "2026-05-10T12:00:00+09:00",
                "ended_at": "2026-05-10T12:01:00+09:00",
                "messages": [
                    {
                        "role": "user",
                        "timestamp": "2026-05-10T12:00:10+09:00",
                        "content": "Please keep marker SERVER_TRANSCRIPT_INGEST and redact TOKEN_VALUE=secret-123.",
                    },
                    {
                        "role": "assistant",
                        "timestamp": "2026-05-10T12:00:20+09:00",
                        "content": "Acknowledged SERVER_TRANSCRIPT_INGEST.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def _request(source_path: Path, *, request_id: str = "req_transcript_ingest") -> dict:
    return {
        "schema_version": "agent_knowledge_transcript_capture.v1",
        "request_id": request_id,
        "provider": "claude",
        "project": PROJECT,
        "source_locator": {
            "locator_hash": "sha256:" + "a" * 64,
            "runtime_handle": str(source_path),
        },
    }


def test_transcript_ingest_worker_enqueues_redacted_chunks_and_marks_ledger(tmp_path):
    source = tmp_path / "source.json"
    _write_source(source)
    spool = _FakeCaptureSpool(tmp_path / "spool")
    spool.enqueue(_request(source))
    ledger = Ledger(tmp_path / "ledger.sqlite")
    sink = _FakeIngressSink()

    result = TranscriptIngestWorker(
        capture_spool=spool,
        ledger=ledger,
        enqueue_sink=sink,
    ).run_once()

    assert result.status == "queued"
    assert result.request_id == "req_transcript_ingest"
    assert result.job_ids == ["job_transcript_001"]
    assert spool.depth_counts() == {"pending": 0, "processing": 0, "acked": 1, "quarantine": 0}
    assert len(sink.calls) == 1
    call = sink.calls[0]
    assert call["source"]["producer"] == "server-transcript-ingest"
    assert call["target_profile"] == DEFAULT_TRANSCRIPT_TARGET_PROFILE
    assert call["kind"] == "conversation_chunk"
    assert call["content_hash"].startswith("sha256:")
    assert call["idempotency_key"].startswith("claude:conversation_chunk:sha256:")
    serialized = json.dumps({"body": call["packed"].body, "metadata": call["packed"].metadata}, sort_keys=True)
    assert "SERVER_TRANSCRIPT_INGEST" in serialized
    assert "secret-123" not in serialized
    assert str(source) not in serialized
    row = ledger.get_by_knowledge_id(result.knowledge_ids[0])
    assert row["status"] == "queued"
    assert row["ingress_job_id"] == "job_transcript_001"


def test_transcript_ingest_worker_dedupes_existing_queued_chunk(tmp_path):
    source = tmp_path / "source.json"
    _write_source(source, session_id="duplicate-session")
    ledger = Ledger(tmp_path / "ledger.sqlite")
    first_spool = _FakeCaptureSpool(tmp_path / "spool-1")
    first_spool.enqueue(_request(source, request_id="req_first"))
    sink = _FakeIngressSink()
    first = TranscriptIngestWorker(capture_spool=first_spool, ledger=ledger, enqueue_sink=sink).run_once()

    second_spool = _FakeCaptureSpool(tmp_path / "spool-2")
    second_spool.enqueue(_request(source, request_id="req_second"))
    second = TranscriptIngestWorker(capture_spool=second_spool, ledger=ledger, enqueue_sink=sink).run_once()

    assert first.status == "queued"
    assert second.status == "queued"
    assert first.knowledge_ids == second.knowledge_ids
    assert first.job_ids == second.job_ids
    assert len(sink.calls) == 1


def test_transcript_ingest_worker_keeps_chunk_rejection_local(tmp_path):
    source = tmp_path / "source.json"
    _write_source(source, session_id="rejected-session")
    spool = _FakeCaptureSpool(tmp_path / "spool")
    spool.enqueue(_request(source))
    result = TranscriptIngestWorker(
        capture_spool=spool,
        ledger=Ledger(tmp_path / "ledger.sqlite"),
        enqueue_sink=_FakeIngressSink(reject_after=0),
    ).run_once()

    assert result.status == "index_timeout"
    assert result.rejected_chunk_count == 1
    assert result.rejected_chunk_ids
    assert spool.depth_counts() == {"pending": 0, "processing": 0, "acked": 1, "quarantine": 0}


def test_transcript_ingest_worker_quarantines_source_failure_without_raw_path(tmp_path):
    missing = tmp_path / "missing.json"
    spool = _FakeCaptureSpool(tmp_path / "spool")
    spool.enqueue(_request(missing))
    result = TranscriptIngestWorker(
        capture_spool=spool,
        ledger=Ledger(tmp_path / "ledger.sqlite"),
        enqueue_sink=_FakeIngressSink(),
    ).run_once()

    assert result.status == "quarantined"
    assert result.error_class == "source_unreadable"
    assert str(missing) not in result.message
    assert spool.depth_counts() == {"pending": 0, "processing": 0, "acked": 0, "quarantine": 1}
    quarantined = next((tmp_path / "spool" / "quarantine").glob("*.json"))
    payload = json.loads(quarantined.read_text(encoding="utf-8"))
    assert payload["last_failure"]["error_class"] == "source_unreadable"
    assert str(missing) not in json.dumps(payload["last_failure"], sort_keys=True)


def test_transcript_ingest_worker_is_server_only_boundary(tmp_path):
    source = tmp_path / "source.json"
    _write_source(source)
    spool = _FakeCaptureSpool(tmp_path / "spool")
    spool.enqueue(_request(source))
    ledger = Ledger(tmp_path / "ledger.sqlite")

    try:
        TranscriptIngestWorker(capture_spool=spool, ledger=ledger, enqueue_sink=None)
    except ValueError as exc:
        assert "enqueue sink" in str(exc)
    else:
        raise AssertionError("expected missing enqueue sink to fail")

    import agent_knowledge.session_memory.transcript_ingest as top_level

    assert top_level.TranscriptIngestWorker is TranscriptIngestWorker
    assert not hasattr(top_level, "IngressQueueClient")

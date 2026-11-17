"""Progress events, materialization counters, live relay, and outcomes side channel.

No network/DB: embedding transports are in-memory fakes, the CouchDB store uses an
injected transport, and the source store is InMemoryCouchDBSourceStore.
"""

from __future__ import annotations

import asyncio
import json
import os
import select
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_and_project,
    materialize_session_memory,
)
from agent_knowledge.couchdb_source.source_revision import (
    activate_source_revision,
    resolve_active_source_revision,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.local_embedding.client import (
    CanonicalLocalEmbeddingClient,
    LocalEmbeddingClientError,
)
from agent_knowledge.pipeline_progress import SCHEMA_VERSION, emit_progress, is_progress_line
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession
from agent_knowledge.transport_contract import ProxyResponse

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER / "deploy/session-memory"))
from stream_report import PROGRESS_ENUMS, PROGRESS_SCHEMAS  # noqa: E402

STREAM_REPORT = WORKER / "deploy/session-memory/stream_report.py"
PROVIDER = "codex"
PROJECT = "progress-integration"


# ---------------------------------------------------------------------------
# Event schema
# ---------------------------------------------------------------------------


def test_emit_progress_writes_fixed_schema_and_enums() -> None:
    import io

    stream = io.StringIO()
    emit_progress("embedding_batch", 3, 10, stream=stream)
    assert stream.getvalue() == (
        '{"done":3,"schema_version":"pipeline_progress.v1",'
        '"stage":"embedding_batch","total":10}\n'
    )
    assert is_progress_line(stream.getvalue())


@pytest.mark.parametrize(
    "stage,done,total",
    [
        ("not_a_stage", 1, 2),
        ("embedding_batch", -1, 2),
        ("embedding_batch", 1, 2.0),
        ("embedding_batch", True, 2),
        ("embedding_batch", 3, 2),
    ],
)
def test_emit_progress_rejects_non_allowlisted_values(stage, done, total) -> None:
    import io

    with pytest.raises(ValueError):
        emit_progress(stage, done, total, stream=io.StringIO())


def test_filter_allowlist_covers_progress_module() -> None:
    assert SCHEMA_VERSION in PROGRESS_SCHEMAS
    assert {"selection", "materialization", "projection", "embedding_batch"} <= PROGRESS_ENUMS["stage"]


# ---------------------------------------------------------------------------
# Client: one event per successfully validated batch
# ---------------------------------------------------------------------------


def _vec(index: int, dim: int = 256) -> list[float]:
    return [((index * 37 + j * 11) % 997 + 1) / 1000.0 for j in range(dim)]


class _Transport:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.fail_first = False

    def __call__(self, batch: list[str]):
        if self.fail_first:
            self.fail_first = False
            raise LocalEmbeddingClientError("transport_unavailable", "down", retryable=True)
        self.batches.append(list(batch))
        return [_vec(i) for i in range(len(batch))], len(batch)


class _AsyncTransport(_Transport):
    async def __call__(self, batch: list[str]):
        return super().__call__(batch)


def test_sync_client_emits_cumulative_chunks_per_batch() -> None:
    events: list[tuple[str, int, int]] = []
    transport = _Transport()
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:1/v1",
        max_retries=1,
        retry_backoff_seconds=0,
        progress_callback=lambda stage, done, total: events.append((stage, done, total)),
    )
    client._send_chunk_batch_sync = transport  # type: ignore[method-assign]
    client.embed_documents(["a" * 300_000])

    assert len(transport.batches) > 1
    total = sum(len(batch) for batch in transport.batches)
    assert [event[0] for event in events] == ["embedding_batch"] * len(events)
    assert [event[1] for event in events] == [
        sum(len(batch) for batch in transport.batches[: index + 1])
        for index in range(len(events))
    ]
    assert all(event[2] == total for event in events)
    assert events[-1][1] == total


def test_client_emits_only_after_retry_succeeds() -> None:
    events: list[tuple[str, int, int]] = []
    transport = _Transport()
    transport.fail_first = True
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:1/v1",
        max_retries=1,
        retry_backoff_seconds=0,
        sleep_fn=lambda _seconds: None,
        progress_callback=lambda stage, done, total: events.append((stage, done, total)),
    )
    client._call_http_sync = transport  # type: ignore[method-assign]
    client.embed_documents(["x" * 8192])
    assert len(events) == len(transport.batches) == 1
    assert events[0] == ("embedding_batch", 1, 1)


def test_client_emits_no_event_when_batch_fails() -> None:
    events: list[tuple[str, int, int]] = []

    def failing(batch):
        raise LocalEmbeddingClientError("transport_unavailable", "down", retryable=False)

    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:1/v1",
        max_retries=0,
        progress_callback=lambda stage, done, total: events.append((stage, done, total)),
    )
    client._send_chunk_batch_sync = failing  # type: ignore[method-assign]
    with pytest.raises(LocalEmbeddingClientError):
        client.embed_documents(["x" * 8192])
    assert events == []


def test_async_client_emits_cumulative_chunks_per_batch() -> None:
    events: list[tuple[str, int, int]] = []
    transport = _AsyncTransport()
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:1/v1",
        max_retries=0,
        progress_callback=lambda stage, done, total: events.append((stage, done, total)),
    )
    client._send_chunk_batch_async = transport  # type: ignore[method-assign]
    asyncio.run(client.aembed_documents(["a" * 300_000, "b" * 40_000]))
    assert len(events) == len(transport.batches)
    assert events[-1][1] == events[-1][2] == sum(len(batch) for batch in transport.batches)


def test_progress_callback_failure_never_breaks_embedding() -> None:
    def crash(stage, done, total):
        raise RuntimeError("progress sink down")

    transport = _Transport()
    client = CanonicalLocalEmbeddingClient(
        base_url="http://127.0.0.1:1/v1", max_retries=0, progress_callback=crash
    )
    client._send_chunk_batch_sync = transport  # type: ignore[method-assign]
    assert client.embed_documents(["x" * 8192])[0].chunk_count == 1


# ---------------------------------------------------------------------------
# Materialization: verifiable fetch/validation counters
# ---------------------------------------------------------------------------


def _sid() -> str:
    return dm.build_session_id_hash(PROVIDER, "progress-integration-session")


def _put_session(store: InMemoryCouchDBSourceStore) -> None:
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=_sid(),
                provider=PROVIDER,
                project=PROJECT,
                started_at="2026-08-04T00:00:00Z",
                ended_at="2026-08-04T00:02:00Z",
            )
        )
    )


def _put_chunk(store: InMemoryCouchDBSourceStore, *, index: int, text: str) -> None:
    store.put(
        dm.build_conversation_chunk_document(
            chunk=TranscriptChunk.from_text(
                chunk_id=f"chunk_{index}",
                session_id_hash=_sid(),
                provider=PROVIDER,
                project=PROJECT,
                turn_start_index=index,
                turn_end_index=index,
                text=text,
                observed_at_start="2026-08-04T00:01:00Z",
                observed_at_end="2026-08-04T00:01:00Z",
            )
        )
    )


def _seed(store: InMemoryCouchDBSourceStore, *, pin: bool) -> int:
    _put_session(store)
    _put_chunk(store, index=0, text="first chunk body " * 20)
    _put_chunk(store, index=1, text="second chunk body " * 20)
    if pin:
        resolved = activate_source_revision(store=store, session_id_hash=_sid())
        return 1 + len(resolved.conversation_chunks) + len(resolved.tool_evidence_bundles)
    return 3


def test_pinned_materialization_emits_one_counter_per_validated_member() -> None:
    store = InMemoryCouchDBSourceStore()
    members = _seed(store, pin=True)
    events: list[tuple[int, int]] = []
    resolved = resolve_active_source_revision(
        store=store, session_id_hash=_sid(), progress_callback=lambda done, total: events.append((done, total))
    )
    assert len(resolved.conversation_chunks) == 2
    assert [done for done, _ in events] == list(range(1, members + 1))
    assert all(total == members for _, total in events)


def test_legacy_materialization_emits_fetch_and_body_counters() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, pin=False)
    events: list[tuple[str, int, int]] = []
    materialize_session_memory(
        session_id_hash=_sid(),
        store=store,
        progress_callback=lambda stage, done, total: events.append((stage, done, total)),
    )
    assert {stage for stage, _, _ in events} == {"materialization", "materialization_body"}
    fetch = [(done, total) for stage, done, total in events if stage == "materialization"]
    body = [(done, total) for stage, done, total in events if stage == "materialization_body"]
    assert all(total == 0 for _, total in fetch)
    assert [done for done, _ in fetch] == sorted(done for done, _ in fetch)
    assert fetch[-1][0] >= 3
    assert body and body[-1][0] == body[-1][1]


def test_materialize_and_project_forwards_progress_callback() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, pin=True)
    events: list[tuple[str, int, int]] = []
    result = materialize_and_project(
        session_id_hash=_sid(),
        store=store,
        projector=None,
        progress_callback=lambda stage, done, total: events.append((stage, done, total)),
    )
    assert result["fully_materialized"] is True
    assert any(stage == "materialization" for stage, _, _ in events)
    assert any(stage == "materialization_body" for stage, _, _ in events)


def test_materialization_progress_callback_failure_never_breaks_resolution() -> None:
    store = InMemoryCouchDBSourceStore()
    _seed(store, pin=True)

    def crash(done, total):
        raise RuntimeError("sink down")

    resolved = resolve_active_source_revision(
        store=store, session_id_hash=_sid(), progress_callback=crash
    )
    assert len(resolved.conversation_chunks) == 2


def test_find_by_session_emits_one_event_per_fetched_page() -> None:
    pages = [
        {"docs": [{"_id": "doc-a"}], "bookmark": "page-1"},
        {"docs": [{"_id": "doc-b"}], "bookmark": "page-2"},
        {"docs": [], "bookmark": "page-2"},
    ]
    calls: list[dict] = []

    def transport(_method, _url, _headers, body):
        calls.append(json.loads(body.decode("utf-8")))
        return ProxyResponse(200, json.dumps(pages[len(calls) - 1]).encode(), {})

    store = CouchDBHttpSourceStore(
        base_url="http://couch.test:5984", db="transcript_source", transport=transport
    )
    events: list[tuple[int, int]] = []
    docs = store.find_by_session(
        session_id_hash=_sid(),
        doc_type=dm.SourceDocType.CONVERSATION_CHUNK,
        progress_callback=lambda done, total: events.append((done, total)),
    )
    assert [doc["_id"] for doc in docs] == ["doc-a", "doc-b"]
    assert events == [(1, 0), (2, 0)]
    assert calls[0]["limit"] == 10000 and "bookmark" not in calls[0]
    assert calls[1]["bookmark"] == "page-1"


# ---------------------------------------------------------------------------
# Filter: live relay, sanitization, report separation, outcomes side channel
# ---------------------------------------------------------------------------


def _run_filter(payload: bytes, *, outcomes: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{WORKER / 'lib'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    if outcomes is not None:
        env["SESSION_MEMORY_BUILD_OUTCOMES_FILE"] = str(outcomes)
    return subprocess.run(
        [sys.executable, str(STREAM_REPORT)],
        input=payload,
        capture_output=True,
        env=env,
        timeout=30,
    )


def test_filter_relays_progress_and_keeps_report(tmp_path) -> None:
    payload = b"\n".join(
        [
            b'{"done":0,"schema_version":"pipeline_progress.v1","stage":"selection","total":0}',
            b'{"body":"user transcript secret sk-live-abc"}',
            b'{"done":2,"schema_version":"pipeline_progress.v1","stage":"materialization","total":2}',
            b'{"failed":0,"projected":1,"selected":1,"skipped":0}',
        ]
    ) + b"\n"
    result = _run_filter(payload)
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.decode().splitlines() if line]
    assert json.loads(lines[0])["stage"] == "selection"
    assert json.loads(lines[1])["stage"] == "materialization"
    assert json.loads(lines[-1]) == {
        "schema_version": "build_guard_summary.v1",
        "status": "ok",
        "selected": 1,
        "projected": 1,
        "failed": 0,
        "skipped": 0,
    }
    assert len(lines) == 3
    assert "sk-live-abc" not in result.stdout.decode()
    assert "user transcript" not in result.stdout.decode()


def test_filter_sanitizes_progress_injection(tmp_path) -> None:
    payload = (
        b'{"body":"secret transcript","done":1,"extra":"sk-live-xyz",'
        b'"schema_version":"pipeline_progress.v1","stage":"materialization",'
        b'"session_id_hash":"sha256:' + b"a" * 64 + b'","total":2}\n'
        b'{"failed":0,"projected":1,"selected":1,"skipped":0}\n'
    )
    result = _run_filter(payload)
    assert result.returncode == 0
    output = result.stdout.decode()
    assert "sk-live-xyz" not in output
    assert "secret transcript" not in output
    assert "session_id_hash" not in output
    first = json.loads(output.splitlines()[0])
    assert set(first) == {"schema_version", "stage", "done", "total"}


def test_filter_progress_does_not_become_the_report(tmp_path) -> None:
    payload = (
        b'{"done":1,"failed":0,"projected":1,"schema_version":"pipeline_progress.v1",'
        b'"selected":1,"skipped":0,"stage":"projection","total":1}\n'
        b'{"failed":2,"projected":0,"selected":2,"skipped":0}\n'
    )
    result = _run_filter(payload)
    assert result.returncode == 1
    lines = [line for line in result.stdout.decode().splitlines() if line]
    summary = json.loads(lines[-1])
    assert summary["status"] == "failed"
    assert summary["failed"] == 2


def test_filter_still_fails_closed_without_report() -> None:
    payload = b'{"done":1,"schema_version":"pipeline_progress.v1","stage":"projection","total":1}\n'
    result = _run_filter(payload)
    assert result.returncode == 3
    assert json.loads(result.stdout.decode().splitlines()[-1])["error"] == "missing_report"


def test_filter_writes_outcomes_side_channel_with_allowlisted_rows(tmp_path) -> None:
    outcomes = tmp_path / "outcomes.json"
    valid = "sha256:" + "b" * 64
    payload = (
        b'{"done":1,"schema_version":"pipeline_progress.v1","stage":"projection","total":1}\n'
        + json.dumps(
            {
                "selected": 3,
                "projected": 1,
                "failed": 1,
                "skipped": 1,
                "sessions": [
                    {"session_id_hash": valid, "status": "projected", "error_class": ""},
                    {"session_id_hash": "not-a-hash", "status": "projected"},
                    {"session_id_hash": "sha256:" + "c" * 64, "status": "bogus"},
                    {
                        "session_id_hash": "sha256:" + "d" * 64,
                        "status": "failed",
                        "error_class": "RuntimeError",
                        "body": "secret transcript",
                    },
                ],
            }
        ).encode()
        + b"\n"
    )
    result = _run_filter(payload, outcomes=outcomes)
    assert result.returncode == 1
    payload = json.loads(outcomes.read_text())
    assert payload["schema_version"] == "build_outcomes.v1"
    assert payload["sessions"] == [
        {"session_id_hash": valid, "status": "projected", "error_class": ""},
        {"session_id_hash": "sha256:" + "d" * 64, "status": "failed", "error_class": "RuntimeError"},
    ]
    assert "secret transcript" not in outcomes.read_text()
    assert stat.S_IMODE(outcomes.stat().st_mode) == 0o600


def test_filter_relays_progress_before_the_build_finishes() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{WORKER / 'lib'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    proc = subprocess.Popen(
        [sys.executable, str(STREAM_REPORT)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(
            b'{"done":1,"schema_version":"pipeline_progress.v1","stage":"materialization","total":2}\n'
        )
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], 0.9)
        first_line = proc.stdout.readline() if ready else b""
        proc.stdin.write(b'{"failed":0,"projected":0,"selected":0,"skipped":0}\n')
        proc.stdin.close()
        rc = proc.wait(timeout=10)
        rest = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
    assert rc == 0
    assert b"pipeline_progress.v1" in first_line
    assert json.loads(rest.decode().splitlines()[-1])["status"] == "ok"


# ---------------------------------------------------------------------------
# build_cli stage markers
# ---------------------------------------------------------------------------


def _approval(path: Path, argv: list[str]) -> Path:
    payload = {
        "schema_version": "agent_knowledge_live_approval.v1",
        "operation": "couchdb_session_memory_build",
        "operator_approval": {"approved": True, "by": "test"},
        "redaction_required": True,
        "rollback_or_abort_criteria": ["abort on nonzero exit"],
        "timeout_seconds": 300,
        "command": {"argv": argv},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_build_cli_emits_selection_and_per_session_stages(tmp_path, monkeypatch) -> None:
    events: list[tuple[str, int, int]] = []
    sessions = [{"session_id_hash": "sha256:" + "a" * 64}, {"session_id_hash": "sha256:" + "b" * 64}]

    monkeypatch.setattr(build_cli, "emit_progress", lambda stage, done, total, **_kw: events.append((stage, done, total)))
    monkeypatch.setattr(build_cli, "_select_sessions_for_cycle", lambda *a, **kw: sessions)

    class _Projector:
        embedding_profile = ""

        def close(self):
            return None

    monkeypatch.setattr(build_cli, "_build_pg_projector", lambda environ: _Projector())
    from agent_knowledge.couchdb_source import session_memory_materializer

    def fake_materialize(**kwargs):
        kwargs["progress_callback"]("materialization", 1, 3)
        kwargs["progress_callback"]("materialization", 3, 3)
        kwargs["progress_callback"]("materialization_body", 2, 2)
        return {"fully_materialized": True, "projection": {"status": "projected"}}

    monkeypatch.setattr(session_memory_materializer, "materialize_and_project", fake_materialize)
    monkeypatch.setenv("COUCHDB_URL", "http://localhost:5984")
    monkeypatch.setenv("SESSION_MEMORY_PROJECTION_BACKEND", "postgres_pgvector")
    monkeypatch.setenv("PGVECTOR_DSN", "postgresql://u:p@127.0.0.1:5432/db")

    approval = _approval(tmp_path / "approval.json", ["--approval", str(tmp_path / "approval.json")])
    rc = build_cli.main(["--approval", str(approval)])

    assert rc == 0
    assert events == [
        ("selection", 0, 0),
        ("selection", 2, 2),
        ("materialization", 1, 3),
        ("materialization", 3, 3),
        ("materialization_body", 2, 2),
        ("projection", 1, 2),
        ("materialization", 1, 3),
        ("materialization", 3, 3),
        ("materialization_body", 2, 2),
        ("projection", 2, 2),
    ]


def test_pg_projector_wires_progress_into_the_embedding_client(monkeypatch) -> None:
    captured: dict = {}

    class _Provider:
        def close(self):
            return None

    class _Chunks:
        def __init__(self, *args, **kwargs):
            pass

    class _Projector:
        embedding_profile = ""

    def fake_provider(*, environ, progress_callback=None):
        captured["callback"] = progress_callback
        return _Provider()

    from agent_knowledge.rag_ingress import embedding_provider, pg_backfill, pg_embedding_chunks
    from agent_knowledge.postgres_store import pgvector_store

    monkeypatch.setattr(embedding_provider, "build_openai_embedding_provider", fake_provider)
    monkeypatch.setattr(pg_embedding_chunks, "BoundedPgEmbedding", _Chunks)
    monkeypatch.setattr(pg_backfill, "PgSessionMemoryProjector", lambda **kw: _Projector())
    monkeypatch.setattr(pgvector_store, "PgVectorStore", lambda **kw: object())

    env = {
        "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://u:p@127.0.0.1:5432/db",
        "LLM_BRAIN_EMBEDDING_PROVIDER": "model2vec",
        "LLM_BRAIN_EMBEDDING_MODEL": "local-model2vec-256-v1",
        "LLM_BRAIN_EMBEDDING_BASE_URL": "http://127.0.0.1:1/v1",
        "LLM_BRAIN_EMBEDDING_DIM": "256",
        "LLM_BRAIN_EMBEDDING_PROFILE_ID": "lbrain-memory-model2vec-256-v1",
        "NEURON_EMBEDDING_PROFILE": "lbrain-memory-model2vec-256-v1",
    }
    build_cli._build_pg_projector(env)
    assert captured["callback"] is build_cli.emit_progress


def test_pg_projector_and_outbox_cli_accept_legacy_single_kwarg_provider_factory(
    monkeypatch,
) -> None:
    """Monkeypatched factories taking only `(*, environ)` must keep working without TypeError."""
    from agent_knowledge.postgres_store import embedding_outbox_cli, pgvector_store
    from agent_knowledge.rag_ingress import embedding_provider, pg_backfill, pg_embedding_chunks

    called = {"count": 0}

    class _Provider:
        model = "gemini-embedding-2"
        size = 3072

        def close(self):
            return None

    def legacy_factory(*, environ):
        called["count"] += 1
        assert environ["NEURON_LBRAIN_PGVECTOR_DSN"].startswith("postgresql://")
        return _Provider()

    monkeypatch.setattr(embedding_provider, "build_openai_embedding_provider", legacy_factory)
    monkeypatch.setattr(pg_embedding_chunks, "BoundedPgEmbedding", lambda *a, **kw: object())
    monkeypatch.setattr(pg_backfill, "PgSessionMemoryProjector", lambda **kw: "ok-projector")
    monkeypatch.setattr(pgvector_store, "PgVectorStore", lambda **kw: object())

    env = {
        "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://u:p@127.0.0.1:5432/db",
        "LLM_BRAIN_EMBEDDING_BASE_URL": "http://127.0.0.1:1/v1",
        "LLM_BRAIN_EMBEDDING_MODEL": "gemini-embedding-2",
        "LLM_BRAIN_EMBEDDING_DIM": "3072",
    }
    assert build_cli._build_pg_projector(env) == "ok-projector"
    assert called["count"] == 1

    monkeypatch.setattr(embedding_outbox_cli, "build_openai_embedding_provider", legacy_factory)
    monkeypatch.setattr(embedding_outbox_cli, "PgVectorStore", lambda **kw: object())

    class _IdleWorker:
        last_batch_attempted = 0
        last_batch_completed = 0
        last_batch_failed = 0
        last_batch_stale = 0

        def __init__(self, **kwargs):
            self.batch_size = kwargs.get("batch_size", 1)

        def run_once(self):
            return 0

    monkeypatch.setattr(embedding_outbox_cli, "OutboxWorker", _IdleWorker)
    rc = embedding_outbox_cli.main(
        ["--execute", "--allow-model-calls", "--once", "--lease-seconds", "180"],
        environ=env,
    )
    assert rc == 0
    assert called["count"] == 2

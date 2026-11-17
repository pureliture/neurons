"""Offline contracts for the configured PG embedding consumer."""
from types import SimpleNamespace
import os
import pytest

from agent_knowledge.postgres_store.outbox_worker import OutboxWorker


def test_failed_embedding_batch_is_not_reported_as_success(caplog):
    failures = []
    job = SimpleNamespace(outbox_id=7, target_type="memory_card", target_id="card", content_hash="sha256:fixture", payload_text="approved memory")
    store = SimpleNamespace(
        claim_outbox_leases=lambda **kwargs: [job],
        mark_outbox_failed=lambda *args, **kwargs: failures.append(kwargs),
    )

    def unavailable(_text):
        raise RuntimeError("fixture provider unavailable")

    worker = OutboxWorker(store=store, embedding_fn=unavailable, worker_id="fixture")
    assert worker.run_once() == 1
    assert len(failures) == 1
    assert getattr(worker, "last_batch_failed", None) == 1
    assert getattr(worker, "last_batch_completed", None) == 0


def test_provider_error_does_not_leak_source_details(caplog):
    failures = []
    job = SimpleNamespace(outbox_id=7, target_type="memory_card", target_id="card", content_hash="sha256:fixture", payload_text="approved memory")
    store = SimpleNamespace(claim_outbox_leases=lambda **kwargs: [job], mark_outbox_failed=lambda *args, **kwargs: failures.append(kwargs))

    def unavailable(_text):
        raise RuntimeError("fixture-private-detail")

    OutboxWorker(store=store, embedding_fn=unavailable, worker_id="fixture").run_once()
    assert "fixture-private-detail" not in caplog.text
    assert "fixture-private-detail" not in failures[0]["error_message"]


def test_lost_lease_failure_log_does_not_echo_exception(caplog):
    job = SimpleNamespace(outbox_id=7, target_type="memory_card", target_id="card", content_hash="sha256:fixture", payload_text="approved memory")

    def lost_lease(*args, **kwargs):
        raise ValueError("fixture-private-detail")

    def unavailable(_text):
        raise RuntimeError("provider unavailable")

    store = SimpleNamespace(claim_outbox_leases=lambda **kwargs: [job], mark_outbox_failed=lost_lease)
    worker = OutboxWorker(store=store, embedding_fn=unavailable, worker_id="fixture")
    assert worker.run_once() == 1
    assert "fixture-private-detail" not in caplog.text


def test_consumer_entrypoint_is_disabled_before_building_clients(monkeypatch, capsys):
    import importlib
    import importlib.util
    import json
    module_name = "agent_knowledge.postgres_store.embedding_outbox_cli"
    assert importlib.util.find_spec(module_name) is not None
    cli = importlib.import_module(module_name)

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled consumer must not construct DB/provider clients")

    monkeypatch.setattr(cli, "PgVectorStore", forbidden)
    monkeypatch.setattr(cli, "build_openai_embedding_provider", forbidden)
    assert cli.main([]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "activation_required"
    assert report["network_used"] is False
    assert report["mutation_performed"] is False


def test_consumer_processes_real_worker_once_with_configured_profile(monkeypatch, capsys):
    import importlib
    import importlib.util
    import json
    module_name = "agent_knowledge.postgres_store.embedding_outbox_cli"
    assert importlib.util.find_spec(module_name) is not None
    cli = importlib.import_module(module_name)
    claims, embeds, writes = [], [], []
    job = SimpleNamespace(outbox_id=7, target_type="memory_card", target_id="card", content_hash="sha256:fixture", payload_text="approved memory")
    store = SimpleNamespace(
        claim_outbox_leases=lambda **kwargs: claims.append(kwargs) or [job],
        cas_update_embedding=lambda **kwargs: writes.append(kwargs) or True,
    )
    provider = SimpleNamespace(model="gemini-embedding-2", size=3072, embed=lambda text: embeds.append(text) or [1.0] + [0.0] * 3071, close=lambda: None)
    monkeypatch.setattr(cli, "PgVectorStore", lambda **kwargs: store)
    monkeypatch.setattr(cli, "build_openai_embedding_provider", lambda **kwargs: provider)
    env = {"NEURON_LBRAIN_PGVECTOR_DSN": "fixture", "LLM_BRAIN_EMBEDDING_MODEL": "gemini-embedding-2", "LLM_BRAIN_EMBEDDING_DIM": "3072", "LLM_BRAIN_EMBEDDING_BASE_URL": "http://invalid.example.invalid/v1"}
    assert cli.main(["--execute", "--allow-model-calls", "--once", "--max-jobs", "1"], environ=env) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["completed"] == 1
    assert report["failed"] == 0
    assert report["model_calls"] == 1
    assert embeds == ["approved memory"]
    assert len(writes) == 1
    assert claims[0]["batch_size"] == 1


def test_product_router_admits_embedding_consumer():
    from agent_knowledge.cli import COMMAND_HANDLERS
    assert "pg-embedding-outbox" in COMMAND_HANDLERS


def test_consumer_rejects_wrong_embedding_profile_without_clients(monkeypatch, capsys):
    import json
    from agent_knowledge.postgres_store import embedding_outbox_cli as cli
    calls = []
    monkeypatch.setattr(cli, "PgVectorStore", lambda **kwargs: calls.append("db"))
    monkeypatch.setattr(cli, "build_openai_embedding_provider", lambda **kwargs: calls.append("model"))
    env = {"NEURON_LBRAIN_PGVECTOR_DSN": "fixture", "LLM_BRAIN_EMBEDDING_MODEL": "other-model", "LLM_BRAIN_EMBEDDING_DIM": "3072", "LLM_BRAIN_EMBEDDING_BASE_URL": "http://invalid.example.invalid/v1"}
    assert cli.main(["--execute", "--allow-model-calls", "--once"], environ=env) == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_rejected"


def test_consumer_never_sends_credential_shaped_text_to_provider(monkeypatch, capsys):
    import json
    from agent_knowledge.postgres_store import embedding_outbox_cli as cli
    failures, embeds = [], []
    job = SimpleNamespace(outbox_id=7, target_type="memory_card", target_id="card", content_hash="sha256:fixture", payload_text="password=fixture-unsafe-value")
    store = SimpleNamespace(claim_outbox_leases=lambda **kwargs: [job], mark_outbox_failed=lambda *args, **kwargs: failures.append(kwargs))
    provider = SimpleNamespace(model="gemini-embedding-2", size=3072, embed=lambda text: embeds.append(text) or [1.0] * 3072, close=lambda: None)
    monkeypatch.setattr(cli, "PgVectorStore", lambda **kwargs: store)
    monkeypatch.setattr(cli, "build_openai_embedding_provider", lambda **kwargs: provider)
    env = {"NEURON_LBRAIN_PGVECTOR_DSN": "fixture", "LLM_BRAIN_EMBEDDING_MODEL": "gemini-embedding-2", "LLM_BRAIN_EMBEDDING_DIM": "3072", "LLM_BRAIN_EMBEDDING_BASE_URL": "http://invalid.example.invalid/v1"}
    assert cli.main(["--execute", "--allow-model-calls", "--once"], environ=env) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 1
    assert report["model_calls"] == 0
    assert report["completed"] == 0
    assert embeds == []
    assert len(failures) == 1


def test_consumer_attempt_ceiling_survives_multiple_batches(monkeypatch, capsys):
    import json
    from agent_knowledge.postgres_store import embedding_outbox_cli as cli
    claims, embeds = [], []
    job = SimpleNamespace(outbox_id=7, target_type="memory_card", target_id="card", content_hash="sha256:fixture", payload_text="approved memory")
    store = SimpleNamespace(claim_outbox_leases=lambda **kwargs: claims.append(kwargs) or [job] * kwargs["batch_size"], cas_update_embedding=lambda **kwargs: True)
    provider = SimpleNamespace(model="gemini-embedding-2", size=3072, embed=lambda text: embeds.append(text) or [1.0] * 3072, close=lambda: None)
    monkeypatch.setattr(cli, "PgVectorStore", lambda **kwargs: store)
    monkeypatch.setattr(cli, "build_openai_embedding_provider", lambda **kwargs: provider)
    env = {"NEURON_LBRAIN_PGVECTOR_DSN": "fixture", "LLM_BRAIN_EMBEDDING_MODEL": "gemini-embedding-2", "LLM_BRAIN_EMBEDDING_DIM": "3072", "LLM_BRAIN_EMBEDDING_BASE_URL": "http://invalid.example.invalid/v1"}
    assert cli.main(["--execute", "--allow-model-calls", "--batch-size", "2", "--max-jobs", "3", "--lease-seconds", "300"], environ=env) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["attempted"] == 3
    assert len(embeds) == 3
    assert [claim["batch_size"] for claim in claims] == [2, 1]


@pytest.mark.skipif(not os.environ.get("LBRAIN_TEST_PG_DSN"), reason="Requires disposable local PostgreSQL")
def test_consumer_cli_persists_then_does_not_reembed_with_real_pg(isolated_pg_store, monkeypatch, capsys):
    """Real DB/worker, injected test-only embedder; not a provider quality test."""
    import hashlib
    import json
    from agent_knowledge.postgres_store import embedding_outbox_cli as cli
    from agent_knowledge.postgres_store.pgvector_store import MemoryCard
    store = isolated_pg_store
    card = MemoryCard(memory_id="runtime_card_fixture", project="fixture", card_type="decision", title="Approved runtime fixture", summary="Use the private service", lifecycle_state="human_accepted", authorization_status="active", content_hash="sha256:" + hashlib.sha256(b"fixture").hexdigest())
    store.upsert_card(card)
    embeds = []
    provider = SimpleNamespace(model="gemini-embedding-2", size=3072, embed=lambda text: embeds.append(text) or [1.0] + [0.0] * 3071, close=lambda: None)
    monkeypatch.setattr(cli, "PgVectorStore", lambda **kwargs: store)
    monkeypatch.setattr(cli, "build_openai_embedding_provider", lambda **kwargs: provider)
    env = {"NEURON_LBRAIN_PGVECTOR_DSN": "isolated-test-only", "LLM_BRAIN_EMBEDDING_MODEL": "gemini-embedding-2", "LLM_BRAIN_EMBEDDING_DIM": "3072", "LLM_BRAIN_EMBEDDING_BASE_URL": "http://invalid.example.invalid/v1"}
    argv = ["--execute", "--allow-model-calls", "--once", "--max-jobs", "1"]
    assert cli.main(argv, environ=env) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["completed"] == 1
    stored = store.get_card(card.memory_id)
    assert stored.embedding_state == "ready"
    assert len(stored.embedding) == 3072
    jobs = store.list_outbox_jobs()
    assert len(jobs) == 1 and jobs[0].status == "completed"
    assert cli.main(argv, environ=env) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["attempted"] == 0
    assert len(embeds) == 1


def test_local_outbox_worker_logs_non_sensitive_stage_without_leaking_text(caplog):
    import logging
    from agent_knowledge.local_embedding import LocalEmbeddingClientError
    from agent_knowledge.postgres_store.outbox_worker import LocalEmbeddingOutboxWorker

    failures = []
    job = SimpleNamespace(
        outbox_id=9,
        target_type="session_chunk",
        target_id="seg_fixture",
        source_content_hash="sha256:" + "a" * 64,
        input_hash="sha256:" + "b" * 64,
        profile_id="lbrain-memory-model2vec-256-v1",
        payload_text="sensitive transcript fixture text",
        attempt_count=1,
        fencing_token=1,
    )
    store = SimpleNamespace(
        claim_local_outbox_leases=lambda **kwargs: [job],
        mark_local_outbox_failed=lambda **kwargs: failures.append(kwargs),
    )

    def _boom(_text: str):
        raise LocalEmbeddingClientError(
            "internal_error",
            "RAW_SECRET_PATH /Users/alice/leak",
            retryable=False,
            stage="batch_http",
        )

    worker = LocalEmbeddingOutboxWorker(store=store, embedding_fn=_boom, worker_id="test-w")
    with caplog.at_level(logging.ERROR):
        assert worker.run_once() == 1

    assert len(failures) == 1
    assert failures[0]["error_code"] == "terminal_error:internal_error"
    assert failures[0]["retryable"] is False
    assert "RAW_SECRET_PATH" not in caplog.text
    assert "/Users/alice" not in caplog.text
    assert "stage=batch_http" in caplog.text


def test_requeue_local_dead_letters_retires_already_ready_rows_offline():
    from agent_knowledge.postgres_store.pgvector_store import PgVectorStore

    executed: list[tuple[str, tuple]] = []

    class _FakeCursor:
        def __init__(self, rowcount: int, rows=None) -> None:
            self.rowcount = rowcount
            self._rows = rows if rows is not None else []

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _FakeDb:
        def cursor(self):
            return _FakeCursor(rowcount=0, rows=[])

        def execute(self, sql: str, params=()):
            executed.append((sql, tuple(params)))
            if "SET status = 'cas_skipped'" in sql:
                return _FakeCursor(rowcount=1)
            return _FakeCursor(rowcount=0, rows=[])

    class _DispatchCursor(_FakeCursor):
        def __init__(self, db):
            super().__init__(rowcount=0, rows=[])
            self._db = db

        def execute(self, sql: str, params=()):
            res = self._db.execute(sql, params)
            self.rowcount = res.rowcount
            self._rows = res._rows
            return self

    class _FakeConn:
        def cursor(self):
            return _DispatchCursor(self)

        def execute(self, sql: str, params=()):
            executed.append((sql, tuple(params)))
            if "SET status = 'cas_skipped'" in sql:
                return _FakeCursor(rowcount=1)
            return _FakeCursor(rowcount=0, rows=[])

    class _ScopeCtx:
        def __init__(self) -> None:
            self._db = _FakeConn()

        def __enter__(self):
            return self._db

        def __exit__(self, exc_type, exc, tb):
            return False

    store = object.__new__(PgVectorStore)
    store._scope = lambda **_kw: _ScopeCtx()  # type: ignore[method-assign]

    res = PgVectorStore.requeue_local_dead_letters(store, target_ids=["seg_ready"])
    assert res == 1
    assert any("SET status = 'cas_skipped'" in sql for sql, _ in executed)
    assert any("state = 'ready'" in sql for sql, _ in executed)


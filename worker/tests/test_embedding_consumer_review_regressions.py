"""Offline regressions from independent consumer review."""
from types import SimpleNamespace

from agent_knowledge.postgres_store import embedding_outbox_cli as cli

ENV = {
    "NEURON_LBRAIN_PGVECTOR_DSN": "fixture",
    "LLM_BRAIN_EMBEDDING_MODEL": "gemini-embedding-2",
    "LLM_BRAIN_EMBEDDING_DIM": "3072",
    "LLM_BRAIN_EMBEDDING_BASE_URL": "http://invalid.example.invalid/v1",
    "LLM_BRAIN_EMBEDDING_TIMEOUT_SECONDS": "30",
}


def test_batch_lease_covers_all_serial_provider_calls_before_clients(monkeypatch, capsys):
    import json
    clients = []
    provider = SimpleNamespace(model="gemini-embedding-2", size=3072, embed=lambda text: [1.0] + [0.0] * 3071, close=lambda: None)
    store = SimpleNamespace(claim_outbox_leases=lambda **kwargs: [])
    monkeypatch.setattr(cli, "build_openai_embedding_provider", lambda **kwargs: clients.append("provider") or provider)
    monkeypatch.setattr(cli, "PgVectorStore", lambda **kwargs: clients.append("db") or store)
    code = cli.main(["--execute", "--allow-model-calls", "--once", "--batch-size", "2", "--max-jobs", "2", "--lease-seconds", "105"], environ=ENV)
    assert code == 2
    assert clients == []
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "configuration_rejected"
    assert report["model_calls"] == 0


def test_mid_batch_database_error_preserves_confirmed_progress(monkeypatch, capsys):
    import json
    import psycopg
    jobs = [SimpleNamespace(outbox_id=index, target_type="memory_card", target_id=f"card_{index}", content_hash="sha256:fixture", payload_text=text) for index, text in [(1, "first"), (2, "second")]]
    embeds, writes = [], []

    def embed(text):
        embeds.append(text)
        if text == "second":
            raise RuntimeError("fixture provider failure")
        return [1.0] + [0.0] * 3071

    def failed(*args, **kwargs):
        raise psycopg.OperationalError("fixture failure-record database outage")

    provider = SimpleNamespace(model="gemini-embedding-2", size=3072, embed=embed, close=lambda: None)
    store = SimpleNamespace(claim_outbox_leases=lambda **kwargs: jobs, cas_update_embedding=lambda **kwargs: writes.append(kwargs) or True, mark_outbox_failed=failed)
    monkeypatch.setattr(cli, "build_openai_embedding_provider", lambda **kwargs: provider)
    monkeypatch.setattr(cli, "PgVectorStore", lambda **kwargs: store)
    assert cli.main(["--execute", "--allow-model-calls", "--once", "--batch-size", "2", "--lease-seconds", "300"], environ=ENV) == 1
    report = json.loads(capsys.readouterr().out)
    assert len(writes) == 1
    assert len(embeds) == 2
    assert report["attempted"] == 2
    assert report["completed"] == 1
    assert report["failed"] == 1
    assert report["model_calls"] == 2
    assert report["status"] == "consumer_failed"


def test_injected_timeout_is_the_actual_sdk_timeout(monkeypatch):
    import sys
    from agent_knowledge.rag_ingress.qdrant_embedding import build_openai_embedding_provider
    settings = []

    def sdk(**kwargs):
        settings.append(kwargs)
        return SimpleNamespace(embeddings=SimpleNamespace(create=lambda **params: SimpleNamespace(data=[SimpleNamespace(embedding=[1.0] + [0.0] * 3071)])), close=lambda: None)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=sdk))
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_TIMEOUT_SECONDS", "900")
    provider = build_openai_embedding_provider(environ={**ENV, "LLM_BRAIN_EMBEDDING_TIMEOUT_SECONDS": "1", "LLM_BRAIN_EMBEDDING_API_KEY": "fixture"})
    assert settings[0]["timeout"] == 1.0
    assert len(provider.embed("approved fixture")) == 3072
    provider.close()

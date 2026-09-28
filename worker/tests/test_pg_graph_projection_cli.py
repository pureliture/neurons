"""Outbox CLI must not report failed projection as a successful cron run."""
from __future__ import annotations

from unittest.mock import patch

from agent_knowledge.postgres_store.graph_projection_cli import main


def test_once_reports_failed_claims_and_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("NEURON_LBRAIN_PGVECTOR_DSN", "postgresql://test-only/unused")

    class Worker:
        last_batch_failed = 1

        def __init__(self, **_kwargs):
            pass

        def run_once(self):
            return 2

    with patch("agent_knowledge.postgres_store.graph_projection_cli.PgVectorStore"), patch(
        "agent_knowledge.llm_brain_core.runtime_graph.build_graph_adapter_from_env"
    ) as build_graph, patch(
        "agent_knowledge.postgres_store.graph_projection_cli.GraphProjectionWorker", Worker
    ):
        assert main(["--once"]) == 1
    build_graph.assert_called_once_with(enable_flag=True, required_flag=True)
    output = capsys.readouterr().out
    assert "claimed 2" in output and "failed 1" in output


def test_once_clean_batch_succeeds(monkeypatch, capsys):
    monkeypatch.setenv("NEURON_LBRAIN_PGVECTOR_DSN", "postgresql://test-only/unused")

    class Worker:
        last_batch_failed = 0

        def __init__(self, **_kwargs):
            pass

        def run_once(self):
            return 1

    with patch("agent_knowledge.postgres_store.graph_projection_cli.PgVectorStore"), patch(
        "agent_knowledge.llm_brain_core.runtime_graph.build_graph_adapter_from_env"
    ), patch("agent_knowledge.postgres_store.graph_projection_cli.GraphProjectionWorker", Worker):
        assert main(["--once"]) == 0
    assert "failed 0" in capsys.readouterr().out

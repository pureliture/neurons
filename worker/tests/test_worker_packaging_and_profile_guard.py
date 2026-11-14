"""Regression tests for worker httpx/openai<3 runtime pin and Builder profile resolution."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tomllib
from types import SimpleNamespace

import pytest

WORKER_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_and_dockerfiles_pin_httpx_and_openai_v2() -> None:
    pyproject = tomllib.loads((WORKER_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    assert any(d.startswith("httpx>=") and "<1" in d for d in deps), deps
    assert any(d.startswith("openai>=") and "<3" in d for d in deps), deps

    for df_name in ("Dockerfile", "Dockerfile.session-memory", "Dockerfile.mcp-http", "Dockerfile.tools"):
        content = (WORKER_ROOT / df_name).read_text(encoding="utf-8")
        assert '"openai>=1.0"' not in content, f"{df_name} still contains unpinned openai>=1.0"
        assert "openai>=1.91.0,<3" in content, f"{df_name} missing bounded openai<3"
        assert "httpx>=0.27,<1" in content, f"{df_name} missing explicit httpx<1"


def test_local_embedding_canonical_import_does_not_eagerly_load_graphiti(monkeypatch: pytest.MonkeyPatch) -> None:
    for mod_name in list(sys.modules):
        if mod_name == "graphiti_core" or mod_name.startswith(("graphiti_core.", "agent_knowledge.local_embedding")):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    from agent_knowledge.local_embedding.canonical import canonical_input_hash_v1

    assert canonical_input_hash_v1("hello")
    assert "graphiti_core" not in sys.modules


def test_builder_profile_resolution_matches_pg_projector(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {
        "SESSION_MEMORY_PROJECTION_BACKEND": "postgres_pgvector",
        "NEURON_EMBEDDING_PROFILE": "lbrain-memory-model2vec-256-v1",
        "LLM_BRAIN_EMBEDDING_PROFILE": "lbrain-memory-model2vec-256-v1",
        "LLM_BRAIN_EMBEDDING_MODEL": "local-model2vec-256-v1",
        "LLM_BRAIN_EMBEDDING_PROVIDER": "model2vec",
        "LLM_BRAIN_EMBEDDING_BASE_URL": "https://automatelife-macmini.tailbf74be.ts.net/model2vec/v1",
        "LLM_BRAIN_EMBEDDING_DIM": "256",
        "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://u:p@127.0.0.1:5432/db",
    }
    from agent_knowledge.couchdb_source.build_cli import _build_pg_projector
    from agent_knowledge.rag_ingress.embedding_provider import resolve_embedding_config
    from agent_knowledge.rag_ingress.pg_backfill import validated_pg_embedding_profile

    cfg = resolve_embedding_config(env)
    resolved = validated_pg_embedding_profile(SimpleNamespace(model=cfg["model"], size=cfg["dim"]))
    projector = _build_pg_projector(env)
    try:
        assert resolved == "lbrain-memory-model2vec-256-v1"
        assert projector.embedding_profile == resolved
    finally:
        projector.store.close() if hasattr(projector, "store") and hasattr(projector.store, "close") else None

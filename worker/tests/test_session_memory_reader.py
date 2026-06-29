from __future__ import annotations

from agent_knowledge.index_client import session_memory_records_from_retired_index_bridge


class _FakeRetiredIndexBridge:
    def list_documents(self, dataset_id, *, page=1, page_size=100, keywords=""):
        if page > 1:
            return []
        return [
            {"id": "d1", "name": "ak-session-memory-codex-workspace-index-advisor-abc123", "content_hash": "sha256:h1"},
            {"id": "d2", "name": "ak-session-memory-antigravity-workspace-stocks-xyz"},
            {"id": "d3", "name": "ak-session-memory-codex-workspace-index-advisor-def456"},
        ]

    def list_document_chunks(self, dataset_id, document_id):
        return {
            "d1": ["# Session Memory\nDecision: local ledger is canonical, RetiredIndexBridge is mirror."],
            "d2": ["other project content"],
            "d3": ["# Session Memory\nDecision: supersede detector uses embedding + LLM judge."],
        }[document_id]


def test_session_memory_records_filters_by_project_and_carries_meta():
    records = session_memory_records_from_retired_index_bridge(
        _FakeRetiredIndexBridge(), ["sid"], project="workspace-index-advisor", provider="codex", limit=10
    )

    # d2 (different project) is filtered out; d1 + d3 (codex/workspace-index-advisor) kept
    ids = sorted(r["metadata"]["knowledge_id"] for r in records)
    assert ids == ["d1", "d3"]
    r = next(r for r in records if r["metadata"]["knowledge_id"] == "d1")
    assert r["metadata"]["project"] == "workspace-index-advisor"
    assert r["metadata"]["provider"] == "codex"
    assert "local ledger is canonical" in r["content"]
    assert r["content_hash"].startswith("sha256:")

"""Response-shape contract for brain.resolve query results.

Current contract: item bodies are never truncated. Pages are packed by the wire
byte budget (default 128 KB, `NEURON_LBRAIN_MAX_BYTES` override), the cursor
advances by the number of items actually packed, and `has_more` is true only
while unreturned items remain. Consumers that need fewer fields pass `fields`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.mcp_payload import tool_result_bytes

import agent_knowledge.llm_brain_core.graph_first_resolver as resolver_module
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver

HASH_A = "sha256:" + "a" * 64


def _card(index: int, *, summary_chars: int = 80) -> dict:
    return {
        "memory_id": f"mem_{index}",
        "project": "neurons",
        "card_type": "decision",
        "title": f"결정 {index}",
        "summary": "요약 " * (summary_chars // 3 + 1),
        "typed_payload": {"decision": f"결정 본문 {index}"},
        "currentness": "current",
        "content_hash": HASH_A,
    }


def _episode(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        payload={"authority_memory_id": f"mem_{index}", "content_hash": HASH_A}
    )


class Store:
    def __init__(self, cards=None, *, chunks=None):
        self.cards = list(cards or [])
        self.chunks = list(chunks or [])

    def graph_projection_health(self, project, *, as_of=None, steward_only=False):
        assert steward_only is True
        return {"unprojected": False, "projection_lag_ms": None}

    def list_authorized_cards(self, **kwargs):
        wanted = kwargs.get("memory_ids")
        cards = self.cards if wanted is None else [c for c in self.cards if c["memory_id"] in wanted]
        return cards[: kwargs.get("limit", 100)]

    def search_session_chunks(self, *_args, **_kwargs):
        return list(self.chunks)


class Graph:
    def __init__(self, episodes=(), status="available"):
        self.episodes = tuple(episodes)
        self.status = status

    def search_context(self, **kwargs):
        assert kwargs["brain_id"] == "/project/neurons"
        return SimpleNamespace(status=self.status, episodes=self.episodes)


def _chunk_row(length: int, *, chunk_id: str = "chunk_1") -> dict:
    body = f"# 세션 개요 {chunk_id}\n" + ("긴 대화 본문 content " * (length // 20 + 1))
    return {
        "chunk_id": chunk_id,
        "project": "neurons",
        "content_markdown": body,
        "content_hash": sha256_hash(body),
        "similarity_score": 0.95,
        "currentness": "current",
    }


def _resolver(*, cards=(), episodes=(), chunks=()) -> GraphFirstResolver:
    return GraphFirstResolver(
        Store(cards=cards, chunks=chunks),
        Graph(episodes),
        lambda _query: [0.1, 0.2],
    )


def _walk_pages(resolver, *, limit: int, max_pages: int = 20, **extra) -> list[dict]:
    pages = []
    cursor = None
    for _ in range(max_pages):
        kwargs = {"project": "neurons", "mode": "query", "query": "본문", "limit": limit, **extra}
        if cursor:
            kwargs["cursor"] = cursor
        page = resolver.resolve(**kwargs)
        assert "error_code" not in page
        pages.append(page)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    assert cursor is None, "cursor did not terminate"
    return pages


def test_96kb_cards_page_through_without_body_loss():
    chunks = [_chunk_row(96_000, chunk_id=f"chunk_{index}") for index in range(5)]
    resolver = _resolver(chunks=chunks)

    pages = _walk_pages(resolver, limit=5)

    items = [item for page in pages for item in page["items"]]
    assert len(items) == 5
    assert len({item["id"] for item in items}) == 5
    assert all(len(item["summary"]) > 70_000 for item in items)
    for page in pages:
        assert page["items"]
        assert tool_result_bytes(page) <= resolver_module._MAX_BYTES or len(page["items"]) == 1
    assert pages[-1]["metadata"]["has_more"] is False


def test_pages_are_packed_by_byte_budget_not_by_truncation():
    cards = [_card(index, summary_chars=200) for index in range(12)]
    resolver = _resolver(cards=cards, episodes=[_episode(index) for index in range(12)])

    pages = _walk_pages(resolver, limit=5)

    assert [len(page["items"]) for page in pages] == [5, 5, 2]
    items = [item for page in pages for item in page["items"]]
    assert [item["id"] for item in items] == [f"mem_{index}" for index in range(12)]
    normalized = [resolver_module._safe(card["summary"], len(card["summary"]) + 1) for card in cards]
    assert [item["summary"] for item in items] == normalized


def test_fields_selection_returns_only_requested_fields_plus_id():
    resolver = _resolver(cards=[_card(0)], episodes=[_episode(0)])

    result = resolver.resolve(
        project="neurons",
        mode="query",
        query="본문",
        limit=5,
        fields=["title", "summary"],
    )

    assert result["items"][0].keys() == {"id", "title", "summary"}
    assert result["items"][0]["id"] == "mem_0"


def test_unknown_fields_are_rejected():
    import pytest

    resolver = _resolver(cards=[_card(0)], episodes=[_episode(0)])
    with pytest.raises(Exception):
        resolver.resolve(project="neurons", mode="query", query="본문", fields=["nope"])


def test_every_item_keeps_id_and_no_truncation_flag():
    resolver = _resolver(cards=[_card(0), _card(1)], episodes=[_episode(0), _episode(1)])
    result = resolver.resolve(project="neurons", mode="query", query="본문", limit=5)

    assert [item["id"] for item in result["items"]] == ["mem_0", "mem_1"]
    for item in result["items"]:
        assert "summary_truncated" not in item


def test_default_budget_is_128kb_and_env_override_is_honored():
    assert resolver_module._MAX_BYTES >= 131072

    env = dict(os.environ)
    env["NEURON_LBRAIN_MAX_BYTES"] = "65536"
    env["PYTHONPATH"] = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lib")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import agent_knowledge.llm_brain_core.graph_first_resolver as m; print(m._MAX_BYTES)",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert proc.stdout.strip() == "65536"


def test_detail_fetch_returns_full_body_for_a_query_item():
    body = "# 세션 개요\n" + ("상세 본문 " * 12_000)
    chunk_id = "seg_" + "a" * 52

    resolver = GraphFirstResolver(
        Store(),
        session_memory_fetch=lambda _prefix, _project: {
            "memory_id": chunk_id,
            "summary": body,
            "content_hash": sha256_hash(body),
            "currentness": "current",
        },
    )
    memory_id = f"chunk:{chunk_id[:56]}"

    pages = []
    cursor = None
    for _ in range(20):
        kwargs = {"project": "neurons", "memory_id": memory_id}
        if cursor:
            kwargs["cursor"] = cursor
        page = resolver.resolve(**kwargs)
        pages.append(page)
        cursor = page.get("next_cursor")
        if not cursor:
            break

    assert cursor is None
    fetched = "".join(page["items"][0]["summary"] for page in pages)
    stripped = body.strip()
    expected = resolver_module._safe(stripped, len(stripped) + 1)
    assert fetched == expected
    full_card_body = resolver_module._safe(resolver_module._session_chunk_body(body), sys.maxsize)
    if len(pages) == 1:
        assert pages[0]["items"][0]["content_hash"] == sha256_hash(full_card_body)
    else:
        assert pages[-1]["items"][0]["full_content_hash"] == sha256_hash(full_card_body)
    assert all(tool_result_bytes(page) <= resolver_module._MAX_BYTES for page in pages)
    assert all("summary_truncated" not in page["items"][0] for page in pages)

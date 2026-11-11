import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.llm_brain_core.graph_first_resolver import (
    GraphFirstResolver,
    _ResolveRequest,
    _item,
    _session_chunk_body,
)
from agent_knowledge.rag_ingress.pg_recall import (
    _group_segment_recall_hits,
    build_pg_brain_query_search_from_env,
)
from agent_knowledge.model_connectors import (
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL,
    LOCAL_EMBEDDING_PROFILE_ID,
)


def _segment_text(index: int, start: int, end: int, body: str) -> str:
    return (
        "# session-memory synthetic project\n"
        "session_id_hash: sha256:" + "a" * 64 + "\n"
        f"segment: {index} turns {start}-{end}\n\n{body}"
    )


def _hit(session: str, chunk_id: str, score: float, ids: list[str], body: str) -> dict:
    text = _segment_text(ids.index(chunk_id), 1, 2, body)
    return {
        "session_id_hash": session,
        "provider": "test",
        "project": "synthetic",
        "memory_id": chunk_id,
        "content_hash": sha256_hash(text),
        "summary": text,
        "score": score,
        "currentness": "current",
        "representation": "segment_v1",
        "segment_ids": ids,
        "temporal": {"time_confidence": "high"},
    }


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def get_chunk(self, chunk_id):
        return self.rows.get(chunk_id)


class _ResolverStore:
    def graph_projection_health(self, _project, **_kwargs):
        return {"unprojected": False, "projection_lag_ms": None}

    def list_authorized_cards(self, **_kwargs):
        return []

    def search_session_chunks(self, *_args, **_kwargs):
        return []


class _Authority:
    def __init__(self, ids_by_session, hashes_by_session=None):
        self.ids_by_session = ids_by_session
        self.hashes_by_session = hashes_by_session or {}
        self.calls = []

    def resolve(self, hit):
        session_id = hit["session_id_hash"]
        self.calls.append(session_id)
        if hit["memory_id"] not in self.ids_by_session.get(session_id, ()):
            return None
        return {
            "representation": "segment_v1",
            "segment_ids": self.ids_by_session[session_id],
            "segment_content_hashes": self.hashes_by_session.get(session_id, ()),
            "provider": hit.get("provider"),
            "project": hit.get("project"),
        }


def _row(chunk_id: str, session_id: str, index: int, start: int, end: int, body: str):
    text = _segment_text(index, start, end, body)
    return SimpleNamespace(
        chunk_id=chunk_id,
        session_id_hash=session_id,
        project="synthetic",
        provider="test",
        content_markdown=text,
        content_hash=sha256_hash(text),
    )


def test_segment_recall_groups_sessions_applies_penalty_and_keeps_session_v1():
    ids_a = ["a0", "a1"]
    ids_b = ["b0", "b1", "b2", "b3"]
    hits = [
        _hit("session-a", "a0", 0.82, ids_a, "A left"),
        _hit("session-a", "a1", 0.905, ids_a, "A best"),
        _hit("session-b", "b3", 0.91, ids_b, "B best"),
        {
            "memory_id": "legacy",
            "representation": "session_v1",
            "score": 0.899,
            "summary": "legacy session",
        },
    ]
    a0_row = _row("a0", "session-a", 0, 1, 2, "A left")
    b2_row = _row("b2", "session-b", 2, 5, 6, "B left")
    a1_hash = _hit("session-a", "a1", 0.905, ids_a, "A best")["content_hash"]
    b3_hash = _hit("session-b", "b3", 0.91, ids_b, "B best")["content_hash"]
    rows = {
        "a0": a0_row,
        "b2": b2_row,
    }
    authority = _Authority(
        {"session-a": ids_a, "session-b": ids_b},
        {
            "session-a": [a0_row.content_hash, a1_hash],
            "session-b": [
                sha256_hash("b0"),
                sha256_hash("b1"),
                b2_row.content_hash,
                b3_hash,
            ],
        },
    )

    results = _group_segment_recall_hits(
        hits,
        pg_store=_Store(rows),
        authority_resolver=authority,
        length_penalty=0.01,
    )

    assert [hit["memory_id"] for hit in results] == ["legacy", "a1", "b3"]
    assert sum(hit.get("session_id_hash") == "session-a" for hit in results) == 1
    assert "A left" in results[1]["summary"] and "A best" in results[1]["summary"]
    assert abs(results[1]["score"] - (0.905 - 0.01 * math.log(2))) < 1e-9
    assert results[1]["segment_turn_range"] == {"start": 1, "end": 2}
    assert results[1]["content_hash"] == sha256_hash(results[1]["summary"])
    assert results[1]["best_segment_content_hash"] == a1_hash
    assert results[1]["returned_segment_ids"] == ["a0", "a1"]
    assert results[1]["returned_segment_content_hashes"] == [
        a0_row.content_hash,
        a1_hash,
    ]
    assert authority.calls.count("session-a") == 1
    assert authority.calls.count("session-b") == 1


def test_segment_recall_does_not_expand_across_session_boundary():
    ids = ["s0", "s1"]
    hit = _hit("session-a", "s0", 0.9, ids, "current session")
    foreign = _row("s1", "session-b", 1, 3, 4, "foreign session")
    authority = _Authority(
        {"session-a": ids},
        {
            "session-a": [
                hit["content_hash"],
                sha256_hash(_segment_text(1, 1, 2, "expected neighbor")),
            ]
        },
    )

    result = _group_segment_recall_hits(
        [hit],
        pg_store=_Store({"s1": foreign}),
        authority_resolver=authority,
        length_penalty=0.01,
    )

    assert len(result) == 1
    assert "current session" in result[0]["summary"]
    assert "foreign session" not in result[0]["summary"]
    assert result[0]["segment_turn_range"] == {"start": 1, "end": 2}


def test_segment_recall_resolves_authority_once_for_both_neighbors():
    ids = ["left", "middle", "right"]
    left = _row("left", "session-a", 0, 1, 2, "left body")
    right = _row("right", "session-a", 2, 5, 6, "right body")
    best = _hit("session-a", "middle", 0.95, ids, "middle body")
    authority = _Authority(
        {"session-a": ids},
        {
            "session-a": [
                left.content_hash,
                best["content_hash"],
                right.content_hash,
            ]
        },
    )

    results = _group_segment_recall_hits(
        [best],
        pg_store=_Store({"left": left, "right": right}),
        authority_resolver=authority,
        length_penalty=0.01,
    )

    assert len(results) == 1
    assert "left body" in results[0]["summary"]
    assert "middle body" in results[0]["summary"]
    assert "right body" in results[0]["summary"]
    assert authority.calls == ["session-a"]


def _candidate_row(
    session_id: str,
    chunk_id: str,
    index: int,
    distance: float,
    body: str,
):
    row = _row(chunk_id, session_id, index, index + 1, index + 1, body)
    row.embedding_state = "ready"
    row.embedding_model = LOCAL_EMBEDDING_MODEL
    row.embedding = [1.0] * LOCAL_EMBEDDING_DIM
    row.created_at = datetime.now(timezone.utc)
    row.observed_at = row.created_at
    candidate = {
        "chunk_id": chunk_id,
        "session_id_hash": session_id,
        "project": "synthetic",
        "provider": "test",
        "content_markdown": row.content_markdown,
        "content_hash": row.content_hash,
        "distance": distance,
        "embedding_state": "ready",
        "embedding_model": LOCAL_EMBEDDING_MODEL,
        "embedding_profile": LOCAL_EMBEDDING_PROFILE_ID,
        "_test_row": row,
    }
    return row, candidate


def _install_diversity_search(
    monkeypatch,
    *,
    stage1_candidates,
    approved_candidates,
    receipts,
    rejected_sessions=(),
):
    rows = {row.chunk_id: row for row, _candidate in stage1_candidates}
    rows.update(
        {
            candidate["chunk_id"]: candidate["_test_row"]
            for candidate in approved_candidates.values()
        }
    )

    class Store:
        embedding_profile = None

        def __init__(self):
            self.round_calls = []
            self.approved_id_calls = []

        def search_session_chunks_local(
            self,
            *,
            limit,
            exclude_session_id_hashes=(),
            **_kwargs,
        ):
            excluded = set(exclude_session_id_hashes)
            self.round_calls.append((limit, set(excluded)))
            candidates = [
                candidate
                for _row, candidate in stage1_candidates
                if candidate["session_id_hash"] not in excluded
            ]
            return sorted(
                candidates,
                key=lambda item: (float(item["distance"]), item["chunk_id"]),
            )[:limit]

        def search_session_chunks_local_by_ids(
            self,
            query_vector,
            chunk_ids,
            *,
            limit=1,
            conn=None,
            profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        ):
            del query_vector
            self.approved_id_calls.append((list(chunk_ids), limit, conn, profile_id))
            candidates = [
                candidate
                for chunk_id in chunk_ids
                if (candidate := approved_candidates.get(chunk_id)) is not None
            ]
            return sorted(
                candidates,
                key=lambda item: (float(item["distance"]), item["chunk_id"]),
            )[:limit]

        def get_chunk(self, chunk_id):
            return rows.get(chunk_id)

        def get_session_chunk_by_id_prefix(
            self,
            *,
            project,
            chunk_id_prefix,
            conn=None,
        ):
            del conn
            matches = [
                row
                for row in rows.values()
                if row.project == project and row.chunk_id.startswith(chunk_id_prefix)
            ]
            return matches[0] if len(matches) == 1 else None

    class Embedder:
        model = LOCAL_EMBEDDING_MODEL
        size = LOCAL_EMBEDDING_DIM

        def embed(self, _text):
            return [1.0] * self.size

    class Authority:
        def __init__(self):
            self.session_calls = []

        def resolve_session_segments(self, hit):
            session_id = hit["session_id_hash"]
            self.session_calls.append(session_id)
            if session_id in rejected_sessions:
                return None
            return receipts.get(session_id)

        def resolve(self, _hit):
            pytest.fail("segment receipt should be resolved only once per candidate session")

    store = Store()
    authority = Authority()
    monkeypatch.setattr(
        "agent_knowledge.rag_ingress.pg_recall.PgVectorStore",
        lambda **_kwargs: store,
    )
    monkeypatch.setattr(
        "agent_knowledge.rag_ingress.embedding_provider.build_openai_embedding_provider",
        lambda **_kwargs: Embedder(),
    )
    monkeypatch.setattr(
        "agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "agent_knowledge.rag_ingress.pg_recall.CouchDBProjectionStateAuthorityResolver",
        lambda *_args, **_kwargs: authority,
    )
    search = build_pg_brain_query_search_from_env(
        {
            "NEURON_LBRAIN_PGVECTOR_DSN": "synthetic",
            "COUCHDB_URL": "http://synthetic.invalid",
            "SESSION_MEMORY_REPRESENTATION": "segment_v1",
        }
    )
    assert search is not None
    return search, store, authority


def _receipt(session_id: str, rows) -> dict:
    return {
        "representation": "segment_v1",
        "segment_ids": [row.chunk_id for row in rows],
        "segment_content_hashes": [row.content_hash for row in rows],
        "provider": "test",
        "project": "synthetic",
    }


def _install_fetch_search(
    monkeypatch,
    rows,
    receipt_rows=None,
    candidate_distances=None,
):
    candidates = []
    approved = {}
    for index, row in enumerate(rows):
        candidate = {
            "chunk_id": row.chunk_id,
            "session_id_hash": row.session_id_hash,
            "project": row.project,
            "provider": row.provider,
            "content_markdown": row.content_markdown,
            "content_hash": row.content_hash,
            "distance": (
                candidate_distances[index]
                if candidate_distances is not None
                else index / 10
            ),
            "embedding_state": "ready",
            "embedding_model": LOCAL_EMBEDDING_MODEL,
            "embedding_profile": LOCAL_EMBEDDING_PROFILE_ID,
            "_test_row": row,
        }
        candidates.append((row, candidate))
        approved[row.chunk_id] = candidate
    receipt_rows = rows if receipt_rows is None else receipt_rows
    receipts = {rows[0].session_id_hash: _receipt(rows[0].session_id_hash, receipt_rows)}
    return _install_diversity_search(
        monkeypatch,
        stage1_candidates=candidates,
        approved_candidates=approved,
        receipts=receipts,
    )


def test_segment_fetch_matches_query_stitched_summary_and_hash(monkeypatch):
    left, _ = _candidate_row(
        "fetch-session", "seg_" + "a" * 60, 0, 0.2, "left fetch body"
    )
    best, _ = _candidate_row(
        "fetch-session", "seg_" + "b" * 60, 1, 0.1, "best fetch body"
    )
    right, _ = _candidate_row(
        "fetch-session", "seg_" + "c" * 60, 2, 0.3, "right fetch body"
    )
    search, _store, _authority = _install_fetch_search(
        monkeypatch,
        [left, best, right],
        candidate_distances=[0.2, 0.1, 0.3],
    )

    query_hit = search("synthetic query", "/project/synthetic")[0]
    fetch_hit = search.fetch_session_memory_segment(best.chunk_id[:56], "synthetic")

    assert fetch_hit is not None
    assert fetch_hit["summary"] == query_hit["summary"]
    assert fetch_hit["content_hash"] == query_hit["content_hash"]
    assert fetch_hit["content_hash"] == sha256_hash(fetch_hit["summary"])
    assert fetch_hit["returned_segment_ids"] == query_hit["returned_segment_ids"]
    assert fetch_hit["returned_segment_content_hashes"] == query_hit[
        "returned_segment_content_hashes"
    ]


def test_segment_fetch_rejects_other_project_and_previous_revision(monkeypatch):
    row, _ = _candidate_row(
        "fetch-session", "seg_" + "a" * 60, 0, 0.1, "fetch body"
    )
    search, _store, _authority = _install_fetch_search(monkeypatch, [row])

    assert search.fetch_session_memory_segment(row.chunk_id[:56], "other") is None

    current, _ = _candidate_row(
        "fetch-session", "seg_" + "b" * 60, 1, 0.1, "current revision"
    )
    search, _store, _authority = _install_fetch_search(
        monkeypatch,
        [row],
        receipt_rows=[current],
    )
    assert search.fetch_session_memory_segment(row.chunk_id[:56], "synthetic") is None


def test_segment_fetch_rejects_hash_mismatch_ambiguous_prefix_and_canary(monkeypatch):
    row, _ = _candidate_row(
        "fetch-session", "seg_" + "a" * 60, 0, 0.1, "fetch body"
    )
    row.content_hash = sha256_hash("different body")
    search, _store, _authority = _install_fetch_search(monkeypatch, [row])
    assert search.fetch_session_memory_segment(row.chunk_id[:56], "synthetic") is None

    prefix = "seg_" + "d" * 52
    left, _ = _candidate_row(
        "fetch-session", prefix + "0" * 8, 0, 0.1, "left body"
    )
    right, _ = _candidate_row(
        "fetch-session", prefix + "1" * 8, 1, 0.2, "right body"
    )
    search, _store, _authority = _install_fetch_search(monkeypatch, [left, right])
    assert search.fetch_session_memory_segment(prefix, "synthetic") is None

    canary, _ = _candidate_row(
        "fetch-session", "seg_" + "e" * 60, 0, 0.1, "canary body"
    )
    canary.provider = "lbrain-temporal-canary"
    search, _store, _authority = _install_fetch_search(monkeypatch, [canary])
    assert search.fetch_session_memory_segment(canary.chunk_id[:56], "synthetic") is None


def test_segment_fetch_propagates_authority_errors(monkeypatch):
    row, _ = _candidate_row(
        "fetch-session", "seg_" + "a" * 60, 0, 0.1, "fetch body"
    )
    search, _store, authority = _install_fetch_search(monkeypatch, [row])

    def fail(_hit):
        raise RuntimeError("authority unavailable")

    authority.resolve_session_segments = fail
    with pytest.raises(RuntimeError, match="authority unavailable"):
        search.fetch_session_memory_segment(row.chunk_id[:56], "synthetic")


def test_segment_recall_uses_session_diversity_rounds_to_find_fifth_session(monkeypatch):
    stage1_candidates = []
    approved_candidates = {}
    receipts = {}
    for session_index in range(4):
        session_id = f"session-{session_index}"
        session_rows = []
        for segment_index in range(125):
            distance = session_index * 0.1 + segment_index / 100_000
            row, candidate = _candidate_row(
                session_id,
                f"seg-{session_index}-{segment_index:03d}",
                segment_index,
                distance,
                f"long session {session_index} segment {segment_index}",
            )
            session_rows.append(row)
            stage1_candidates.append((row, candidate))
            approved_candidates[row.chunk_id] = candidate
        receipts[session_id] = _receipt(session_id, session_rows)

    target_row, target_candidate = _candidate_row(
        "session-4",
        "seg-session-4-current",
        0,
        0.9,
        "fifth related session",
    )
    stage1_candidates.append((target_row, target_candidate))
    approved_candidates[target_row.chunk_id] = target_candidate
    receipts["session-4"] = _receipt("session-4", [target_row])

    search, store, authority = _install_diversity_search(
        monkeypatch,
        stage1_candidates=stage1_candidates,
        approved_candidates=approved_candidates,
        receipts=receipts,
    )

    results = search("synthetic query", "/project/synthetic")

    assert len(results) == 5
    assert {hit["session_id_hash"] for hit in results} == {
        f"session-{index}" for index in range(5)
    }
    assert len(store.round_calls) == 5
    assert all(limit == 100 for limit, _ in store.round_calls)
    assert [len(excluded) for _, excluded in store.round_calls] == [0, 1, 2, 3, 4]
    assert len(authority.session_calls) == 5
    assert len(store.approved_id_calls) == 5


def test_segment_recall_rescores_stale_best_hit_from_current_receipt_segments(monkeypatch):
    stale_row, stale_candidate = _candidate_row(
        "stale-session",
        "stale-segment",
        0,
        0.01,
        "old revision marker",
    )
    current_row, current_candidate = _candidate_row(
        "stale-session",
        "current-segment",
        0,
        0.8,
        "current approved body",
    )
    receipt = _receipt("stale-session", [current_row])
    search, _store, authority = _install_diversity_search(
        monkeypatch,
        stage1_candidates=[(stale_row, stale_candidate)],
        approved_candidates={current_row.chunk_id: current_candidate},
        receipts={"stale-session": receipt},
    )

    results = search("synthetic query", "/project/synthetic")

    assert len(results) == 1
    assert results[0]["memory_id"] == "current-segment"
    assert "current approved body" in results[0]["summary"]
    assert "old revision marker" not in results[0]["summary"]
    assert results[0]["best_segment_content_hash"] == current_row.content_hash
    assert results[0]["returned_segment_ids"] == ["current-segment"]
    assert results[0]["returned_segment_content_hashes"] == [current_row.content_hash]
    assert results[0]["content_hash"] == sha256_hash(results[0]["summary"])
    assert authority.session_calls == ["stale-session"]


def test_segment_recall_errors_after_five_full_rounds_with_fewer_than_five_results(
    monkeypatch,
):
    stage1_candidates = []
    approved_candidates = {}
    receipts = {}
    for session_index in range(5):
        session_id = f"session-{session_index}"
        session_rows = []
        for segment_index in range(100):
            row, candidate = _candidate_row(
                session_id,
                f"seg-{session_index}-{segment_index:03d}",
                segment_index,
                session_index * 0.1 + segment_index / 100_000,
                f"session {session_index} segment {segment_index}",
            )
            session_rows.append(row)
            stage1_candidates.append((row, candidate))
            approved_candidates[row.chunk_id] = candidate
        receipts[session_id] = _receipt(session_id, session_rows)

    search, store, _authority = _install_diversity_search(
        monkeypatch,
        stage1_candidates=stage1_candidates,
        approved_candidates=approved_candidates,
        receipts=receipts,
        rejected_sessions=("session-0",),
    )

    with pytest.raises(RuntimeError, match="^PG recall candidate limit exhausted$"):
        search("synthetic query", "/project/synthetic")

    assert len(store.round_calls) == 5
    assert all(limit == 100 for limit, _ in store.round_calls)


def test_segment_recall_returns_small_corpus_without_candidate_exhaustion(monkeypatch):
    stage1_candidates = []
    approved_candidates = {}
    receipts = {}
    for session_index in range(3):
        session_id = f"session-{session_index}"
        row, candidate = _candidate_row(
            session_id,
            f"seg-{session_index}",
            0,
            session_index / 10,
            f"small session {session_index}",
        )
        stage1_candidates.append((row, candidate))
        approved_candidates[row.chunk_id] = candidate
        receipts[session_id] = _receipt(session_id, [row])

    search, store, _authority = _install_diversity_search(
        monkeypatch,
        stage1_candidates=stage1_candidates,
        approved_candidates=approved_candidates,
        receipts=receipts,
    )

    results = search("synthetic query", "/project/synthetic")

    assert len(results) == 3
    assert len(store.round_calls) == 2
    assert store.round_calls[-1][1] == {
        "session-0",
        "session-1",
        "session-2",
    }


def test_segment_recall_rejects_neighbor_row_with_receipt_id_but_wrong_hash():
    ids = ["left", "right"]
    hit = _hit("session-a", "left", 0.9, ids, "left body")
    row = _row("right", "session-a", 1, 3, 4, "tampered body")
    authority = _Authority(
        {"session-a": ids},
        {
            "session-a": [
                hit["content_hash"],
                sha256_hash(_segment_text(1, 3, 4, "expected body")),
            ]
        },
    )

    results = _group_segment_recall_hits(
        [hit],
        pg_store=_Store({"right": row}),
        authority_resolver=authority,
        length_penalty=0.01,
    )

    assert len(results) == 1
    assert "left body" in results[0]["summary"]
    assert "tampered body" not in results[0]["summary"]


def test_graph_first_resolver_maps_authorized_segment_card_range():
    text = _segment_text(0, 7, 9, "synthetic final-turn marker")
    resolver = GraphFirstResolver(
        SimpleNamespace(search_session_chunks=lambda *_args, **_kwargs: []),
        session_memory_search=lambda _query, _project: [
            {
                "memory_id": "seg_" + "a" * 60,
                "summary": text,
                "content_hash": sha256_hash(text),
                "score": 0.9,
                "segment_turn_range": {"start": 7, "end": 9},
            },
            {
                "memory_id": "legacy-session",
                "summary": "legacy session body",
                "content_hash": sha256_hash("legacy session body"),
                "score": 0.8,
            },
        ],
    )

    cards = resolver._session_chunk_cards(
        _ResolveRequest(project="synthetic", query="final-turn marker")
    )

    assert len(cards) == 2
    assert cards[0]["segment_turn_range"] == {"start": 7, "end": 9}
    assert cards[0]["typed_payload"]["segment_turn_range"] == {"start": 7, "end": 9}
    assert "synthetic final-turn marker" in cards[0]["summary"]
    assert _item(cards[0])["segment_turn_range"] == {"start": 7, "end": 9}
    assert cards[1]["memory_id"] == "chunk:legacy-session"
    assert "segment_turn_range" not in cards[1]


def test_segment_body_preserves_content_before_a_transcript_heading():
    text = (
        "# session-memory synthetic project\n"
        "session_id_hash: sha256:" + "a" * 64 + "\n"
        "segment: 0 turns 3-4\n"
        "## conversation\n"
        "first segment content\n"
        "## Transcript\n"
        "last segment marker\n"
    )

    body = _session_chunk_body(text)

    assert "first segment content" in body
    assert "last segment marker" in body


def test_segment_card_keeps_tail_marker_within_brain_resolve_response_budget():
    marker = "P3 final turn marker violet lantern cobalt"
    body = "## Transcript\n" + ("synthetic segment content. " * 245) + marker
    text = _segment_text(0, 3, 4, body)
    resolver = GraphFirstResolver(
        _ResolverStore(),
        SimpleNamespace(
            search_context=lambda **_kwargs: SimpleNamespace(
                status="available",
                episodes=(),
            )
        ),
        lambda _query: [0.1],
        session_memory_search=lambda _query, _project: [
            {
                "memory_id": "seg_" + "a" * 60,
                "summary": text,
                "content_hash": sha256_hash(text),
                "score": 0.9,
                "currentness": "current",
                "segment_turn_range": {"start": 3, "end": 4},
            }
        ],
    )

    result = resolver.resolve(project="synthetic", query=marker, limit=5)

    assert "error_code" not in result
    assert len(result["items"]) == 1
    assert marker in result["items"][0]["summary"]
    assert result["items"][0]["typed_payload"] == {
        "segment_turn_range": {"start": 3, "end": 4}
    }
    assert "summary" not in result["statuses"][0]

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from types import SimpleNamespace

from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.couchdb_source.session_memory_materializer import (
    MaterializedSessionMemory,
)
from agent_knowledge.model_connectors import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL,
)
from agent_knowledge.session_memory.chunk_overlap import ChunkView
from agent_knowledge.session_memory import shadow_eval


def _view(session_id_hash: str, turn: int, text: str) -> ChunkView:
    return ChunkView(
        content_hash=sha256_hash(f"{session_id_hash}:{turn}:{text}"),
        turn_start_index=turn,
        turn_end_index=turn,
        part_index=1,
        part_count=1,
        char_start=0,
        char_end=len(text),
        redaction_version="test",
        text=text,
    )


def _materialized(
    session_id_hash: str,
    texts: tuple[str, ...] = ("head turn", "middle turn", "tail turn"),
    *,
    body_size: int | None = None,
    fully_materialized: bool = True,
) -> MaterializedSessionMemory:
    views = tuple(
        _view(session_id_hash, index, text)
        for index, text in enumerate(texts)
    )
    body = shadow_eval._render_m0_body(
        SimpleNamespace(
            provider="test-provider",
            project="synthetic-project",
            session_id_hash=session_id_hash,
            tool_evidence_bodies=(),
        ),
        views,
    )
    if body_size is not None:
        body = "x" * body_size
    return MaterializedSessionMemory(
        session_id_hash=session_id_hash,
        provider="test-provider",
        project="synthetic-project",
        target_profile="session-memory",
        body=body,
        content_hash=sha256_hash(body),
        conversation_chunk_count=len(views),
        tool_evidence_bundle_count=0,
        fully_materialized=fully_materialized,
        conversation_views=views,
    )


class ReadOnlyFakeStore:
    def __init__(self, session_ids: list[str]) -> None:
        self.session_ids = list(session_ids)
        self.iter_calls = 0

    def iter_by_type(self, doc_type: str, *, fields, limit: int):
        self.iter_calls += 1
        assert doc_type == "transcript_session"
        for session_id_hash in self.session_ids[:limit]:
            yield {"session_id_hash": session_id_hash}

    def put(self, *_args, **_kwargs):
        raise AssertionError("write API called")

    def put_if_absent(self, *_args, **_kwargs):
        raise AssertionError("write API called")

    def delete(self, *_args, **_kwargs):
        raise AssertionError("write API called")


class DeterministicEmbedder:
    model = LOCAL_EMBEDDING_MODEL
    size = LOCAL_EMBEDDING_DIM

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [float(value + 1) for value in digest[:8]]


def _run(
    session_ids: list[str],
    materials: dict[str, MaterializedSessionMemory],
    *,
    seed: int = 380,
    per_bucket: int = 60,
    max_bytes: int = shadow_eval.DEFAULT_MAX_BYTES,
    max_sessions_scan: int = 20_000,
):
    store = ReadOnlyFakeStore(session_ids)
    embedder = DeterministicEmbedder()
    report = shadow_eval.run_shadow_eval(
        store=store,
        embedder=embedder,
        seed=seed,
        per_bucket=per_bucket,
        max_bytes=max_bytes,
        max_sessions_scan=max_sessions_scan,
        materializer=lambda *, session_id_hash, store: materials[session_id_hash],
    )
    return report, embedder, store


def test_shadow_sampling_and_metrics_are_deterministic_for_same_seed():
    session_ids = [f"session-{index}" for index in range(10)]
    materials = {
        session_id: _materialized(
            session_id,
            (f"{session_id} first", f"{session_id} middle", f"{session_id} tail"),
        )
        for session_id in session_ids
    }

    first, _, _ = _run(session_ids, materials, seed=380, per_bucket=4)
    second, _, _ = _run(session_ids, materials, seed=380, per_bucket=4)

    first.pop("elapsed_seconds")
    second.pop("elapsed_seconds")
    assert first == second
    assert first["sampling"]["sampled_sessions"] == 4


def test_leave_one_out_removes_query_view_from_m0_body_and_segments():
    materialized = _materialized(
        "session-loo",
        ("unique-head-query", "unique-middle-query", "unique-tail-query"),
    )

    for view_index, view in enumerate(materialized.conversation_views):
        leave_one_out = shadow_eval._materialized_without_view(
            materialized,
            view_index,
        )
        query_text = view.text
        assert query_text not in leave_one_out.body
        segment_text = "\n".join(
            segment.text
            for segment in shadow_eval.build_segments(
                leave_one_out,
                max_bytes=shadow_eval.SEGMENT_MAX_BYTES,
            )
        )
        assert query_text not in segment_text


def test_original_m0_embedding_uses_the_same_rendered_format_as_leave_one_out():
    session_id_hash = "session-m0-format"
    materialized = replace(
        _materialized(session_id_hash),
        body="materializer body format differs",
    )
    expected_body = shadow_eval._render_m0_body(
        materialized,
        materialized.conversation_views,
    )

    _, embedder, _ = _run(
        [session_id_hash],
        {session_id_hash: materialized},
    )

    assert expected_body in embedder.calls
    assert materialized.body not in embedder.calls


def test_output_whitelist_violation_exits_two_without_writing_report(
    tmp_path,
    monkeypatch,
    capsys,
):
    output = tmp_path / "unsafe.json"
    monkeypatch.setattr(
        shadow_eval,
        "run_shadow_eval",
        lambda **_kwargs: {"schema_version": shadow_eval.SCHEMA_VERSION, "leak": "raw"},
    )

    result = shadow_eval.main(
        ["--out", str(output)],
        store=ReadOnlyFakeStore([]),
        embedder=DeterministicEmbedder(),
    )

    assert result == 2
    assert not output.exists()
    assert capsys.readouterr().out == ""


def test_shadow_eval_never_calls_store_write_apis():
    session_ids = ["session-read-only"]
    materials = {session_ids[0]: _materialized(session_ids[0])}

    report, embedder, store = _run(session_ids, materials)

    assert report["sampling"]["sampled_sessions"] == 1
    assert embedder.calls
    assert not hasattr(store, "write_calls")


def test_bucket_sampling_and_oversize_exclusion_counts():
    session_ids = [
        "bucket-small",
        "bucket-16k",
        "bucket-64k",
        "bucket-256k",
        "bucket-1m",
        "oversize",
        "not-full",
    ]
    materials = {
        "bucket-small": _materialized("bucket-small", body_size=1024),
        "bucket-16k": _materialized("bucket-16k", body_size=20 * 1024),
        "bucket-64k": _materialized("bucket-64k", body_size=70 * 1024),
        "bucket-256k": _materialized("bucket-256k", body_size=300 * 1024),
        "bucket-1m": _materialized("bucket-1m", body_size=2 * 1024 * 1024),
        "oversize": _materialized("oversize", body_size=9 * 1024 * 1024),
        "not-full": _materialized(
            "not-full",
            body_size=1024,
            fully_materialized=False,
        ),
    }

    report, _, _ = _run(
        session_ids,
        materials,
        per_bucket=1,
        max_bytes=shadow_eval.DEFAULT_MAX_BYTES,
    )

    assert report["sampling"]["sampled_sessions"] == 5
    assert report["sampling"]["excluded_oversize_sessions"] == 1
    assert report["sampling"]["excluded_not_fully_materialized_sessions"] == 1
    assert {
        key: value["sampled"]
        for key, value in report["sampling"]["buckets"].items()
    } == {key: 1 for key in shadow_eval.BUCKETS}
    assert report["sampling"]["buckets"]["lt_16k"]["eligible"] == 1


def test_gemini_profile_is_rejected_before_couchdb_reads(tmp_path, capsys):
    class GeminiEmbedder:
        model = DEFAULT_EMBEDDING_MODEL
        size = DEFAULT_EMBEDDING_DIM

        def embed(self, _text: str) -> list[float]:
            raise AssertionError("Gemini embedding must not be called")

    store = ReadOnlyFakeStore(["should-not-be-read"])
    result = shadow_eval.main(
        ["--out", str(tmp_path / "profile-rejected.json")],
        store=store,
        embedder=GeminiEmbedder(),
    )
    captured = capsys.readouterr()

    assert result == 1
    assert store.iter_calls == 0
    assert captured.out == ""
    assert captured.err == "shadow_eval_requires_local_profile\n"


def test_vectorized_similarity_preserves_naive_rank():
    query = [0.8, -0.3, 0.7]
    candidate_segments = {
        "target": [[0.7, -0.2, 0.6], [0.1, 0.9, -0.4]],
        "second": [[-0.2, 0.6, 0.7]],
        "third": [[0.8, 0.1, 0.2], [-0.7, -0.5, 0.3]],
    }
    alpha = 0.02
    naive_scores = {
        candidate_id: max(shadow_eval._cosine(query, vector) for vector in vectors)
        - alpha * math.log(len(vectors))
        for candidate_id, vectors in candidate_segments.items()
    }
    normalized_query = shadow_eval._normalize_vector(query)
    vectorized_scores = {
        candidate_id: float(
            (shadow_eval._normalized_matrix(vectors) @ normalized_query).max()
        )
        - alpha * math.log(len(vectors))
        for candidate_id, vectors in candidate_segments.items()
    }

    assert shadow_eval._rank_for_target(naive_scores, "target") == (
        shadow_eval._rank_for_target(vectorized_scores, "target")
    )
    for candidate_id in naive_scores:
        assert abs(naive_scores[candidate_id] - vectorized_scores[candidate_id]) < 1e-12


def test_shadow_candidate_pools_match_recall_limits_and_leave_misses_unranked():
    m0_scores = {
        f"long-{session_index:03d}": 0.9 - session_index / 10_000
        for session_index in range(shadow_eval._RECALL_CANDIDATE_LIMIT)
    }
    m0_scores["target"] = 0.1
    assert (
        shadow_eval._rank_for_target(
            m0_scores,
            "target",
            candidate_limit=shadow_eval._RECALL_CANDIDATE_LIMIT,
        )
        is None
    )

    pageable_segments = [
        (f"long-{session_index}", f"seg-{session_index}-{index}", 0.9 - rank / 10_000)
        for rank, (session_index, index) in enumerate(
            (session_index, index)
            for session_index in range(4)
            for index in range(25)
        )
    ]
    pageable_segments.append(("fifth-session", "seg-fifth", 0.1))
    pageable_pool = shadow_eval._segment_candidate_pool(pageable_segments)
    assert len(pageable_pool) == len(pageable_segments)
    assert len({session_id for session_id, _, _ in pageable_pool}) == 5

    crowded_segments = [
        (f"long-{session_index}", f"seg-{session_index}-{index:03d}", 0.9 - rank / 10_000)
        for rank, (session_index, index) in enumerate(
            (session_index, index)
            for session_index in range(4)
            for index in range(100)
        )
    ]
    crowded_segments.append(("target", "seg-target", 0.1))
    crowded_pool = shadow_eval._segment_candidate_pool(crowded_segments)
    assert len(crowded_pool) == (
        shadow_eval._RECALL_CANDIDATE_LIMIT
        * shadow_eval._SEGMENT_RECALL_MAX_PAGES
    )
    assert "target" not in {session_id for session_id, _, _ in crowded_pool}
    crowded_scores = {
        session_id: max(
            similarity
            for candidate_session, _, similarity in crowded_pool
            if candidate_session == session_id
        )
        for session_id, _, _ in crowded_pool
    }
    assert shadow_eval._rank_for_target(crowded_scores, "target") is None


def test_embeddings_are_cached_by_text_hash_and_reported():
    session_ids = ["cache-a", "cache-b"]
    materials = {
        session_id: _materialized(
            session_id,
            ("shared turn", "shared turn", "shared turn"),
        )
        for session_id in session_ids
    }

    report, embedder, _ = _run(session_ids, materials)

    assert report["embedding_calls"] == len(embedder.calls)
    assert report["embedding_cache_hits"] > 0

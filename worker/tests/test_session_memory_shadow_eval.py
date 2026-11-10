from __future__ import annotations

import hashlib
from types import SimpleNamespace

from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.couchdb_source.session_memory_materializer import (
    MaterializedSessionMemory,
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

    def iter_by_type(self, doc_type: str, *, fields, limit: int):
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

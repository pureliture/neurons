from types import SimpleNamespace

from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.session_memory.chunk_overlap import ChunkView
from agent_knowledge.session_memory.segments import build_segments


def _view(
    index: int,
    text: str,
    *,
    start: int | None = None,
    end: int | None = None,
) -> ChunkView:
    return ChunkView(
        content_hash=sha256_hash(f"view-{index}-{text}"),
        turn_start_index=index if start is None else start,
        turn_end_index=index if end is None else end,
        part_index=1,
        part_count=1,
        char_start=0,
        char_end=len(text),
        redaction_version="test",
        text=text,
    )


def _materialized(views: tuple[ChunkView, ...], evidence: tuple[str, ...] = ()):
    return SimpleNamespace(
        provider="test",
        project="segment-test",
        session_id_hash=sha256_hash("session"),
        conversation_views=views,
        tool_evidence_bodies=evidence,
    )


def test_build_segments_preserves_turn_boundaries_and_max_bytes():
    views = (_view(1, "a" * 70), _view(2, "b" * 70), _view(3, "c" * 70))

    segments = build_segments(_materialized(views), max_bytes=300)

    assert [(segment.turn_start, segment.turn_end) for segment in segments] == [
        (1, 2),
        (3, 3),
    ]
    assert all(len(segment.text.encode("utf-8")) <= 300 for segment in segments)
    assert segments[0].member_hashes == tuple(view.content_hash for view in views[:2])


def test_view_over_max_bytes_is_kept_as_a_single_segment():
    view = _view(1, "long-view-" * 20)

    segments = build_segments(_materialized((view,)), max_bytes=64)

    assert len(segments) == 1
    assert segments[0].member_hashes == (view.content_hash,)
    assert segments[0].turn_start == segments[0].turn_end == 1


def test_segment_ids_and_content_hashes_are_deterministic_and_bounded():
    views = (_view(1, "first"), _view(2, "second"))
    materialized = _materialized(views)

    first = build_segments(materialized, max_bytes=2048)
    second = build_segments(materialized, max_bytes=2048)

    assert first == second
    assert all(len(segment.segment_id) <= 64 for segment in first)
    assert all(segment.segment_id.startswith("seg_") for segment in first)
    assert all(segment.content_hash == sha256_hash(segment.text) for segment in first)


def test_append_keeps_prefix_segment_identity_and_changes_only_tail_identity():
    initial = (_view(1, "a" * 70), _view(2, "b" * 70), _view(3, "c" * 70))
    appended = (*initial, _view(4, "d" * 70))

    before = build_segments(_materialized(initial), max_bytes=300)
    after = build_segments(_materialized(appended), max_bytes=300)

    assert [segment.member_hashes for segment in before] == [
        (initial[0].content_hash, initial[1].content_hash),
        (initial[2].content_hash,),
    ]
    assert after[0].segment_id == before[0].segment_id
    assert after[-1].segment_id != before[-1].segment_id


def test_tool_evidence_is_attached_only_to_last_segment():
    views = (_view(1, "a" * 70), _view(2, "b" * 70), _view(3, "c" * 70))

    segments = build_segments(
        _materialized(views, ("evidence one", "evidence two")),
        max_bytes=200,
    )

    assert "## tool_evidence_summary" not in segments[0].text
    assert "## tool_evidence_summary" in segments[-1].text
    assert "evidence one" in segments[-1].text
    assert "evidence two" in segments[-1].text

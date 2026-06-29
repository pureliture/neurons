"""Unit tests for the shared conversation_chunk overlap canonicalization policy.

This is the single source of truth for "drop a shorter chunk subsumed by a longer
one (turn-window containment + text containment), and collapse exact duplicates",
reused by both the canonical M3 materializer and the regeneration path.
"""

from agent_knowledge.session_memory.chunk_overlap import (
    ChunkView,
    canonicalize_chunk_views,
)


def _view(text, *, turn_start, turn_end, content_hash=None, part_index=1, part_count=1, char_start=0, char_end=0, redaction_version="redaction.v2"):
    return ChunkView(
        content_hash=content_hash if content_hash is not None else f"sha256:{abs(hash(text)):064x}"[:71],
        turn_start_index=turn_start,
        turn_end_index=turn_end,
        part_index=part_index,
        part_count=part_count,
        char_start=char_start,
        char_end=char_end,
        redaction_version=redaction_version,
        text=text,
    )


def test_no_chunks_returns_empty():
    kept, report = canonicalize_chunk_views([])
    assert kept == []
    assert report["kept_count"] == 0


def test_single_chunk_is_kept():
    v = _view("hello", turn_start=1, turn_end=2)
    kept, _ = canonicalize_chunk_views([v])
    assert kept == [v]


def test_exact_duplicate_is_collapsed():
    a = _view("same body", turn_start=1, turn_end=2, content_hash="sha256:" + "a" * 64)
    b = _view("same body", turn_start=1, turn_end=2, content_hash="sha256:" + "a" * 64)
    kept, report = canonicalize_chunk_views([a, b])
    assert len(kept) == 1
    assert report["exact_duplicate_count"] == 1


def test_subsumed_shorter_chunk_is_dropped():
    # longer chunk (turns 1-4) strictly contains the shorter (turns 2-3) window AND
    # contains its text -> shorter is dropped, only the longer survives.
    longer = _view("alpha beta gamma delta", turn_start=1, turn_end=4)
    shorter = _view("beta gamma", turn_start=2, turn_end=3)
    kept, report = canonicalize_chunk_views([shorter, longer])
    assert kept == [longer]
    assert report["subsumed_overlap_count"] == 1


def test_non_overlapping_chunks_both_kept_in_order():
    a = _view("first part", turn_start=1, turn_end=2)
    b = _view("second part", turn_start=3, turn_end=4)
    kept, report = canonicalize_chunk_views([a, b])
    assert kept == [a, b]
    assert report["subsumed_overlap_count"] == 0


def test_partial_overlap_without_containment_keeps_both():
    # windows cross (1-3 and 2-4) but neither strictly contains the other -> both kept.
    a = _view("turns one two three", turn_start=1, turn_end=3)
    b = _view("turns two three four", turn_start=2, turn_end=4)
    kept, _ = canonicalize_chunk_views([a, b])
    assert set(kept) == {a, b}


def test_window_contained_but_text_not_contained_keeps_both():
    # longer window contains shorter window, but the shorter's text is NOT inside the
    # longer's text -> conservative: not subsumed, both kept.
    longer = _view("alpha beta gamma delta", turn_start=1, turn_end=4)
    shorter = _view("UNRELATED CONTENT", turn_start=2, turn_end=3)
    kept, _ = canonicalize_chunk_views([shorter, longer])
    assert set(kept) == {shorter, longer}

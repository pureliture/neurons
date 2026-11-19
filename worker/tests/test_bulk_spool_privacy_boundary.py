"""Bounded synthetic tests for spool page privacy and canonical-byte parity."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from agent_knowledge.couchdb_source.spool_reader import SpooledText
from agent_knowledge.llm_brain_core.bulk_full_input import PrivacyGateRejected
from agent_knowledge.llm_brain_core.bulk_streaming import (
    expected_source_bytes, iter_planned_chunks, planned_chunk_key,
)
from agent_knowledge.llm_brain_core.runtime import _strip_chunk_metadata_header

P = "synthetic-provider"


class FakeSpool(SpooledText):
    """In-memory SpooledText with controlled page boundaries and close tracking."""

    def __init__(self, pages: list[str], *, lane: str = "evidence") -> None:
        self._pages = list(pages)
        raw = b"".join(page.encode("utf-8") for page in pages)
        self.content_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.byte_length = len(raw)
        self.lane = lane
        self.closed = False

    def iter_pages(self, size: int = 64 * 1024):
        yield from self._pages

    def close(self, *, retain: bool = False) -> None:
        self.closed = True


def _store(body, lane="evidence"):
    doc = {
        "body": body,
        "content_hash": body.content_hash if isinstance(body, SpooledText)
        else "sha256:" + hashlib.sha256(str(body).encode("utf-8")).hexdigest(),
        "redaction_version": "v1",
    }
    member = SimpleNamespace(
        doc_id="doc-" + doc["content_hash"][7:15], lane=lane, ordinal=0,
        order_key=("synthetic", 0), content_hash=doc["content_hash"],
        redaction_version="v1",
    )
    plan = SimpleNamespace(source_hash="sha256:source", members=(member,))
    return SimpleNamespace(get=lambda doc_id: doc), plan, doc


def _chunk_input(chunk):
    if hasattr(chunk.text, "iter_pages"):
        return "".join(chunk.text.iter_pages())
    return str(chunk.text)


def test_cross_page_forbidden_marker_fails_closed():
    body = FakeSpool(["ABC", "DEF"])
    store, plan, _doc = _store(body)
    with pytest.raises(PrivacyGateRejected):
        list(iter_planned_chunks(store, plan, forbidden_markers=(b"CDE",)))
    assert body.closed is True


def test_utf8_cross_page_forbidden_marker_fails_closed():
    body = FakeSpool(["가나", "다"])
    store, plan, _doc = _store(body)
    with pytest.raises(PrivacyGateRejected):
        list(iter_planned_chunks(store, plan, forbidden_markers=("나다".encode("utf-8"),)))
    assert body.closed is True


def test_multiple_markers_including_cross_page_fails_closed():
    body = FakeSpool(["AB", "CD", "EF"])
    store, plan, _doc = _store(body)
    with pytest.raises(PrivacyGateRejected):
        list(iter_planned_chunks(
            store, plan, forbidden_markers=(b"CD", b"BCE"),
        ))
    assert body.closed is True


def test_empty_forbidden_marker_fails_closed():
    body = FakeSpool(["ABC"])
    store, plan, _doc = _store(body)
    with pytest.raises(PrivacyGateRejected):
        list(iter_planned_chunks(store, plan, forbidden_markers=(b"",)))
    assert body.closed is True


def test_clean_spool_stream_is_delivered():
    body = FakeSpool(["ABC", "DEF"])
    store, plan, _doc = _store(body)
    chunks = list(iter_planned_chunks(store, plan, forbidden_markers=(b"XYZ",)))
    assert _chunk_input(chunks[0]) == "ABCDEF"


def test_spool_and_nonspool_conversation_have_identical_input_bytes_and_hash():
    header = (
        "session_id_hash: synthetic\n"
        "turn_start_index: 0\n"
        "turn_end_index: 0\n"
        "\n"
    )
    content = ("본문 A" * 3000) + "\n마지막 줄\n"
    raw = header + content
    canonical = _strip_chunk_metadata_header(raw)

    nonspool_store, nonspool_plan, _ = _store(raw, lane="conversation")
    nonspool = list(iter_planned_chunks(nonspool_store, nonspool_plan))[0]

    # Split the same raw document across an arbitrary page boundary.
    cut = len(header) + 17
    spool_store, spool_plan, _ = _store(
        FakeSpool([raw[:cut], raw[cut:]], lane="conversation"), lane="conversation",
    )
    spool = list(iter_planned_chunks(spool_store, spool_plan))[0]

    nonspool_bytes = _chunk_input(nonspool).encode("utf-8")
    spool_bytes = _chunk_input(spool).encode("utf-8")
    assert nonspool_bytes == canonical.encode("utf-8")
    assert spool_bytes == nonspool_bytes
    assert hashlib.sha256(spool_bytes).hexdigest() == hashlib.sha256(nonspool_bytes).hexdigest()
    assert spool.byte_end - spool.byte_start == len(nonspool_bytes)
    assert spool.canonical_hash == nonspool.canonical_hash


def test_expected_source_bytes_uses_canonical_spool_bytes():
    header = "session_id_hash: synthetic\nturn_start_index: 0\nturn_end_index: 0\n\n"
    content = "본문 B" * 2000
    raw = header + content
    canonical = _strip_chunk_metadata_header(raw)
    spool_store, spool_plan, _ = _store(
        FakeSpool([raw[:100], raw[100:]], lane="conversation"), lane="conversation",
    )
    expectation = expected_source_bytes(spool_store, spool_plan)
    assert expectation["body_bytes"] == len(canonical.encode("utf-8"))


def test_empty_forbidden_marker_rejects_empty_body_and_closes_spool():
    body = FakeSpool([])
    store, plan, _doc = _store(body)
    with pytest.raises(PrivacyGateRejected):
        list(iter_planned_chunks(store, plan, forbidden_markers=(b"",)))
    assert body.closed is True


def test_resumed_completed_chunk_is_not_reopened_for_privacy_gate(tmp_path):
    body = FakeSpool(["SECRET"])
    store, plan, _doc = _store(body)
    completed_key = planned_chunk_key(plan, plan.members[0])

    class _State:
        def is_session_complete(self, *, chunk_keys, **kwargs):
            return bool(chunk_keys) and all(key == completed_key for key in chunk_keys)

    from agent_knowledge.llm_brain_core import bulk_stream_runner as runner

    original_select = runner._select_sessions_bounded
    original_authority = runner._pg_authority_by_hash
    original_revision = runner.session_source_revision_from_couchdb_source
    original_plan = runner.plan_validated_source
    runner._select_sessions_bounded = lambda *a, **k: SimpleNamespace(sessions=[{
        "session_id_hash": "synthetic-session", "project": "synthetic-project",
    }])
    runner._pg_authority_by_hash = lambda *a, **k: {}
    runner.session_source_revision_from_couchdb_source = lambda **k: plan.source_hash
    runner.plan_validated_source = lambda *a, **k: plan
    try:
        report = runner.run_streamed_full_input(
            ledger_path=tmp_path / "ledger.db", source_store=store,
            extractor=SimpleNamespace(), writer=SimpleNamespace(),
            full_input_state=_State(), policy_version="synthetic", project="synthetic",
            limit=1, forbidden_markers=(b"SECRET",),
        )
    finally:
        runner._select_sessions_bounded = original_select
        runner._pg_authority_by_hash = original_authority
        runner.session_source_revision_from_couchdb_source = original_revision
        runner.plan_validated_source = original_plan
    assert report["skipped_resumed"] == 1
    assert report["projected"] == 0
    assert body.closed is False


def test_extractor_exception_closes_spooled_text(tmp_path):
    body = FakeSpool(["ABC", "DEF"])
    store, plan, _doc = _store(body)
    chunks = list(iter_planned_chunks(store, plan))
    text = chunks[0].text
    try:
        with pytest.raises(RuntimeError):
            raise RuntimeError("synthetic extractor failure")
    finally:
        close = getattr(text, "close", None)
        if close is not None:
            close()
    assert body.closed is True


def test_spool_single_line_over_64k_without_header_matches_nonspool():
    raw = "FRONT-" + "".join(f"[seg:{i}:한글😀]" + ("X" * 30) for i in range(3000)) + "-END"
    assert len(raw.encode("utf-8")) > 128 * 1024
    assert "\n" not in raw

    nonspool_store, nonspool_plan, _ = _store(raw, lane="conversation")
    nonspool = list(iter_planned_chunks(nonspool_store, nonspool_plan))[0]

    pages = [raw[i:i + 32 * 1024] for i in range(0, len(raw), 32 * 1024)]
    spool_store, spool_plan, _ = _store(FakeSpool(pages, lane="conversation"), lane="conversation")
    spool = list(iter_planned_chunks(spool_store, spool_plan))[0]

    nonspool_bytes = _chunk_input(nonspool).encode("utf-8")
    spool_bytes = _chunk_input(spool).encode("utf-8")
    assert nonspool_bytes == _strip_chunk_metadata_header(raw).encode("utf-8")
    assert spool_bytes == nonspool_bytes
    assert spool.canonical_hash == nonspool.canonical_hash


def test_spool_single_line_over_64k_with_header_and_crlf_matches_nonspool():
    header = "turn_start_index: 0\r\n\r\n"
    long_line = "LONG-" + ("가나다😀" * 18000) + "-MID"
    raw = header + long_line + "\r\nTAIL-LINE\r\n"
    assert len(long_line.encode("utf-8")) > 128 * 1024

    nonspool_store, nonspool_plan, _ = _store(raw, lane="conversation")
    nonspool = list(iter_planned_chunks(nonspool_store, nonspool_plan))[0]

    # Split right between \r and \n after long_line across FakeSpool pages.
    crlf_cut = len(header) + len(long_line) + 1
    mid_cut = len(header) + 65536
    pages = [raw[:mid_cut], raw[mid_cut:crlf_cut], raw[crlf_cut:]]
    spool_store, spool_plan, _ = _store(FakeSpool(pages, lane="conversation"), lane="conversation")
    spool = list(iter_planned_chunks(spool_store, spool_plan))[0]

    nonspool_bytes = _chunk_input(nonspool).encode("utf-8")
    spool_bytes = _chunk_input(spool).encode("utf-8")
    assert nonspool_bytes == _strip_chunk_metadata_header(raw).encode("utf-8")
    assert spool_bytes == nonspool_bytes
    assert spool.canonical_hash == nonspool.canonical_hash

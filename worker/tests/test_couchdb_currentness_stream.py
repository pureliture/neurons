"""Contract tests for bounded canonical source-currentness streaming."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import couchdb_currentness_stream as cs
from agent_knowledge.couchdb_source import source_revision as sr
from agent_knowledge.couchdb_source.document_model import SourceDocType


_HASH = "sha256:" + "a" * 64


def _write_page(tmp_path: Path, payload: bytes) -> Path:
    page = tmp_path / "page.json"
    page.write_bytes(payload)
    return page


def test_streamed_canonical_hash_matches_python_for_nested_unicode_and_duplicate_keys(tmp_path):
    document = {
        "_id": "src-1",
        "_rev": "1-x",
        "doc_type": "conversation_chunk",
        "session_id_hash": "session-1",
        "content_hash": _HASH,
        "body": "마지막 😀 \" \\ \n \u007f",
        "nested": {"z": 1, "a": {"b": [1, 1.0, -0.0, True, None]}},
        "parser_version": "parser-9",
        "chunker_version": "chunker-9",
        "idempotency_key": "excluded",
        "payload_hash": "excluded",
    }
    serialized = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    raw = (
        '{"docs":['
        + serialized.replace(
            '"body":', '"body":"first","body":', 1
        )
        + '],"bookmark":"next-page"}'
    ).encode("utf-8")
    expected_document = json.loads(raw)["docs"][0]

    rows, bookmark = cs.parse_spooled_descriptor_page(
        _write_page(tmp_path, raw), max_page_bytes=len(raw) + 1024, max_items=8
    )

    assert bookmark == "next-page"
    assert len(rows) == 1
    assert rows[0]["__source_document_hash"] == sr._source_document_hash(expected_document)
    assert rows[0]["content_hash"] == _HASH
    assert rows[0]["parser_version"] == "parser-9"
    assert rows[0]["chunker_version"] == "chunker-9"
    assert "body" not in rows[0]


def test_streamed_descriptor_matches_full_descriptor(tmp_path):
    document = {
        "_id": "src-2",
        "_rev": "1-y",
        "doc_type": SourceDocType.CONVERSATION_CHUNK,
        "session_id_hash": "session-2",
        "content_hash": _HASH,
        "body": "본문 😀",
        "observed_at_start": "t0",
        "observed_at_end": "t1",
        "turn_start_index": 0,
        "turn_end_index": 1,
        "part_index": 2,
        "part_count": 3,
        "redaction_version": "redaction-1",
        "parser_version": "parser-1",
        "chunker_version": "chunker-1",
    }
    raw = json.dumps({"docs": [document], "bookmark": ""}, ensure_ascii=False).encode("utf-8")
    rows, _ = cs.parse_spooled_descriptor_page(
        _write_page(tmp_path, raw), max_page_bytes=len(raw) + 1024, max_items=8
    )

    assert sr._source_member_descriptor_from_streamed_document(
        rows[0], session_id_hash="session-2"
    ) == sr._source_member_descriptor(document, session_id_hash="session-2")

    tampered = dict(document, body="본문 😀 tampered")
    tampered_raw = json.dumps({"docs": [tampered], "bookmark": ""}, ensure_ascii=False).encode("utf-8")
    tampered_rows, _ = cs.parse_spooled_descriptor_page(
        _write_page(tmp_path, tampered_raw),
        max_page_bytes=len(tampered_raw) + 1024,
        max_items=8,
    )
    assert (
        tampered_rows[0]["__source_document_hash"]
        != rows[0]["__source_document_hash"]
    )



def test_streamed_document_hash_fallback_token_matches_full_document(tmp_path):
    document = {
        "_id": "session-3",
        "doc_type": SourceDocType.TRANSCRIPT_SESSION,
        "session_id_hash": "session-3",
        "started_at": "t0",
        "ended_at": "t1",
    }
    raw = json.dumps({"docs": [document], "bookmark": ""}, ensure_ascii=False).encode("utf-8")
    rows, _ = cs.parse_spooled_descriptor_page(
        _write_page(tmp_path, raw), max_page_bytes=len(raw) + 1024, max_items=8
    )
    full_budget = {"rows": 0, "compact_bytes": 0}
    stream_budget = {"rows": 0, "compact_bytes": 0}

    assert sr._currentness_component(document, budget=full_budget)["revision_token"] == (
        sr._currentness_component(rows[0], budget=stream_budget)["revision_token"]
    )

@pytest.mark.parametrize(
    "raw",
    [
        b'{"docs":[{"_id":"x","body":"bad\\q"}]}',
        b'{"docs":[{"_id":"x","body":"\xff"}]}',
        b'{"docs":[],"docs":[]}',
        b'{"docs":[{"_id":"x","body":"truncated}',
    ],
)
def test_invalid_stream_fail_closed(tmp_path, raw):
    with pytest.raises(cs.CurrentnessStreamInvalid):
        cs.parse_spooled_descriptor_page(
            _write_page(tmp_path, raw), max_page_bytes=len(raw) + 1024, max_items=8
        )


def test_empty_docs_page_is_bounded_and_valid(tmp_path):
    raw = b'{"docs":[],"bookmark":"after-empty","warning":"none"}'
    rows, bookmark = cs.parse_spooled_descriptor_page(
        _write_page(tmp_path, raw), max_page_bytes=len(raw) + 1024, max_items=8
    )
    assert rows == []
    assert bookmark == "after-empty"


def test_canonical_temp_budget_fail_closed(tmp_path, monkeypatch):
    raw = json.dumps(
        {"docs": [{"_id": "x", "body": "a" * 4096}], "bookmark": ""}
    ).encode("utf-8")
    monkeypatch.setenv("NEURON_SOURCE_CURRENTNESS_STREAM_SPOOL_BYTES", "128")
    with pytest.raises(cs.CurrentnessStreamBudgetExceeded):
        cs.parse_spooled_descriptor_page(
            _write_page(tmp_path, raw), max_page_bytes=len(raw) + 1024, max_items=8
        )


@pytest.mark.parametrize(
    "document",
    [
        {"n": -0},
        {"n": -0.0},
        {"n": 1e3},
        {"n": 1e-7},
        {"n": 1.2345678901234567},
        {"escaped\u0000key": "value", 'escape"key': 2, "한글": True},
        {"lone": "\ud800", "low": "\udfff"},
        {"n": None, "b": False, "t": True},
        {"nested": {"z": [{"b": 2, "a": [None, True, False]}]}},
    ],
)
def test_standard_json_boundaries_match_python_canonicalizer(tmp_path, document):
    document.update(
        {
            "_id": "src-boundary",
            "doc_type": SourceDocType.CONVERSATION_CHUNK,
            "session_id_hash": "session-boundary",
        }
    )
    raw = json.dumps({"docs": [document], "bookmark": ""}, ensure_ascii=True).encode("utf-8")
    rows, _ = cs.parse_spooled_descriptor_page(
        _write_page(tmp_path, raw), max_page_bytes=len(raw) + 1024, max_items=8
    )
    assert rows[0]["__source_document_hash"] == sr._source_document_hash(document)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_tokens_fail_closed_and_differ_from_python_acceptance(tmp_path, token):
    raw = (
        '{"docs":[{"_id":"x","doc_type":"conversation_chunk",'
        '"session_id_hash":"s","n":' + token + '}],"bookmark":""}'
    ).encode("utf-8")
    with pytest.raises(cs.CurrentnessStreamInvalid):
        cs.parse_spooled_descriptor_page(
            _write_page(tmp_path, raw), max_page_bytes=len(raw) + 1024, max_items=8
        )

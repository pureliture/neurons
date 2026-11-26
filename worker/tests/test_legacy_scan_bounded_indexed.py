"""Legacy currentness scan is bounded/indexed and preserves fail-closed gates."""

from __future__ import annotations

import json
import pytest
from typing import Any

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source import source_revision as sr
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import couchdb_projection_cli
from agent_knowledge.llm_brain_core.couchdb_projection_cli import (
    GraphProjectionDeadlineExceeded,
    _iter_session_revisions,
)
from agent_knowledge.public_safe_util import extract_safe_error_diagnostics
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROVIDER = "codex"
PROJECT = "neurons"


SESSION_INDEX_HINT = ["session", "mango_session_id_hash_doc_type"]
_RECORDING_INDEXES: list[dict[str, Any]] = [
    {
        "name": "by_type",
        "ddoc": "design/legacy",
        "def": {"fields": [{"doc_type": "asc"}]},
    },
    {
        "name": "sid_type",
        "ddoc": "design/session",
        "def": {"fields": [{"session_id_hash": "asc"}, {"doc_type": "asc"}]},
    },
    {
        "name": "mango_session_id_hash_doc_type",
        "ddoc": "design/session",
        "def": {"fields": [{"session_id_hash": "asc"}, {"doc_type": "asc"}]},
    },
    {
        "name": "mango_session_id_hash_only",
        "ddoc": "design/session2",
        "def": {"fields": [{"session_id_hash": "asc"}]},
    },
]


def _seed_session(store: InMemoryCouchDBSourceStore, raw_id: str, *, tampered: bool = False) -> str:
    sid = dm.build_session_id_hash(PROVIDER, raw_id)
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=sid,
                provider=PROVIDER,
                project=PROJECT,
                started_at="2026-11-01T00:00:00Z",
                ended_at="2026-11-01T00:01:00Z",
            )
        )
    )
    store.put(
        dm.build_conversation_chunk_document(
            chunk=TranscriptChunk.from_text(
                chunk_id=f"chunk-{raw_id}",
                session_id_hash=sid,
                provider=PROVIDER,
                project=PROJECT,
                turn_start_index=0,
                turn_end_index=0,
                text=f"synthetic body for {raw_id}",
            )
        )
    )
    if tampered:
        bundle_id = dm.tool_evidence_bundle_doc_id(sid, 0)
        # A body-bearing bundle with no material hash cannot be verified; the
        # bulk scan must omit it and the per-session resolver must fail closed.
        store._docs[bundle_id] = {
            "_id": bundle_id,
            "doc_type": dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
            "session_id_hash": sid,
            "provider": PROVIDER,
            "project": PROJECT,
            "body": "tampered body without a material hash",
        }
    return sid


class _RecordingStore:
    """Records query shape while delegating reads to the in-memory store."""

    def __init__(
        self,
        inner: InMemoryCouchDBSourceStore,
        *,
        indexes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.inner = inner
        self.base_url = "http://couchdb.test"
        self.db = "transcript_source"
        self.indexes = list(_RECORDING_INDEXES if indexes is None else indexes)
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def _request(self, _method: str, _path: str) -> tuple[int, dict]:
        return 200, {"indexes": self.indexes}

    def iter_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
        **extra: Any,
    ):
        self.queries.append(
            (
                doc_type,
                {
                    "fields": fields,
                    "selector": dict(selector or {}),
                    "limit": limit,
                    "page_size": page_size,
                    **extra,
                },
            )
        )
        # The in-memory store does not implement Mango transport options.
        return self.inner.iter_by_type(
            doc_type, fields=fields, selector=selector, limit=limit, page_size=page_size
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_large_source_set_uses_bounded_indexed_reads() -> None:
    inner = InMemoryCouchDBSourceStore()
    sids = [_seed_session(inner, f"synthetic-{index}") for index in range(40)]
    store = _RecordingStore(inner)

    hashes = sr.iter_legacy_source_hashes(store, page_size=17, max_sessions=5)

    assert len(hashes) == 5
    assert set(hashes).issubset(set(sids))

    discovery, *children = store.queries
    assert discovery[0] == dm.SourceDocType.TRANSCRIPT_SESSION
    assert discovery[1]["limit"] == 5
    assert discovery[1]["page_size"] == 17
    assert discovery[1]["use_index"] == ["legacy", "by_type"]
    assert discovery[1]["allow_fallback"] is False
    assert children
    for doc_type, kwargs in children:
        assert doc_type in (
            dm.SourceDocType.CONVERSATION_CHUNK,
            dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
        )
        assert kwargs["page_size"] == 17
        assert kwargs["use_index"] == SESSION_INDEX_HINT
        assert kwargs["allow_fallback"] is False
        selected = kwargs["selector"]["session_id_hash"]["$in"]
        assert 1 <= len(selected) <= 5
        assert set(selected).issubset(set(sids))
    # One discovery scan plus at most two scoped child scans and one scoped
    # verification scan: never one request per source document.
    assert len(store.queries) <= 4


def test_default_legacy_scan_page_size_stays_bounded() -> None:
    inner = InMemoryCouchDBSourceStore()
    for index in range(25):
        _seed_session(inner, f"pilot-{index}")
    store = _RecordingStore(inner)

    hashes = sr.iter_legacy_source_hashes(store, max_sessions=25)

    assert len(hashes) == 25
    # 2,048-row _find pages timed out in the deployed pod (30s client timeout).
    assert 256 <= sr.LEGACY_SOURCE_SCAN_PAGE_SIZE <= 512
    discovery, *children = store.queries
    assert discovery[0] == dm.SourceDocType.TRANSCRIPT_SESSION
    assert discovery[1]["limit"] == 25
    assert discovery[1]["page_size"] == sr.LEGACY_SOURCE_SCAN_PAGE_SIZE
    assert discovery[1]["use_index"] == ["legacy", "by_type"]
    assert discovery[1]["allow_fallback"] is False
    assert children
    for doc_type, kwargs in children:
        assert doc_type in (
            dm.SourceDocType.CONVERSATION_CHUNK,
            dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
        )
        assert kwargs["page_size"] == sr.LEGACY_SOURCE_SCAN_PAGE_SIZE
        assert kwargs["use_index"] == SESSION_INDEX_HINT
        assert kwargs["allow_fallback"] is False
        selected = kwargs["selector"]["session_id_hash"]["$in"]
        assert len(selected) == 25
        assert set(selected) == set(hashes)


def test_child_pass_forces_session_first_index_including_bodyless_verification() -> None:
    inner = InMemoryCouchDBSourceStore()
    sids = [_seed_session(inner, f"hint-{index}") for index in range(25)]
    bundle_id = dm.tool_evidence_bundle_doc_id(sids[0], 0)
    inner._docs[bundle_id] = {
        "_id": bundle_id,
        "doc_type": dm.SourceDocType.TOOL_EVIDENCE_BUNDLE,
        "session_id_hash": sids[0],
        "provider": PROVIDER,
        "project": PROJECT,
    }
    store = _RecordingStore(inner)

    hashes = sr.iter_legacy_source_hashes(store, max_sessions=25)

    assert len(hashes) == 25
    discovery, *children = store.queries
    assert discovery[0] == dm.SourceDocType.TRANSCRIPT_SESSION
    assert discovery[1]["limit"] == 25
    assert discovery[1]["page_size"] == sr.LEGACY_SOURCE_SCAN_PAGE_SIZE
    assert discovery[1]["use_index"] == ["legacy", "by_type"]
    assert discovery[1]["allow_fallback"] is False
    # chunk pass, bundle pass, and bodyless full-document verification pass.
    assert len(children) == 3
    for doc_type, kwargs in children:
        assert doc_type == dm.SourceDocType.TOOL_EVIDENCE_BUNDLE or doc_type == (
            dm.SourceDocType.CONVERSATION_CHUNK
        )
        assert kwargs["page_size"] == sr.LEGACY_SOURCE_SCAN_PAGE_SIZE
        assert kwargs["use_index"] == SESSION_INDEX_HINT
        assert kwargs["allow_fallback"] is False
        assert kwargs["selector"]["session_id_hash"]["$in"] == sorted(sids)


def test_oversized_session_filter_keeps_explicit_by_type_fallback() -> None:
    inner = InMemoryCouchDBSourceStore()
    for index in range(sr.LEGACY_SCAN_SELECTOR_MAX_SESSIONS + 1):
        _seed_session(inner, f"wide-{index}")
    store = _RecordingStore(inner)

    hashes = sr.iter_legacy_source_hashes(
        store, max_sessions=sr.LEGACY_SCAN_SELECTOR_MAX_SESSIONS + 1
    )

    assert len(hashes) == sr.LEGACY_SCAN_SELECTOR_MAX_SESSIONS + 1
    discovery, *children = store.queries
    assert discovery[1]["use_index"] == ["legacy", "by_type"]
    assert children
    for _, kwargs in children:
        assert "session_id_hash" not in kwargs["selector"]
        assert kwargs["use_index"] == ["legacy", "by_type"]
        assert kwargs["allow_fallback"] is False


def test_absent_composite_index_accepts_session_leading_index() -> None:
    inner = InMemoryCouchDBSourceStore()
    _seed_session(inner, "session-only-index")
    store = _RecordingStore(
        inner,
        indexes=[
            _RECORDING_INDEXES[0],
            {
                "name": "mango_session_id_hash_only",
                "ddoc": "design/session2",
                "def": {"fields": [{"session_id_hash": "asc"}]},
            },
        ],
    )

    assert sr.iter_legacy_source_hashes(store)

    _, *children = store.queries
    assert children
    for _, kwargs in children:
        assert kwargs["use_index"] == ["session2", "mango_session_id_hash_only"]
        assert kwargs["allow_fallback"] is False


def test_missing_session_index_fails_closed_without_child_scan() -> None:
    inner = InMemoryCouchDBSourceStore()
    _seed_session(inner, "no-session-index")
    store = _RecordingStore(inner, indexes=[_RECORDING_INDEXES[0]])

    with pytest.raises(sr.SourceRevisionResolutionError, match="session-leading"):
        sr.iter_legacy_source_hashes(store)

    assert len(store.queries) == 1  # discovery only; no unindexed child walk


def test_capped_scan_omits_unverifiable_source_and_full_resolver_fails_closed() -> None:
    inner = InMemoryCouchDBSourceStore()
    good_sid = _seed_session(inner, "good")
    tampered_sid = _seed_session(inner, "tampered", tampered=True)
    store = _RecordingStore(inner)

    hashes = sr.iter_legacy_source_hashes(store, max_sessions=1)

    assert tampered_sid not in hashes
    assert set(hashes) <= {good_sid, tampered_sid}

    # The omitted/tampered session is never silently trusted as current: the
    # selection path resolves it independently and marks it as an error.
    sessions = list(
        _iter_session_revisions(
            inner,
            project=PROJECT,
            provider="",
            legacy_hashes=hashes,
        )
    )
    by_sid = {str(session.get("session_id_hash") or ""): session for session in sessions}
    assert by_sid[tampered_sid].get(couchdb_projection_cli.SELECTION_ERROR_FIELD) or by_sid[
        tampered_sid
    ].get(couchdb_projection_cli.SELECTION_DEFERRED_FIELD)


def test_deadline_error_maps_to_distinct_safe_class_without_raw_paths() -> None:
    diagnostics = extract_safe_error_diagnostics(GraphProjectionDeadlineExceeded())

    assert diagnostics["error_class"] == "GraphProjectionDeadlineExceeded"
    assert diagnostics["reason_code"] == "graph_projection_deadline_exceeded"
    assert "/Users/" not in json.dumps(diagnostics, sort_keys=True)


def test_deadline_failure_keeps_nonzero_exit_code(tmp_path, monkeypatch) -> None:
    from agent_knowledge.llm_brain_core.graph_trigger_cli import run_graph_trigger

    store = InMemoryCouchDBSourceStore()
    _seed_session(store, "deadline-exit")

    monkeypatch.setattr(couchdb_projection_cli, "_build_source_store", lambda **_kwargs: store)

    def _raise_deadline(**_kwargs):
        raise GraphProjectionDeadlineExceeded()

    monkeypatch.setattr(couchdb_projection_cli, "run_couchdb_projection", _raise_deadline)

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        couchdb_url="http://127.0.0.1:5984",
    )

    assert report["status"] == "failed"
    assert report["mutation_performed"] is False
    assert report["step"]["exit_code"] == 1
    assert report["step"]["report"]["error_class"] == "GraphProjectionDeadlineExceeded"
    assert report["step"]["report"]["reason_code"] == "graph_projection_deadline_exceeded"

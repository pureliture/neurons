"""Tests for the couchdb-session-memory-build CLI (build_cli.main).

Uses InMemoryCouchDBSourceStore seeded with synthetic sessions. Tests:
- dry-run: selects pending sessions and performs no projector writes
- live run requires a valid approval file (missing / unapproved -> fail-closed)
- live run with RecordingSessionMemoryProjector projects pending sessions
  and emits the expected JSON report
- idempotent re-run: already-projected sessions are skipped
"""
from __future__ import annotations

import json
import os
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.build_cli import (
    BUILD_CLI_SCHEMA_VERSION,
    _close_if_supported,
    _select_sessions_needing_projection,
    main,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    RecordingSessionMemoryProjector,
    materialize_and_project,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.runtime import (
    session_source_revision_from_couchdb_source,
)
from agent_knowledge.session_memory.transcript_model import (
    TranscriptChunk,
    TranscriptSession,
)


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------

def _build_synthetic_session(
    store: InMemoryCouchDBSourceStore,
    *,
    provider: str,
    project: str,
    raw_id: str,
    body: str = "user asked; assistant answered",
) -> str:
    """Seed one complete session (transcript_session + chunk + coverage). Returns session_id_hash."""
    sid = dm.build_session_id_hash(provider, raw_id)
    session = TranscriptSession(
        session_id_hash=sid,
        provider=provider,
        project=project,
        started_at="2026-06-17T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_00",
        session_id_hash=sid,
        provider=provider,
        project=project,
        turn_start_index=0,
        turn_end_index=0,
        text=body,
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    cov = dm.build_coverage_manifest_document(
        session_id_hash=sid,
        provider=provider,
        project=project,
        conversation_chunk_count=1,
        tool_evidence_bundle_count=0,
        conversation_content_hashes=[chunk_doc["content_hash"]],
        tool_evidence_coverage_hashes=[],
        conversation_revision_tokens=[
            dm.build_source_revision_token(
                chunk_doc,
                material_hash_field="content_hash",
            )
        ],
        observed_at_start=session.started_at,
        observed_at_end=session.started_at,
        project_authority={"project": project, "ambiguous": False, "eligible_for_retirement": True},
    )
    store.put(cov)
    return sid


def _mark_projected(store: InMemoryCouchDBSourceStore, sid: str, provider: str, project: str) -> None:
    """Mark a session as already projected (PROJECTED status)."""
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid,
        source_store=store,
    )
    state = dm.build_projection_state_document(
        session_id_hash=sid,
        provider=provider,
        project=project,
        projection_status=dm.ProjectionStatus.PROJECTED,
        session_memory_knowledge_id="index-ref-fake",
        active_content_hash="sha256:" + "a" * 64,
    )
    state["source_hash"] = source_hash
    state["projected_source_hash"] = source_hash
    store.put(state)


def _write_approval(tmp_path: Path, *, argv: list[str]) -> Path:
    payload = {
        "schema_version": "agent_knowledge_live_approval.v1",
        "operation": "couchdb_session_memory_build",
        "operator_approval": {"approved": True},
        "redaction_required": True,
        "rollback_or_abort_criteria": ["abort on error"],
        "timeout_seconds": 60,
        "command": {"argv": argv},
    }
    p = tmp_path / "approval.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class CountingGetStore(InMemoryCouchDBSourceStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_count = 0

    def get(self, doc_id: str) -> dict | None:
        self.get_count += 1
        return super().get(doc_id)


class RecordingSelectionStore(InMemoryCouchDBSourceStore):
    def __init__(self) -> None:
        super().__init__()
        self.find_calls: list[dict] = []
        self.iter_calls: list[dict] = []
        self.iter_yield_counts: dict[str, int] = {}

    def find_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
    ) -> list[dict]:
        self._record_call(self.find_calls, doc_type, fields, selector, limit, page_size)
        docs = self._matching_docs(doc_type, selector=selector, fields=fields)
        return docs[:limit] if limit > 0 else docs

    def iter_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
    ):
        self._record_call(self.iter_calls, doc_type, fields, selector, limit, page_size)
        yielded = 0
        for doc in self._matching_docs(doc_type, selector=selector, fields=fields):
            if limit > 0 and yielded >= limit:
                break
            yielded += 1
            self.iter_yield_counts[doc_type] = self.iter_yield_counts.get(doc_type, 0) + 1
            yield doc

    def _matching_docs(
        self,
        doc_type: str,
        *,
        selector: dict | None = None,
        fields: list[str] | None = None,
    ) -> list[dict]:
        selector = selector or {}
        docs: list[dict] = []
        for doc in sorted(self._docs.values(), key=lambda item: str(item.get("_id") or "")):
            if doc.get("doc_type") != doc_type:
                continue
            if any(doc.get(key) != value for key, value in selector.items()):
                continue
            docs.append({field: doc.get(field) for field in fields} if fields else dict(doc))
        return docs

    @staticmethod
    def _record_call(
        calls: list[dict],
        doc_type: str,
        fields: list[str] | None,
        selector: dict | None,
        limit: int,
        page_size: int,
    ) -> None:
        calls.append(
            {
                "doc_type": doc_type,
                "fields": fields,
                "selector": selector,
                "limit": limit,
                "page_size": page_size,
            }
        )


# ---------------------------------------------------------------------------
# Helper that runs main() with an InMemoryCouchDBSourceStore
# ---------------------------------------------------------------------------

def _run_main(
    argv: list[str],
    store: InMemoryCouchDBSourceStore,
    *,
    projector=None,
    extra_env: dict | None = None,
) -> tuple[int, dict]:
    """Run main() with the InMemoryStore injected; capture stdout JSON."""
    env = {
        "COUCHDB_URL": "http://localhost:5984",
        "COUCHDB_USER": "admin",
        "COUCHDB_PASSWORD": "secret",
        "COUCHDB_DB": "transcript_source",
        "RETIRED_INDEX_BRIDGE_URL": "http://localhost:9380",
        "RETIRED_INDEX_BRIDGE_API_KEY": "test-token",
    }
    if extra_env:
        env.update(extra_env)

    buf = StringIO()

    # Patch the store creation AND the projector (optional)
    def _fake_store_factory(*a, **kw):
        return store

    patches: list = [
        patch("agent_knowledge.couchdb_source.build_cli.CouchDBHttpSourceStore", _fake_store_factory),
        patch.dict(os.environ, env),
        patch("sys.stdout", buf),
    ]
    if projector is not None:
        patches.append(
            patch("agent_knowledge.couchdb_source.build_cli.RetiredIndexBridgeSessionMemoryProjector", lambda **kw: projector)
        )

    with __builtins_context(*patches):
        rc = main(argv)

    output = buf.getvalue().strip()
    report = json.loads(output) if output else {}
    return rc, report


class __builtins_context:
    """Context manager that applies a list of patch objects."""
    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.__enter__()
        return self

    def __exit__(self, *args):
        for p in reversed(self._patches):
            p.__exit__(*args)


# ---------------------------------------------------------------------------
# Simpler helper using contextlib.ExitStack
# ---------------------------------------------------------------------------

import contextlib


def _run(argv: list[str], store: InMemoryCouchDBSourceStore, *, projector=None, extra_env=None) -> tuple[int, dict]:
    """Run main() with an InMemoryCouchDBSourceStore injected.

    build_cli.py uses local imports inside main(), so we patch at the source module
    level. The local import ``from .couchdb_http_store import CouchDBHttpSourceStore``
    resolves from ``agent_knowledge.couchdb_source.couchdb_http_store``.
    """
    env = {
        "COUCHDB_URL": "http://localhost:5984",
        "COUCHDB_USER": "admin",
        "COUCHDB_PASSWORD": "secret",
        "COUCHDB_DB": "transcript_source",
        "RETIRED_INDEX_BRIDGE_URL": "http://localhost:9380",
        "RETIRED_INDEX_BRIDGE_API_KEY": "test-token",
    }
    if extra_env:
        env.update(extra_env)

    buf = StringIO()

    # Fake CouchDBHttpSourceStore that ignores constructor args and returns our in-memory store
    class _FakeHttpStore:
        def __init__(self, *a, **kw):
            pass

        def find_by_type(self, *a, **kw):
            return store.find_by_type(*a, **kw)

        def iter_by_type(self, *a, **kw):
            return store.iter_by_type(*a, **kw)

        def get(self, *a, **kw):
            return store.get(*a, **kw)

        def put(self, *a, **kw):
            return store.put(*a, **kw)

        def put_if_revision(self, *a, **kw):
            return store.put_if_revision(*a, **kw)

        def merge_transcript_session_aggregate(self, *a, **kw):
            return store.merge_transcript_session_aggregate(*a, **kw)

        def find_by_session(self, *a, **kw):
            return store.find_by_session(*a, **kw)

        def delete(self, *a, **kw):
            return store.delete(*a, **kw)

    with contextlib.ExitStack() as stack:
        # Patch the class in its home module so the local import resolves to our fake
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore", _FakeHttpStore)
        )
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(patch("sys.stdout", buf))
        if projector is not None:
            # Patch at the source module (index_projector), not in build_cli which imports it
            stack.enter_context(
                patch(
                    "agent_knowledge.couchdb_source.index_projector.RetiredIndexBridgeSessionMemoryProjector",
                    lambda **kw: projector,
                )
            )
        rc = main(argv)

    output = buf.getvalue().strip()
    report = json.loads(output) if output else {}
    return rc, report


# ---------------------------------------------------------------------------
# session selection helper tests
# ---------------------------------------------------------------------------

class TestSelectSessionsNeedingProjection:
    def test_returns_sessions_without_projection_state(self) -> None:
        store = InMemoryCouchDBSourceStore()
        sid1 = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        sid2 = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s2")

        selected = _select_sessions_needing_projection(store, limit=0)
        sids = {s.get("session_id_hash") for s in selected}
        assert sid1 in sids
        assert sid2 in sids

    def test_excludes_already_projected(self) -> None:
        store = InMemoryCouchDBSourceStore()
        sid1 = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        sid2 = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s2")
        _mark_projected(store, sid1, "claude", "neurons")

        selected = _select_sessions_needing_projection(store, limit=0)
        sids = {s.get("session_id_hash") for s in selected}
        assert sid1 not in sids
        assert sid2 in sids

    def test_reselects_projected_session_when_source_changes_but_state_is_untouched(self) -> None:
        store = InMemoryCouchDBSourceStore()
        sid = _build_synthetic_session(
            store, provider="claude", project="neurons", raw_id="source-changed"
        )
        _mark_projected(store, sid, "claude", "neurons")
        state_id = dm.projection_state_doc_id(sid)
        state_before = store.get(state_id)

        distinct_chunk = TranscriptChunk.from_text(
            chunk_id="chunk_01",
            session_id_hash=sid,
            provider="claude",
            project="neurons",
            turn_start_index=1,
            turn_end_index=1,
            text="a distinct follow-up was observed",
        )
        store.put(dm.build_conversation_chunk_document(chunk=distinct_chunk))

        assert store.get(state_id) == state_before

        selected = _select_sessions_needing_projection(store, limit=0)

        assert [row["session_id_hash"] for row in selected] == [sid]
        raw_source_hash = session_source_revision_from_couchdb_source(
            session_id_hash=sid,
            source_store=store,
        )

        materialize_and_project(
            session_id_hash=sid,
            store=store,
            projector=RecordingSessionMemoryProjector(),
        )

        coverage = store.get(dm.coverage_manifest_doc_id(sid))
        projected = store.get(dm.projection_state_doc_id(sid))
        assert coverage is not None
        assert projected is not None
        assert coverage["conversation_chunk_count"] == 2
        assert coverage["source_hash"] == raw_source_hash
        assert projected["projected_source_hash"] == raw_source_hash
        assert _select_sessions_needing_projection(store, limit=0) == []

    def test_exact_duplicate_source_keeps_projected_session_skipped(self) -> None:
        store = InMemoryCouchDBSourceStore()
        sid = _build_synthetic_session(
            store, provider="claude", project="neurons", raw_id="exact-duplicate"
        )
        _mark_projected(store, sid, "claude", "neurons")

        duplicate = TranscriptChunk.from_text(
            chunk_id="chunk_00",
            session_id_hash=sid,
            provider="claude",
            project="neurons",
            turn_start_index=0,
            turn_end_index=0,
            text="user asked; assistant answered",
        )
        stored = store.put(dm.build_conversation_chunk_document(chunk=duplicate))

        assert stored.outcome == "duplicate"
        assert _select_sessions_needing_projection(store, limit=0) == []

    def test_ignores_stale_state_source_hash_when_source_matches_projected_hash(self) -> None:
        store = InMemoryCouchDBSourceStore()
        sid = _build_synthetic_session(
            store, provider="claude", project="neurons", raw_id="state-only-drift"
        )
        _mark_projected(store, sid, "claude", "neurons")
        state_id = dm.projection_state_doc_id(sid)
        state = dict(store.get(state_id) or {})
        state["source_hash"] = dm.sha256_hash("stale state-only source revision")
        store.put(state)

        assert _select_sessions_needing_projection(store, limit=0) == []

    def test_reselects_legacy_projected_session_without_matching_source_hash(self) -> None:
        store = InMemoryCouchDBSourceStore()
        sid = _build_synthetic_session(
            store, provider="claude", project="neurons", raw_id="legacy-unknown-source"
        )
        legacy = dm.build_projection_state_document(
            session_id_hash=sid,
            provider="claude",
            project="neurons",
            projection_status=dm.ProjectionStatus.PROJECTED,
            session_memory_knowledge_id="index-ref-fake",
            active_content_hash="sha256:" + "a" * 64,
        )
        store.put(legacy)

        selected = _select_sessions_needing_projection(store, limit=0)

        assert [row["session_id_hash"] for row in selected] == [sid]

    def test_ignores_projected_state_with_non_authoritative_id(self) -> None:
        store = InMemoryCouchDBSourceStore()
        sid = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        stale_state = dm.build_projection_state_document(
            session_id_hash=sid,
            provider="claude",
            project="neurons",
            projection_status=dm.ProjectionStatus.PROJECTED,
            session_memory_knowledge_id="index-ref-fake",
            active_content_hash="sha256:" + "a" * 64,
        )
        stale_state["_id"] = "projection_state:legacy-stale-id"
        store.put(stale_state)

        selected = _select_sessions_needing_projection(store, limit=0)

        assert [s.get("session_id_hash") for s in selected] == [sid]

    def test_limit_caps_selection(self) -> None:
        store = InMemoryCouchDBSourceStore()
        for i in range(5):
            _build_synthetic_session(store, provider="claude", project="neurons", raw_id=f"s{i}")

        selected = _select_sessions_needing_projection(store, limit=2)
        assert len(selected) == 2

    def test_limit_zero_means_no_cap(self) -> None:
        store = InMemoryCouchDBSourceStore()
        for i in range(4):
            _build_synthetic_session(store, provider="claude", project="neurons", raw_id=f"s{i}")

        selected = _select_sessions_needing_projection(store, limit=0)
        assert len(selected) == 4

    def test_scopes_selection_by_project_and_provider(self) -> None:
        store = InMemoryCouchDBSourceStore()
        a = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="a")
        b = _build_synthetic_session(store, provider="codex", project="neurons", raw_id="b")
        c = _build_synthetic_session(store, provider="claude", project="dendrite", raw_id="c")

        by_project = {
            s.get("session_id_hash")
            for s in _select_sessions_needing_projection(store, limit=0, project="neurons")
        }
        assert by_project == {a, b}

        by_provider = {
            s.get("session_id_hash")
            for s in _select_sessions_needing_projection(store, limit=0, provider="claude")
        }
        assert by_provider == {a, c}

        scoped = {
            s.get("session_id_hash")
            for s in _select_sessions_needing_projection(
                store, limit=0, project="neurons", provider="claude"
            )
        }
        assert scoped == {a}

    def test_selection_does_not_get_each_projected_session(self) -> None:
        store = CountingGetStore()
        for i in range(20):
            sid = _build_synthetic_session(store, provider="claude", project="neurons", raw_id=f"done-{i}")
            _mark_projected(store, sid, "claude", "neurons")
        pending = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="pending")
        store.get_count = 0

        selected = _select_sessions_needing_projection(store, limit=1)

        assert [s.get("session_id_hash") for s in selected] == [pending]
        assert store.get_count == 0

    def test_pushes_projected_status_and_scope_to_store_selectors(self) -> None:
        store = RecordingSelectionStore()
        pending = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="pending")
        other_project = _build_synthetic_session(store, provider="claude", project="dendrite", raw_id="other")
        projected = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="projected")
        _mark_projected(store, projected, "claude", "neurons")
        _mark_projected(store, other_project, "claude", "dendrite")

        selected = _select_sessions_needing_projection(
            store,
            limit=0,
            project="neurons",
            provider="claude",
        )

        assert [s.get("session_id_hash") for s in selected] == [pending]
        state_call = next(
            call for call in store.find_calls if call["doc_type"] == dm.SourceDocType.PROJECTION_STATE
        )
        assert state_call["selector"] == {
            "projection_status": dm.ProjectionStatus.PROJECTED,
            "project": "neurons",
            "provider": "claude",
        }
        session_call = next(
            call for call in store.iter_calls if call["doc_type"] == dm.SourceDocType.TRANSCRIPT_SESSION
        )
        assert session_call["selector"] == {"project": "neurons", "provider": "claude"}

    def test_limit_stops_session_iteration_before_full_corpus(self) -> None:
        store = RecordingSelectionStore()
        for i in range(20):
            _build_synthetic_session(store, provider="claude", project="neurons", raw_id=f"pending-{i:02d}")

        selected = _select_sessions_needing_projection(store, limit=2)

        assert len(selected) == 2
        assert store.iter_yield_counts[dm.SourceDocType.TRANSCRIPT_SESSION] == 2
        session_call = next(
            call for call in store.iter_calls if call["doc_type"] == dm.SourceDocType.TRANSCRIPT_SESSION
        )
        assert session_call["page_size"] == 2

    def test_in_memory_selection_uses_deterministic_id_order(self) -> None:
        store = InMemoryCouchDBSourceStore()
        second = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="b")
        first = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="a")

        selected = _select_sessions_needing_projection(store, limit=1)

        assert [s.get("session_id_hash") for s in selected] == sorted([first, second])[:1]


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_selects_pending_sessions(self) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s2")

        rc, report = _run(["--dry-run"], store)

        assert rc == 0
        assert report["schema_version"] == BUILD_CLI_SCHEMA_VERSION
        assert report["dry_run"] is True
        assert report["selected"] == 2
        assert report["projected"] == 0

    def test_dry_run_performs_no_writes(self) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")

        projector = RecordingSessionMemoryProjector()
        rc, report = _run(["--dry-run"], store, projector=projector)

        assert rc == 0
        assert projector.calls == []  # never called

    def test_dry_run_does_not_require_approval(self) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")

        # No --approval flag at all -> dry-run should succeed
        rc, report = _run(["--dry-run"], store)
        assert rc == 0

    def test_dry_run_with_limit(self) -> None:
        store = InMemoryCouchDBSourceStore()
        for i in range(4):
            _build_synthetic_session(store, provider="claude", project="neurons", raw_id=f"s{i}")

        rc, report = _run(["--dry-run", "--limit", "2"], store)
        assert rc == 0
        assert report["selected"] == 2

    def test_dry_run_empty_store(self) -> None:
        store = InMemoryCouchDBSourceStore()
        rc, report = _run(["--dry-run"], store)
        assert rc == 0
        assert report["selected"] == 0

    def test_dry_run_skips_already_projected(self) -> None:
        store = InMemoryCouchDBSourceStore()
        sid1 = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        _mark_projected(store, sid1, "claude", "neurons")
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s2")

        rc, report = _run(["--dry-run"], store)
        assert rc == 0
        assert report["selected"] == 1  # only s2 is pending


# ---------------------------------------------------------------------------
# Approval gate tests (live run)
# ---------------------------------------------------------------------------

class TestApprovalGate:
    def test_live_run_without_approval_fails_closed(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")

        # No --approval arg
        rc, report = _run([], store)
        assert rc == 2
        assert report.get("error") == "approval_rejected"

    def test_live_run_with_missing_approval_file_fails(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")

        rc, report = _run(["--approval", str(tmp_path / "nonexistent.json")], store)
        assert rc == 2
        assert "approval_rejected" == report.get("error")

    def test_live_run_with_unapproved_payload_fails(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        argv = ["--approval", str(tmp_path / "approval.json")]

        payload = {
            "schema_version": "agent_knowledge_live_approval.v1",
            "operation": "couchdb_session_memory_build",
            "operator_approval": {"approved": False},  # not approved
            "redaction_required": True,
            "rollback_or_abort_criteria": ["abort on error"],
            "timeout_seconds": 60,
            "command": {"argv": argv},
        }
        (tmp_path / "approval.json").write_text(json.dumps(payload), encoding="utf-8")

        rc, report = _run(argv, store)
        assert rc == 2
        assert report.get("error") == "approval_rejected"

    def test_live_run_with_wrong_operation_fails(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        argv = ["--approval", str(tmp_path / "approval.json")]

        payload = {
            "schema_version": "agent_knowledge_live_approval.v1",
            "operation": "wrong_operation",  # mismatch
            "operator_approval": {"approved": True},
            "redaction_required": True,
            "rollback_or_abort_criteria": ["abort on error"],
            "timeout_seconds": 60,
            "command": {"argv": argv},
        }
        (tmp_path / "approval.json").write_text(json.dumps(payload), encoding="utf-8")

        rc, report = _run(argv, store)
        assert rc == 2
        assert report.get("error") == "approval_rejected"

    def test_live_run_with_argv_mismatch_fails(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        actual_argv = ["--approval", str(tmp_path / "approval.json")]
        # approval contains different argv
        wrong_argv = ["--approval", str(tmp_path / "approval.json"), "--extra"]

        payload = {
            "schema_version": "agent_knowledge_live_approval.v1",
            "operation": "couchdb_session_memory_build",
            "operator_approval": {"approved": True},
            "redaction_required": True,
            "rollback_or_abort_criteria": ["abort on error"],
            "timeout_seconds": 60,
            "command": {"argv": wrong_argv},  # different from actual
        }
        (tmp_path / "approval.json").write_text(json.dumps(payload), encoding="utf-8")

        rc, report = _run(actual_argv, store)
        assert rc == 2
        assert report.get("error") == "approval_rejected"


# ---------------------------------------------------------------------------
# Live run tests (with valid approval)
# ---------------------------------------------------------------------------

class TestLiveRun:
    def test_live_run_projects_pending_sessions(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s2")

        approval_path = tmp_path / "approval.json"
        argv = ["--approval", str(approval_path)]
        approval_payload = {
            "schema_version": "agent_knowledge_live_approval.v1",
            "operation": "couchdb_session_memory_build",
            "operator_approval": {"approved": True},
            "redaction_required": True,
            "rollback_or_abort_criteria": ["abort on error"],
            "timeout_seconds": 60,
            "command": {"argv": argv},
        }
        approval_path.write_text(json.dumps(approval_payload), encoding="utf-8")

        projector = RecordingSessionMemoryProjector()
        rc, report = _run(argv, store, projector=projector)

        assert rc == 0
        assert report["schema_version"] == BUILD_CLI_SCHEMA_VERSION
        assert report["dry_run"] is False
        assert report["selected"] == 2
        assert report["projected"] == 2
        assert report["failed"] == 0
        assert len(projector.calls) == 2

    def test_live_run_emits_valid_json_report(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="codex", project="openclaw", raw_id="s1")

        approval_path = tmp_path / "approval.json"
        argv = ["--approval", str(approval_path)]
        approval_payload = {
            "schema_version": "agent_knowledge_live_approval.v1",
            "operation": "couchdb_session_memory_build",
            "operator_approval": {"approved": True},
            "redaction_required": True,
            "rollback_or_abort_criteria": ["abort on error"],
            "timeout_seconds": 60,
            "command": {"argv": argv},
        }
        approval_path.write_text(json.dumps(approval_payload), encoding="utf-8")

        projector = RecordingSessionMemoryProjector()
        rc, report = _run(argv, store, projector=projector)

        # Report must have all required keys
        for key in ("schema_version", "dry_run", "selected", "projected", "failed", "skipped"):
            assert key in report, f"missing key: {key}"

    def test_live_run_records_projection_state_in_store(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        sid = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="sess-a")

        approval_path = tmp_path / "approval.json"
        argv = ["--approval", str(approval_path)]
        approval_payload = {
            "schema_version": "agent_knowledge_live_approval.v1",
            "operation": "couchdb_session_memory_build",
            "operator_approval": {"approved": True},
            "redaction_required": True,
            "rollback_or_abort_criteria": ["abort on error"],
            "timeout_seconds": 60,
            "command": {"argv": argv},
        }
        approval_path.write_text(json.dumps(approval_payload), encoding="utf-8")

        projector = RecordingSessionMemoryProjector()
        rc, _ = _run(argv, store, projector=projector)
        assert rc == 0

        state = store.get(dm.projection_state_doc_id(sid))
        assert state is not None
        assert state["projection_status"] == dm.ProjectionStatus.PROJECTED

    def test_live_run_projector_target_profile_is_session_memory(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")

        approval_path = tmp_path / "approval.json"
        argv = ["--approval", str(approval_path)]
        approval_payload = {
            "schema_version": "agent_knowledge_live_approval.v1",
            "operation": "couchdb_session_memory_build",
            "operator_approval": {"approved": True},
            "redaction_required": True,
            "rollback_or_abort_criteria": ["abort on error"],
            "timeout_seconds": 60,
            "command": {"argv": argv},
        }
        approval_path.write_text(json.dumps(approval_payload), encoding="utf-8")

        projector = RecordingSessionMemoryProjector()
        _run(argv, store, projector=projector)

        assert projector.calls[0]["target_profile"] == dm.RETIRED_INDEX_BRIDGE_RECALL_PROFILE


class TestProjectorCleanup:
    def test_close_if_supported_calls_close_once(self) -> None:
        calls = []

        class _Closable:
            def close(self):
                calls.append("closed")

        _close_if_supported(_Closable())

        assert calls == ["closed"]

    def test_close_if_supported_ignores_missing_or_failing_close(self) -> None:
        class _Failing:
            def close(self):
                raise RuntimeError("close failed")

        _close_if_supported(object())
        _close_if_supported(_Failing())


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_already_projected_sessions_skipped_on_rerun(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        sid1 = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s2")

        # First run: projects both
        approval_path = tmp_path / "approval.json"
        argv = ["--approval", str(approval_path)]
        approval_payload = {
            "schema_version": "agent_knowledge_live_approval.v1",
            "operation": "couchdb_session_memory_build",
            "operator_approval": {"approved": True},
            "redaction_required": True,
            "rollback_or_abort_criteria": ["abort on error"],
            "timeout_seconds": 60,
            "command": {"argv": argv},
        }
        approval_path.write_text(json.dumps(approval_payload), encoding="utf-8")

        projector1 = RecordingSessionMemoryProjector()
        rc, report1 = _run(argv, store, projector=projector1)
        assert rc == 0
        assert report1["projected"] == 2
        assert len(projector1.calls) == 2

        # Second run: both already PROJECTED -> none selected
        projector2 = RecordingSessionMemoryProjector()
        rc, report2 = _run(argv, store, projector=projector2)
        assert rc == 0
        assert report2["selected"] == 0
        assert report2["projected"] == 0
        assert len(projector2.calls) == 0

    def test_partial_projection_reruns_only_pending(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        sid1 = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s2")

        # Manually mark only s1 as projected
        _mark_projected(store, sid1, "claude", "neurons")

        approval_path = tmp_path / "approval.json"
        argv = ["--approval", str(approval_path)]
        approval_payload = {
            "schema_version": "agent_knowledge_live_approval.v1",
            "operation": "couchdb_session_memory_build",
            "operator_approval": {"approved": True},
            "redaction_required": True,
            "rollback_or_abort_criteria": ["abort on error"],
            "timeout_seconds": 60,
            "command": {"argv": argv},
        }
        approval_path.write_text(json.dumps(approval_payload), encoding="utf-8")

        projector = RecordingSessionMemoryProjector()
        rc, report = _run(argv, store, projector=projector)

        assert rc == 0
        assert report["selected"] == 1  # only s2 pending
        assert report["projected"] == 1
        assert len(projector.calls) == 1


# ---------------------------------------------------------------------------
# Report redaction: no raw ids/paths/bodies in output
# ---------------------------------------------------------------------------

class TestReportRedaction:
    def test_report_does_not_contain_raw_session_id_hash(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        sid = _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")

        rc, report = _run(["--dry-run"], store)

        output_str = json.dumps(report)
        # The raw sha256 hex portion of the session_id_hash should not appear in the report
        # (report only contains counts)
        assert sid not in output_str

    def test_report_keys_are_only_counts_and_schema(self, tmp_path) -> None:
        store = InMemoryCouchDBSourceStore()
        _build_synthetic_session(store, provider="claude", project="neurons", raw_id="s1")

        rc, report = _run(["--dry-run"], store)

        allowed_keys = {"schema_version", "dry_run", "selected", "projected", "failed", "skipped"}
        assert set(report.keys()) <= allowed_keys

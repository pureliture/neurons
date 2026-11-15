"""Selector/entrypoint lifecycle for terminal local dead letters (synthetic stores)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.build_cli import (
    ProjectionDeadLetterLookupError,
    _select_sessions_needing_projection,
    main,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_session_memory,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.runtime import (
    session_source_revision_from_couchdb_source,
)
from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.rag_ingress.pg_embedding_chunks import (
    LOCAL_EMBEDDING_DEAD_LETTER,
    PG_INPUT_POLICY_VERSION,
)
from agent_knowledge.session_memory.transcript_model import (
    TranscriptChunk,
    TranscriptSession,
)


def _seed_session(store: InMemoryCouchDBSourceStore, *, raw_id: str) -> str:
    provider, project = "claude", "neurons"
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
        text="user asked; assistant answered",
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider=provider,
            project=project,
            conversation_chunk_count=1,
            tool_evidence_bundle_count=0,
            conversation_content_hashes=[chunk_doc["content_hash"]],
            tool_evidence_coverage_hashes=[],
            conversation_revision_tokens=[
                dm.build_source_revision_token(
                    chunk_doc, material_hash_field="content_hash"
                )
            ],
            observed_at_start=session.started_at,
            observed_at_end=session.started_at,
            project_authority={
                "project": project,
                "ambiguous": False,
                "eligible_for_retirement": True,
            },
        )
    )
    return sid


def _put_terminal_state(
    store: InMemoryCouchDBSourceStore,
    sid: str,
    *,
    source_hash: str,
    reason: str = LOCAL_EMBEDDING_DEAD_LETTER,
    status: str = dm.ProjectionStatus.FAILED,
) -> None:
    state = dm.build_projection_state_document(
        session_id_hash=sid,
        provider="claude",
        project="neurons",
        projection_status=dm.ProjectionStatus.PENDING,
    )
    state["source_hash"] = source_hash
    state["backend_receipts"] = {}
    state["projection_attempt"] = {
        "postgres_pgvector_local": {
            "status": status,
            "reason": reason,
            "source_hash": source_hash,
            "recorded_at": "2026-11-15T00:00:00+00:00",
            "policy_version": PG_INPUT_POLICY_VERSION,
        }
    }
    if status == dm.ProjectionStatus.FAILED:
        state["failure_source_hash"] = source_hash
    store.put(state)


class _Lookup:
    def __init__(self, blocked: set[str] | None = None, error: Exception | None = None):
        self.blocked = set(blocked or ())
        self.error = error
        self.calls: list[dict] = []

    def has_dead_letter_local_outbox_for_session(self, *, session_id_hash, profile_id):
        self.calls.append(
            {"session_id_hash": session_id_hash, "profile_id": profile_id}
        )
        if self.error is not None:
            raise self.error
        return session_id_hash in self.blocked


def _select(store, lookup):
    return _select_sessions_needing_projection(
        store,
        limit=0,
        backend="postgres_pgvector",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        representation="session_v1",
        dead_letter_lookup=lookup,
    )


def test_terminal_dead_letter_suppresses_until_requeued_and_allows_new_source():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store, raw_id="terminal-suppress")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store
    )
    _put_terminal_state(store, sid, source_hash=source_hash)
    lookup = _Lookup(blocked={sid})

    assert _select(store, lookup) == []

    # Operator requeues/completes the dead letter: no dead_letter remains.
    lookup.blocked.clear()
    selected = _select(store, lookup)
    assert [row["session_id_hash"] for row in selected] == [sid]

    # A failure recorded for a different source must never suppress the new one.
    lookup.blocked.add(sid)
    _put_terminal_state(store, sid, source_hash="sha256:" + "0" * 64)
    selected = _select(store, lookup)
    assert [row["session_id_hash"] for row in selected] == [sid]


def test_lookup_error_is_explicit_fail_closed():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store, raw_id="terminal-error")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store
    )
    _put_terminal_state(store, sid, source_hash=source_hash)
    lookup = _Lookup(blocked={sid}, error=RuntimeError("db down"))

    with pytest.raises(ProjectionDeadLetterLookupError):
        _select(store, lookup)


def test_missing_lookup_is_explicit_fail_closed():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store, raw_id="terminal-missing-lookup")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store
    )
    _put_terminal_state(store, sid, source_hash=source_hash)

    with pytest.raises(ProjectionDeadLetterLookupError):
        _select(store, None)


def test_no_receipt_session_is_selected():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store, raw_id="no-receipt")

    selected = _select(store, _Lookup())

    assert [row["session_id_hash"] for row in selected] == [sid]


def test_projected_attempt_does_not_block_even_with_stale_dead_letter():
    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store, raw_id="projected-success")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store
    )
    _put_terminal_state(
        store,
        sid,
        source_hash=source_hash,
        status=dm.ProjectionStatus.PROJECTED,
    )
    state = store.get(dm.projection_state_doc_id(sid))
    state["backend_receipts"] = {
        "postgres_pgvector_local": {
            LOCAL_EMBEDDING_PROFILE_ID: {
                "projection_status": dm.ProjectionStatus.PROJECTED,
                "projected_source_hash": source_hash,
                "active_content_hash": "sha256:" + "a" * 64,
                "session_memory_knowledge_id": "ref",
                "provider": "claude",
                "project": "neurons",
                "receipt_version": 3,
            }
        }
    }
    store.put(state)

    assert _select(store, _Lookup(blocked={sid})) == []


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.conn.queries.append((sql, params))

    def fetchone(self):
        return (self.conn.result,)


class _Conn:
    def __init__(self, result: bool):
        self.result = result
        self.queries: list[tuple[str, tuple]] = []

    def cursor(self):
        return _Cursor(self)


def test_store_lookup_contract_includes_latest_row_guard():
    conn = _Conn(True)
    store = PgVectorStore(connection=conn)

    assert store.has_dead_letter_local_outbox_for_session(
        session_id_hash="sha256:" + "1" * 64,
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
    )
    sql, params = conn.queries[-1]
    assert "session_memory_chunks" in sql
    assert "q.status = 'dead_letter'" in sql
    assert "n.outbox_id > q.outbox_id" in sql
    assert params == (LOCAL_EMBEDDING_PROFILE_ID, "sha256:" + "1" * 64)

    assert store.has_dead_letter_local_outbox(
        target_type="session_chunk",
        target_id="a" * 64,
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        source_content_hash="sha256:" + "2" * 64,
        input_hash="sha256:" + "3" * 64,
    )
    sql, params = conn.queries[-1]
    assert "n.outbox_id > q.outbox_id" in sql
    assert params[:7] == (
        "session_chunk",
        "a" * 64,
        LOCAL_EMBEDDING_PROFILE_ID,
        "sha256:" + "2" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        "sha256:" + "3" * 64,
    )


class _SuccessProjector:
    embedding_profile = LOCAL_EMBEDDING_PROFILE_ID

    def project(self, *, target_profile, document):
        return "local-ref"

    def close(self):
        return None


def _run_main(argv, store, lookup, monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore",
        lambda **kwargs: store,
    )
    monkeypatch.setattr(build_cli, "_build_pg_dead_letter_lookup", lambda environ: lookup)
    monkeypatch.setenv("COUCHDB_URL", "http://fake")
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_MODE", "full")
    monkeypatch.setenv(
        "SESSION_MEMORY_SELECTION_CHECKPOINT", str(tmp_path / "checkpoint.json")
    )
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(argv)
    return rc, json.loads(out.getvalue().strip().splitlines()[-1])


def test_public_entrypoint_suppresses_then_projects_after_requeue(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_MODEL", "local-model2vec-256-v1")
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_DIM", "256")
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_PROVIDER", "model2vec")
    monkeypatch.setenv(
        "LLM_BRAIN_EMBEDDING_PROFILE_ID", LOCAL_EMBEDDING_PROFILE_ID
    )
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_BASE_URL", "http://127.0.0.1:1/v1")

    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store, raw_id="entrypoint")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store
    )
    _put_terminal_state(store, sid, source_hash=source_hash)
    lookup = _Lookup(blocked={sid})

    rc, report = _run_main(["--dry-run"], store, lookup, monkeypatch, tmp_path)
    assert rc == 0 and report["selected"] == 0

    # Requeue observed: same source becomes selectable.
    lookup.blocked.clear()
    rc, report = _run_main(["--dry-run"], store, lookup, monkeypatch, tmp_path)
    assert rc == 0 and report["selected"] == 1

    # Live run: selection -> real materializer -> fake projector success.
    monkeypatch.setattr(
        "agent_knowledge.session_memory.native_memory_sync_approval."
        "validate_memory_enqueue_approval",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(build_cli, "_build_pg_projector", lambda environ: _SuccessProjector())
    rc, report = _run_main(["--approval", "dummy"], store, lookup, monkeypatch, tmp_path)
    assert rc == 0
    assert report["selected"] == 1 and report["projected"] == 1

    # Success updated the attempt/receipt; a later stale lookup must not relock.
    lookup.blocked.add(sid)
    rc, report = _run_main(["--dry-run"], store, lookup, monkeypatch, tmp_path)
    assert rc == 0 and report["selected"] == 0


def test_public_entrypoint_lookup_error_aborts_without_advancing_checkpoint(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_MODEL", "local-model2vec-256-v1")
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_DIM", "256")
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_PROVIDER", "model2vec")
    monkeypatch.setenv(
        "LLM_BRAIN_EMBEDDING_PROFILE_ID", LOCAL_EMBEDDING_PROFILE_ID
    )
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_BASE_URL", "http://127.0.0.1:1/v1")

    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store, raw_id="entrypoint-error")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store
    )
    _put_terminal_state(store, sid, source_hash=source_hash)
    lookup = _Lookup(blocked={sid}, error=RuntimeError("db down"))

    rc, report = _run_main(["--dry-run"], store, lookup, monkeypatch, tmp_path)

    assert rc == 2
    assert report["error"] == "dead_letter_lookup_unavailable"
    assert report["selected"] == 0
    assert not (tmp_path / "checkpoint.json").exists()


def test_transient_lookup_error_preserves_existing_pending_checkpoint(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_MODEL", "local-model2vec-256-v1")
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_DIM", "256")
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_PROVIDER", "model2vec")
    monkeypatch.setenv(
        "LLM_BRAIN_EMBEDDING_PROFILE_ID", LOCAL_EMBEDDING_PROFILE_ID
    )
    monkeypatch.setenv("LLM_BRAIN_EMBEDDING_BASE_URL", "http://127.0.0.1:1/v1")

    store = InMemoryCouchDBSourceStore()
    sid = _seed_session(store, raw_id="checkpoint-error")
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store
    )
    _put_terminal_state(store, sid, source_hash=source_hash)
    checkpoint_path = tmp_path / "checkpoint.json"
    original = {
        "schema_version": "session_memory_selection_checkpoint.v1",
        "last_seq": "",
        "pending_session_ids": [sid],
        "pending_session_attempts": {sid: 0},
    }
    checkpoint_path.write_text(json.dumps(original), encoding="utf-8")
    lookup = _Lookup(blocked={sid}, error=RuntimeError("db down"))

    rc, report = _run_main(["--dry-run"], store, lookup, monkeypatch, tmp_path)

    assert rc == 2 and report["error"] == "dead_letter_lookup_unavailable"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8")) == original

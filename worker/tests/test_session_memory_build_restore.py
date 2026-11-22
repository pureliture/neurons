from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.runtime import session_source_revision_from_couchdb_source
from agent_knowledge.public_safe_util import public_safe_mask_body
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

PROJECT = "neurons"
PROVIDER = "claude"
CHUNK_PREFIX = "pg-ready-"


class _FakeProjector:
    embedding_profile = ""

    def project(self, *, target_profile: str, document: dict) -> str:
        del target_profile, document
        return "fake-pg-row"


class _FakePgStore:
    def __init__(self, *, expected_hash: str, raise_on_get: bool = False) -> None:
        self.expected_hash = expected_hash
        self.raise_on_get = raise_on_get
        self.get_calls = 0
        self.identity_calls = 0

    def get_chunk(self, chunk_id: str):
        del chunk_id
        self.get_calls += 1
        if self.raise_on_get:
            raise RuntimeError("simulated pg read failure")
        return SimpleNamespace(
            content_hash=self.expected_hash,
            embedding_state="ready",
        )

    def find_ready_chunk_by_identity(self, **kwargs):
        self.identity_calls += 1
        del kwargs
        return SimpleNamespace(content_hash=self.expected_hash)



class _RaisingPutStore(InMemoryCouchDBSourceStore):
    def put(self, document: dict):
        if document.get("source_hash_repaired_at"):
            raise RuntimeError("simulated couchdb put failure")
        return super().put(document)

def _seed(store: InMemoryCouchDBSourceStore, raw_id: str) -> tuple[str, str]:
    sid = dm.build_session_id_hash(PROVIDER, raw_id)
    session = TranscriptSession(
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        started_at="2026-06-17T00:00:00Z",
    )
    store.put(dm.build_transcript_session_document(session=session))
    chunk = TranscriptChunk.from_text(
        chunk_id="chunk_00",
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        turn_start_index=0,
        turn_end_index=0,
        text=f"body for {raw_id}",
    )
    chunk_doc = dm.build_conversation_chunk_document(chunk=chunk)
    store.put(chunk_doc)
    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider=PROVIDER,
            project=PROJECT,
            conversation_chunk_count=1,
            tool_evidence_bundle_count=0,
            conversation_content_hashes=[chunk_doc["content_hash"]],
            tool_evidence_coverage_hashes=[],
            conversation_revision_tokens=[
                dm.build_source_revision_token(chunk_doc, material_hash_field="content_hash")
            ],
            observed_at_start=session.started_at,
            observed_at_end=session.started_at,
            project_authority={
                "project": PROJECT,
                "ambiguous": False,
                "eligible_for_retirement": True,
            },
        )
    )
    source_hash = session_source_revision_from_couchdb_source(
        session_id_hash=sid, source_store=store
    )
    state = dm.build_projection_state_document(
        session_id_hash=sid,
        provider=PROVIDER,
        project=PROJECT,
        projection_status=dm.ProjectionStatus.PROJECTED,
        projected_source_hash="sha256:" + "b" * 64,
        active_content_hash="sha256:" + "a" * 64,
        source_hash=source_hash,
    )
    state["backend_receipts"] = {
        "postgres_pgvector": {
            "projection_status": dm.ProjectionStatus.PROJECTED,
            "session_memory_knowledge_id": CHUNK_PREFIX + sid[-8:],
        }
    }
    store.put(state)
    active_hash = "sha256:" + "a" * 64
    return sid, active_hash


def _write_checkpoint(path: Path, pending: list[str]) -> None:
    build_cli._write_selection_checkpoint(
        str(path),
        "0",
        pending_session_ids=pending,
        full_walk_in_progress=True,
        embedding_profile="",
        representation="session_v1",
    )


def _write_approval(tmp_path: Path, argv: list[str]) -> None:
    (tmp_path / "approval.json").write_text(
        json.dumps(
            {
                "schema_version": "agent_knowledge_live_approval.v1",
                "operation": "couchdb_session_memory_build",
                "operator_approval": {"approved": True},
                "redaction_required": True,
                "rollback_or_abort_criteria": ["abort on error"],
                "timeout_seconds": 60,
                "command": {"argv": argv},
            }
        ),
        encoding="utf-8",
    )


def _run_build(
    tmp_path: Path,
    store: InMemoryCouchDBSourceStore,
    *,
    pending: list[str],
    pg_store: _FakePgStore,
    limit: int = 50,
) -> tuple[int | None, dict | None, str, BaseException | None]:
    checkpoint = tmp_path / "selection-checkpoint.json"
    _write_checkpoint(checkpoint, pending)
    argv = ["--limit", str(limit), "--approval", str(tmp_path / "approval.json")]
    _write_approval(tmp_path, argv)
    env = {
        "COUCHDB_URL": "http://localhost:5984",
        "COUCHDB_USER": "",
        "COUCHDB_PASSWORD": "",
        "COUCHDB_DB": "transcript_source",
        "SESSION_MEMORY_PROJECTION_BACKEND": "postgres_pgvector",
        "SESSION_MEMORY_SELECTION_MODE": "full",
        "SESSION_MEMORY_SELECTION_CHECKPOINT": str(checkpoint),
    }
    stdout, stderr = io.StringIO(), io.StringIO()
    code = None
    report = None
    error = None
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore",
                lambda **kwargs: store,
            )
        )
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(
            patch("agent_knowledge.couchdb_source.build_cli._build_pg_projector", lambda _e: _FakeProjector())
        )
        stack.enter_context(
            patch(
                "agent_knowledge.couchdb_source.build_cli._build_pg_dead_letter_lookup",
                lambda _e: pg_store,
            )
        )
        stack.enter_context(patch("sys.stdout", stdout))
        stack.enter_context(patch("sys.stderr", stderr))
        try:
            code = build_cli.main(argv)
            report = json.loads(stdout.getvalue().splitlines()[-1])
        except BaseException as exc:  # noqa: BLE001 - reproduction harness
            error = exc
    return code, report, stderr.getvalue(), error


def test_pg_read_error_must_not_kill_child(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    sid, expected_hash = _seed(store, "pg-read-error")
    pg = _FakePgStore(expected_hash=expected_hash, raise_on_get=True)

    code, report, stderr, error = _run_build(
        tmp_path, store, pending=[sid], pg_store=pg
    )

    assert error is None, f"child failed: {type(error).__name__}: {stderr[-2000:]}"
    assert code == 0
    assert report is not None
    assert report["failed"] == 0


def test_pending_fingerprint_changes_after_two_cycles(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    sid, expected_hash = _seed(store, "drain")
    pg = _FakePgStore(expected_hash=expected_hash)
    path = tmp_path / "selection-checkpoint.json"
    _write_checkpoint(path, [sid])
    before = json.loads(path.read_text(encoding="utf-8"))["pending_session_ids"]

    _run_build(tmp_path, store, pending=[sid], pg_store=pg)
    after_first = build_cli._pending_session_ids(
        json.loads(path.read_text(encoding="utf-8"))
    )

    assert before != after_first


def test_pg_lookup_calls_are_bounded_by_selection_limit(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    sids = []
    expected_hash = ""
    for index in range(5):
        sid, expected_hash = _seed(store, f"bounded-{index}")
        sids.append(sid)
    pg = _FakePgStore(expected_hash=expected_hash)

    _, _, stderr, error = _run_build(
        tmp_path, store, pending=sids, pg_store=pg, limit=5
    )

    assert error is None
    assert pg.get_calls <= 5
    assert stderr.count("[selection] ready_check") <= 5
    assert "hash_source=materialized" not in stderr


def test_repair_put_error_must_not_kill_child(tmp_path: Path) -> None:
    store = _RaisingPutStore()
    sid, expected_hash = _seed(store, "put-error")
    pg = _FakePgStore(expected_hash=expected_hash)

    code, report, stderr, error = _run_build(
        tmp_path, store, pending=[sid], pg_store=pg
    )

    assert error is None, f"child failed: {stderr[-2000:]}"
    assert code == 0
    assert report is not None

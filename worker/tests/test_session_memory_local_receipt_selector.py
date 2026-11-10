from __future__ import annotations

import contextlib
import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import agent_knowledge.couchdb_source.build_cli as build_cli
from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.build_cli import (
    _evaluate_session_change,
    _select_sessions_needing_projection,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_and_project,
    materialize_session_memory,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.rag_ingress.pg_embedding_privacy import EGRESS_POLICY_ERROR
from test_couchdb_build_cli import (
    _build_synthetic_session,
    _mark_projected,
    _write_approval,
)


class _OrderedInMemoryCouchDBSourceStore(InMemoryCouchDBSourceStore):
    def __init__(self) -> None:
        super().__init__()
        self.session_order: dict[str, int] = {}

    def iter_by_type(
        self,
        doc_type: str,
        *,
        fields: list[str] | None = None,
        selector: dict | None = None,
        limit: int = 0,
        page_size: int = 10000,
    ):
        documents = list(
            super().iter_by_type(
                doc_type,
                fields=fields,
                selector=selector,
                limit=0,
                page_size=page_size,
            )
        )
        if doc_type == dm.SourceDocType.TRANSCRIPT_SESSION:
            documents.sort(
                key=lambda document: self.session_order.get(
                    str(document.get("session_id_hash") or ""), len(self.session_order)
                )
            )
        if limit > 0:
            documents = documents[:limit]
        yield from documents


class _FakeProjector:
    def __init__(self, *, embedding_profile: str = "", pg_store=None) -> None:
        self.embedding_profile = embedding_profile
        self.pg_store = pg_store
        self.calls: list[dict] = []

    def project(self, *, target_profile: str, document: dict) -> str:
        self.calls.append(document)
        if self.pg_store is not None:
            self.pg_store.upsert(
                session_id_hash=document["session_id_hash"],
                source_hash=document["source_hash"],
            )
        return "fake-pg-row"


class _TerminalFailureLocalProjector(_FakeProjector):
    def project(self, *, target_profile: str, document: dict) -> str:
        raise ValueError(EGRESS_POLICY_ERROR)


class _FakePgStore:
    def __init__(self) -> None:
        self.rows: set[tuple[str, str]] = set()
        self.inserted = 0
        self.reused = 0

    def upsert(self, *, session_id_hash: str, source_hash: str) -> None:
        key = (session_id_hash, source_hash)
        if key in self.rows:
            self.reused += 1
        else:
            self.rows.add(key)
            self.inserted += 1


def _build_session(store: InMemoryCouchDBSourceStore, raw_id: str) -> str:
    return _build_synthetic_session(
        store,
        provider="claude",
        project="neurons",
        raw_id=raw_id,
    )


def _build_ordered_sessions(
    labels: tuple[str, ...] = ("A", "B", "C"),
) -> tuple[_OrderedInMemoryCouchDBSourceStore, dict[str, str]]:
    store = _OrderedInMemoryCouchDBSourceStore()
    sessions = {label: _build_session(store, label) for label in labels}
    store.session_order = {sessions[label]: index for index, label in enumerate(labels)}
    return store, sessions


def _project_locally(
    store: InMemoryCouchDBSourceStore,
    session_id_hash: str,
    *,
    projector: _FakeProjector | None = None,
) -> dict:
    active_projector = projector or _FakeProjector(
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID
    )
    return materialize_and_project(
        session_id_hash=session_id_hash,
        store=store,
        projector=active_projector,
        backend="postgres_pgvector",
    )


def _local_receipt(
    store: InMemoryCouchDBSourceStore,
    session_id_hash: str,
    *,
    profile_key: str = LOCAL_EMBEDDING_PROFILE_ID,
) -> dict:
    state = store.get(dm.projection_state_doc_id(session_id_hash)) or {}
    receipts = state.get("backend_receipts") or {}
    assert set(receipts) == {"postgres_pgvector_local"}
    local_receipts = receipts["postgres_pgvector_local"]
    assert profile_key in local_receipts
    assert not state.get("projected_source_hash")
    return local_receipts[profile_key]


def _mark_postgres_receipt(store: InMemoryCouchDBSourceStore, session_id_hash: str) -> None:
    _mark_projected(store, session_id_hash, "claude", "neurons")
    state = store.get(dm.projection_state_doc_id(session_id_hash))
    source_hash = state["source_hash"]
    state["projection_status"] = dm.ProjectionStatus.PENDING
    state.pop("projected_source_hash", None)
    state["backend_receipts"] = {
        "postgres_pgvector": {
            "projection_status": dm.ProjectionStatus.PROJECTED,
            "projected_source_hash": source_hash,
        }
    }
    store.put(state)


def _run_cli(
    tmp_path: Path,
    store: InMemoryCouchDBSourceStore,
    *,
    projector: _FakeProjector,
    checkpoint: Path,
    args: list[str] | None = None,
    profile_env: dict[str, str] | None = None,
) -> tuple[int, dict, str]:
    approval_path = tmp_path / "approval.json"
    argv = [*(args or []), "--approval", str(approval_path)]
    _write_approval(tmp_path, argv=argv)
    env = {
        "COUCHDB_URL": "http://localhost:5984",
        "COUCHDB_USER": "",
        "COUCHDB_PASSWORD": "",
        "COUCHDB_DB": "transcript_source",
        "SESSION_MEMORY_PROJECTION_BACKEND": "postgres_pgvector",
        "SESSION_MEMORY_SELECTION_MODE": "auto",
        "SESSION_MEMORY_SELECTION_CHECKPOINT": str(checkpoint),
    }
    env.update(profile_env or {})
    output = StringIO()
    errors = StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore",
                lambda **kwargs: store,
            )
        )
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(
            patch(
                "agent_knowledge.couchdb_source.build_cli._build_pg_projector",
                lambda _environ: projector,
            )
        )
        stack.enter_context(patch("sys.stdout", output))
        stack.enter_context(patch("sys.stderr", errors))
        return_code = build_cli.main(argv)
    return return_code, json.loads(output.getvalue()), errors.getvalue()


_LOCAL_PROFILE_ENV = {
    "LLM_BRAIN_EMBEDDING_PROVIDER": "model2vec",
    "LLM_BRAIN_EMBEDDING_MODEL": "local-model2vec-256-v1",
    "LLM_BRAIN_EMBEDDING_DIM": "256",
}
_DEFAULT_PROFILE_ENV = {
    "LLM_BRAIN_EMBEDDING_PROVIDER": "openai",
    "LLM_BRAIN_EMBEDDING_MODEL": "gemini-embedding-2",
    "LLM_BRAIN_EMBEDDING_DIM": "3072",
}


def test_full_walk_skips_local_receipt_for_active_profile() -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "full-walk-local")
    projected = _project_locally(store, session_id_hash)
    receipt = _local_receipt(store, session_id_hash)

    assert receipt["projection_status"] == dm.ProjectionStatus.PROJECTED
    assert receipt["projected_source_hash"] == projected["source_hash"]
    selected = _select_sessions_needing_projection(
        store,
        10,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )

    assert selected == []


def test_full_walk_empty_profile_keeps_the_legacy_pg_receipt_lane() -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "full-walk-legacy-profile")
    _project_locally(store, session_id_hash)

    selected = _select_sessions_needing_projection(
        store,
        10,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
        embedding_profile="",
    )

    assert [session["session_id_hash"] for session in selected] == [session_id_hash]


def test_incremental_evaluator_skips_local_receipt_for_active_profile() -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "incremental-local")
    projected = _project_locally(store, session_id_hash)
    receipt = _local_receipt(store, session_id_hash)

    assert receipt["projected_source_hash"] == projected["source_hash"]
    needs_projection, selected = _evaluate_session_change(
        store,
        session_id_hash,
        provider="claude",
        project="neurons",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )

    assert needs_projection is False and selected is None


def test_incremental_evaluator_empty_profile_keeps_the_legacy_pg_receipt_lane() -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "incremental-legacy-profile")
    _project_locally(store, session_id_hash)

    needs_projection, selected = _evaluate_session_change(
        store,
        session_id_hash,
        provider="claude",
        project="neurons",
        embedding_profile="",
    )

    assert needs_projection is True
    assert selected is not None
    assert selected["session_id_hash"] == session_id_hash


def test_local_receipts_in_production_backend_do_not_starve_new_sessions() -> None:
    store, sessions = _build_ordered_sessions()
    _project_locally(store, sessions["A"])
    _project_locally(store, sessions["B"])

    selected = _select_sessions_needing_projection(
        store,
        2,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )

    assert [session["session_id_hash"] for session in selected] == [sessions["C"]]


def test_postgres_backend_receipts_allow_the_new_session_through_the_limit() -> None:
    store, sessions = _build_ordered_sessions()
    for label in ("A", "B"):
        _mark_postgres_receipt(store, sessions[label])

    selected = _select_sessions_needing_projection(
        store,
        2,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
        embedding_profile="",
    )

    assert [session["session_id_hash"] for session in selected] == [sessions["C"]]


def test_different_profile_local_receipt_does_not_complete_the_active_profile() -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "other-profile")
    _project_locally(store, session_id_hash)

    state = store.get(dm.projection_state_doc_id(session_id_hash))
    local_receipts = state["backend_receipts"]["postgres_pgvector_local"]
    local_receipts["other-profile"] = local_receipts.pop(LOCAL_EMBEDDING_PROFILE_ID)
    store.put(state)

    selected = _select_sessions_needing_projection(
        store,
        10,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )

    assert [session["session_id_hash"] for session in selected] == [session_id_hash]


def test_old_local_receipt_is_selected_after_source_changes() -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "changed-source")
    projected = _project_locally(store, session_id_hash)
    receipt = _local_receipt(store, session_id_hash)

    _build_synthetic_session(
        store,
        provider="claude",
        project="neurons",
        raw_id="changed-source",
        body="source changed after local projection",
    )
    current = materialize_session_memory(session_id_hash=session_id_hash, store=store)
    selected = _select_sessions_needing_projection(
        store,
        10,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )

    assert receipt["projected_source_hash"] == projected["source_hash"]
    assert receipt["projected_source_hash"] != current.source_hash
    assert [session["session_id_hash"] for session in selected] == [session_id_hash]


def test_local_terminal_failure_is_blocked_until_source_changes() -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "terminal-local-failure")
    result = _project_locally(
        store,
        session_id_hash,
        projector=_TerminalFailureLocalProjector(
            embedding_profile=LOCAL_EMBEDDING_PROFILE_ID
        ),
    )
    state = store.get(dm.projection_state_doc_id(session_id_hash))
    local_attempt = state["projection_attempt"]["postgres_pgvector_local"]

    assert result["projection"]["status"] == dm.ProjectionStatus.FAILED
    assert local_attempt["source_hash"] == result["source_hash"]
    selected_unchanged = _select_sessions_needing_projection(
        store,
        10,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )
    assert selected_unchanged == []

    _build_synthetic_session(
        store,
        provider="claude",
        project="neurons",
        raw_id="terminal-local-failure",
        body="source changed after terminal local failure",
    )
    selected_changed = _select_sessions_needing_projection(
        store,
        10,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )

    assert [session["session_id_hash"] for session in selected_changed] == [session_id_hash]


def test_cli_full_cycle_uses_local_profile_receipt_on_second_run(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "cli-cycle-local")
    fake_pg_store = _FakePgStore()
    projector = _FakeProjector(
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        pg_store=fake_pg_store,
    )
    checkpoint = tmp_path / "selection-checkpoint.json"

    first_rc, first, _ = _run_cli(
        tmp_path,
        store,
        projector=projector,
        checkpoint=checkpoint,
        profile_env=_LOCAL_PROFILE_ENV,
    )
    second_rc, second, _ = _run_cli(
        tmp_path,
        store,
        projector=projector,
        checkpoint=checkpoint,
        profile_env=_LOCAL_PROFILE_ENV,
    )
    receipt = _local_receipt(store, session_id_hash)
    current = materialize_session_memory(session_id_hash=session_id_hash, store=store)
    saved_checkpoint = build_cli._read_selection_checkpoint(str(checkpoint))

    assert first_rc == second_rc == 0
    assert first["selected"] == first["projected"] == 1
    assert second["selected"] == second["projected"] == 0
    assert receipt["projected_source_hash"] == current.source_hash
    assert saved_checkpoint["embedding_profile"] == LOCAL_EMBEDDING_PROFILE_ID
    assert fake_pg_store.inserted == 1
    assert fake_pg_store.reused == 0


def test_cli_capped_cycle_selects_two_new_sessions_after_local_completions(
    tmp_path: Path,
) -> None:
    store, sessions = _build_ordered_sessions(("A", "B", "C", "D"))
    _project_locally(store, sessions["A"])
    _project_locally(store, sessions["B"])
    fake_pg_store = _FakePgStore()
    projector = _FakeProjector(
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        pg_store=fake_pg_store,
    )

    return_code, report, _ = _run_cli(
        tmp_path,
        store,
        projector=projector,
        checkpoint=tmp_path / "selection-checkpoint.json",
        args=["--limit", "2"],
        profile_env=_LOCAL_PROFILE_ENV,
    )

    assert return_code == 0
    assert report["selected"] == report["projected"] == 2
    assert [row["session_id_hash"] for row in report["sessions"]] == [
        sessions["C"],
        sessions["D"],
    ]
    assert fake_pg_store.inserted == 2
    assert fake_pg_store.reused == 0


def test_cli_rejects_projector_profile_mismatch_before_explicit_projection(
    tmp_path: Path,
) -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "profile-mismatch")
    fake_pg_store = _FakePgStore()
    projector = _FakeProjector(
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
        pg_store=fake_pg_store,
    )

    return_code, report, _ = _run_cli(
        tmp_path,
        store,
        projector=projector,
        checkpoint=tmp_path / "selection-checkpoint.json",
        args=["--session-id-hash", session_id_hash],
        profile_env=_DEFAULT_PROFILE_ENV,
    )

    assert return_code == 2
    assert report["error"] == "embedding_profile_mismatch"
    assert report["selected"] == 1
    assert report["projected"] == 0
    assert projector.calls == []
    assert fake_pg_store.inserted == 0


@pytest.mark.parametrize("stored_profile", ["previous-profile", None])
def test_checkpoint_profile_change_forces_full_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stored_profile: str | None,
) -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, f"checkpoint-{stored_profile}")
    checkpoint = tmp_path / "selection-checkpoint.json"
    checkpoint_payload = {
        "schema_version": "session_memory_selection_checkpoint.v1",
        "pg_input_policy_version": build_cli.PG_INPUT_POLICY_VERSION,
        "last_seq": "stored-sequence",
    }
    if stored_profile is not None:
        checkpoint_payload["embedding_profile"] = stored_profile
    checkpoint.write_text(json.dumps(checkpoint_payload), encoding="utf-8")
    monkeypatch.setenv("SESSION_MEMORY_SELECTION_CHECKPOINT", str(checkpoint))
    incremental_calls: list[dict] = []

    def record_incremental(*args, **kwargs):
        incremental_calls.append(kwargs)
        return [], {"last_seq": "stored-sequence"}, 0

    monkeypatch.setattr(build_cli, "_select_sessions_incremental", record_incremental)

    selected = build_cli._select_sessions_for_cycle(
        store,
        limit=0,
        project="",
        provider="",
        backend="postgres_pgvector",
        mode="auto",
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )
    output = capsys.readouterr()
    saved_checkpoint = build_cli._read_selection_checkpoint(str(checkpoint))

    assert [session["session_id_hash"] for session in selected] == [session_id_hash]
    assert incremental_calls == []
    assert "[selection] mode=full" in output.err
    assert saved_checkpoint["embedding_profile"] == LOCAL_EMBEDDING_PROFILE_ID

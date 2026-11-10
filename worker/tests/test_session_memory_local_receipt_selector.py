from __future__ import annotations

import contextlib
import json
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch

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


def _build_ordered_sessions() -> tuple[_OrderedInMemoryCouchDBSourceStore, dict[str, str]]:
    store = _OrderedInMemoryCouchDBSourceStore()
    sessions = {label: _build_session(store, label) for label in ("A", "B", "C")}
    store.session_order = {sessions[label]: index for index, label in enumerate(("A", "B", "C"))}
    return store, sessions


def _project_locally(
    store: InMemoryCouchDBSourceStore,
    session_id_hash: str,
    *,
    embedding_profile: str = LOCAL_EMBEDDING_PROFILE_ID,
    backend: str = "postgres_pgvector",
    projector: _FakeProjector | None = None,
) -> dict:
    active_projector = projector or _FakeProjector(embedding_profile=embedding_profile)
    return materialize_and_project(
        session_id_hash=session_id_hash,
        store=store,
        projector=active_projector,
        backend=backend,
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


def test_full_walk_does_not_reselect_a_locally_projected_session() -> None:
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
    )
    selected_ids = [session["session_id_hash"] for session in selected]

    assert selected_ids == [], (
        f"full walk reselected local-completed sessions {selected_ids!r}"
    )


def test_incremental_evaluator_does_not_reselect_a_locally_projected_session() -> None:
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
    )

    assert needs_projection is False and selected is None, (
        "incremental evaluator reselected local-completed session "
        f"{(selected or {}).get('session_id_hash')!r}"
    )


def test_local_receipts_in_production_backend_starve_a_new_session_at_limit() -> None:
    store, sessions = _build_ordered_sessions()
    _project_locally(store, sessions["A"])
    _project_locally(store, sessions["B"])

    selected = _select_sessions_needing_projection(
        store,
        2,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
    )
    selected_ids = [session["session_id_hash"] for session in selected]

    assert selected_ids == [sessions["C"]], (
        f"limit=2 selected earlier locally-projected sessions {selected_ids!r} instead of new C"
    )


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
    )

    assert [session["session_id_hash"] for session in selected] == [sessions["C"]]


def test_different_profile_local_receipt_does_not_complete_the_active_profile() -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "other-profile")
    _project_locally(
        store,
        session_id_hash,
        embedding_profile="other-profile",
        backend="postgres_pgvector_local",
    )

    state = store.get(dm.projection_state_doc_id(session_id_hash))
    assert set(state["backend_receipts"]["postgres_pgvector_local"]) == {"other-profile"}
    _local_receipt(store, session_id_hash, profile_key="other-profile")
    selected = _select_sessions_needing_projection(
        store,
        10,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
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
    )

    assert receipt["projected_source_hash"] == projected["source_hash"]
    assert receipt["projected_source_hash"] != current.source_hash
    assert [session["session_id_hash"] for session in selected] == [session_id_hash]


def test_explicit_local_backend_does_not_reselect_a_locally_projected_session() -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "explicit-local-backend")
    _project_locally(store, session_id_hash)
    _local_receipt(store, session_id_hash)

    selected = _select_sessions_needing_projection(
        store,
        10,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector_local",
    )

    selected_ids = [session["session_id_hash"] for session in selected]

    assert selected_ids == [], (
        f"explicit local backend reselected local-completed sessions {selected_ids!r}"
    )


def test_explicit_local_backend_starvation_leaves_the_new_session_selected() -> None:
    store, sessions = _build_ordered_sessions()
    _project_locally(store, sessions["A"])
    _project_locally(store, sessions["B"])

    selected = _select_sessions_needing_projection(
        store,
        2,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector_local",
    )
    selected_ids = [session["session_id_hash"] for session in selected]

    assert selected_ids == [sessions["C"]], (
        f"explicit local backend selected {selected_ids!r} instead of new C"
    )


def test_local_terminal_failure_is_reselected_by_the_production_backend() -> None:
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
    selected = _select_sessions_needing_projection(
        store,
        10,
        provider="claude",
        project="neurons",
        backend="postgres_pgvector",
    )

    assert [session["session_id_hash"] for session in selected] == [session_id_hash]


def test_cli_full_cycle_reprojects_local_receipt_and_reuses_the_pg_row(tmp_path: Path) -> None:
    store = InMemoryCouchDBSourceStore()
    session_id_hash = _build_session(store, "cli-cycle-local")
    fake_pg_store = _FakePgStore()
    checkpoint = tmp_path / "selection-checkpoint.json"
    argv = ["--approval", str(tmp_path / "approval.json")]
    _write_approval(tmp_path, argv=argv)

    def run_cycle() -> dict:
        output = StringIO()
        env = {
            "COUCHDB_URL": "http://localhost:5984",
            "COUCHDB_USER": "",
            "COUCHDB_PASSWORD": "",
            "COUCHDB_DB": "transcript_source",
            "SESSION_MEMORY_PROJECTION_BACKEND": "postgres_pgvector",
            "SESSION_MEMORY_SELECTION_MODE": "full",
            "SESSION_MEMORY_SELECTION_CHECKPOINT": str(checkpoint),
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch(
                    "agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore",
                    lambda **kwargs: store,
                )
            )
            stack.enter_context(
                patch.dict(os.environ, env, clear=True)
            )
            stack.enter_context(
                patch(
                    "agent_knowledge.couchdb_source.build_cli._build_pg_projector",
                    lambda _environ: _FakeProjector(
                        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
                        pg_store=fake_pg_store,
                    ),
                )
            )
            stack.enter_context(patch("sys.stdout", output))
            return_code = build_cli.main(argv)
        assert return_code == 0
        return json.loads(output.getvalue())

    first = run_cycle()
    second = run_cycle()
    receipt = _local_receipt(store, session_id_hash)
    current = materialize_session_memory(session_id_hash=session_id_hash, store=store)

    assert first["selected"] == first["projected"] == 1
    assert second["selected"] == second["projected"] == 1
    assert receipt["projected_source_hash"] == current.source_hash
    assert receipt["projection_status"] == dm.ProjectionStatus.PROJECTED
    assert fake_pg_store.inserted == 1
    assert fake_pg_store.reused == 1

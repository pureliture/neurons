from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from agent_knowledge.cli import COMMAND_HANDLERS
from agent_knowledge.couchdb_source.document_model import projection_state_doc_id
from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.postgres_store.pgvector_store import (
    PgVectorStore,
    SessionChunk,
)
from agent_knowledge.session_memory import session_memory_stale_plan as stale_plan


NOW = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)


def _row(
    session_id_hash: str,
    chunk_id: str,
    *,
    observed_at: datetime | None = None,
    project: str = "synthetic-private-project",
) -> SessionChunk:
    return SessionChunk(
        chunk_id=chunk_id,
        session_id_hash=session_id_hash,
        project=project,
        provider="synthetic-provider",
        chunk_index=0,
        content_markdown=f"synthetic body for {chunk_id}",
        content_hash="sha256:" + "a" * 64,
        embedding_state="ready",
        embedding=[0.1, 0.2],
        observed_at=observed_at,
    )


def _receipt_state(session_id_hash: str, receipt: dict) -> dict:
    return {
        "_id": projection_state_doc_id(session_id_hash),
        "backend_receipts": {
            "postgres_pgvector_local": {
                LOCAL_EMBEDDING_PROFILE_ID: receipt,
            }
        },
    }


def _v3_receipt(chunk_id: str) -> dict:
    return {
        "receipt_version": 3,
        "session_memory_knowledge_id": chunk_id,
    }


def _v4_receipt(segment_ids: list[str]) -> dict:
    return {
        "receipt_version": 4,
        "segment_ids": list(segment_ids),
    }


class ReadOnlySourceStore:
    def __init__(self, states: dict[str, dict]) -> None:
        self.states = states

    def get(self, doc_id: str) -> dict | None:
        for session_id_hash, state in self.states.items():
            if doc_id == projection_state_doc_id(session_id_hash):
                return state
        return None

    def put(self, *_args, **_kwargs):
        raise AssertionError("CouchDB write API called")

    def delete(self, *_args, **_kwargs):
        raise AssertionError("CouchDB delete API called")


class ReadOnlyPgStore:
    def __init__(self, session_rows: list[tuple[str, tuple[SessionChunk, ...]]]) -> None:
        self.session_rows = list(session_rows)
        self.iteration_args: tuple[int, int] | None = None

    def iter_session_chunk_batches(
        self,
        *,
        batch_size: int,
        limit_sessions: int,
    ):
        self.iteration_args = (batch_size, limit_sessions)
        yield from self.session_rows[:limit_sessions]

    def insert_chunk(self, *_args, **_kwargs):
        raise AssertionError("PostgreSQL write API called")

    def delete_session_chunk(self, *_args, **_kwargs):
        raise AssertionError("PostgreSQL delete API called")


class RecordingAuthorityResolver:
    def __init__(
        self,
        accepted_ids_by_session: dict[str, set[str]],
        metadata_by_session: dict[str, dict] | None = None,
    ) -> None:
        self.accepted_ids_by_session = accepted_ids_by_session
        self.metadata_by_session = metadata_by_session or {}
        self.calls: list[dict] = []

    def resolve(self, hit: dict) -> dict | None:
        self.calls.append(hit)
        accepted = self.accepted_ids_by_session.get(hit["session_id_hash"], set())
        if hit["memory_id"] not in accepted:
            return None
        return self.metadata_by_session.get(hit["session_id_hash"], {"current": True})


def _planner(
    rows: list[tuple[str, tuple[SessionChunk, ...]]],
    states: dict[str, dict],
    accepted_ids: dict[str, set[str]],
    *,
    retention_days: int = 14,
    now: datetime = NOW,
    limit_sessions: int = 20_000,
    authority_metadata: dict[str, dict] | None = None,
):
    resolver = RecordingAuthorityResolver(accepted_ids, authority_metadata)
    pg_store = ReadOnlyPgStore(rows)
    planner = stale_plan.SessionMemoryStalePlanner(
        source_store=ReadOnlySourceStore(states),
        pg_store=pg_store,
        retention_days=retention_days,
        limit_sessions=limit_sessions,
        batch_sessions=2,
        now_fn=lambda: now,
        authority_resolver=resolver,
    )
    return planner.run(), resolver, pg_store


def test_v3_current_receipt_and_previous_revision_are_classified():
    session_id_hash = "synthetic-session-v3"
    current = _row(session_id_hash, "current-v3")
    previous = _row(session_id_hash, "previous-v3")
    report, resolver, _ = _planner(
        [(session_id_hash, (current, previous))],
        {session_id_hash: _receipt_state(session_id_hash, _v3_receipt(current.chunk_id))},
        {session_id_hash: {current.chunk_id}},
    )

    assert report["classifications"]["current"]["rows"] == 1
    assert report["classifications"]["stale"]["rows"] == 1
    assert report["classifications"]["unresolved"]["rows"] == 0
    assert report["by_representation"]["v3"]["current"]["rows"] == 1
    assert report["by_representation"]["v3"]["stale"]["rows"] == 1
    assert report["stale_sessions"] == 1
    assert len(resolver.calls) == 1


def test_v4_receipt_members_are_current_and_rows_outside_the_list_are_stale():
    session_id_hash = "synthetic-session-v4"
    segment_a = _row(session_id_hash, "segment-current-a")
    segment_b = _row(session_id_hash, "segment-current-b")
    old_segment = _row(session_id_hash, "segment-old")
    report, resolver, _ = _planner(
        [(session_id_hash, (segment_a, segment_b, old_segment))],
        {
            session_id_hash: _receipt_state(
                session_id_hash,
                _v4_receipt([segment_a.chunk_id, segment_b.chunk_id]),
            )
        },
        {session_id_hash: {segment_a.chunk_id}},
        authority_metadata={
            session_id_hash: {
                "representation": "segment_v1",
                "segment_ids": [segment_a.chunk_id, segment_b.chunk_id],
            }
        },
    )

    assert report["classifications"]["current"]["rows"] == 2
    assert report["classifications"]["stale"]["rows"] == 1
    assert report["by_representation"]["v4"]["current"]["rows"] == 2
    assert report["by_representation"]["v4"]["stale"]["rows"] == 1
    assert len(resolver.calls) == 1


def test_missing_receipt_is_unresolved_and_never_a_stale_candidate():
    session_id_hash = "synthetic-session-unresolved"
    row = _row(session_id_hash, "unresolved-row")
    report, resolver, _ = _planner(
        [(session_id_hash, (row,))],
        {},
        {},
    )

    assert report["classifications"]["unresolved"]["rows"] == 1
    assert report["classifications"]["stale"]["rows"] == 0
    assert report["failure_reasons"]["projection_state_missing"] == {
        "sessions": 1,
        "rows": 1,
    }
    assert resolver.calls == []


def test_retention_cutoff_is_strict_and_counts_only_older_stale_rows():
    session_id_hash = "synthetic-session-retention"
    cutoff = NOW - timedelta(days=14)
    current = _row(session_id_hash, "retained-current")
    on_boundary = _row(
        session_id_hash,
        "stale-on-boundary",
        observed_at=cutoff,
    )
    older = _row(
        session_id_hash,
        "stale-before-boundary",
        observed_at=cutoff - timedelta(microseconds=1),
    )
    report, _, _ = _planner(
        [(session_id_hash, (current, on_boundary, older))],
        {session_id_hash: _receipt_state(session_id_hash, _v3_receipt(current.chunk_id))},
        {session_id_hash: {current.chunk_id}},
    )

    assert report["classifications"]["stale"]["rows"] == 2
    assert report["classifications"]["stale_past_retention"]["rows"] == 1
    assert report["by_representation"]["v3"]["stale_past_retention"]["rows"] == 1


def test_report_contains_no_raw_ids_or_project_names_and_performs_no_writes():
    session_id_hash = "opaque-session-id"
    current = _row(session_id_hash, "opaque-current-row")
    stale = _row(session_id_hash, "opaque-old-row")
    report, _, pg_store = _planner(
        [(session_id_hash, (current, stale))],
        {session_id_hash: _receipt_state(session_id_hash, _v3_receipt(current.chunk_id))},
        {session_id_hash: {current.chunk_id}},
    )
    serialized = json.dumps(report, sort_keys=True)

    assert "opaque-session-id" not in serialized
    assert "opaque-current-row" not in serialized
    assert "opaque-old-row" not in serialized
    assert "synthetic-private-project" not in serialized
    assert report["mutation_performed"] is False
    assert report["raw_ids_printed"] is False
    assert report["project_names_printed"] is False
    assert pg_store.iteration_args == (2, 20_000)


def test_command_is_registered_and_has_no_execute_or_delete_option():
    assert COMMAND_HANDLERS[stale_plan.COMMAND] is stale_plan.main
    with pytest.raises(SystemExit) as error:
        stale_plan._build_parser().parse_args(["--execute"])
    assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        stale_plan._build_parser().parse_args(["--delete"])
    assert error.value.code == 2


class _BatchCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query: str, params=()) -> None:
        self.connection.queries.append(query)
        if "SELECT DISTINCT session_id_hash" in query:
            if "WHERE session_id_hash > %s" in query:
                last_session_id, limit = params
                session_ids = [
                    session_id
                    for session_id in sorted(self.connection.rows_by_session)
                    if session_id > last_session_id
                ]
            else:
                (limit,) = params
                session_ids = sorted(self.connection.rows_by_session)
            self.rows = [
                {"session_id_hash": session_id}
                for session_id in session_ids[:limit]
            ]
            return
        session_ids = set(params[0])
        self.rows = [
            row
            for session_id in sorted(session_ids)
            for row in self.connection.rows_by_session[session_id]
        ]

    def fetchall(self):
        return self.rows


class _BatchConnection:
    def __init__(self, rows_by_session: dict[str, list[dict]]) -> None:
        self.rows_by_session = rows_by_session
        self.queries: list[str] = []

    def cursor(self):
        return _BatchCursor(self)


def _pg_row(session_id_hash: str, chunk_id: str, index: int) -> dict:
    return {
        "chunk_id": chunk_id,
        "session_id_hash": session_id_hash,
        "project": "batch-project",
        "provider": "synthetic-provider",
        "chunk_index": index,
        "content_markdown": "body",
        "token_count": 1,
        "content_hash": "sha256:" + "b" * 64,
        "embedding_model": "local-model",
        "embedding_state": "ready",
        "embedding_revision": 1,
        "embedding": None,
        "created_at": NOW,
        "updated_at": NOW,
        "observed_at": NOW,
    }


def test_pg_chunk_reader_keeps_sessions_whole_and_obeys_session_limit():
    connection = _BatchConnection(
        {
            "session-a": [
                _pg_row("session-a", "a-1", 0),
                _pg_row("session-a", "a-2", 1),
            ],
            "session-b": [_pg_row("session-b", "b-1", 0)],
            "session-c": [_pg_row("session-c", "c-1", 0)],
        }
    )
    store = PgVectorStore(connection=connection)

    batches = list(store.iter_session_chunk_batches(batch_size=2, limit_sessions=2))

    assert [(session_id, len(rows)) for session_id, rows in batches] == [
        ("session-a", 2),
        ("session-b", 1),
    ]
    assert connection.queries
    assert all(query.lstrip().startswith("SELECT") for query in connection.queries)

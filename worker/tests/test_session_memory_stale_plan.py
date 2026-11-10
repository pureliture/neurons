from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from agent_knowledge.cli import COMMAND_HANDLERS
from agent_knowledge.couchdb_source.document_model import projection_state_doc_id
from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.postgres_store.pgvector_store import (
    PgVectorStore,
    SessionChunkMeta,
)
from agent_knowledge.session_memory import session_memory_stale_plan as stale_plan


NOW = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)


def _row(
    session_id_hash: str,
    chunk_id: str,
    *,
    observed_at: datetime | None = None,
    created_at: datetime | None = NOW,
    content_bytes: int | None = None,
    project: str = "synthetic-private-project",
) -> SessionChunkMeta:
    body = f"synthetic body for {chunk_id}"
    return SessionChunkMeta(
        chunk_id=chunk_id,
        session_id_hash=session_id_hash,
        project=project,
        provider="synthetic-provider",
        content_hash="sha256:" + "a" * 64,
        content_bytes=(
            content_bytes
            if content_bytes is not None
            else len(body.encode("utf-8"))
        ),
        created_at=created_at,
        updated_at=created_at,
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
    def __init__(
        self,
        session_rows: list[tuple[str, tuple[SessionChunkMeta, ...]]],
    ) -> None:
        self.session_rows = list(session_rows)
        self.iteration_args: tuple[int, int] | None = None

    def iter_session_chunk_meta_batches(
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
    rows: list[tuple[str, tuple[SessionChunkMeta, ...]]],
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


def test_retention_uses_replacement_created_at_and_strict_cutoff():
    session_id_hash = "synthetic-session-retention"
    cutoff = NOW - timedelta(days=14)
    current = _row(
        session_id_hash,
        "retained-current",
        created_at=cutoff - timedelta(microseconds=1),
    )
    old_observation = _row(
        session_id_hash,
        "old-observation",
        observed_at=NOW - timedelta(days=60),
    )
    boundary_current = _row(
        session_id_hash,
        "boundary-current",
        created_at=cutoff,
    )
    report, _, _ = _planner(
        [(session_id_hash, (current, old_observation, boundary_current))],
        {session_id_hash: _receipt_state(session_id_hash, _v3_receipt(current.chunk_id))},
        {session_id_hash: {current.chunk_id}},
    )

    assert report["classifications"]["stale"]["rows"] == 2
    assert report["classifications"]["stale_past_retention"]["rows"] == 2
    assert report["by_representation"]["v3"]["stale_past_retention"]["rows"] == 2


def test_old_observed_at_does_not_override_recent_replacement_time():
    session_id_hash = "synthetic-session-recent-replacement"
    current = _row(
        session_id_hash,
        "recent-current",
        created_at=NOW - timedelta(days=1),
    )
    stale = _row(
        session_id_hash,
        "old-stale-row",
        observed_at=NOW - timedelta(days=60),
        created_at=NOW - timedelta(days=60),
    )
    report, _, _ = _planner(
        [(session_id_hash, (current, stale))],
        {session_id_hash: _receipt_state(session_id_hash, _v3_receipt(current.chunk_id))},
        {session_id_hash: {current.chunk_id}},
    )

    assert report["classifications"]["stale"]["rows"] == 1
    assert report["classifications"]["stale_past_retention"]["rows"] == 0


def test_replacement_created_at_equal_to_cutoff_is_not_past_retention():
    session_id_hash = "synthetic-session-retention-boundary"
    cutoff = NOW - timedelta(days=14)
    current = _row(session_id_hash, "boundary-current", created_at=cutoff)
    stale = _row(
        session_id_hash,
        "very-old-stale",
        observed_at=NOW - timedelta(days=60),
    )
    report, _, _ = _planner(
        [(session_id_hash, (current, stale))],
        {session_id_hash: _receipt_state(session_id_hash, _v3_receipt(current.chunk_id))},
        {session_id_hash: {current.chunk_id}},
    )

    assert report["classifications"]["stale_past_retention"]["rows"] == 0


def test_unknown_replacement_created_at_is_not_past_retention():
    session_id_hash = "synthetic-session-unknown-replacement-time"
    current = _row(session_id_hash, "unknown-created-at", created_at=None)
    stale = _row(
        session_id_hash,
        "old-stale",
        observed_at=NOW - timedelta(days=60),
    )
    report, _, _ = _planner(
        [(session_id_hash, (current, stale))],
        {session_id_hash: _receipt_state(session_id_hash, _v3_receipt(current.chunk_id))},
        {session_id_hash: {current.chunk_id}},
    )

    assert report["classifications"]["stale_past_retention"]["rows"] == 0


def test_retention_uses_max_created_at_across_current_segments():
    session_id_hash = "synthetic-session-max-created-at"
    older_current = _row(
        session_id_hash,
        "older-current-segment",
        created_at=NOW - timedelta(days=30),
    )
    recent_current = _row(
        session_id_hash,
        "recent-current-segment",
        created_at=NOW - timedelta(days=1),
    )
    stale = _row(
        session_id_hash,
        "stale-segment",
        observed_at=NOW - timedelta(days=60),
        created_at=NOW - timedelta(days=60),
    )
    report, _, _ = _planner(
        [(session_id_hash, (older_current, recent_current, stale))],
        {
            session_id_hash: _receipt_state(
                session_id_hash,
                _v4_receipt([older_current.chunk_id, recent_current.chunk_id]),
            )
        },
        {
            session_id_hash: {
                older_current.chunk_id,
                recent_current.chunk_id,
            }
        },
        authority_metadata={
            session_id_hash: {
                "representation": "segment_v1",
                "segment_ids": [older_current.chunk_id, recent_current.chunk_id],
            }
        },
    )

    assert report["classifications"]["stale_past_retention"]["rows"] == 0


def test_other_receipts_protect_rows_even_when_active_receipt_is_unresolved():
    session_id_hash = "synthetic-session-protected-unresolved"
    legacy = _row(session_id_hash, "legacy-protected")
    other_v3 = _row(session_id_hash, "other-profile-v3")
    other_v4 = _row(session_id_hash, "other-profile-v4")
    unresolved = _row(session_id_hash, "unresolved-row")
    state = _receipt_state(
        session_id_hash,
        _v3_receipt("active-receipt-row-missing"),
    )
    receipts = state["backend_receipts"]
    receipts["postgres_pgvector"] = {
        "session_memory_knowledge_id": legacy.chunk_id,
    }
    local_receipts = receipts["postgres_pgvector_local"]
    local_receipts["other-profile-v3"] = _v3_receipt(other_v3.chunk_id)
    local_receipts["other-profile-v4"] = _v4_receipt([other_v4.chunk_id])
    report, resolver, _ = _planner(
        [
            (
                session_id_hash,
                (legacy, other_v3, other_v4, unresolved),
            )
        ],
        {session_id_hash: state},
        {},
    )

    assert report["classifications"]["protected_other_receipt"]["rows"] == 3
    assert report["by_representation"]["v3"]["protected_other_receipt"]["rows"] == 3
    assert report["classifications"]["unresolved"]["rows"] == 1
    assert report["failure_reasons"]["current_row_missing"]["rows"] == 1
    assert resolver.calls == []


def test_current_receipt_takes_priority_over_protection():
    session_id_hash = "synthetic-session-current-priority"
    current = _row(session_id_hash, "current-also-legacy-protected")
    state = _receipt_state(
        session_id_hash,
        _v3_receipt(current.chunk_id),
    )
    state["backend_receipts"]["postgres_pgvector"] = {
        "session_memory_knowledge_id": current.chunk_id,
    }
    report, _, _ = _planner(
        [(session_id_hash, (current,))],
        {session_id_hash: state},
        {session_id_hash: {current.chunk_id}},
    )

    assert report["classifications"]["current"]["rows"] == 1
    assert report["classifications"]["protected_other_receipt"]["rows"] == 0


def test_empty_authorized_segment_ids_fail_closed_without_keyerror():
    session_id_hash = "synthetic-session-empty-authorized"
    segment = _row(session_id_hash, "segment-current")
    report, _, _ = _planner(
        [(session_id_hash, (segment,))],
        {
            session_id_hash: _receipt_state(
                session_id_hash,
                _v4_receipt([segment.chunk_id]),
            )
        },
        {session_id_hash: {segment.chunk_id}},
        authority_metadata={
            session_id_hash: {
                "representation": "segment_v1",
                "segment_ids": [],
            }
        },
    )

    assert report["classifications"]["unresolved"]["rows"] == 1
    assert report["failure_reasons"]["authority_rejected"]["rows"] == 1


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
        if query.strip().upper() == "SET TRANSACTION READ ONLY":
            self.rows = []
            return
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
        "content_hash": "sha256:" + "b" * 64,
        "content_bytes": 4 + index,
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

    batches = list(
        store.iter_session_chunk_meta_batches(batch_size=2, limit_sessions=2)
    )

    assert [(session_id, len(rows)) for session_id, rows in batches] == [
        ("session-a", 2),
        ("session-b", 1),
    ]
    assert connection.queries
    assert connection.queries[0].strip().upper() == "SET TRANSACTION READ ONLY"
    select_queries = connection.queries[1:]
    assert all(query.lstrip().startswith("SELECT") for query in select_queries)
    row_query = select_queries[1].lower()
    assert "octet_length(content_markdown) as content_bytes" in row_query
    assert "select content_markdown" not in row_query
    assert "embedding" not in row_query
    assert batches[0][1][0].content_bytes == 4

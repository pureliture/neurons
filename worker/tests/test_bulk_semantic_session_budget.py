"""Per-session read budget, deferral, and escalation for the bulk semantic lane."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest

import test_couchdb_graph_bulk_semantic_cli as graph_helpers
import test_couchdb_http_store as http_helpers
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.source_revision import (
    SourceRevisionDeferredBudget,
    activate_source_revision,
    resolve_active_source_revision,
)
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core import runtime


class _BatchTrackingStore:
    """Delegate reads to the real HTTP store while recording batched fetches."""

    def __init__(self, inner: CouchDBHttpSourceStore) -> None:
        self.inner = inner
        self.get_many_calls: list[list[str]] = []
        self.get_calls: list[str] = []

    def get(self, doc_id: str):  # type: ignore[no-untyped-def]
        self.get_calls.append(doc_id)
        return self.inner.get(doc_id)

    def get_many(self, doc_ids, *, page_size: int = 100):  # type: ignore[no-untyped-def]
        ids = [str(doc_id) for doc_id in doc_ids]
        self.get_many_calls.append(ids)
        return {doc_id: self.inner.get(doc_id) for doc_id in ids}


class _AllDocsFakeCouch(http_helpers.FakeCouch):
    """FakeCouch plus the POST ``_all_docs?include_docs=true`` read path."""

    def __call__(self, method: str, url: str, headers: dict, body: bytes):  # type: ignore[no-untyped-def]
        parsed = urlparse(url)
        if method == "POST" and parsed.path.endswith("/_all_docs"):
            db = [part for part in parsed.path.split("/") if part][0]
            store = self.dbs.setdefault(db, {})
            keys = [str(key) for key in json.loads(body.decode())["keys"]]
            # FakeCouch keeps percent-encoded ids because it never unquotes the
            # request path; index by the canonical id CouchDB would return.
            index = {unquote(stored): document for stored, document in store.items()}
            rows = []
            for key in keys:
                document = store.get(key) or index.get(key)
                if document is not None:
                    row = {
                        "id": key,
                        "key": key,
                        "value": {"rev": document.get("_rev")},
                        "doc": document,
                    }
                else:
                    row = {"id": key, "key": key, "error": "not_found"}
                rows.append(row)
            return http_helpers.ProxyResponse(200, json.dumps({"rows": rows}).encode(), {})
        return super().__call__(method, url, headers, body)


def _two_chunk_session() -> tuple[_BatchTrackingStore, str]:
    fake = _AllDocsFakeCouch()
    store = CouchDBHttpSourceStore(
        base_url="http://couch.test:5984", db="transcript_source", transport=fake
    )
    store.put(http_helpers._session_doc())
    store.put(http_helpers._chunk_doc("first chunk"))
    store.put(http_helpers._chunk_doc("second chunk"))
    session_id_hash = http_helpers._sid()
    activate_source_revision(store=store, session_id_hash=session_id_hash)
    return _BatchTrackingStore(store), session_id_hash


def test_active_revision_uses_batched_member_fetch() -> None:
    tracked, session_id_hash = _two_chunk_session()

    resolved = resolve_active_source_revision(store=tracked, session_id_hash=session_id_hash)

    assert len(resolved.conversation_chunks) == 2
    assert tracked.get_many_calls, "member documents must be prefetched through get_many"
    assert len(tracked.get_many_calls) == 1
    assert len(tracked.get_calls) == 2  # pointer + manifest only


def test_member_budget_defers_before_reading_bodies() -> None:
    tracked, session_id_hash = _two_chunk_session()
    with pytest.raises(SourceRevisionDeferredBudget) as probe:
        resolve_active_source_revision(store=tracked, session_id_hash=session_id_hash, max_member_docs=1)
    members_total = probe.value.members_total
    assert members_total > 2
    tracked.get_many_calls.clear()

    with pytest.raises(SourceRevisionDeferredBudget) as error:
        resolve_active_source_revision(
            store=tracked,
            session_id_hash=session_id_hash,
            max_member_docs=members_total - 1,
        )

    assert error.value.reason_code == "session_member_budget_exceeded"
    assert error.value.members_total == members_total
    assert error.value.members_read == 0
    assert tracked.get_many_calls == []
    assert len(tracked.get_calls) == 4  # pointer+manifest for each probe only


def test_deadline_defers_without_returning_documents() -> None:
    tracked, session_id_hash = _two_chunk_session()

    with pytest.raises(SourceRevisionDeferredBudget) as error:
        resolve_active_source_revision(
            store=tracked,
            session_id_hash=session_id_hash,
            deadline_monotonic=time.monotonic() - 1,
        )

    assert error.value.reason_code == "session_deadline_exceeded"
    assert tracked.get_many_calls == []


def test_materialize_does_not_retry_a_budget_deferral(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _defer(**_: object):
        calls.append(1)
        raise SourceRevisionDeferredBudget(
            "budget", reason_code="session_member_budget_exceeded", members_total=2
        )

    monkeypatch.setattr(runtime, "_materialize_artifact_from_couchdb_source_once", _defer)

    with pytest.raises(SourceRevisionDeferredBudget):
        runtime._materialize_artifact_with_snapshot(
            session_id_hash="sha256:" + "a" * 64,
            source_store=object(),  # type: ignore[arg-type]
            max_attempts=3,
        )

    assert len(calls) == 1


class _ProjectionSelection:
    def __init__(self, sessions: list[dict]) -> None:
        self.sessions = sessions
        self.source_count = len(sessions)
        self.eligible_count = len(sessions)


def _session_descriptor(session_id_hash: str, project: str = "neurons") -> dict:
    return {"session_id_hash": session_id_hash, "project": project, "provider": "codex"}


def _patch_selection_and_episode(monkeypatch: pytest.MonkeyPatch, sessions: list[dict]) -> None:
    monkeypatch.setattr(cli, "_select_sessions_bounded", lambda *a, **k: _ProjectionSelection(sessions))
    monkeypatch.setattr(
        cli,
        "_flush_batch_items",
        lambda items, **_: {
            "llm_batches": 0,
            "fallback_single_session_batches": 0,
            "projected": len(items),
            "failed": 0,
            "entities_written": len(items),
            "relations_written": 0,
        },
    )
    monkeypatch.setattr(
        cli,
        "session_source_revision_from_couchdb_source",
        lambda **_: "sha256:" + "b" * 64,
    )
    monkeypatch.setattr(
        cli,
        "make_bulk_session_input",
        lambda session_key, episode, max_chars: SimpleNamespace(
            session_key=session_key, episode=episode
        ),
    )


def test_bulk_lane_defers_budget_session_and_retries_it_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = [_session_descriptor("sha256:" + "1" * 64), _session_descriptor("sha256:" + "2" * 64)]
    _patch_selection_and_episode(monkeypatch, sessions)
    deferred_path = tmp_path / "bulk-semantic-deferred.jsonl"

    # first cycle: first session defers, second still makes progress
    calls: list[dict] = []

    def _episode_first(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        session_id_hash = str(kwargs["session_id_hash"])
        if session_id_hash == "sha256:" + "1" * 64:
            raise SourceRevisionDeferredBudget(
                "budget",
                reason_code="session_member_budget_exceeded",
                session_id_hash=session_id_hash,
                members_total=7,
                members_read=3,
            )
        return SimpleNamespace(natural_id=session_id_hash)

    monkeypatch.setattr(cli, "session_episode_from_couchdb_source", _episode_first)
    report = cli.run_couchdb_bulk_semantic_projection(
        ledger_path=tmp_path / "ledger.sqlite",
        source_store=object(),  # type: ignore[arg-type]
        limit=10,
        runtime_dir=tmp_path,
        progress_jsonl=tmp_path / "progress.jsonl",
        dead_letter_jsonl=tmp_path / "dead.jsonl",
        deferred_jsonl=deferred_path,
        max_session_member_docs=10,
        session_deadline_seconds=60,
        extractor=graph_helpers._BatchJsonThenSingleExtractor(),
        writer=graph_helpers._FakeBulkWriter(),
        max_sessions_per_call=2,
    )

    assert report["projection"]["deferred"] == 1
    assert report["projection"]["projected"] == 1
    records = [json.loads(line) for line in deferred_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    record = records[0]
    assert record["session_id_hash"] == "sha256:" + "1" * 64
    assert record["attempts"] == 1
    assert record["members_total"] == 7
    assert record["members_read"] == 3
    assert record["reason_code"] == "session_member_budget_exceeded"

    # second cycle: the deferred session is retried first with an escalated budget
    calls.clear()
    second_lines: list[str] = []

    def _episode_second(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        second_lines.append(str(kwargs["session_id_hash"]))
        return SimpleNamespace(natural_id=str(kwargs["session_id_hash"]))

    monkeypatch.setattr(cli, "session_episode_from_couchdb_source", _episode_second)
    report2 = cli.run_couchdb_bulk_semantic_projection(
        ledger_path=tmp_path / "ledger.sqlite",
        source_store=object(),  # type: ignore[arg-type]
        limit=10,
        runtime_dir=tmp_path,
        progress_jsonl=tmp_path / "progress2.jsonl",
        dead_letter_jsonl=tmp_path / "dead2.jsonl",
        deferred_jsonl=deferred_path,
        max_session_member_docs=10,
        session_deadline_seconds=60,
        extractor=graph_helpers._BatchJsonThenSingleExtractor(),
        writer=graph_helpers._FakeBulkWriter(),
        max_sessions_per_call=2,
    )

    assert second_lines[0] == "sha256:" + "1" * 64
    assert calls[0]["max_member_docs"] == 20
    assert calls[0]["deadline_monotonic"] > time.monotonic()
    assert report2["projection"]["deferred"] == 0
    assert deferred_path.read_text() == ""
    start_events = [
        json.loads(line)
        for line in (tmp_path / "progress2.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert start_events[0]["deferred_retried"] == 1


def test_bulk_lane_exhausts_deferred_session_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id_hash = "sha256:" + "3" * 64
    _patch_selection_and_episode(monkeypatch, [_session_descriptor(session_id_hash)])
    deferred_path = tmp_path / "bulk-semantic-deferred.jsonl"
    deferred_path.write_text(
        json.dumps(
            {
                "session_id_hash": session_id_hash,
                "project": "neurons",
                "provider": "codex",
                "attempts": cli.DEFERRED_MAX_ATTEMPTS,
                "reason_code": "session_member_budget_exceeded",
                "members_total": 7,
                "members_read": 3,
            }
        )
        + "\n"
    )

    def _always_defer(**kwargs):  # type: ignore[no-untyped-def]
        raise SourceRevisionDeferredBudget(
            "budget",
            reason_code="session_member_budget_exceeded",
            session_id_hash=str(kwargs["session_id_hash"]),
            members_total=7,
            members_read=3,
        )

    monkeypatch.setattr(cli, "session_episode_from_couchdb_source", _always_defer)

    report = cli.run_couchdb_bulk_semantic_projection(
        ledger_path=tmp_path / "ledger.sqlite",
        source_store=object(),  # type: ignore[arg-type]
        limit=10,
        runtime_dir=tmp_path,
        progress_jsonl=tmp_path / "progress.jsonl",
        dead_letter_jsonl=tmp_path / "dead.jsonl",
        deferred_jsonl=deferred_path,
        max_session_member_docs=10,
        session_deadline_seconds=60,
        extractor=graph_helpers._BatchJsonThenSingleExtractor(),
        writer=graph_helpers._FakeBulkWriter(),
        max_sessions_per_call=2,
    )

    assert report["projection"]["deferred_exhausted"] == 1
    assert report["projection"]["failed"] == 1
    assert deferred_path.read_text() == ""
    dead_letters = [
        json.loads(line) for line in (tmp_path / "dead.jsonl").read_text().splitlines() if line.strip()
    ]
    assert len(dead_letters) == 1
    assert dead_letters[0]["reason_code"] == "session_budget_exhausted"
    assert dead_letters[0]["error_param"] == "session_member_budget_exceeded"

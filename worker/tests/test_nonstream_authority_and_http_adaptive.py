"""Nonstream authority lookup and real HTTP adaptive-read failure paths."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from agent_knowledge.couchdb_source.couchdb_http_store import (
    CouchDBError,
    CouchDBHttpSourceStore,
)
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_full_input import SourceInvalid
from agent_knowledge.llm_brain_core.bulk_streaming import _fetch_with_split


def _h(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _insert_card(store, project: str, content_hash: str) -> str:
    memory_id = "mem_" + uuid.uuid4().hex[:16]
    envelope = {
        "memory_id": memory_id, "project": project, "card_type": "preference",
        "approval_state": "approved", "steward_proposal_kind": "", "lifecycle_state": "accepted",
        "authorization_status": "active", "currentness": "current", "content_hash": content_hash,
    }
    with store._scope(write=True) as db:
        db.execute(
            "INSERT INTO memory_cards (memory_id, project, card_type, title, summary, "
            "lifecycle_state, authorization_status, currentness, content_hash, steward_envelope) "
            "VALUES (%s, %s, 'preference', 'synthetic', 'synthetic', "
            "'accepted', 'active', 'current', %s, %s::jsonb)",
            [memory_id, project, content_hash, json.dumps(envelope)],
        )
    return memory_id


def test_nonstream_cli_authority_lookup_handles_101_and_ambiguity(isolated_pg_store,
                                                                  monkeypatch):
    project = "nonstream-" + uuid.uuid4().hex[:10]
    hashes = [_h(f"other-{i}-{project}") for i in range(101)]
    target = _h("target-" + project)
    hashes[100] = target
    expected = _insert_card(isolated_pg_store, project, target)
    ambiguous = _h("ambiguous-" + project)
    hashes[99] = ambiguous
    _insert_card(isolated_pg_store, project, ambiguous)
    _insert_card(isolated_pg_store, project, ambiguous)
    monkeypatch.setenv("TRIAL_PG_DSN", isolated_pg_store.dsn)

    as_of = datetime.now(timezone.utc) + timedelta(seconds=60)
    result = cli._pg_authority_by_hash(
        isolated_pg_store.dsn, project, hashes, as_of=as_of
    )
    assert result[target][0]["authority_memory_id"] == expected
    assert ambiguous not in result


class _AdaptiveHandler(BaseHTTPRequestHandler):
    mode = "split"
    count = 0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        keys = payload.get("keys") or []
        _AdaptiveHandler.count += 1
        if self.mode == "interrupt" and _AdaptiveHandler.count == 1:
            self.send_response(500)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.mode == "single_huge" or (self.mode == "split" and len(keys) > 2):
            body = json.dumps({
                "rows": [{"key": k, "doc": {"body": "x" * 700000}} for k in keys]
            }).encode()
        else:
            body = json.dumps({"rows": [
                {"key": k, "doc": {"body": "한글"}} for k in keys
            ]}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture
def adaptive_http():
    server = HTTPServer(("127.0.0.1", 0), _AdaptiveHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _members(keys: list[str]):
    return [SimpleNamespace(
        doc_id=k, content_hash=_h(k), lane="conversation", ordinal=i, redaction_version="1"
    ) for i, k in enumerate(keys)]


def test_real_http_splits_oversized_multi_doc_response(adaptive_http):
    _AdaptiveHandler.mode = "split"
    _AdaptiveHandler.count = 0
    store = CouchDBHttpSourceStore(base_url=adaptive_http, db="synthetic")
    keys = [f"doc-{i}" for i in range(8)]
    result = _fetch_with_split(store.get_many, _members(keys))
    assert list(result.keys()) == keys
    assert all(result[k]["body"] == "한글" for k in keys)


def test_real_http_single_oversized_doc_fails_closed(adaptive_http):
    _AdaptiveHandler.mode = "single_huge"
    _AdaptiveHandler.count = 0
    store = CouchDBHttpSourceStore(base_url=adaptive_http, db="synthetic")
    with pytest.raises(SourceInvalid, match="single source document"):
        _fetch_with_split(store.get_many, _members(["doc-0"]))


def test_real_http_interruption_fails_closed(adaptive_http):
    _AdaptiveHandler.mode = "interrupt"
    _AdaptiveHandler.count = 0
    store = CouchDBHttpSourceStore(base_url=adaptive_http, db="synthetic")
    with pytest.raises(CouchDBError):
        _fetch_with_split(store.get_many, _members(["doc-0", "doc-1"]))

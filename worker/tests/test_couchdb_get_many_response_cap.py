"""get_many must cap HTTP response bytes before JSON parsing."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_knowledge.couchdb_source.couchdb_http_store import (
    CouchDBHttpSourceStore,
    CouchDBResponseTooLarge,
)


class _Handler(BaseHTTPRequestHandler):
    body = b"{}"

    def do_POST(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        return


@pytest.fixture
def local_http():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_get_many_caps_response_bytes_before_json_parse(local_http):
    payload = {"rows": [{"key": "d1", "doc": {"body": "한글" * 200000}}]}
    _Handler.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    store = CouchDBHttpSourceStore(base_url=local_http, db="synthetic")
    with pytest.raises(CouchDBResponseTooLarge):
        store.get_many(["d1"], page_size=8, max_response_bytes=4096)


def test_get_many_without_cap_preserves_existing_callers(local_http):
    payload = {"rows": [{"key": "d1", "doc": {"body": "한글"}}]}
    _Handler.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    store = CouchDBHttpSourceStore(base_url=local_http, db="synthetic")
    assert store.get_many(["d1"], page_size=8)["d1"]["body"] == "한글"


def test_get_many_cap_allows_response_within_bound(local_http):
    payload = {"rows": [{"key": "d1", "doc": {"body": "한글"}}]}
    _Handler.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    store = CouchDBHttpSourceStore(base_url=local_http, db="synthetic")
    result = store.get_many(["d1"], page_size=8, max_response_bytes=4096)
    assert result["d1"]["body"] == "한글"

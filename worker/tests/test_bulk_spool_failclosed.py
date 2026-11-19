"""Spool failure paths: broken JSON, outer counter, stale cleanup, disk full."""

from __future__ import annotations

import errno
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.spool_reader import (
    SpoolDiskFull,
    SpoolError,
    _DOCUMENT_NAMESPACE,
    _write_manifest,
    cleanup_stale_spools,
    namespace_base,
    spool_base,
)


@pytest.fixture(autouse=True)
def clean_spools():
    base = spool_base()
    for child in list(base.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
    yield
    for child in list(base.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


class _BrokenHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"body": "unterminated'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def test_broken_json_fails_closed_and_cleans_spool():
    server = HTTPServer(("127.0.0.1", 0), _BrokenHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = CouchDBHttpSourceStore(
            base_url=f"http://127.0.0.1:{server.server_port}", db="synthetic"
        )
        with pytest.raises(SpoolError):
            store.open_document_spool("doc", expected_hash="sha256:" + "0" * 64)
        assert not [p for p in namespace_base(_DOCUMENT_NAMESPACE).iterdir() if p.name != "namespace.lock"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_outer_cli_failure_records_allowlisted_reason(monkeypatch, capsys):
    from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli

    def boom(**kwargs):
        raise ValueError("synthetic failure")

    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: object())
    monkeypatch.setattr(cli, "run_couchdb_bulk_semantic_projection", boom)
    rc = cli.main(["--ledger", ":memory:"])
    report = json.loads(capsys.readouterr().err)
    assert rc == 1
    assert report["failure_reasons"] == {"source_invalid": 1}
    assert "synthetic failure" not in json.dumps(report)


def test_stale_partial_spool_from_dead_worker_is_removed():
    stale = namespace_base(_DOCUMENT_NAMESPACE) / "stale-partial"
    stale.mkdir(mode=0o700, exist_ok=True)
    (stale / "manifest.json").write_text(
        json.dumps({"state": "partial", "pid": 99999999, "worker_id": "dead"})
    )
    cleanup_stale_spools(current_worker_id="live")
    assert not stale.exists()


def test_disk_full_fails_closed_without_partial_spool():
    directory = namespace_base(_DOCUMENT_NAMESPACE) / "diskfull"
    directory.mkdir(mode=0o700, exist_ok=True)
    original_open = Path.open

    def enospc(self, *args, **kwargs):
        if args and "w" in str(args[0]):
            raise OSError(errno.ENOSPC, "fake disk full")
        return original_open(self, *args, **kwargs)

    with patch.object(Path, "open", enospc):
        with pytest.raises(SpoolDiskFull):
            _write_manifest(directory, {"state": "partial"})
    assert not list(directory.iterdir())

"""Local synthetic spool lifecycle and fail-closed tests (no production I/O)."""

from __future__ import annotations
import errno
import fcntl
import hashlib
import json
import os
import signal
import sys
import subprocess
import threading
from contextlib import nullcontext
import time
from pathlib import Path

import pytest
from agent_knowledge.couchdb_source import couchdb_currentness_stream as cs
from agent_knowledge.couchdb_source import couchdb_http_store as chs
from agent_knowledge.couchdb_source import spool_reader as sr

_LIB = Path(__file__).resolve().parents[1] / "lib"
_DOC_ID = "synthetic-doc"
_HASH = "sha256:" + "a" * 64
_BODY = "synthetic body for spool lifecycle"


def _isolate_spool(tmp_path: Path, monkeypatch) -> Path:
    base = tmp_path / "spool-root"
    base.mkdir(mode=0o700)
    monkeypatch.setattr(sr, "spool_base", lambda: base)
    monkeypatch.setenv("NEURON_BULK_WORKER_ID", "parent-worker")
    return base


def _write_manifest(directory: Path, *, state: str, pid: int) -> None:
    sr._write_manifest(
        directory,
        {
            "schema_version": "bulk_spool.v1",
            "state": state,
            "doc_id": _DOC_ID,
            "worker_id": "child-worker",
            "pid": pid,
            "content_hash": _HASH,
            "body_bytes": len(_BODY.encode("utf-8")),
            "response_bytes": 1024,
            "metadata": {"_id": _DOC_ID},
            "updated_at": time.time(),
        },
    )


def _spawn_holder(base: Path, *, state: str) -> subprocess.Popen:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--hold-spool",
        str(base),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["NEURON_BULK_WORKER_ID"] = "child-worker"
    env["NEURON_SPOOL_TEST_STATE"] = state
    return subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path.name}")


def test_sigkilled_partial_spool_is_removed_by_cleanup(tmp_path, monkeypatch):
    base = _isolate_spool(tmp_path, monkeypatch)
    directory = sr.spool_dir_for(_DOC_ID, _HASH)
    proc = _spawn_holder(base, state="partial")
    try:
        _wait_for(directory / sr._MANIFEST)
        _wait_for(directory / sr._BODY)
        assert json.loads((directory / sr._MANIFEST).read_text())["state"] == "partial"
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        assert sr.cleanup_stale_spools(current_worker_id="parent-worker") == 1
        assert not directory.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_live_locked_spool_is_not_removed_by_cleanup(tmp_path, monkeypatch):
    base = _isolate_spool(tmp_path, monkeypatch)
    directory = sr.spool_dir_for(_DOC_ID, _HASH)
    proc = _spawn_holder(base, state="complete")
    try:
        _wait_for(directory / sr._MANIFEST)
        assert sr.cleanup_stale_spools(current_worker_id="parent-worker") == 0
        assert directory.exists()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_second_process_cannot_acquire_same_spool(tmp_path, monkeypatch):
    base = _isolate_spool(tmp_path, monkeypatch)
    proc = _spawn_holder(base, state="partial")
    try:
        _wait_for(sr.spool_dir_for(_DOC_ID, _HASH) / sr._LOCK)
        with pytest.raises(sr.SpoolBusy):
            sr.acquire_spool(_DOC_ID, _HASH, worker_id="other-worker")
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_spool_files_are_private_and_no_temp_manifest_leaks(tmp_path, monkeypatch):
    base = _isolate_spool(tmp_path, monkeypatch)
    directory, lease = sr.acquire_spool(_DOC_ID, _HASH, worker_id="parent-worker")
    try:
        body = directory / sr._BODY
        body.write_text(_BODY, encoding="utf-8")
        os.chmod(body, 0o600)
        _write_manifest(directory, state="complete", pid=os.getpid())
        assert oct(base.stat().st_mode & 0o777) == "0o700"
        assert oct(directory.stat().st_mode & 0o777) == "0o700"
        assert oct((directory / sr._LOCK).stat().st_mode & 0o777) == "0o600"
        assert oct((directory / sr._MANIFEST).stat().st_mode & 0o777) == "0o600"
        assert oct(body.stat().st_mode & 0o777) == "0o600"
        assert not list(directory.glob(".manifest-*"))
    finally:
        lease.close()
        sr.remove_spool(directory)


def test_disk_full_fail_closed_without_body_output(tmp_path, monkeypatch, capsys):
    base = _isolate_spool(tmp_path, monkeypatch)
    directory, lease = sr.acquire_spool(_DOC_ID, _HASH, worker_id="parent-worker")
    body_bytes = _BODY.encode("utf-8")
    digest = hashlib.sha256(body_bytes).hexdigest()
    expected = "sha256:" + digest
    raw = tmp_path / "response.json"
    raw.write_text(
        json.dumps(
            {
                "_id": _DOC_ID,
                "_rev": "1-x",
                "body": _BODY,
                "content_hash": expected,
                "doc_type": "conversation_chunk",
                "redaction_version": "r1",
            }
        ),
        encoding="utf-8",
    )
    original_open = Path.open

    class FailingWriter:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def write(self, data):
            raise OSError(errno.ENOSPC, "synthetic disk full")

        def __getattr__(self, name):
            return getattr(self._handle, name)

    def failing_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if Path(path).name == sr._BODY and "w" in (args[0] if args else kwargs.get("mode", "r")):
            return FailingWriter(handle)
        return handle

    monkeypatch.setattr(Path, "open", failing_open)
    try:
        with pytest.raises(sr.SpoolDiskFull):
            sr.parse_spooled_document(
                raw,
                expected_hash=expected,
                max_body_bytes=len(body_bytes),
                directory=directory,
                doc_id=_DOC_ID,
                worker_id="parent-worker",
                response_bytes=raw.stat().st_size,
            )
        captured = capsys.readouterr()
        assert _BODY not in captured.out
        assert _BODY not in captured.err
        assert not (directory / sr._BODY).exists()
        assert not (directory / sr._MANIFEST).exists()
    finally:
        lease.close()
        sr.remove_spool(directory)


def _spawn_currentness_child(base: Path, mode: str) -> subprocess.Popen:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        f"--{mode}",
        str(base),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["NEURON_BULK_WORKER_ID"] = "crash-child"
    return subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_document_cleanup_preserves_live_http_spool_from_production_call(tmp_path, monkeypatch):
    base = _isolate_spool(tmp_path, monkeypatch)
    proc = _spawn_currentness_child(base, "http-spool")
    live = None
    try:
        live = _wait_for_namespace_dir(sr._CURRENTNESS_HTTP_NAMESPACE)
        assert sr.cleanup_stale_spools(current_worker_id="other-worker") == 0
        assert live.exists()
    finally:
        proc.kill()
        proc.wait(timeout=10)
        assert sr.cleanup_currentness_spools(current_worker_id="other-worker") == 1
        assert not live.exists()


def _wait_for_namespace_dir(namespace: str, timeout: float = 10.0) -> Path:
    base = sr.namespace_base(namespace)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dirs = [p for p in base.iterdir() if p.is_dir(follow_symlinks=False)]
        if dirs:
            return dirs[0]
        time.sleep(0.05)
    raise AssertionError(f"no {namespace} spool created")


def test_http_currentness_sigkill_is_cleaned_by_next_production_call(tmp_path, monkeypatch):
    base = _isolate_spool(tmp_path, monkeypatch)
    proc = _spawn_currentness_child(base, "http-spool")
    stale = None
    try:
        stale = _wait_for_namespace_dir(sr._CURRENTNESS_HTTP_NAMESPACE)
        proc.kill()
        proc.wait(timeout=10)
        assert stale.exists()

        class Response:
            status_code = 200
            body = b'{"docs":[],"bookmark":""}'

        def transport(method, url, headers, body):
            del method, url, headers, body
            return Response()

        store = chs.CouchDBHttpSourceStore(
            base_url="http://127.0.0.1:9",
            db="synthetic",
            transport=transport,
        )
        rows = list(store.iter_by_session_descriptors(
            session_id_hash="s",
            doc_type="conversation_chunk",
            fields=None,
            selector=None,
            page_size=8,
            max_page_bytes=64 * 1024,
        ))
        assert rows == []
        assert not stale.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_canonical_currentness_sigkill_is_cleaned_by_next_production_call(tmp_path, monkeypatch):
    base = _isolate_spool(tmp_path, monkeypatch)
    proc = _spawn_currentness_child(base, "canonical-spool")
    stale = None
    try:
        stale = _wait_for_namespace_dir(sr._CURRENTNESS_CANONICAL_NAMESPACE)
        proc.kill()
        proc.wait(timeout=10)
        assert stale.exists()

        raw = tmp_path / "next-page.json"
        raw.write_bytes(b'{"docs":[],"bookmark":""}')
        rows, bookmark = cs.parse_spooled_descriptor_page(
            raw, max_page_bytes=1024, max_items=8
        )
        assert rows == [] and bookmark == ""
        assert not stale.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_document_cleanup_never_removes_live_currentness_spool(tmp_path, monkeypatch):
    _isolate_spool(tmp_path, monkeypatch)
    directory, lease = sr.create_owned_spool(
        sr._CURRENTNESS_CANONICAL_NAMESPACE,
        prefix="neuron-currentness-canon-",
        kind="currentness-canonical",
        worker_id="parent-worker",
    )
    try:
        assert sr.cleanup_stale_spools(current_worker_id="other-worker") == 0
        assert sr.cleanup_currentness_spools(current_worker_id="other-worker") == 0
        assert directory.exists()
    finally:
        lease.close()
        sr.remove_spool(directory)


def test_cleanup_ignores_symlinks(tmp_path, monkeypatch):
    base = _isolate_spool(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = sr.namespace_base(sr._CURRENTNESS_HTTP_NAMESPACE) / "link"
    link.symlink_to(outside, target_is_directory=True)
    assert sr.cleanup_currentness_spools() == 0
    assert link.is_symlink() and outside.exists()


def test_owned_spool_creation_disk_fail_has_no_residual(tmp_path, monkeypatch):
    _isolate_spool(tmp_path, monkeypatch)
    original = sr._write_manifest

    def failing_manifest(directory, metadata):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(sr, "_write_manifest", failing_manifest)
    with pytest.raises(OSError):
        sr.create_owned_spool(
            sr._CURRENTNESS_HTTP_NAMESPACE,
            prefix="neuron-currentness-http-",
            kind="currentness-http",
        )
    monkeypatch.setattr(sr, "_write_manifest", original)
    leftovers = [
        p
        for p in sr.namespace_base(sr._CURRENTNESS_HTTP_NAMESPACE).iterdir()
        if p.name != sr._NAMESPACE_LOCK
    ]
    assert leftovers == []


def _spawn_race_child(base: Path, namespace: str, barrier: Path, *, unsafe: bool) -> subprocess.Popen:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--race-create",
        str(base),
        namespace,
        str(barrier),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    if unsafe:
        env["NEURON_TEST_DISABLE_NAMESPACE_LOCK"] = "1"
    else:
        env.pop("NEURON_TEST_DISABLE_NAMESPACE_LOCK", None)
    return subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.mark.parametrize(
    "namespace",
    [
        sr._DOCUMENT_NAMESPACE,
        sr._CURRENTNESS_HTTP_NAMESPACE,
        sr._CURRENTNESS_CANONICAL_NAMESPACE,
    ],
)
def test_namespace_lock_prevents_create_cleanup_race(tmp_path, monkeypatch, namespace):
    base = _isolate_spool(tmp_path, monkeypatch)
    barrier = tmp_path / "barrier"
    barrier.mkdir(mode=0o700)
    created = barrier / "created"
    release = barrier / "release"
    proc = _spawn_race_child(base, namespace, barrier, unsafe=False)
    try:
        _wait_for(created)
        directory = Path(created.read_text())
        result: dict[str, int] = {}

        def cleanup():
            if namespace == sr._DOCUMENT_NAMESPACE:
                result["removed"] = sr.cleanup_stale_spools()
            else:
                result["removed"] = sr.cleanup_currentness_spools()

        thread = threading.Thread(target=cleanup)
        thread.start()
        thread.join(timeout=0.5)
        assert thread.is_alive(), "cleanup was not serialized behind namespace creation"
        release.write_text("1")
        thread.join(timeout=10)
        assert result.get("removed") == 0
        assert directory.exists()
        _wait_for(barrier / "done")
    finally:
        release.write_text("1")
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


@pytest.mark.parametrize(
    "namespace",
    [
        sr._DOCUMENT_NAMESPACE,
        sr._CURRENTNESS_HTTP_NAMESPACE,
        sr._CURRENTNESS_CANONICAL_NAMESPACE,
    ],
)
def test_race_reproduction_without_namespace_lock(tmp_path, monkeypatch, namespace):
    base = _isolate_spool(tmp_path, monkeypatch)
    monkeypatch.setattr(sr, "_namespace_lock", lambda _namespace: nullcontext())
    barrier = tmp_path / "barrier"
    barrier.mkdir(mode=0o700)
    created = barrier / "created"
    release = barrier / "release"
    proc = _spawn_race_child(base, namespace, barrier, unsafe=True)
    try:
        _wait_for(created)
        directory = Path(created.read_text())
        if namespace == sr._DOCUMENT_NAMESPACE:
            removed = sr.cleanup_stale_spools()
        else:
            removed = sr.cleanup_currentness_spools()
        assert removed == 1
        assert not directory.exists()
    finally:
        release.write_text("1")
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_pid_reuse_and_expired_ttl_never_delete_live_worker(tmp_path, monkeypatch):
    _isolate_spool(tmp_path, monkeypatch)
    directory = sr.namespace_base(sr._CURRENTNESS_HTTP_NAMESPACE) / "live-pid-expired"
    directory.mkdir(mode=0o700)
    handle = (directory / sr._LOCK).open("a+b")
    os.chmod(directory / sr._LOCK, 0o600)
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        sr._write_manifest(directory, {"state": "partial", "pid": os.getpid(), "created_at": 0})
        assert sr.cleanup_currentness_spools() == 0
        assert directory.exists()
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
        sr.remove_spool(directory)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(2)
    mode, base_arg = sys.argv[1], Path(sys.argv[2])
    if mode == "--race-create" and len(sys.argv) != 5:
        raise SystemExit(2)
    if mode != "--race-create" and len(sys.argv) != 3:
        raise SystemExit(2)
    sys.path.insert(0, str(_LIB))
    from agent_knowledge.couchdb_source import couchdb_currentness_stream as child_cs
    from agent_knowledge.couchdb_source import couchdb_http_store as child_chs
    from agent_knowledge.couchdb_source import spool_reader as child_sr

    child_sr.spool_base = lambda: base_arg
    child_cs.spool_base = lambda: base_arg
    child_chs.spool_base = lambda: base_arg

    if mode == "--race-create":
        namespace_arg, barrier_arg = sys.argv[3], Path(sys.argv[4])
        if os.environ.get("NEURON_TEST_DISABLE_NAMESPACE_LOCK"):
            child_sr._namespace_lock = lambda _namespace: nullcontext()
        original_mkdtemp = child_sr.tempfile.mkdtemp

        def barrier_mkdtemp(*args, **kwargs):
            path = Path(original_mkdtemp(*args, **kwargs))
            (barrier_arg / "created").write_text(str(path))
            while not (barrier_arg / "release").exists():
                time.sleep(0.02)
            return str(path)

        child_sr.tempfile.mkdtemp = barrier_mkdtemp
        child_sr.create_owned_spool(
            namespace_arg, prefix="neuron-race-", kind="race-test"
        )
        (barrier_arg / "done").write_text("1")
        time.sleep(30)
    elif mode == "--hold-spool":
        directory, _lease = child_sr.acquire_spool(
            _DOC_ID, _HASH, worker_id=os.environ.get("NEURON_BULK_WORKER_ID", "child-worker")
        )
        (directory / sr._BODY).write_text(_BODY, encoding="utf-8")
        child_sr._write_manifest(directory, {
            "schema_version": "bulk_spool.v1",
            "state": os.environ.get("NEURON_SPOOL_TEST_STATE", "partial"),
            "doc_id": _DOC_ID,
            "worker_id": os.environ.get("NEURON_BULK_WORKER_ID", "child-worker"),
            "pid": os.getpid(),
            "content_hash": _HASH,
            "body_bytes": len(_BODY.encode("utf-8")),
            "response_bytes": 1024,
            "metadata": {"_id": _DOC_ID},
            "updated_at": time.time(),
        })
        time.sleep(60)
    elif mode == "--http-spool":
        class BlockingResponse:
            status_code = 200
            body = b'{"docs":[],"bookmark":""}'

        def blocking_transport(method, url, headers, body):
            del method, url, headers, body
            time.sleep(60)
            return BlockingResponse()

        store = child_chs.CouchDBHttpSourceStore(
            base_url="http://127.0.0.1:9",
            db="synthetic",
            transport=blocking_transport,
        )
        list(store.iter_by_session_descriptors(
            session_id_hash="s", doc_type="conversation_chunk", fields=None,
            selector=None, page_size=8, max_page_bytes=64 * 1024,
        ))
    elif mode == "--canonical-spool":
        page = base_arg.parent / "crash-page.json"
        page.write_bytes(json.dumps({
            "docs": [{
                "_id": "x",
                "doc_type": "conversation_chunk",
                "session_id_hash": "s",
                "body": "a" * 2_000_000,
            }],
            "bookmark": "",
        }).encode("utf-8"))
        child_cs._COPY_SIZE = 1
        child_cs.parse_spooled_descriptor_page(
            page, max_page_bytes=len(page.read_bytes()), max_items=8
        )
    else:
        raise SystemExit(2)

"""Private, bounded, resumable spool reader for synthetic CouchDB documents."""

from __future__ import annotations
import codecs
import errno
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
_SPOOL_PREFIX = "neuron-bulk-spool"
_DOCUMENT_NAMESPACE = "documents"
_CURRENTNESS_HTTP_NAMESPACE = "currentness-http"
_CURRENTNESS_CANONICAL_NAMESPACE = "currentness-canonical"
_MANIFEST = "manifest.json"
_LOCK = "lock"
_BODY = "body.bin"
_NAMESPACE_LOCK = "namespace.lock"

class SpoolError(Exception):
    pass


class SpoolBudgetExceeded(SpoolError):
    pass


class SpoolDiskFull(SpoolError):
    pass


class SpoolBusy(SpoolError):
    pass


def spool_base() -> Path:
    path = Path(tempfile.gettempdir()) / f"{_SPOOL_PREFIX}-{os.getuid()}"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def namespace_base(namespace: str) -> Path:
    if namespace not in {
        _DOCUMENT_NAMESPACE,
        _CURRENTNESS_HTTP_NAMESPACE,
        _CURRENTNESS_CANONICAL_NAMESPACE,
    }:
        raise ValueError("unknown spool namespace")
    path = spool_base() / namespace
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


@contextmanager
def _namespace_lock(namespace: str) -> Iterator[None]:
    base = namespace_base(namespace)
    lock_path = base / _NAMESPACE_LOCK
    handle = lock_path.open("a+b")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()

def current_worker_id() -> str:
    return os.environ.get("NEURON_BULK_WORKER_ID") or f"{os.getpid()}-{threading.get_ident()}"


def body_budget_bytes() -> int:
    return max(1, int(os.environ.get("NEURON_BULK_SPOOL_BODY_BUDGET_BYTES") or 32 * 1024 * 1024))


def response_budget_bytes() -> int:
    return max(1, int(os.environ.get("NEURON_BULK_SPOOL_RESPONSE_BUDGET_BYTES") or 48 * 1024 * 1024))


def spool_ttl_seconds() -> int:
    return max(1, int(os.environ.get("NEURON_BULK_SPOOL_TTL_SECONDS") or 3600))


def spool_dir_for(doc_id: str, expected_hash: str) -> Path:
    key = hashlib.sha256(f"{doc_id}|{expected_hash}".encode("utf-8")).hexdigest()[:24]
    return namespace_base(_DOCUMENT_NAMESPACE) / key


@dataclass
class SpoolLease:
    directory: Path
    handle: object

    def close(self) -> None:
        try:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()


def acquire_spool(doc_id: str, expected_hash: str, *, worker_id: str) -> tuple[Path, SpoolLease]:
    del worker_id
    with _namespace_lock(_DOCUMENT_NAMESPACE):
        directory = spool_dir_for(doc_id, expected_hash)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        lock_path = directory / _LOCK
        handle = lock_path.open("a+b")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise SpoolBusy("spool is owned by another worker") from None
        return directory, SpoolLease(directory, handle)


def _write_manifest(directory: Path, manifest: dict) -> None:
    target = directory / _MANIFEST
    tmp = directory / (".manifest-" + uuid4_hex())
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        if _disk_full(exc):
            raise SpoolDiskFull("temporary spool disk full") from None
        raise


def uuid4_hex() -> str:
    import uuid
    return uuid.uuid4().hex


def read_manifest(directory: Path) -> dict | None:
    path = directory / _MANIFEST
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def remove_spool(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def create_owned_spool(
    namespace: str,
    *,
    prefix: str,
    kind: str,
    worker_id: str | None = None,
) -> tuple[Path, SpoolLease]:
    """Atomically create, lock, and claim a spool within its namespace."""

    with _namespace_lock(namespace):
        base = namespace_base(namespace)
        directory = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        os.chmod(directory, 0o700)
        lock_path = directory / _LOCK
        handle = lock_path.open("a+b")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _write_manifest(directory, {
                "schema_version": "owned_spool.v1",
                "state": "partial",
                "kind": kind,
                "namespace": namespace,
                "worker_id": worker_id or current_worker_id(),
                "pid": os.getpid(),
                "created_at": time.time(),
            })
        except BaseException:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return directory, SpoolLease(directory, handle)


def _cleanup_namespace(
    namespace: str,
    *,
    current_worker_id: str | None = None,
) -> int:
    del current_worker_id  # API compatibility; safety is lock/manifest based.
    with _namespace_lock(namespace):
        base = namespace_base(namespace)
        removed = 0
        for entry in list(base.iterdir()):
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name == _NAMESPACE_LOCK:
                continue
            lock_path = entry / _LOCK
            if lock_path.is_symlink():
                continue
            if not lock_path.exists():
                with lock_path.open("a+b"):
                    pass
                os.chmod(lock_path, 0o600)
            handle = lock_path.open("a+b")
            try:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                manifest = read_manifest(entry)
                age = time.time() - entry.stat().st_mtime
                expired = age > spool_ttl_seconds()
                pid = int((manifest or {}).get("pid") or 0)
                live_pid = _pid_alive(pid)
                stale = False
                if manifest is None:
                    stale = True
                elif live_pid:
                    stale = False
                elif manifest.get("state") == "partial":
                    stale = expired or not live_pid
                elif expired:
                    stale = True
                if stale:
                    remove_spool(entry)
                    removed += 1
            finally:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                finally:
                    handle.close()
        return removed


def cleanup_stale_spools(*, current_worker_id: str | None = None) -> int:
    """Document-spool cleanup only; never scans currentness spool namespaces."""

    return _cleanup_namespace(
        _DOCUMENT_NAMESPACE, current_worker_id=current_worker_id
    )


def cleanup_currentness_spools(*, current_worker_id: str | None = None) -> int:
    """Clean only stale currentness HTTP/canonical spools from prior crashes."""

    return (
        _cleanup_namespace(
            _CURRENTNESS_HTTP_NAMESPACE, current_worker_id=current_worker_id
        )
        + _cleanup_namespace(
            _CURRENTNESS_CANONICAL_NAMESPACE, current_worker_id=current_worker_id
        )
    )


class SpooledBody:
    """Caller-owned 0600 body file inside a 0700 spool directory."""

    def __init__(self, path: Path, root: Path, digest: str, byte_length: int,
                 metadata: dict[str, str], *, lease: SpoolLease | None = None) -> None:
        self.path = path
        self._root = root
        self.digest = digest
        self.byte_length = byte_length
        self.metadata = metadata
        self._lease = lease
        self._closed = False

    def iter_bytes(self, page_size: int = 64 * 1024) -> Iterator[bytes]:
        with self.path.open("rb") as handle:
            while True:
                page = handle.read(page_size)
                if not page:
                    return
                yield page

    def close(self, *, retain: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        if self._lease is not None:
            self._lease.close()
        if not retain:
            remove_spool(self._root)


class SpooledText:
    """Page iterator over a spooled body; never materializes the full source text."""

    def __init__(self, body: SpooledBody) -> None:
        self.body = body
        self.byte_length = body.byte_length
        self.content_hash = body.digest

    def iter_pages(self, size: int = 64 * 1024) -> Iterator[str]:
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending = ""
        for raw in self.body.iter_bytes(size):
            text = pending + decoder.decode(raw, final=False)
            while len(text) > size:
                cut = text.rfind("\n", 0, size)
                if cut < size // 2:
                    cut = size
                yield text[:cut]
                text = text[cut:]
            pending = text
        tail = pending + decoder.decode(b"", final=True)
        if tail:
            yield tail

    def close(self, *, retain: bool = False) -> None:
        self.body.close(retain=retain)


def _disk_full(exc: OSError) -> bool:
    return exc.errno in (errno.ENOSPC, errno.EDQUOT)


def _find_key(handle, key: bytes) -> int:
    target = b'"' + key + b'"'
    offset = 0
    overlap = b""
    handle.seek(0)
    while True:
        chunk = handle.read(64 * 1024)
        if not chunk:
            raise SpoolError("missing JSON field")
        data = overlap + chunk
        position = data.find(target)
        if position >= 0:
            return offset - len(overlap) + position + len(target)
        overlap = data[-(len(target) - 1):]
        offset += len(chunk)


def _read_string(handle, start: int, sink=None) -> str:
    handle.seek(start)
    while True:
        char = handle.read(1)
        if not char or char not in (b" ", b"\n", b"\r", b"\t", b":"):
            break
    if char != b'"':
        raise SpoolError("expected JSON string")
    pieces: list[str] = []
    while True:
        byte = handle.read(1)
        if not byte:
            raise SpoolError("unterminated JSON string")
        if byte == b'"':
            break
        if byte != b"\\":
            if sink is not None:
                sink(byte)
            else:
                pieces.append(byte.decode("utf-8"))
            continue
        escaped = handle.read(1)
        if escaped == b"u":
            raw = handle.read(4)
            value = json.loads(b'"\\u' + raw + b'"')
            if 0xD800 <= ord(value) <= 0xDBFF:
                marker = handle.read(2)
                low = handle.read(4)
                if marker != b"\\u":
                    raise SpoolError("invalid surrogate pair")
                value = json.loads(b'"\\u' + raw + b"\\u" + low + b'"')
            encoded = value.encode("utf-8")
        else:
            encoded = json.loads(b'"\\' + escaped + b'"').encode("utf-8")
        if sink is not None:
            sink(encoded)
        else:
            pieces.append(encoded.decode("utf-8"))
    return "".join(pieces)


def load_complete_spool(directory: Path, *, expected_hash: str,
                        lease: SpoolLease) -> SpooledBody | None:
    manifest = read_manifest(directory)
    body_path = directory / _BODY
    if manifest is None or manifest.get("state") != "complete" or not body_path.exists():
        return None
    if str(manifest.get("content_hash") or "") != expected_hash:
        return None
    digest = hashlib.sha256()
    total = 0
    try:
        with body_path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except OSError:
        return None
    if "sha256:" + digest.hexdigest() != expected_hash or total != int(manifest.get("body_bytes") or -1):
        return None
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    return SpooledBody(body_path, directory, expected_hash, total,
                       {str(k): str(v) for k, v in metadata.items()}, lease=lease)


def parse_spooled_document(raw_path: Path, *, expected_hash: str, max_body_bytes: int,
                           directory: Path, doc_id: str, worker_id: str,
                           response_bytes: int) -> SpooledBody:
    body_path = directory / _BODY
    digest = hashlib.sha256()
    total = 0
    try:
        with raw_path.open("rb") as handle:
            body_start = _find_key(handle, b"body")
            with body_path.open("wb") as sink:
                def write(chunk: bytes) -> None:
                    nonlocal total
                    total += len(chunk)
                    if total > max_body_bytes:
                        raise SpoolBudgetExceeded("body exceeds temporary spool budget")
                    try:
                        sink.write(chunk)
                    except OSError as exc:
                        if _disk_full(exc):
                            raise SpoolDiskFull("temporary spool disk full") from None
                        raise
                    digest.update(chunk)

                _read_string(handle, body_start, sink=write)
            metadata: dict[str, str] = {}
            for key in ("_id", "_rev", "content_hash", "doc_type", "redaction_version"):
                metadata[key] = _read_string(handle, _find_key(handle, key.encode()))
        if "sha256:" + digest.hexdigest() != expected_hash:
            raise SpoolError("document body hash mismatch")
        if metadata.get("content_hash") != expected_hash:
            raise SpoolError("document hash does not match planned source hash")
        _write_manifest(directory, {
            "schema_version": "bulk_spool.v1", "state": "complete",
            "doc_id": doc_id, "worker_id": worker_id, "pid": os.getpid(),
            "content_hash": expected_hash, "body_bytes": total,
            "response_bytes": response_bytes, "metadata": metadata,
            "updated_at": time.time(),
        })
        raw_path.unlink(missing_ok=True)
        return SpooledBody(body_path, directory, expected_hash, total, metadata)
    except Exception:
        body_path.unlink(missing_ok=True)
        raise

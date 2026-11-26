"""Bounded, streaming canonical source descriptors for CouchDB currentness.

The active source-revision path must hash every public source field, including
large transcript bodies, with the same canonical JSON byte sequence as the
in-memory implementation.  This module reads a spooled CouchDB ``_find``
response incrementally and writes canonical JSON bytes into a SHA-256 sink.
Large scalar strings are streamed; nested objects are sorted through 0600
temporary files, so resident memory stays bounded by buffers and small
metadata fields rather than document size.

Contract limits are stricter than an unbounded in-memory ``json.loads``: JSON
depth is 64, object keys and captured scalar values are 16,384 characters,
number tokens are 1,024 bytes, and response pages/temp spools have explicit
byte budgets. Standard JSON is canonicalized byte-for-byte; Python's
nonstandard ``NaN``/``Infinity`` extensions are rejected rather than accepted.
Unsupported or oversized input fails closed; this is not a claim of arbitrary
document processing.
"""

from __future__ import annotations
import hashlib
import json
import math
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .spool_reader import (
    _CURRENTNESS_CANONICAL_NAMESPACE,
    cleanup_currentness_spools,
    create_owned_spool,
    current_worker_id,
)

_READ_SIZE = 64 * 1024
_COPY_SIZE = 64 * 1024
_MAX_DEPTH = 64
_MAX_TOKEN_BYTES = 1024
_MAX_KEY_CHARS = 16 * 1024
_MAX_CAPTURE_CHARS = 16 * 1024
_MAX_OBJECT_KEYS = 100_000
_MAX_ARRAY_ITEMS = 100_000

_EXCLUDED_TOP_LEVEL_FIELDS = frozenset({"_rev", "idempotency_key", "payload_hash", "envelope_fingerprint"})

_CAPTURE_FIELDS = frozenset(
    {
        "_id",
        "doc_type",
        "session_id_hash",
        "content_hash",
        "coverage_hash",
        "source_hash",
        "observed_at_start",
        "observed_at_end",
        "started_at",
        "ended_at",
        "turn_start_index",
        "turn_end_index",
        "part_index",
        "part_count",
        "char_start",
        "char_end",
        "evidence_index_start",
        "evidence_index_end",
        "redaction_version",
        "parser_version",
        "chunker_version",
        "source_snapshot_hash",
        "predecessor_manifest_hash",
        "source_snapshot_schema_version",
        "source_snapshot_scope",
        "source_snapshot_origin_id",
        "current_source_scope",
        "supersedes_source_document_hash",
    }
)


class CurrentnessStreamError(RuntimeError):
    code = "source_revision_stream_error"


class CurrentnessStreamBudgetExceeded(CurrentnessStreamError):
    code = "source_revision_stream_budget_exceeded"


class CurrentnessStreamInvalid(CurrentnessStreamError):
    code = "source_revision_stream_invalid"


def stream_spool_budget_bytes(max_page_bytes: int) -> int:
    """Canonical temporary bytes allowed while parsing one response page.

    JSON escaping can expand raw UTF-8 by at most 6x (``ensure_ascii=True``), so
    the default budget is six response pages.  The raw HTTP response spool is
    bounded separately by ``max_page_bytes``.
    """

    configured = os.environ.get("NEURON_SOURCE_CURRENTNESS_STREAM_SPOOL_BYTES")
    if configured:
        return max(1, int(configured))
    return max(1, int(max_page_bytes)) * 6


class _NullSink:
    __slots__ = ()

    def write(self, data: bytes) -> None:
        del data


@dataclass
class _HashSink:
    hash: object

    def __init__(self) -> None:
        self.hash = hashlib.sha256()

    def write(self, data: bytes) -> None:
        self.hash.update(data)

    def digest(self) -> str:
        return "sha256:" + self.hash.hexdigest()


@dataclass
class _ByteReader:
    handle: object
    _buffer: bytes = b""
    _pos: int = 0

    def _ensure(self, count: int) -> None:
        count = max(0, int(count))
        if len(self._buffer) - self._pos >= count:
            return
        chunks = [self._buffer[self._pos :]]
        remaining = count - len(chunks[0])
        while remaining > 0:
            chunk = self.handle.read(max(_READ_SIZE, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        self._buffer = b"".join(chunks)
        self._pos = 0

    def peek_byte(self) -> int:
        self._ensure(1)
        if self._pos >= len(self._buffer):
            return -1
        return self._buffer[self._pos]

    def peek_bytes(self, count: int) -> bytes:
        self._ensure(count)
        return self._buffer[self._pos : self._pos + count]

    def read_byte(self) -> int:
        value = self.peek_byte()
        if value < 0:
            raise CurrentnessStreamInvalid("unexpected end of JSON stream")
        self._pos += 1
        return value

    def read_bytes(self, count: int) -> bytes:
        count = int(count)
        if count < 0:
            raise CurrentnessStreamInvalid("invalid JSON token length")
        self._ensure(count)
        value = self._buffer[self._pos : self._pos + count]
        if len(value) != count:
            raise CurrentnessStreamInvalid("unexpected end of JSON stream")
        self._pos += count
        return value

    def skip_whitespace(self) -> None:
        while True:
            value = self.peek_byte()
            if value not in (0x20, 0x09, 0x0A, 0x0D):
                return
            self._pos += 1

    def at_end(self) -> bool:
        self.skip_whitespace()
        return self.peek_byte() < 0


class _TempSpool:
    def __init__(self, *, budget_bytes: int) -> None:
        cleanup_currentness_spools()
        self._root, self._lease = create_owned_spool(
            _CURRENTNESS_CANONICAL_NAMESPACE,
            prefix="neuron-currentness-canon-",
            kind="currentness-canonical",
            worker_id=current_worker_id(),
        )
        self._budget = max(1, int(budget_bytes))
        self._total = 0
        self._sizes: dict[Path, int] = {}
        self._counter = 0

    def new_file(self) -> Path:
        self._counter += 1
        path = self._root / f"value-{self._counter:08d}.json"
        handle = path.open("xb")
        os.chmod(path, 0o600)
        handle.close()
        return path

    def reserve(self, count: int) -> None:
        self._total += int(count)
        if self._total > self._budget:
            raise CurrentnessStreamBudgetExceeded("canonical temp spool budget exceeded")

    def record(self, path: Path, count: int) -> None:
        self._sizes[path] = int(count)

    def remove(self, path: Path) -> None:
        size = self._sizes.pop(path, 0)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self._total = max(0, self._total - size)

    def close(self) -> None:
        self._lease.close()
        shutil.rmtree(self._root, ignore_errors=True)


class _ValueSink:
    __slots__ = ("_spool", "_path", "_handle", "_count", "_closed")

    def __init__(self, spool: _TempSpool, path: Path) -> None:
        self._spool = spool
        self._path = path
        self._handle = path.open("wb")
        self._count = 0
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise CurrentnessStreamInvalid("canonical value sink is closed")
        self._spool.reserve(len(data))
        self._handle.write(data)
        self._count += len(data)

    def close(self) -> Path:
        if not self._closed:
            self._handle.close()
            self._closed = True
            self._spool.record(self._path, self._count)
        return self._path


def _emit_codepoint(codepoint: int, sink: object) -> None:
    if codepoint == 0x22:
        sink.write(b'\\"')
    elif codepoint == 0x5C:
        sink.write(b"\\\\")
    elif codepoint == 0x08:
        sink.write(b"\\b")
    elif codepoint == 0x09:
        sink.write(b"\\t")
    elif codepoint == 0x0A:
        sink.write(b"\\n")
    elif codepoint == 0x0C:
        sink.write(b"\\f")
    elif codepoint == 0x0D:
        sink.write(b"\\r")
    elif codepoint < 0x20:
        sink.write(f"\\u{codepoint:04x}".encode("ascii"))
    elif codepoint < 0x7F:
        sink.write(bytes((codepoint,)))
    elif codepoint <= 0xFFFF:
        sink.write(f"\\u{codepoint:04x}".encode("ascii"))
    else:
        adjusted = codepoint - 0x10000
        high = 0xD800 + (adjusted >> 10)
        low = 0xDC00 + (adjusted & 0x3FF)
        sink.write(f"\\u{high:04x}\\u{low:04x}".encode("ascii"))


def _hash_codepoint(hash_sink: object | None, codepoint: int) -> None:
    if hash_sink is None:
        return
    try:
        hash_sink.update(chr(codepoint).encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise CurrentnessStreamInvalid("invalid surrogate in body text") from exc

def _decode_hex4(reader: _ByteReader) -> int:
    token = reader.read_bytes(4)
    try:
        return int(token.decode("ascii"), 16)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CurrentnessStreamInvalid("invalid JSON unicode escape") from exc


def _decode_json_string(
    reader: _ByteReader,
    sink: object,
    *,
    capture: bool,
    hash_sink: object | None = None,
) -> str | None:
    if reader.read_byte() != 0x22:
        raise CurrentnessStreamInvalid("expected JSON string")
    sink.write(b'"')
    characters: list[str] | None = [] if capture else None
    while True:
        value = reader.read_byte()
        if value == 0x22:
            sink.write(b'"')
            break
        if value == 0x5C:
            escape = reader.read_byte()
            if escape == 0x75:
                codepoint = _decode_hex4(reader)
                if 0xD800 <= codepoint <= 0xDBFF:
                    pair = reader.peek_bytes(6)
                    if (
                        len(pair) == 6
                        and pair[0] == 0x5C
                        and pair[1] == 0x75
                    ):
                        try:
                            low = int(pair[2:6].decode("ascii"), 16)
                        except (UnicodeDecodeError, ValueError):
                            low = -1
                        if 0xDC00 <= low <= 0xDFFF:
                            reader.read_bytes(6)
                            codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
                _emit_codepoint(codepoint, sink)
                _hash_codepoint(hash_sink, codepoint)
                if characters is not None:
                    characters.append(chr(codepoint))
            else:
                simple = {
                    0x22: 0x22,
                    0x5C: 0x5C,
                    0x2F: 0x2F,
                    0x62: 0x08,
                    0x66: 0x0C,
                    0x6E: 0x0A,
                    0x72: 0x0D,
                    0x74: 0x09,
                }.get(escape)
                if simple is None:
                    raise CurrentnessStreamInvalid("invalid JSON string escape")
                _emit_codepoint(simple, sink)
                _hash_codepoint(hash_sink, simple)
                if characters is not None:
                    characters.append(chr(simple))
            continue
        if value < 0x20:
            raise CurrentnessStreamInvalid("unescaped control character in JSON string")
        if value < 0x80:
            codepoint = value
        else:
            if 0xC2 <= value <= 0xDF:
                length = 2
                minimum = 0x80
            elif 0xE0 <= value <= 0xEF:
                length = 3
                minimum = 0x800
            elif 0xF0 <= value <= 0xF4:
                length = 4
                minimum = 0x10000
            else:
                raise CurrentnessStreamInvalid("invalid UTF-8 lead byte in JSON string")
            rest = reader.read_bytes(length - 1)
            if any(byte < 0x80 or byte > 0xBF for byte in rest):
                raise CurrentnessStreamInvalid("invalid UTF-8 continuation byte in JSON string")
            codepoint = value & (0x7F >> length)
            for byte in rest:
                codepoint = (codepoint << 6) | (byte & 0x3F)
            if codepoint < minimum or 0xD800 <= codepoint <= 0xDFFF or codepoint > 0x10FFFF:
                raise CurrentnessStreamInvalid("invalid UTF-8 codepoint in JSON string")
        _emit_codepoint(codepoint, sink)
        _hash_codepoint(hash_sink, codepoint)
        if characters is not None:
            characters.append(chr(codepoint))
        if characters is not None and len(characters) > _MAX_CAPTURE_CHARS:
            raise CurrentnessStreamBudgetExceeded("captured JSON string exceeded limit")
    if characters is None:
        return None
    return "".join(characters)


def _canonicalize_number(reader: _ByteReader, sink: object) -> None:
    token = bytearray()
    while True:
        value = reader.peek_byte()
        if value < 0 or value in (0x20, 0x09, 0x0A, 0x0D, 0x2C, 0x5D, 0x7D):
            break
        token.append(reader.read_byte())
        if len(token) > _MAX_TOKEN_BYTES:
            raise CurrentnessStreamBudgetExceeded("JSON number token exceeded limit")
    if not token:
        raise CurrentnessStreamInvalid("empty JSON number token")
    try:
        parsed = json.loads(token.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentnessStreamInvalid("invalid JSON number token") from exc
    if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
        raise CurrentnessStreamInvalid("invalid JSON number token")
    if isinstance(parsed, float) and not math.isfinite(parsed):
        raise CurrentnessStreamInvalid("non-finite JSON number token")
    sink.write(json.dumps(parsed, ensure_ascii=True, separators=(",", ":")).encode("ascii"))


def _canonicalize_literal(reader: _ByteReader, sink: object) -> None:
    value = reader.peek_byte()
    literal = {0x74: b"true", 0x66: b"false", 0x6E: b"null"}.get(value)
    if literal is None:
        raise CurrentnessStreamInvalid("unexpected JSON token")
    if reader.read_bytes(len(literal)) != literal:
        raise CurrentnessStreamInvalid("invalid JSON literal")
    sink.write(literal)


def _canonicalize_value(
    reader: _ByteReader,
    sink: object,
    spool: _TempSpool,
    *,
    depth: int,
    hash_sink: object | None = None,
) -> None:
    if depth > _MAX_DEPTH:
        raise CurrentnessStreamBudgetExceeded("JSON nesting depth exceeded limit")
    reader.skip_whitespace()
    value = reader.peek_byte()
    if value == 0x7B:
        _canonicalize_object(reader, sink, spool, depth=depth + 1)
    elif value == 0x5B:
        reader.read_byte()
        sink.write(b"[")
        reader.skip_whitespace()
        if reader.peek_byte() == 0x5D:
            reader.read_byte()
            sink.write(b"]")
            return
        count = 0
        while True:
            count += 1
            if count > _MAX_ARRAY_ITEMS:
                raise CurrentnessStreamBudgetExceeded("JSON array item count exceeded limit")
            _canonicalize_value(reader, sink, spool, depth=depth + 1)
            reader.skip_whitespace()
            separator = reader.read_byte()
            if separator == 0x2C:
                sink.write(b",")
                reader.skip_whitespace()
                continue
            if separator != 0x5D:
                raise CurrentnessStreamInvalid("invalid JSON array separator")
            sink.write(b"]")
            return
    elif value == 0x22:
        _decode_json_string(reader, sink, capture=False, hash_sink=hash_sink)
    elif value in (0x2D, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39):
        _canonicalize_number(reader, sink)
    else:
        _canonicalize_literal(reader, sink)


def _canonicalize_object(
    reader: _ByteReader,
    sink: object,
    spool: _TempSpool,
    *,
    depth: int,
) -> None:
    if reader.read_byte() != 0x7B:
        raise CurrentnessStreamInvalid("expected JSON object")
    entries: dict[str, Path] = {}
    reader.skip_whitespace()
    if reader.peek_byte() == 0x7D:
        reader.read_byte()
        sink.write(b"{}")
        return
    while True:
        reader.skip_whitespace()
        key = _decode_json_string(reader, _NullSink(), capture=True)
        if key is None or len(key) > _MAX_KEY_CHARS:
            raise CurrentnessStreamBudgetExceeded("JSON object key exceeded limit")
        reader.skip_whitespace()
        if reader.read_byte() != 0x3A:
            raise CurrentnessStreamInvalid("expected colon in JSON object")
        path = spool.new_file()
        value_sink = _ValueSink(spool, path)
        _canonicalize_value(reader, value_sink, spool, depth=depth)
        value_sink.close()
        if key in entries:
            spool.remove(entries[key])
        entries[key] = path
        if len(entries) > _MAX_OBJECT_KEYS:
            raise CurrentnessStreamBudgetExceeded("JSON object key count exceeded limit")
        reader.skip_whitespace()
        separator = reader.read_byte()
        if separator == 0x2C:
            continue
        if separator != 0x7D:
            raise CurrentnessStreamInvalid("invalid JSON object separator")
        break
    sink.write(b"{")
    ordered = sorted(entries.items(), key=lambda item: item[0])
    for index, (key, path) in enumerate(ordered):
        if index:
            sink.write(b",")
        sink.write(json.dumps(key, ensure_ascii=True).encode("ascii"))
        sink.write(b":")
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_COPY_SIZE)
                if not chunk:
                    break
                sink.write(chunk)
        spool.remove(path)
    sink.write(b"}")


def _skip_value(reader: _ByteReader, *, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise CurrentnessStreamBudgetExceeded("JSON nesting depth exceeded limit")
    reader.skip_whitespace()
    value = reader.peek_byte()
    if value == 0x22:
        _decode_json_string(reader, _NullSink(), capture=False)
        return
    if value == 0x7B:
        reader.read_byte()
        reader.skip_whitespace()
        if reader.peek_byte() == 0x7D:
            reader.read_byte()
            return
        while True:
            reader.skip_whitespace()
            _decode_json_string(reader, _NullSink(), capture=False)
            reader.skip_whitespace()
            if reader.read_byte() != 0x3A:
                raise CurrentnessStreamInvalid("expected colon in skipped JSON object")
            _skip_value(reader, depth=depth + 1)
            reader.skip_whitespace()
            separator = reader.read_byte()
            if separator == 0x2C:
                continue
            if separator != 0x7D:
                raise CurrentnessStreamInvalid("invalid skipped JSON object")
            return
    if value == 0x5B:
        reader.read_byte()
        reader.skip_whitespace()
        if reader.peek_byte() == 0x5D:
            reader.read_byte()
            return
        while True:
            _skip_value(reader, depth=depth + 1)
            reader.skip_whitespace()
            separator = reader.read_byte()
            if separator == 0x2C:
                continue
            if separator != 0x5D:
                raise CurrentnessStreamInvalid("invalid skipped JSON array")
            return
    if value in (0x2D, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39):
        _canonicalize_number(reader, _NullSink())
        return
    _canonicalize_literal(reader, _NullSink())


def _read_captured_value(spool: _TempSpool, path: Path, *, field: str) -> object:
    if spool._sizes.get(path, 0) > _MAX_CAPTURE_CHARS * 6:
        raise CurrentnessStreamBudgetExceeded(f"captured field {field} exceeded byte limit")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentnessStreamInvalid(f"captured field {field} is invalid JSON") from exc
    return value


def _load_captured_value(spool: _TempSpool, path: Path, *, field: str) -> object:
    value = _read_captured_value(spool, path, field=field)
    spool.remove(path)
    return value


def _parse_source_document(
    reader: _ByteReader,
    spool: _TempSpool,
) -> dict:
    if reader.read_byte() != 0x7B:
        raise CurrentnessStreamInvalid("currentness document is not an object")
    entries: dict[str, Path] = {}
    fields: dict[str, object] = {}
    reader.skip_whitespace()
    if reader.peek_byte() == 0x7D:
        reader.read_byte()
        source_hash = _HashSink()
        source_hash.write(b"{}")
    else:
        while True:
            reader.skip_whitespace()
            key = _decode_json_string(reader, _NullSink(), capture=True)
            if key is None or len(key) > _MAX_KEY_CHARS:
                raise CurrentnessStreamBudgetExceeded("JSON object key exceeded limit")
            reader.skip_whitespace()
            if reader.read_byte() != 0x3A:
                raise CurrentnessStreamInvalid("expected colon in source document")
            if key in _EXCLUDED_TOP_LEVEL_FIELDS:
                _skip_value(reader)
            else:
                path = spool.new_file()
                value_sink = _ValueSink(spool, path)
                body_hash = hashlib.sha256() if key == "body" else None
                _canonicalize_value(
                    reader, value_sink, spool, depth=1, hash_sink=body_hash
                )
                value_sink.close()
                if key in entries:
                    spool.remove(entries[key])
                entries[key] = path
                if body_hash is not None:
                    fields["__body_hash"] = "sha256:" + body_hash.hexdigest()
                if key in _CAPTURE_FIELDS:
                    fields[key] = _read_captured_value(spool, path, field=key)
                if len(entries) > _MAX_OBJECT_KEYS:
                    raise CurrentnessStreamBudgetExceeded("JSON object key count exceeded limit")
            reader.skip_whitespace()
            separator = reader.read_byte()
            if separator == 0x2C:
                continue
            if separator != 0x7D:
                raise CurrentnessStreamInvalid("invalid source document separator")
            break
        source_hash = _HashSink()
        source_hash.write(b"{")
        ordered = sorted(entries.items(), key=lambda item: item[0])
        for index, (key, path) in enumerate(ordered):
            if index:
                source_hash.write(b",")
            source_hash.write(json.dumps(key, ensure_ascii=True).encode("ascii"))
            source_hash.write(b":")
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(_COPY_SIZE)
                    if not chunk:
                        break
                    source_hash.write(chunk)
            spool.remove(path)
        source_hash.write(b"}")
    fields["__source_document_hash"] = source_hash.digest()
    return fields


def parse_spooled_descriptor_page(
    path: Path,
    *,
    max_page_bytes: int,
    max_items: int,
) -> tuple[list[dict], str]:
    """Parse one spooled ``_find`` page into small descriptor rows and bookmark."""

    spool = _TempSpool(budget_bytes=stream_spool_budget_bytes(max_page_bytes))
    try:
        with path.open("rb") as handle:
            reader = _ByteReader(handle)
            if reader.read_byte() != 0x7B:
                raise CurrentnessStreamInvalid("CouchDB response is not an object")
            rows: list[dict] = []
            bookmark = ""
            saw_docs = False
            reader.skip_whitespace()
            if reader.peek_byte() != 0x7D:
                while True:
                    reader.skip_whitespace()
                    key = _decode_json_string(reader, _NullSink(), capture=True)
                    if key is None or len(key) > _MAX_KEY_CHARS:
                        raise CurrentnessStreamBudgetExceeded("response key exceeded limit")
                    reader.skip_whitespace()
                    if reader.read_byte() != 0x3A:
                        raise CurrentnessStreamInvalid("expected colon in CouchDB response")
                    reader.skip_whitespace()
                    if key == "docs":
                        if saw_docs:
                            raise CurrentnessStreamInvalid("duplicate docs field in CouchDB response")
                        saw_docs = True
                        if reader.read_byte() != 0x5B:
                            raise CurrentnessStreamInvalid("CouchDB docs field is not an array")
                        reader.skip_whitespace()
                        if reader.peek_byte() == 0x5D:
                            reader.read_byte()
                        else:
                            while True:
                                if len(rows) >= max_items:
                                    raise CurrentnessStreamBudgetExceeded(
                                        "CouchDB response exceeded requested page size"
                                    )
                                rows.append(_parse_source_document(reader, spool))
                                reader.skip_whitespace()
                                separator = reader.read_byte()
                                if separator == 0x2C:
                                    reader.skip_whitespace()
                                    continue
                                if separator != 0x5D:
                                    raise CurrentnessStreamInvalid("invalid CouchDB docs array")
                                break
                    elif key == "bookmark":
                        target = spool.new_file()
                        target_sink = _ValueSink(spool, target)
                        _canonicalize_value(reader, target_sink, spool, depth=1)
                        target_sink.close()
                        value = _load_captured_value(spool, target, field="bookmark")
                        if value is not None and not isinstance(value, str):
                            raise CurrentnessStreamInvalid("CouchDB bookmark is not a string")
                        bookmark = str(value or "")
                    else:
                        _skip_value(reader)
                    reader.skip_whitespace()
                    separator = reader.read_byte()
                    if separator == 0x2C:
                        continue
                    if separator != 0x7D:
                        raise CurrentnessStreamInvalid("invalid CouchDB response object")
                    break
            else:
                reader.read_byte()
            if not reader.at_end():
                raise CurrentnessStreamInvalid("trailing data in CouchDB response")
            if not saw_docs:
                raise CurrentnessStreamInvalid("CouchDB response omitted docs")
            return rows, bookmark
    finally:
        spool.close()


__all__ = [
    "CurrentnessStreamError",
    "parse_spooled_descriptor_page",
    "stream_spool_budget_bytes",
]

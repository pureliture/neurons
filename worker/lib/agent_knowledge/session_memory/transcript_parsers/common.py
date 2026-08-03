from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..transcript_model import (
    TranscriptSession,
    TranscriptToolEvent,
    TranscriptTurn,
)

PARSER_VERSION = "provider-transcript-parser.v1"
TOOL_EVIDENCE_EXTRACTOR_VERSION = "codex-tool-evidence-extractor.v1"
GROK_PARSER_VERSION = "grok-updates-jsonl-parser.v1"
MAX_LOCATOR_ADMISSION_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class ParsedTranscript:
    session: TranscriptSession
    turns: list[TranscriptTurn]
    tool_events: list[TranscriptToolEvent] = field(default_factory=list)
    parser_warnings: list[str] = field(default_factory=list)
    source_status: str = "source_locator_private_spool_only"


@dataclass(frozen=True)
class LocatorAdmission:
    """Private caller's immutable bounds and expected raw-source fingerprint.

    This contract is intentionally consumed only by the Codex streaming
    admission path. Existing provider parsers retain their current loading
    behavior and compatibility surface.
    """

    expected_raw_sha256: str
    expected_byte_count: int
    max_bytes: int
    max_line_bytes: int
    max_record_count: int
    max_pending_tool_calls: int


@dataclass(frozen=True)
class RawSourceFingerprint:
    raw_sha256: str
    byte_count: int
    fd_identity: tuple[int, int, int, int, int]


def _source_admission_error() -> ValueError:
    return ValueError("source_admission_failed")


def validate_locator_admission(admission: LocatorAdmission) -> None:
    """Reject unsafe admission bounds before opening a private source locator."""
    if not isinstance(admission, LocatorAdmission):
        raise _source_admission_error()
    expected_hash = admission.expected_raw_sha256
    if not (
        isinstance(expected_hash, str)
        and expected_hash.startswith("sha256:")
        and len(expected_hash) == len("sha256:") + 64
        and all(char in "0123456789abcdef" for char in expected_hash.removeprefix("sha256:"))
    ):
        raise _source_admission_error()
    numeric_bounds = (
        admission.expected_byte_count,
        admission.max_bytes,
        admission.max_line_bytes,
        admission.max_record_count,
        admission.max_pending_tool_calls,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in numeric_bounds):
        raise _source_admission_error()
    if admission.expected_byte_count < 0 or not 0 < admission.max_bytes <= MAX_LOCATOR_ADMISSION_BYTES:
        raise _source_admission_error()
    if admission.expected_byte_count > admission.max_bytes:
        raise _source_admission_error()
    if any(value <= 0 for value in numeric_bounds[2:]):
        raise _source_admission_error()


def _fd_identity(fd: int) -> tuple[int, int, int, int, int]:
    try:
        source_stat = os.fstat(fd)
    except OSError as exc:
        raise _source_admission_error() from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise _source_admission_error()
    return (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_stat.st_ctime_ns,
    )


def _fingerprint_admitted_fd(fd: int, admission: LocatorAdmission) -> RawSourceFingerprint:
    """Fingerprint one bounded pass and reject an unstable private file descriptor."""
    before = _fd_identity(fd)
    digest = hashlib.sha256()
    byte_count = 0
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, 1024 * 1024):
            byte_count += len(chunk)
            if byte_count > admission.max_bytes:
                raise _source_admission_error()
            digest.update(chunk)
    except OSError as exc:
        raise _source_admission_error() from exc
    after = _fd_identity(fd)
    if before != after:
        raise _source_admission_error()
    return RawSourceFingerprint(
        raw_sha256="sha256:" + digest.hexdigest(),
        byte_count=byte_count,
        fd_identity=before,
    )


def _consume_admitted_jsonl_fd(
    fd: int,
    admission: LocatorAdmission,
    consume_record: Callable[[dict], None],
) -> RawSourceFingerprint:
    """Consume JSONL records from one bounded streaming pass without a record list."""
    before = _fd_identity(fd)
    digest = hashlib.sha256()
    byte_count = 0
    record_count = 0
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb", closefd=True) as handle:
            while raw_line := handle.readline(admission.max_line_bytes + 1):
                byte_count += len(raw_line)
                if byte_count > admission.max_bytes or len(raw_line) > admission.max_line_bytes:
                    raise _source_admission_error()
                digest.update(raw_line)
                line = raw_line.strip()
                if not line:
                    continue
                record_count += 1
                if record_count > admission.max_record_count:
                    raise _source_admission_error()
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise _source_admission_error() from exc
                if not isinstance(record, dict):
                    raise _source_admission_error()
                consume_record(record)
    except OSError as exc:
        raise _source_admission_error() from exc
    if not record_count or before != _fd_identity(fd):
        raise _source_admission_error()
    return RawSourceFingerprint(
        raw_sha256="sha256:" + digest.hexdigest(),
        byte_count=byte_count,
        fd_identity=before,
    )


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_json_source(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError("source_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("source_parse_failed: invalid json") from exc
    if not isinstance(payload, dict):
        raise ValueError("source_parse_failed: source root must be an object")
    return payload


def _load_jsonl_source(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("source_parse_failed: jsonl record must be an object")
                records.append(record)
    except FileNotFoundError as exc:
        raise ValueError("source_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("source_parse_failed: invalid jsonl") from exc
    if not records:
        raise ValueError("source_parse_failed: empty jsonl")
    return records


def _extract_message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _extract_claude_message_text(content) -> str:
    return _extract_message_text(content)


def _normalize_role(role) -> str:
    role_text = str(role or "").lower()
    if role_text in {"assistant", "model"}:
        return "assistant"
    if role_text == "user":
        return "user"
    if role_text.startswith("tool"):
        return "tool_summary"
    return "system_observed"

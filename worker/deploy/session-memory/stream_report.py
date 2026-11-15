"""Byte-bounded, fixed-allowlist filter for the session-memory build CLI.

Reads child stdout/stderr as bytes, never echoes raw output, and retains only the
last JSON-looking line up to MAX_REPORT_BYTES. A single very long line never
materializes: non-JSON bytes are discarded and a JSON line stops accumulating at
the cap. The only output is a fixed allowlist summary (no transcript/secrets).
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import BinaryIO

SCHEMA_VERSION = "build_guard_summary.v1"
MAX_REPORT_BYTES = 1048576
CHUNK_SIZE = 65536
MAX_OUTCOME_ROWS = 4096
OUTCOMES_SCHEMA_VERSION = "build_outcomes.v1"
PROGRESS_SCHEMAS = frozenset(
    {
        "embedding_progress.v1",
        "session_memory_progress.v1",
        "build_progress.v1",
        "pipeline_progress.v1",
    }
)
OUTCOME_STATUSES = frozenset({"projected", "pending", "skipped", "failed"})
PROGRESS_NUMERIC_KEYS = (
    "done", "total", "chunks_done", "chunks_total", "processed",
    "selected", "projected", "failed", "skipped",
)
PROGRESS_ENUMS = {
    "stage": frozenset(
        {
            "start", "running", "complete", "completed", "failed", "pending", "building",
            "selection", "materialization", "materialization_body", "projection", "embedding_batch",
        }
    ),
    "phase": frozenset({"start", "running", "complete", "completed", "failed", "pending", "building"}),
    "unit": frozenset({"chunks", "sessions", "bytes"}),
    "status": frozenset({"ok", "failed", "pending", "running", "completed"}),
}
_SESSION_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ERROR_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,63}$")
_WHITESPACE = b" \t\r\f\v"


class _LineScanner:
    def __init__(self, max_bytes: int, on_json_line=None) -> None:
        self.max_bytes = max(1, int(max_bytes))
        self.on_json_line = on_json_line
        self.state = "start"  # start | json | discard
        self.accum = bytearray()
        self.oversize = False
        self.last_report: bytes | None = None
        self.last_oversize = False

    def _append(self, data: bytes) -> None:
        if self.oversize:
            return
        remaining = self.max_bytes - len(self.accum)
        if len(data) <= remaining:
            self.accum.extend(data)
        else:
            self.accum.extend(data[:remaining])
            self.oversize = True

    def feed(self, segment: bytes) -> None:
        if self.state == "start":
            index = 0
            while index < len(segment) and segment[index] in _WHITESPACE:
                index += 1
            if index >= len(segment):
                return
            if segment[index:index + 1] == b"{":
                self.state = "json"
                self.accum = bytearray()
                self.oversize = False
                self._append(segment[index:])
            else:
                self.state = "discard"
        elif self.state == "json":
            self._append(segment)

    def finish_line(self) -> None:
        if self.state == "json":
            if self.oversize:
                self.last_report = None
                self.last_oversize = True
            else:
                raw = bytes(self.accum)
                if _is_progress_event(raw):
                    # Relay only; a progress event must never become the report.
                    if self.on_json_line is not None:
                        self.on_json_line(raw)
                    self.state = "start"
                    self.accum = bytearray()
                    self.oversize = False
                    return
                self.last_report = raw
                self.last_oversize = False
                if self.on_json_line is not None:
                    self.on_json_line(raw)
        self.state = "start"
        self.accum = bytearray()
        self.oversize = False

    def eof(self) -> None:
        if self.state != "start":
            self.finish_line()


def _is_progress_event(raw: bytes) -> bool:
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(event, dict) and event.get("schema_version") in PROGRESS_SCHEMAS


def scan_stream(stream: BinaryIO, *, max_bytes: int = MAX_REPORT_BYTES,
                chunk_size: int = CHUNK_SIZE,
                on_json_line=None) -> tuple[bytes | None, bool]:
    scanner = _LineScanner(max_bytes, on_json_line=on_json_line)
    # read1 returns already-available bytes instead of waiting for a full chunk,
    # so progress is relayed while the build is still running.
    reader = getattr(stream, "read1", None) or stream.read
    while True:
        chunk = reader(max(1, int(chunk_size)))
        if not chunk:
            break
        start = 0
        while True:
            newline = chunk.find(b"\n", start)
            if newline < 0:
                scanner.feed(chunk[start:])
                break
            scanner.feed(chunk[start:newline])
            scanner.finish_line()
            start = newline + 1
            if start >= len(chunk):
                break
    scanner.eof()
    return scanner.last_report, scanner.last_oversize


def forward_progress(raw: bytes) -> None:
    """Print only a fixed-allowlist sanitized progress event."""
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(event, dict):
        return
    schema = event.get("schema_version")
    if schema not in PROGRESS_SCHEMAS:
        return
    safe: dict = {"schema_version": schema}
    for key in PROGRESS_NUMERIC_KEYS:
        value = event.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            safe[key] = value
    for key, allowed in PROGRESS_ENUMS.items():
        value = event.get(key)
        if isinstance(value, str) and value in allowed:
            safe[key] = value
    print(json.dumps(safe, sort_keys=True, separators=(",", ":")), flush=True)


def extract_outcomes(raw: bytes | None) -> list[dict]:
    if raw is None:
        return []
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(report, dict):
        return []
    rows = report.get("sessions")
    if not isinstance(rows, list):
        return []
    outcomes: list[dict] = []
    for row in rows:
        if len(outcomes) >= MAX_OUTCOME_ROWS:
            break
        if not isinstance(row, dict):
            continue
        session_id_hash = row.get("session_id_hash")
        status = row.get("status")
        if not isinstance(session_id_hash, str) or not _SESSION_HASH.fullmatch(session_id_hash):
            continue
        if status not in OUTCOME_STATUSES:
            continue
        error_class = row.get("error_class")
        if not isinstance(error_class, str) or not _ERROR_CLASS.fullmatch(error_class):
            error_class = "unknown" if error_class else ""
        outcomes.append(
            {
                "session_id_hash": session_id_hash,
                "status": status,
                "error_class": error_class,
            }
        )
    return outcomes


def write_outcomes(path: str, outcomes: list[dict]) -> None:
    if not path:
        return
    payload = {
        "schema_version": OUTCOMES_SCHEMA_VERSION,
        "sessions": outcomes,
    }
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)


def summarize(raw: bytes | None) -> dict:
    if raw is None:
        return {"schema_version": SCHEMA_VERSION, "error": "missing_report"}
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "error": "invalid_report"}
    if not isinstance(report, dict):
        return {"schema_version": SCHEMA_VERSION, "error": "invalid_report_shape"}
    if not any(key in report for key in ("selected", "projected", "failed")):
        return {"schema_version": SCHEMA_VERSION, "error": "missing_report"}
    try:
        selected = int(report.get("selected") or 0)
        projected = int(report.get("projected") or 0)
        failed = int(report.get("failed") or 0)
        skipped = int(report.get("skipped") or 0)
    except (TypeError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "error": "invalid_report_counters"}
    status = "failed" if failed > 0 else (
        "no_progress" if selected > 0 and projected == 0 else "ok"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "selected": selected,
        "projected": projected,
        "failed": failed,
        "skipped": skipped,
    }


def main() -> int:
    try:
        raw, oversize = scan_stream(sys.stdin.buffer, on_json_line=forward_progress)
        outcomes = [] if oversize else extract_outcomes(raw)
        try:
            write_outcomes(os.environ.get("SESSION_MEMORY_BUILD_OUTCOMES_FILE", ""), outcomes)
        except OSError:
            outcomes = []
        summary = (
            {"schema_version": SCHEMA_VERSION, "error": "report_too_large"}
            if oversize
            else summarize(raw)
        )
    except Exception:
        summary = {"schema_version": SCHEMA_VERSION, "error": "internal_error"}
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    if "error" in summary:
        return 3
    return 1 if summary["status"] in {"failed", "no_progress"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

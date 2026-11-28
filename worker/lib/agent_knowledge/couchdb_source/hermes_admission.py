"""Fail-closed Hermes sqlite -> provider_transcript_fixture.v1 admission path.

The Hermes source of truth is a local sqlite database. It is only ever opened
with ``mode=ro``; generated fixtures live in a disposable runtime directory.
The admission manifest is the authority for the exact session set and message
counts, so a session outside the frozen list is never materialized or imported.

No raw transcript, session id, or cwd is written to any report or log. Hashes
and counts are the only exportable forms.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

from .document_model import build_session_id_hash

HERMES_CUTOFF = "2026-11-25T00:00:00Z"
HERMES_ADMISSION_SCHEMA = "hermes_admission_manifest.v1"
HERMES_FIXTURE_SCHEMA = "provider_transcript_fixture.v1"

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_ADMISSION_FIELDS = frozenset({"schema_version", "cutoff", "entries"})
_ENTRY_FIELDS = frozenset({"session_id_hash", "message_count"})


@dataclass(frozen=True)
class HermesAdmissionManifest:
    """Frozen hash -> message_count admission list (no raw transcript)."""

    cutoff: str
    entries: Mapping[str, int]


@dataclass(frozen=True)
class HermesAdmissionSession:
    """A manifest-admitted session. ``session_id`` is never reported."""

    session_id_hash: str
    message_count: int
    cwd: str
    started_at: str
    ended_at: str
    session_id: str = field(repr=False)


@dataclass(frozen=True)
class HermesAdmissionPlan:
    sessions: tuple[HermesAdmissionSession, ...]
    rejected: tuple[tuple[str, str], ...]  # (session_id_hash, reason)
    manifest_count: int

    @property
    def manifest_misses(self) -> int:
        admitted = {session.session_id_hash for session in self.sessions}
        return self.manifest_count - len(admitted)


def load_hermes_admission_manifest(path: str | Path) -> HermesAdmissionManifest:
    """Load a shape-checked manifest. Invalid input never admits a session."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != _ADMISSION_FIELDS:
            raise ValueError
        if (
            payload["schema_version"] != HERMES_ADMISSION_SCHEMA
            or payload["cutoff"] != HERMES_CUTOFF
            or not isinstance(payload["entries"], list)
            or not payload["entries"]
        ):
            raise ValueError
        entries: dict[str, int] = {}
        for entry in payload["entries"]:
            if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
                raise ValueError
            session_id_hash = entry["session_id_hash"]
            count = entry["message_count"]
            if (
                not isinstance(session_id_hash, str)
                or not _HASH_RE.fullmatch(session_id_hash)
                or session_id_hash in entries
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count <= 0
            ):
                raise ValueError
            entries[session_id_hash] = count
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("hermes_admission_manifest_invalid") from exc
    return HermesAdmissionManifest(cutoff=HERMES_CUTOFF, entries=entries)


def hermes_session_id_hash(session_id: str) -> str:
    """Hermes fixture identity: ``sha256("hermes:atlas:<session_id>")``."""
    return build_session_id_hash("hermes", f"atlas:{session_id}")


def _sqlite_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def _connect_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise ValueError("hermes_source_unavailable")
    try:
        connection = sqlite3.connect(_sqlite_uri(path), uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as exc:
        raise ValueError("hermes_source_unavailable") from exc


def _iso_epoch(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return ""


def plan_hermes_import(
    db_path: Path,
    manifest: HermesAdmissionManifest,
    *,
    cutoff: str = HERMES_CUTOFF,
) -> HermesAdmissionPlan:
    """Read session metadata only (ro) and admit exactly the frozen set."""
    if cutoff != HERMES_CUTOFF or manifest.cutoff != HERMES_CUTOFF:
        raise ValueError("hermes_cutoff_mismatch")
    cutoff_epoch = datetime.strptime(cutoff, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    ).timestamp()
    connection = _connect_ro(Path(db_path))
    try:
        rows = connection.execute(
            """
            SELECT s.id, s.started_at, s.ended_at, s.cwd,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count
            FROM sessions s
            WHERE s.started_at < ?
              AND (s.ended_at IS NULL OR s.ended_at < ?)
              AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id)
            ORDER BY s.started_at, s.id
            """,
            (cutoff_epoch, cutoff_epoch),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError("hermes_source_unreadable") from exc
    finally:
        connection.close()

    sessions: list[HermesAdmissionSession] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_id, started_at, ended_at, cwd, message_count in rows:
        session_id_hash = hermes_session_id_hash(str(raw_id))
        seen.add(session_id_hash)
        expected_count = manifest.entries.get(session_id_hash)
        if expected_count is None:
            continue
        if expected_count != int(message_count):
            rejected.append((session_id_hash, "message_count_mismatch"))
            continue
        sessions.append(
            HermesAdmissionSession(
                session_id_hash=session_id_hash,
                message_count=int(message_count),
                cwd=str(cwd or ""),
                started_at=_iso_epoch(started_at),
                ended_at=_iso_epoch(ended_at),
                session_id=str(raw_id),
            )
        )
    return HermesAdmissionPlan(
        sessions=tuple(sessions),
        rejected=tuple(rejected),
        manifest_count=len(manifest.entries),
    )


def materialize_hermes_fixtures(
    db_path: Path,
    plan: HermesAdmissionPlan,
    runtime_dir: Path,
) -> list[Path]:
    """Write admitted sessions as fixtures in the disposable runtime dir only."""
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    connection = _connect_ro(Path(db_path))
    out_paths: list[Path] = []
    try:
        for session in plan.sessions:
            rows = connection.execute(
                """
                SELECT role, content, timestamp FROM messages
                WHERE session_id = ? ORDER BY timestamp, id
                """,
                (session.session_id,),
            ).fetchall()
            turns = []
            for role, content, timestamp in rows:
                text = str(content or "")
                if not text.strip():
                    continue
                turns.append(
                    {
                        "role": str(role or ""),
                        "text": text,
                        "timestamp": _iso_epoch(timestamp),
                    }
                )
            if not turns:
                raise ValueError("hermes_messages_empty_after_admission")
            fixture = {
                "provider": "hermes",
                "schema_version": HERMES_FIXTURE_SCHEMA,
                "session_id": f"atlas:{session.session_id}",
                "started_at": session.started_at,
                "ended_at": session.ended_at,
                "cwd": session.cwd,
                "turns": turns,
            }
            out = runtime_dir / f"hermes-{session.session_id_hash}.json"
            out.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            out_paths.append(out)
    finally:
        connection.close()
    return out_paths


__all__ = [
    "HERMES_CUTOFF",
    "HERMES_ADMISSION_SCHEMA",
    "HERMES_FIXTURE_SCHEMA",
    "HermesAdmissionManifest",
    "HermesAdmissionSession",
    "HermesAdmissionPlan",
    "load_hermes_admission_manifest",
    "hermes_session_id_hash",
    "plan_hermes_import",
    "materialize_hermes_fixtures",
]

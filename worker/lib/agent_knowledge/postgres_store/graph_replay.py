"""One-off graph replay sidecar (M5b, Phase 2).

Scope: enumerate PostgreSQL authority cards/episodes ONCE, build a redacted
episode payload per item, and hand it to the Graphiti adapter seam
(``upsert_episode`` / ``add_episode`` / plain callable). This is an offline,
one-shot replay -- NOT the M7 steady-state lease worker: no outbox leases,
no polling loop, no worker_id claiming, no SQLite cursor.

Checkpoint decision (explicit): FILE checkpoint (JSON). Rationale: the replay
is one-off/offline and must not depend on the M7 ``graph_projection_outbox``
writer/consumer or any live PG table. A file checkpoint keeps reruns safe
without touching the steady-state pipeline.

Canonical key (design 2.4): ``(source_type, source_id, source_revision)`` plus
``content_hash``. Mirrors the DDL
``UNIQUE (source_type, source_id, source_revision)`` in
``pgvector_schema.sql``. Duplicate replay of the same key is skipped
(idempotent) without calling the adapter.

Failure handling: adapter exceptions are classified into ``retry`` /
``dead_letter``. Reports carry only ``key_digest`` + ``reason_code`` -- never
raw transcripts, private paths, or exception text.

PG enumeration is caller-supplied (pass already-read authority rows as plain
mappings); this module never opens a DB connection.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

CHECKPOINT_VERSION = 1
_KEY_SEP = "\x1f"

RETRYABLE_REASON_CODES = frozenset(
    {
        "TimeoutError",
        "ConnectionError",
        "TemporaryFailure",
        "failed",
        "timeout",
        "unavailable",
        "error",
    }
)

# --- redaction (self-contained; mirrors redaction.py intent, no pipeline import) ---

_PRIVATE_PATH_RES = (
    re.compile(r"/Users/[^\s'\"\]]+"),
    re.compile(r"~\/[^\s'\"\]]+"),
    re.compile(r"/private/[^\s'\"\]]+"),
    re.compile(r"/Volumes/[^\s'\"\]]+"),
)
_SECRET_RES = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9_]*(?:TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|[^\s,'\"}]+)", re.IGNORECASE),
)
_RAW_TRANSCRIPT_RE = re.compile(r"\braw[_-]?transcript[A-Za-z0-9_-]*\b", re.IGNORECASE)


def redact_text(text: str) -> str:
    redacted = str(text or "")
    for pat in _PRIVATE_PATH_RES:
        redacted = pat.sub("<redacted:private-path>", redacted)
    for pat in _SECRET_RES:
        redacted = pat.sub("<redacted:secret>", redacted)
    redacted = _RAW_TRANSCRIPT_RE.sub("redacted transcript", redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


# --- canonical key helpers ---

def canonical_key_str(source_type: str, source_id: str, source_revision: str) -> str:
    return _KEY_SEP.join([source_type, source_id, source_revision])


def split_canonical_key(key: str) -> tuple[str, str, str]:
    parts = str(key).split(_KEY_SEP)
    if len(parts) != 3:
        raise ValueError("malformed_canonical_key")
    return parts[0], parts[1], parts[2]


def key_digest(source_type: str, source_id: str, source_revision: str, content_hash: str) -> str:
    raw = "|".join([source_type, source_id, source_revision, content_hash])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --- checkpoint (FILE; rerun-safe) ---

@dataclass
class ReplayCheckpoint:
    completed: set[str] = field(default_factory=set)
    retry_counts: dict[str, int] = field(default_factory=dict)
    dead_letter: set[str] = field(default_factory=set)


def load_checkpoint(path: str | Path | None) -> ReplayCheckpoint:
    if not path:
        return ReplayCheckpoint()
    p = Path(path)
    if not p.exists():
        return ReplayCheckpoint()
    data = json.loads(p.read_text(encoding="utf-8"))
    return ReplayCheckpoint(
        completed=set(data.get("completed", [])),
        retry_counts=dict(data.get("retry_counts", {})),
        dead_letter=set(data.get("dead_letter", [])),
    )


def save_checkpoint(path: str | Path | None, state: ReplayCheckpoint) -> None:
    if not path:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CHECKPOINT_VERSION,
        "completed": sorted(state.completed),
        "retry_counts": dict(state.retry_counts),
        "dead_letter": sorted(state.dead_letter),
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


# --- adapter seam ---

class GraphProjectionSeam(Protocol):
    def upsert_episode(self, payload: Mapping[str, Any]) -> Any: ...  # pragma: no cover


def call_adapter_seam(adapter: Any, payload: Mapping[str, Any]) -> str:
    """Deliver one redacted payload via the Graphiti adapter seam.

    Accepts (in order): ``upsert_episode`` (repo canonical seam),
    ``add_episode`` (design 6.1 wording), or a plain callable. Returns a
    normalized outcome string.
    """
    if hasattr(adapter, "upsert_episode"):
        result = adapter.upsert_episode(payload)
    elif hasattr(adapter, "add_episode"):
        result = adapter.add_episode(payload)
    elif callable(adapter):
        result = adapter(payload)
    else:
        raise TypeError("unsupported_adapter_seam")
    if result is None:
        return "inserted"
    text = str(result).strip().lower()
    if text in {"inserted", "projected", "ok", "success", "completed"}:
        return "inserted"
    if text == "duplicate":
        return "duplicate"
    if text == "skipped_disabled":
        return "skipped_disabled"
    return "failed"


# --- payload + report ---

REQUIRED_KEY_FIELDS = ("source_type", "source_id", "source_revision", "content_hash")


def build_redacted_episode_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    """Build the redacted episode payload for one authority row.

    Keeps the canonical key verbatim; redacts every free-text field. The raw
    ``transcript``/``raw_transcript`` input (if present) is dropped, never
    forwarded.
    """
    for f in REQUIRED_KEY_FIELDS:
        if not str(item.get(f) or ""):
            raise ValueError(f"missing_{f}")
    return {
        "source_type": str(item["source_type"]),
        "source_id": str(item["source_id"]),
        "source_revision": str(item["source_revision"]),
        "content_hash": str(item["content_hash"]),
        "authority_memory_id": str(item.get("memory_id") or item.get("source_id") or ""),
        "project": str(item.get("project") or ""),
        "card_type": str(item.get("card_type") or ""),
        "title": redact_text(str(item.get("title") or "")),
        "summary": redact_text(str(item.get("summary") or "")),
        "typed_payload": _redact_value(item.get("typed_payload") or {}),
        "lifecycle_state": str(item.get("lifecycle_state") or ""),
        "currentness": str(item.get("currentness") or ""),
    }


@dataclass(frozen=True)
class ReplayFailure:
    key_digest: str
    source_type: str
    reason_code: str
    disposition: str  # "retry" | "dead_letter"

    def to_dict(self) -> dict[str, str]:
        return {
            "key_digest": self.key_digest,
            "source_type": self.source_type,
            "reason_code": self.reason_code,
            "disposition": self.disposition,
        }


@dataclass(frozen=True)
class GraphReplayReport:
    status: str
    attempted: int
    projected: int
    skipped_duplicate: int
    retry: int
    dead_letter: int
    failures: tuple[ReplayFailure, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempted": self.attempted,
            "projected": self.projected,
            "skipped_duplicate": self.skipped_duplicate,
            "retry": self.retry,
            "dead_letter": self.dead_letter,
            "failures": [f.to_dict() for f in self.failures],
        }


def _classify_failure(reason_code: str, retries_used: int, max_retries: int) -> str:
    if reason_code.startswith("missing_") or reason_code in {
        "malformed_canonical_key",
        "unsupported_adapter_seam",
        "ValueError",
    }:
        return "dead_letter"
    if reason_code in RETRYABLE_REASON_CODES and retries_used < max_retries:
        return "retry"
    return "dead_letter"


def run_graph_replay(
    items: Sequence[Mapping[str, Any]],
    adapter: Any,
    *,
    checkpoint_path: str | Path | None = None,
    max_retries: int = 3,
    key_fn: Callable[[Mapping[str, Any]], tuple[str, str, str, str]] | None = None,
) -> GraphReplayReport:
    """One-shot replay over authority rows. Loads + saves the FILE checkpoint."""
    state = load_checkpoint(checkpoint_path)
    projected = 0
    skipped_duplicate = 0
    failures: list[ReplayFailure] = []

    for item in items:
        try:
            if key_fn is not None:
                source_type, source_id, source_revision, content_hash = key_fn(item)
            else:
                payload_probe = build_redacted_episode_payload(item)
                source_type = payload_probe["source_type"]
                source_id = payload_probe["source_id"]
                source_revision = payload_probe["source_revision"]
                content_hash = payload_probe["content_hash"]
        except Exception as exc:
            reason = type(exc).__name__ if not str(exc).startswith("missing_") else str(exc)
            if str(exc).startswith("missing_"):
                reason = str(exc)
            failures.append(
                ReplayFailure(
                    key_digest=hashlib.sha256(str(exc).encode()).hexdigest()[:16],
                    source_type=str((item or {}).get("source_type") or "unknown"),
                    reason_code=reason,
                    disposition="dead_letter",
                )
            )
            continue

        key = canonical_key_str(source_type, source_id, source_revision)
        digest = key_digest(source_type, source_id, source_revision, content_hash)

        # Idempotent skip: UNIQUE(source_type, source_id, source_revision).
        if key in state.completed or key in state.dead_letter:
            skipped_duplicate += 1
            continue

        try:
            payload = build_redacted_episode_payload(
                {**dict(item), "source_type": source_type, "source_id": source_id,
                 "source_revision": source_revision, "content_hash": content_hash}
            )
            outcome = call_adapter_seam(adapter, payload)
        except Exception as exc:
            reason_code = type(exc).__name__
            retries_used = state.retry_counts.get(key, 0)
            disposition = _classify_failure(reason_code, retries_used, max_retries)
            if disposition == "retry":
                state.retry_counts[key] = retries_used + 1
            else:
                state.dead_letter.add(key)
            failures.append(ReplayFailure(digest, source_type, reason_code, disposition))
            continue

        if outcome in {"duplicate", "skipped_disabled"}:
            state.completed.add(key)
            skipped_duplicate += 1
        elif outcome == "inserted":
            state.completed.add(key)
            projected += 1
        else:  # adapter-level "failed"
            retries_used = state.retry_counts.get(key, 0)
            disposition = _classify_failure("failed", retries_used, max_retries)
            if disposition == "retry":
                state.retry_counts[key] = retries_used + 1
            else:
                state.dead_letter.add(key)
            failures.append(ReplayFailure(digest, source_type, "failed", disposition))

    save_checkpoint(checkpoint_path, state)

    retry_n = sum(1 for f in failures if f.disposition == "retry")
    dead_n = sum(1 for f in failures if f.disposition == "dead_letter")
    if failures and projected == 0 and skipped_duplicate == 0:
        status = "failed"
    elif failures:
        status = "partial"
    else:
        status = "succeeded"
    return GraphReplayReport(
        status=status,
        attempted=len(items),
        projected=projected,
        skipped_duplicate=skipped_duplicate,
        retry=retry_n,
        dead_letter=dead_n,
        failures=tuple(failures),
    )

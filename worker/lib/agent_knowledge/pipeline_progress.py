"""Redacted pipeline progress events for the existing container-log path.

The freshness checker reads pod logs and counts only real sub-batch completion,
never process liveness. Every event therefore carries a fixed stage enum and
integer counters; no document text, session hash, outbox id, endpoint, or
credential is ever part of an event.

Wire format (one compact JSON object per line, written to stderr so it stays
separate from a CLI's final JSON report on stdout):

    {"done":12,"schema_version":"pipeline_progress.v1","stage":"embedding_batch","total":40}

``build-once.sh`` relays these lines to the container log but never treats them
as the build report.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Iterable

SCHEMA_VERSION = "pipeline_progress.v1"
STAGES = frozenset(
    {
        "selection",
        "materialization",
        "materialization_body",
        "projection",
        "embedding_batch",
    }
)


def _require_count(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"progress {name} must be an integer")
    if value < 0:
        raise ValueError(f"progress {name} must be non-negative")
    return value


def emit_progress(
    stage: str,
    done: int,
    total: int,
    *,
    stream: IO[str] | None = None,
) -> None:
    """Write one fixed-schema progress event to ``stream`` (default stderr).

    ``stage`` must be one of :data:`STAGES`; both counters are non-negative
    integers. ``total=0`` means "not known for this stage", never an id.
    """

    if stage not in STAGES:
        raise ValueError(f"unsupported progress stage: {stage}")
    done_count = _require_count("done", done)
    total_count = _require_count("total", total)
    if total_count and done_count > total_count:
        raise ValueError("progress done must not exceed total")
    event = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "done": done_count,
        "total": total_count,
    }
    target = stream if stream is not None else sys.stderr
    target.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
    target.write("\n")
    target.flush()


def is_progress_line(line: str) -> bool:
    """True when a log line is one of these events (used by the shell filter)."""

    return SCHEMA_VERSION in line


__all__: Iterable[str] = (
    "SCHEMA_VERSION",
    "STAGES",
    "emit_progress",
    "is_progress_line",
)

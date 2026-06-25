"""Parity-soak runner for the session-memory Qdrant mirror vs RAGFlow.

Wraps the pure-compute :func:`compare_recall` / :func:`recall_parity_passes`
harness (``qdrant_read_compare``) for the post-backfill parity gate:

- ``primary_fetch`` is RAGFlow retrieve over the session-memory dataset, each chunk
  authority-joined (so the primary side is itself the authoritative recall set).
- ``mirror_fetch`` is a Qdrant query whose raw candidates are joined to authority
  via :func:`join_mirror_hits_to_authority` with
  :class:`CouchDBProjectionStateAuthorityResolver` -- the SAME projection-state
  join the product read path uses, so a mirror hit only counts if its session is
  still PROJECTED and its content_hash matches the currently-projected body.

MANDATORY non-emptiness guard: :func:`compare_recall` treats an empty primary
top-k as vacuously recall=1.0 / exact-match (``qdrant_read_compare.py:89-90``). A
soak where RAGFlow returns nothing for most queries would therefore report a
*false* green. This runner REJECTS the run (``passed=False``,
``rejected_reason='insufficient_primary_coverage'``) unless the primary fetch
returns >0 hits for at least ``min_nonempty_fraction`` of the cohort. The parity
verdict is only trusted when primary coverage clears that floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .qdrant_authority_join import join_mirror_hits_to_authority
from .qdrant_couchdb_authority import CouchDBProjectionStateAuthorityResolver
from .qdrant_read_compare import Fetcher, compare_recall, recall_parity_passes

PARITY_SCHEMA = "agent_knowledge_qdrant_backfill_parity.v1"
DEFAULT_MIN_NONEMPTY_FRACTION = 0.5


@dataclass(frozen=True)
class ParityResult:
    """Redaction-safe parity outcome (counts/ratios/flags only)."""

    schema_version: str
    passed: bool
    rejected: bool
    rejected_reason: str
    nonempty_primary_count: int
    cohort_size: int
    nonempty_fraction: float
    min_nonempty_fraction: float
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "passed": self.passed,
            "rejected": self.rejected,
            "rejected_reason": self.rejected_reason,
            "nonempty_primary_count": self.nonempty_primary_count,
            "cohort_size": self.cohort_size,
            "nonempty_fraction": round(self.nonempty_fraction, 4),
            "min_nonempty_fraction": self.min_nonempty_fraction,
            "report": dict(self.report),
            "network_used": False,
            "raw_query_printed": False,
        }


def build_authority_joined_mirror_fetch(
    *,
    mirror_query: Callable[[str], list[dict[str, Any]]],
    store: Any,
    filters: dict[str, str] | None = None,
) -> Fetcher:
    """Wrap a raw mirror-candidate query so its hits are authority-joined.

    ``mirror_query(query) -> list[raw mirror hit dicts]`` (each carrying
    ``content_hash`` + ``session_id_hash``). The returned fetcher resolves every
    candidate through the CouchDB projection-state gate and drops anything that does
    not resolve, so the parity harness only ever sees authority-joined mirror hits.
    """

    resolver = CouchDBProjectionStateAuthorityResolver(store, filters=filters)

    def _fetch(query: str) -> list[dict[str, Any]]:
        raw = list(mirror_query(query) or [])
        return join_mirror_hits_to_authority(raw, resolver=resolver, drop_unresolved=True)

    return _fetch


def run_parity_soak(
    queries: list[str],
    *,
    primary_fetch: Fetcher,
    mirror_fetch: Fetcher,
    k: int = 10,
    min_mean_recall_at_k: float,
    require_exact: bool = True,
    min_nonempty_fraction: float = DEFAULT_MIN_NONEMPTY_FRACTION,
) -> ParityResult:
    """Run the parity soak with the mandatory primary non-emptiness guard.

    Both fetchers must already yield AUTHORITY-JOINED hits (the harness rejects raw
    mirror candidates). The verdict is rejected -- never a green pass -- if primary
    coverage is below ``min_nonempty_fraction`` of the cohort.
    """

    cohort = list(queries or [])
    cohort_size = len(cohort)

    # Memoize primary_fetch so the coverage probe below and compare_recall's own
    # internal fetch don't hit the (RAGFlow) primary twice per query.
    _primary_cache: dict[str, list[dict[str, Any]]] = {}

    def cached_primary_fetch(query: str) -> list[dict[str, Any]]:
        if query not in _primary_cache:
            _primary_cache[query] = list(primary_fetch(query) or [])
        return _primary_cache[query]

    # Probe primary coverage BEFORE trusting any recall number.
    nonempty_primary = 0
    for query in cohort:
        if cached_primary_fetch(query):
            nonempty_primary += 1

    fraction = (nonempty_primary / cohort_size) if cohort_size else 0.0

    # Empty cohort OR insufficient primary coverage => reject (vacuous-recall guard).
    if cohort_size == 0 or fraction < float(min_nonempty_fraction):
        return ParityResult(
            schema_version=PARITY_SCHEMA,
            passed=False,
            rejected=True,
            rejected_reason="insufficient_primary_coverage",
            nonempty_primary_count=nonempty_primary,
            cohort_size=cohort_size,
            nonempty_fraction=fraction,
            min_nonempty_fraction=float(min_nonempty_fraction),
            report={},
        )

    report = compare_recall(cohort, primary_fetch=cached_primary_fetch, mirror_fetch=mirror_fetch, k=k)
    passed = recall_parity_passes(
        report,
        min_mean_recall_at_k=float(min_mean_recall_at_k),
        require_exact=bool(require_exact),
    )
    return ParityResult(
        schema_version=PARITY_SCHEMA,
        passed=bool(passed),
        rejected=False,
        rejected_reason="",
        nonempty_primary_count=nonempty_primary,
        cohort_size=cohort_size,
        nonempty_fraction=fraction,
        min_nonempty_fraction=float(min_nonempty_fraction),
        report=report,
    )


__all__ = [
    "PARITY_SCHEMA",
    "DEFAULT_MIN_NONEMPTY_FRACTION",
    "ParityResult",
    "build_authority_joined_mirror_fetch",
    "run_parity_soak",
]

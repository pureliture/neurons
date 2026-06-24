"""M7 read-compare / recall-parity harness: RAGFlow vs Qdrant mirror.

Pure-compute comparison over a query cohort. For each query it takes the primary
recall (RAGFlow retrieval, authority-joined) and the mirror recall (Qdrant query,
authority-joined) as already-fetched hit lists and computes top-k content_hash
overlap, recall@k, and exact-match counts. The live fetchers (RAGFlow retrieve,
Qdrant query, ledger-join) are injected, so this module is fully testable with
fakes and performs no network call itself.

Output is redaction-safe: only a hashed query id and counts/ratios are emitted --
never the raw query text. The exact-match section is shaped to feed the
``read_compare`` block of the searchable-mirror gate evidence packet
(``total_count``/``matched_count``/``mismatch_count`` with mismatch==0 required
for exact parity); ``mean_recall_at_k`` is the additional semantic-parity signal.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

READ_COMPARE_SCHEMA = "agent_knowledge_qdrant_read_compare.v1"

# fetcher(query) -> list of authority-joined hit dicts (each carrying content_hash)
Fetcher = Callable[[str], list[dict[str, Any]]]


@dataclass(frozen=True)
class QueryComparison:
    query_hash: str
    primary_count: int
    mirror_count: int
    overlap_count: int
    recall_at_k: float
    exact_match: bool


def _query_hash(query: str) -> str:
    return "sha256:" + hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()[:16]


def _assert_authority_joined(hit: dict[str, Any]) -> None:
    # The harness compares AUTHORITY-joined hits. Reject a raw, unresolved mirror
    # candidate (canonical_resolution_required is True) so a caller cannot pass
    # un-joined Qdrant hits and get a falsely-passing parity result.
    if hit.get("canonical_resolution_required") is True:
        raise ValueError("read-compare requires authority-joined hits; got an unresolved mirror candidate")


def _top_hashes(hits: Iterable[dict[str, Any]], k: int) -> list[str]:
    # dedup while preserving order, THEN take top-k, so a repeated content_hash in
    # the top results does not shrink the recall denominator.
    out: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        _assert_authority_joined(hit)
        content_hash = str(hit.get("content_hash") or "")
        if not content_hash or content_hash in seen:
            continue
        seen.add(content_hash)
        out.append(content_hash)
        if len(out) >= k:
            break
    return out


def compare_recall(
    queries: list[str],
    *,
    primary_fetch: Fetcher,
    mirror_fetch: Fetcher,
    k: int = 10,
) -> dict[str, Any]:
    """Compare primary vs mirror recall over a query cohort.

    A query is an ``exact_match`` when the primary top-k content_hash set is a
    subset of the mirror top-k set (the mirror surfaces at least everything the
    authority did). ``recall_at_k`` is |overlap| / |primary top-k| per query.
    """

    k = max(1, int(k))
    per_query: list[QueryComparison] = []
    matched_count = 0
    for query in queries:
        primary_hashes = set(_top_hashes(primary_fetch(query), k))
        mirror_hashes = set(_top_hashes(mirror_fetch(query), k))
        overlap = primary_hashes & mirror_hashes
        recall = (len(overlap) / len(primary_hashes)) if primary_hashes else 1.0
        exact_match = primary_hashes.issubset(mirror_hashes)
        if exact_match:
            matched_count += 1
        per_query.append(
            QueryComparison(
                query_hash=_query_hash(query),
                primary_count=len(primary_hashes),
                mirror_count=len(mirror_hashes),
                overlap_count=len(overlap),
                recall_at_k=round(recall, 4),
                exact_match=exact_match,
            )
        )

    total = len(queries)
    mean_recall = round(sum(c.recall_at_k for c in per_query) / total, 4) if total else 1.0
    return {
        "schema_version": READ_COMPARE_SCHEMA,
        "k": k,
        "total_count": total,
        "matched_count": matched_count,
        "mismatch_count": total - matched_count,
        "mean_recall_at_k": mean_recall,
        "per_query": [asdict(c) for c in per_query],
        "network_used": False,
        "raw_query_printed": False,
    }


def recall_parity_passes(report: dict[str, Any], *, min_mean_recall_at_k: float, require_exact: bool = True) -> bool:
    """Gate helper: parity passes when exact-match holds (mismatch==0) if required,
    and mean recall@k meets the threshold. The numeric threshold is set at M7 after
    measuring the RAGFlow baseline (left to the caller, not hard-coded)."""

    if require_exact and report.get("mismatch_count", 1) != 0:
        return False
    return float(report.get("mean_recall_at_k", 0.0)) >= float(min_mean_recall_at_k)


__all__ = [
    "READ_COMPARE_SCHEMA",
    "QueryComparison",
    "compare_recall",
    "recall_parity_passes",
]

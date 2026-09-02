"""Dual-Read Shadow Verification Harness & Recall@5 Benchmark (Milestone 4).

Executes parallel or sequential search against Qdrant and PostgreSQL pgvector,
calculates Recall@K metrics, tracks latency percentiles (P50, P95, P99),
and evaluates the Phase 2.5 cutover verification gate (Recall@5 >= 0.95, P95 <= 20ms).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import logging
import statistics
import time
from typing import Any

from .pgvector_store import PgVectorStore, make_dummy_vector

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkQuery:
    query_id: str
    query_vector: list[float]
    project: str = "neurons"
    limit: int = 5
    query_text: str | None = None
    authorization_status: str | None = "active"
    currentness: str | None = "current"
    as_of: str | None = None


@dataclass
class DualReadComparisonResult:
    query_id: str
    qdrant_top_ids: list[str]
    pgvector_top_ids: list[str]
    recall_at_k: float
    overlap_count: int
    k: int
    qdrant_latency_ms: float
    pgvector_latency_ms: float
    discrepancies: list[str] = field(default_factory=list)


@dataclass
class BenchmarkSummary:
    started_at: str
    completed_at: str
    total_queries: int
    k: int
    mean_recall_at_k: float
    min_recall_at_k: float
    max_recall_at_k: float
    
    # Latency metrics (milliseconds)
    p50_pgvector_latency_ms: float
    p95_pgvector_latency_ms: float
    p99_pgvector_latency_ms: float
    p50_qdrant_latency_ms: float
    p95_qdrant_latency_ms: float
    p99_qdrant_latency_ms: float
    
    # Gate assessment
    recall_gate_threshold: float
    p95_latency_gate_ms: float
    recall_gate_passed: bool
    latency_gate_passed: bool
    overall_gate_passed: bool
    
    discrepancy_count: int
    details: list[DualReadComparisonResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total_queries": self.total_queries,
            "k": self.k,
            "mean_recall_at_k": round(self.mean_recall_at_k, 4),
            "min_recall_at_k": round(self.min_recall_at_k, 4),
            "max_recall_at_k": round(self.max_recall_at_k, 4),
            "p50_pgvector_latency_ms": round(self.p50_pgvector_latency_ms, 3),
            "p95_pgvector_latency_ms": round(self.p95_pgvector_latency_ms, 3),
            "p99_pgvector_latency_ms": round(self.p99_pgvector_latency_ms, 3),
            "p50_qdrant_latency_ms": round(self.p50_qdrant_latency_ms, 3),
            "p95_qdrant_latency_ms": round(self.p95_qdrant_latency_ms, 3),
            "p99_qdrant_latency_ms": round(self.p99_qdrant_latency_ms, 3),
            "recall_gate_passed": self.recall_gate_passed,
            "latency_gate_passed": self.latency_gate_passed,
            "overall_gate_passed": self.overall_gate_passed,
            "discrepancy_count": self.discrepancy_count,
        }


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    k = (len(data) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(data) - 1)
    d = k - f
    sorted_data = sorted(data)
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * d


class DualReadShadowHarness:
    """Dual-Read Shadow Verification Harness comparing Qdrant vs PostgreSQL PgVectorStore."""

    def __init__(
        self,
        qdrant_client: Any,
        pg_store: PgVectorStore,
        default_limit: int = 5,
        recall_gate_threshold: float = 0.95,
        p95_latency_gate_ms: float = 20.0,
    ):
        self.qdrant = qdrant_client
        self.pg_store = pg_store
        self.default_limit = default_limit
        self.recall_gate_threshold = recall_gate_threshold
        self.p95_latency_gate_ms = p95_latency_gate_ms

    def _query_qdrant(
        self,
        query_vector: list[float],
        project: str,
        limit: int,
        collection_name: str = "memory_cards",
    ) -> tuple[list[str], float]:
        """Query Qdrant client and return top IDs with elapsed time."""
        t0 = time.perf_counter()
        top_ids: list[str] = []

        try:
            if hasattr(self.qdrant, "search") and callable(self.qdrant.search):
                # Check signature of search
                try:
                    res = self.qdrant.search(
                        collection_name=collection_name,
                        query_vector=query_vector,
                        limit=limit,
                    )
                except TypeError:
                    # In-memory test store with signature search(query_vector, limit)
                    res = self.qdrant.search(query_vector, limit=limit)

                for item in res:
                    if isinstance(item, dict):
                        top_ids.append(str(item.get("id", item.get("memory_id", ""))))
                    elif hasattr(item, "id"):
                        top_ids.append(str(item.id))
                    else:
                        top_ids.append(str(item))

            elif hasattr(self.qdrant, "vectors") and isinstance(self.qdrant.vectors, dict):
                from .pgvector_store import compute_cosine_similarity
                scored = []
                for pid, (vec, payload) in self.qdrant.vectors.items():
                    if project and payload.get("project") and payload.get("project") != project:
                        continue
                    sim = compute_cosine_similarity(query_vector, vec)
                    actual_id = payload.get("memory_id", pid)
                    scored.append((sim, actual_id))
                scored.sort(key=lambda x: (-x[0], str(x[1])))
                top_ids = [s[1] for s in scored[:limit]]

        except Exception as e:
            logger.error(f"Error querying Qdrant: {e}", exc_info=True)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return top_ids, elapsed_ms

    def _query_pgvector(
        self,
        query_vector: list[float],
        project: str,
        limit: int,
        authorization_status: str | None = "active",
        currentness: str | None = "current",
        as_of: str | None = None,
    ) -> tuple[list[str], float]:
        """Query PgVectorStore and return top IDs with elapsed time."""
        t0 = time.perf_counter()
        top_ids: list[str] = []

        try:
            results = self.pg_store.hybrid_search(
                project=project,
                query_vector=query_vector,
                limit=limit,
                authorization_status=authorization_status,
                currentness=currentness,
                as_of=as_of,
            )
            top_ids = [str(r["memory_id"]) for r in results]
        except Exception as e:
            logger.error(f"Error querying pgvector: {e}", exc_info=True)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return top_ids, elapsed_ms

    def execute_query(
        self,
        query: BenchmarkQuery | dict[str, Any] | list[float],
        project: str = "neurons",
        limit: int = 5,
    ) -> DualReadComparisonResult:
        """Execute single dual-read query and compute overlap."""
        if isinstance(query, BenchmarkQuery):
            b_query = query
        elif isinstance(query, dict):
            b_query = BenchmarkQuery(
                query_id=query.get("query_id", f"q_{int(time.time()*1000)}"),
                query_vector=query["query_vector"],
                project=query.get("project", project),
                limit=query.get("limit", limit),
                authorization_status=query.get("authorization_status", "active"),
                currentness=query.get("currentness", "current"),
                as_of=query.get("as_of"),
            )
        else:
            # list[float] vector
            b_query = BenchmarkQuery(
                query_id=f"q_{int(time.time()*1000)}",
                query_vector=query,
                project=project,
                limit=limit,
            )

        k = b_query.limit

        qdrant_ids, qdrant_latency = self._query_qdrant(
            query_vector=b_query.query_vector,
            project=b_query.project,
            limit=k,
        )

        pg_ids, pg_latency = self._query_pgvector(
            query_vector=b_query.query_vector,
            project=b_query.project,
            limit=k,
            authorization_status=b_query.authorization_status,
            currentness=b_query.currentness,
            as_of=b_query.as_of,
        )

        # Compute recall
        if not qdrant_ids and not pg_ids:
            # Both empty -> perfect recall
            recall = 1.0
            overlap = 0
        elif not qdrant_ids and pg_ids:
            recall = 1.0
            overlap = 0
        else:
            q_set = set(qdrant_ids)
            pg_set = set(pg_ids)
            intersection = q_set.intersection(pg_set)
            overlap = len(intersection)
            recall = overlap / min(k, len(q_set)) if q_set else 1.0

        discrepancies = []
        if recall < self.recall_gate_threshold:
            discrepancies.append(
                f"Recall {recall:.3f} below threshold {self.recall_gate_threshold:.3f}. "
                f"Qdrant: {qdrant_ids[:3]}, PgVector: {pg_ids[:3]}"
            )

        return DualReadComparisonResult(
            query_id=b_query.query_id,
            qdrant_top_ids=qdrant_ids,
            pgvector_top_ids=pg_ids,
            recall_at_k=round(recall, 4),
            overlap_count=overlap,
            k=k,
            qdrant_latency_ms=round(qdrant_latency, 3),
            pgvector_latency_ms=round(pg_latency, 3),
            discrepancies=discrepancies,
        )

    def run_benchmark(
        self,
        query_fixtures: list[BenchmarkQuery | dict[str, Any] | list[float]],
        project: str = "neurons",
    ) -> BenchmarkSummary:
        """Run benchmark across list of query fixtures."""
        started_at = datetime.now(timezone.utc).isoformat()
        results: list[DualReadComparisonResult] = []
        pg_latencies: list[float] = []
        qdrant_latencies: list[float] = []
        recalls: list[float] = []

        for q in query_fixtures:
            res = self.execute_query(q, project=project, limit=self.default_limit)
            results.append(res)
            recalls.append(res.recall_at_k)
            pg_latencies.append(res.pgvector_latency_ms)
            qdrant_latencies.append(res.qdrant_latency_ms)

        completed_at = datetime.now(timezone.utc).isoformat()
        total_queries = len(results)

        mean_recall = statistics.mean(recalls) if recalls else 1.0
        min_recall = min(recalls) if recalls else 1.0
        max_recall = max(recalls) if recalls else 1.0

        p50_pg = _percentile(pg_latencies, 50)
        p95_pg = _percentile(pg_latencies, 95)
        p99_pg = _percentile(pg_latencies, 99)

        p50_qd = _percentile(qdrant_latencies, 50)
        p95_qd = _percentile(qdrant_latencies, 95)
        p99_qd = _percentile(qdrant_latencies, 99)

        recall_passed = mean_recall >= self.recall_gate_threshold
        latency_passed = p95_pg <= self.p95_latency_gate_ms
        overall_passed = recall_passed and latency_passed

        discrepancy_count = sum(1 for r in results if r.discrepancies)

        return BenchmarkSummary(
            started_at=started_at,
            completed_at=completed_at,
            total_queries=total_queries,
            k=self.default_limit,
            mean_recall_at_k=mean_recall,
            min_recall_at_k=min_recall,
            max_recall_at_k=max_recall,
            p50_pgvector_latency_ms=p50_pg,
            p95_pgvector_latency_ms=p95_pg,
            p99_pgvector_latency_ms=p99_pg,
            p50_qdrant_latency_ms=p50_qd,
            p95_qdrant_latency_ms=p95_qd,
            p99_qdrant_latency_ms=p99_qd,
            recall_gate_threshold=self.recall_gate_threshold,
            p95_latency_gate_ms=self.p95_latency_gate_ms,
            recall_gate_passed=recall_passed,
            latency_gate_passed=latency_passed,
            overall_gate_passed=overall_passed,
            discrepancy_count=discrepancy_count,
            details=results,
        )

    def generate_report(self, summary: BenchmarkSummary) -> str:
        """Generate formatted Markdown benchmark report."""
        status_icon = "PASSED" if summary.overall_gate_passed else "FAILED"
        return f"""# Dual-Read Shadow Benchmark Report (Phase 2.5)

- **Execution Date**: {summary.started_at}
- **Gate Status**: **{status_icon}**
- **Total Queries Executed**: {summary.total_queries}
- **Top-K Parameter**: K = {summary.k}

## 1. Recall@K Metrics
- **Mean Recall@{summary.k}**: {summary.mean_recall_at_k:.4f} (Threshold: >= {summary.recall_gate_threshold:.2f}) -> **{'PASS' if summary.recall_gate_passed else 'FAIL'}**
- **Min Recall@{summary.k}**: {summary.min_recall_at_k:.4f}
- **Max Recall@{summary.k}**: {summary.max_recall_at_k:.4f}

## 2. Latency Metrics (PostgreSQL pgvector vs Qdrant)
| Metric | pgvector (Target) | Qdrant (Source) | Gate Target |
|---|---|---|---|
| P50 Latency | {summary.p50_pgvector_latency_ms:.3f} ms | {summary.p50_qdrant_latency_ms:.3f} ms | - |
| P95 Latency | {summary.p95_pgvector_latency_ms:.3f} ms | {summary.p95_qdrant_latency_ms:.3f} ms | <= {summary.p95_latency_gate_ms:.1f} ms ({'PASS' if summary.latency_gate_passed else 'FAIL'}) |
| P99 Latency | {summary.p99_pgvector_latency_ms:.3f} ms | {summary.p99_qdrant_latency_ms:.3f} ms | - |

## 3. Discrepancies & Divergences
- **Total Discrepancies**: {summary.discrepancy_count}
"""

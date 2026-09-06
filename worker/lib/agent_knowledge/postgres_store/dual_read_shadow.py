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
import hashlib
import json
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator
from qdrant_client import QdrantClient, models
from ..model_connectors import DEFAULT_EMBEDDING_DIM

from .pgvector_store import PgVectorStore

logger = logging.getLogger(__name__)

# M5c shadow-gate premises (requirements §3 item 4, design §8 Phase 3).
MIN_CUTOVER_QUERIES = 50
OBSERVATION_WINDOW_SECONDS = 600.0
RELATION_TEMPORAL_GATE = 0.95


class BenchmarkQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    query_id: str
    query_vector: list[float] = Field(min_length=DEFAULT_EMBEDDING_DIM, max_length=DEFAULT_EMBEDDING_DIM)
    project: str = Field(default="neurons", min_length=1, max_length=64)
    limit: int = Field(default=5, ge=1, le=100)
    query_text: str | None = None
    authorization_status: str | None = "active"
    currentness: str | None = "current"
    as_of: str | None = None

    @field_validator("as_of")
    @classmethod
    def validate_as_of(cls, value):
        if value is not None:
            _reference_time(value)
        return value


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
    qdrant_error: str | None = None
    pgvector_error: str | None = None


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
    error_count: int
    sample_size_gate_passed: bool
    benchmark_valid: bool
    details: list[DualReadComparisonResult] = field(default_factory=list)
    evidence_class: str = "test_harness"
    cutover_blockers: list[str] = field(default_factory=list)
    # M5c gate inputs: graph correctness + measured/approved rate bounds +
    # shared 10-minute observation window. None/unverified keeps gate closed.
    relation_temporal_correctness: float | None = None
    false_positive_rate: float | None = None
    fallback_rate: float | None = None
    approved_false_positive_upper: float | None = None
    approved_fallback_rate_upper: float | None = None
    observation_window_seconds: float = 0.0
    observation_window_verified: bool = False

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
            "error_count": self.error_count,
            "sample_size_gate_passed": self.sample_size_gate_passed,
            "benchmark_valid": self.benchmark_valid,
            "evidence_class": self.evidence_class,
            "cutover_blockers": self.cutover_blockers,
            "relation_temporal_correctness": self.relation_temporal_correctness,
            "false_positive_rate": self.false_positive_rate,
            "fallback_rate": self.fallback_rate,
            "approved_false_positive_upper": self.approved_false_positive_upper,
            "approved_fallback_rate_upper": self.approved_fallback_rate_upper,
            "observation_window_seconds": round(self.observation_window_seconds, 3),
            "observation_window_verified": self.observation_window_verified,
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
        minimum_queries_for_cutover: int = 50,
        evidence_class: str = "test_harness",
        backend_preflight_verified: bool = False,
        approved_false_positive_upper: float | None = None,
        approved_fallback_rate_upper: float | None = None,
    ):
        self.qdrant = qdrant_client
        self.pg_store = pg_store
        self.default_limit = default_limit
        self.recall_gate_threshold = recall_gate_threshold
        self.p95_latency_gate_ms = p95_latency_gate_ms
        if minimum_queries_for_cutover < 1:
            raise ValueError("minimum_queries_for_cutover must be positive")
        if evidence_class not in {"test_harness", "live_cutover"}:
            raise ValueError("evidence_class must be test_harness or live_cutover")
        for bound_name, bound in (
            ("approved_false_positive_upper", approved_false_positive_upper),
            ("approved_fallback_rate_upper", approved_fallback_rate_upper),
        ):
            if bound is not None and not 0.0 <= bound <= 1.0:
                raise ValueError(f"{bound_name} must be within [0, 1]")
        if evidence_class == "live_cutover":
            if _is_qdrant_test_double(qdrant_client):
                raise ValueError("live_cutover evidence cannot use an in-memory Qdrant client or mock double")
            if _is_pgvector_test_double(pg_store):
                raise ValueError("live_cutover evidence cannot use a dict-scan/mock pgvector double")
            if not backend_preflight_verified:
                raise ValueError("live_cutover requires verified backend preflight")
        self.minimum_queries_for_cutover = minimum_queries_for_cutover
        self.evidence_class = evidence_class
        self.backend_preflight_verified = backend_preflight_verified
        self.approved_false_positive_upper = approved_false_positive_upper
        self.approved_fallback_rate_upper = approved_fallback_rate_upper

    def _query_qdrant(
        self,
        query_vector: list[float],
        project: str,
        limit: int,
        collection_name: str = "memory_cards",
        authorization_status: str | None = "active",
        currentness: str | None = "current",
        as_of: str | None = None,
    ) -> tuple[list[str], float, str | None]:
        """Query Qdrant client and return top IDs with elapsed time."""
        t0 = time.perf_counter()
        top_ids: list[str] = []
        error_type: str | None = None

        try:
            response = self.qdrant.query_points(
                collection_name=collection_name, query=query_vector, limit=limit,
                query_filter=_authority_query_filter(project, authorization_status, currentness, as_of),
                with_payload=["memory_id"], with_vectors=False,
            )
            for item in response.points:
                memory_id = (item.payload or {}).get("memory_id")
                if not isinstance(memory_id, str) or not memory_id:
                    raise ValueError("canonical_memory_id_missing")
                if memory_id in top_ids:
                    raise ValueError("duplicate_canonical_memory_id")
                top_ids.append(memory_id)
        except Exception:
            error_type = "qdrant_query_failed"
            logger.warning("Qdrant shadow query failed")

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return top_ids, elapsed_ms, error_type

    def _query_pgvector(
        self,
        query_vector: list[float],
        project: str,
        limit: int,
        authorization_status: str | None = "active",
        currentness: str | None = "current",
        as_of: str | None = None,
    ) -> tuple[list[str], float, str | None]:
        """Query PgVectorStore and return top IDs with elapsed time."""
        t0 = time.perf_counter()
        top_ids: list[str] = []
        error_type: str | None = None

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
        except Exception:
            error_type = "pgvector_query_failed"
            logger.warning("PostgreSQL shadow query failed")

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return top_ids, elapsed_ms, error_type

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
                query_id=query.get("query_id", "fixture"),
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
                query_id="fixture",
                query_vector=query,
                project=project,
                limit=limit,
            )

        k = b_query.limit

        qdrant_ids, qdrant_latency, qdrant_error = self._query_qdrant(
            query_vector=b_query.query_vector,
            project=b_query.project,
            limit=k,
            authorization_status=b_query.authorization_status,
            currentness=b_query.currentness,
            as_of=b_query.as_of,
        )

        pg_ids, pg_latency, pgvector_error = self._query_pgvector(
            query_vector=b_query.query_vector,
            project=b_query.project,
            limit=k,
            authorization_status=b_query.authorization_status,
            currentness=b_query.currentness,
            as_of=b_query.as_of,
        )

        # Compute recall. An empty result is only a perfect match when both
        # backends completed successfully and returned empty. A backend error
        # is never converted into an empty result.
        errors = [item for item in (qdrant_error, pgvector_error) if item]
        if errors:
            recall = 0.0
            overlap = 0
        elif not qdrant_ids and not pg_ids:
            recall = 1.0
            overlap = 0
        elif not qdrant_ids and pg_ids:
            # The source side has no reference hit while the target produced
            # one: treat it as a divergence, not an unmeasurable success.
            recall = 0.0
            overlap = 0
        else:
            q_set = set(qdrant_ids)
            pg_set = set(pg_ids)
            intersection = q_set.intersection(pg_set)
            overlap = len(intersection)
            recall = overlap / min(k, len(q_set)) if q_set else 1.0

        discrepancies = []
        if qdrant_error:
            discrepancies.append(f"Qdrant query failed: {qdrant_error}")
        if pgvector_error:
            discrepancies.append(f"PgVector query failed: {pgvector_error}")
        if recall < self.recall_gate_threshold:
            discrepancies.append(
                f"Recall {recall:.3f} below threshold {self.recall_gate_threshold:.3f}."
            )

        return DualReadComparisonResult(
            query_id=b_query.query_id,
            qdrant_top_ids=qdrant_ids,
            pgvector_top_ids=pg_ids,
            recall_at_k=recall,
            overlap_count=overlap,
            k=k,
            qdrant_latency_ms=round(qdrant_latency, 3),
            pgvector_latency_ms=round(pg_latency, 3),
            discrepancies=discrepancies,
            qdrant_error=qdrant_error,
            pgvector_error=pgvector_error,
        )

    def run_benchmark(
        self,
        query_fixtures: list[BenchmarkQuery | dict[str, Any] | list[float]],
        project: str = "neurons",
        relation_temporal_correctness: float | None = None,
        false_positive_rate: float | None = None,
        fallback_rate: float | None = None,
        shared_window_verified: bool = False,
    ) -> BenchmarkSummary:
        """Run benchmark across list of query fixtures."""
        if not query_fixtures:
            raise ValueError("dual-read benchmark requires at least one query fixture")
        for rate_name, rate in (
            ("false_positive_rate", false_positive_rate),
            ("fallback_rate", fallback_rate),
        ):
            if rate is not None and not 0.0 <= rate <= 1.0:
                raise ValueError(f"{rate_name} must be within [0, 1]")
        if relation_temporal_correctness is not None and not 0.0 <= relation_temporal_correctness <= 1.0:
            raise ValueError("relation_temporal_correctness must be within [0, 1]")
        started_at = datetime.now(timezone.utc).isoformat()
        results: list[DualReadComparisonResult] = []
        pg_latencies: list[float] = []
        qdrant_latencies: list[float] = []
        recalls: list[float] = []
        unique_fixtures: set[str] = set()

        for q in query_fixtures:
            fixture = q.model_dump(exclude={"query_id"}) if isinstance(q, BenchmarkQuery) else (
                {key: value for key, value in q.items() if key != "query_id"} if isinstance(q, dict) else q
            )
            unique_fixtures.add(hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest())
            res = self.execute_query(q, project=project, limit=self.default_limit)
            results.append(res)
            recalls.append(res.recall_at_k)
            pg_latencies.append(res.pgvector_latency_ms)
            qdrant_latencies.append(res.qdrant_latency_ms)

        completed_at = datetime.now(timezone.utc).isoformat()
        total_queries = len(results)

        mean_recall = statistics.mean(recalls)
        min_recall = min(recalls)
        max_recall = max(recalls)

        p50_pg = _percentile(pg_latencies, 50)
        p95_pg = _percentile(pg_latencies, 95)
        p99_pg = _percentile(pg_latencies, 99)

        p50_qd = _percentile(qdrant_latencies, 50)
        p95_qd = _percentile(qdrant_latencies, 95)
        p99_qd = _percentile(qdrant_latencies, 99)

        discrepancy_count = sum(1 for r in results if r.discrepancies)
        error_count = sum(
            1
            for r in results
            if r.qdrant_error is not None or r.pgvector_error is not None
        )
        sample_size_gate_passed = len(unique_fixtures) >= max(MIN_CUTOVER_QUERIES, self.minimum_queries_for_cutover)
        benchmark_valid = error_count == 0
        recall_passed = benchmark_valid and mean_recall >= self.recall_gate_threshold
        latency_passed = benchmark_valid and p95_pg <= self.p95_latency_gate_ms
        # 벡터 두 저장소만 비교해서 Graph-first cutover를 승인할 수 없다.
        # M7의 실제 graph 관찰 수집기 연결 전에는 명시적으로 닫아 둔다.
        try:
            window_seconds = max(
                0.0,
                (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds(),
            )
        except ValueError:
            window_seconds = 0.0
        observation_window_verified = bool(shared_window_verified) and window_seconds <= OBSERVATION_WINDOW_SECONDS
        graph_observation_ok = (
            relation_temporal_correctness is not None
            and relation_temporal_correctness >= RELATION_TEMPORAL_GATE
        )
        rate_bounds_ok = (
            false_positive_rate is not None
            and self.approved_false_positive_upper is not None
            and false_positive_rate <= self.approved_false_positive_upper
            and fallback_rate is not None
            and self.approved_fallback_rate_upper is not None
            and fallback_rate <= self.approved_fallback_rate_upper
        )
        all_recall_at_5 = all(r.k == 5 for r in results)
        blockers = []
        if not graph_observation_ok:
            blockers.append("graph_observation_not_connected")
        if not self.backend_preflight_verified:
            blockers.append("backend_preflight_not_verified")
        if not observation_window_verified:
            blockers.append("shared_10_minute_window_not_verified")
        if not rate_bounds_ok:
            blockers.append("approved_rate_bounds_not_verified")
        if self.evidence_class != "live_cutover":
            blockers.append("test_harness_not_cutover_evidence")
        if not sample_size_gate_passed:
            blockers.append("insufficient_unique_queries")
        if not all_recall_at_5:
            blockers.append("recall_at_5_required")
        if not (benchmark_valid and recall_passed and latency_passed):
            blockers.append("vector_metrics_failed")
        overall_passed = (
            self.evidence_class == "live_cutover"
            and benchmark_valid
            and recall_passed
            and latency_passed
            and sample_size_gate_passed
            and observation_window_verified
            and self.backend_preflight_verified
            and graph_observation_ok
            and rate_bounds_ok
            and all_recall_at_5
        )

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
            error_count=error_count,
            sample_size_gate_passed=sample_size_gate_passed,
            benchmark_valid=benchmark_valid,
            details=results,
            evidence_class=self.evidence_class,
            cutover_blockers=blockers,
        )

    def generate_report(self, summary: BenchmarkSummary) -> str:
        """Generate formatted Markdown benchmark report."""
        status_icon = "PASSED" if summary.overall_gate_passed else "FAILED"
        return f"""# Dual-Read Shadow Benchmark Report (Phase 2.5)

- **Execution Date**: {summary.started_at}
- **Evidence Class**: `{summary.evidence_class}`
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
- **Backend Errors**: {summary.error_count}
- **Sample-size Gate**: {'PASS' if summary.sample_size_gate_passed else 'FAIL'} (minimum {self.minimum_queries_for_cutover})
- **Cutover blockers**: {', '.join(summary.cutover_blockers)}
"""


def _has_in_memory_qdrant_state(client: Any) -> bool:
    """Detect the explicit dict seams used by unit-test Qdrant doubles."""

    from qdrant_client.local.qdrant_local import QdrantLocal

    return isinstance(getattr(client, "_client", None), QdrantLocal) or any(
        isinstance(getattr(client, attribute, None), dict)
        for attribute in ("vectors", "collections")
    )


def _is_qdrant_test_double(client: Any) -> bool:
    """True for in-memory Qdrant state, mocks, and hand-rolled doubles.

    live_cutover evidence requires a real QdrantClient over a real backend;
    anything else must fail closed at construction time.
    """
    if _has_in_memory_qdrant_state(client):
        return True
    if not isinstance(client, QdrantClient):
        return True
    module = type(client).__module__
    return module.split(".")[0] in {"unittest", "mock", "pytest_mock"} or "mock" in module


def _is_pgvector_test_double(store: Any) -> bool:
    """True unless the store is the real SQL-backed PgVectorStore.

    Production PgVectorStore never keeps cards in a process-local dict;
    dict-scan doubles (e.g. a ``cards`` dict) are unit-test-only evidence.
    """
    if not isinstance(store, PgVectorStore):
        return True
    return any(
        isinstance(getattr(store, attribute, None), dict)
        for attribute in ("cards", "store", "memory", "vectors")
    )


def _reference_time(as_of: str | None) -> datetime:
    if as_of is None:
        return datetime.now(timezone.utc)
    value = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if len(as_of) == 10:
        value = value.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        raise ValueError("as_of requires timezone")
    return value


def _authority_query_filter(project, authorization_status, currentness, as_of):
    reference = _reference_time(as_of)
    conditions = [
        models.FieldCondition(key="project", match=models.MatchValue(value=project)),
        models.FieldCondition(key="lifecycle_state", match=models.MatchAny(any=["accepted", "human_accepted", "auto_accepted"])),
        models.FieldCondition(key="valid_from", range=models.DatetimeRange(lte=reference)),
        models.Filter(should=[
            models.FieldCondition(key="valid_to", range=models.DatetimeRange(gt=reference)),
            models.IsEmptyCondition(is_empty=models.PayloadField(key="valid_to")),
        ]),
    ]
    if authorization_status is not None:
        conditions.append(models.FieldCondition(key="authorization_status", match=models.MatchValue(value=authorization_status)))
    if currentness is not None:
        values = ["current", "superseded"] if as_of and currentness == "current" else [currentness]
        conditions.append(models.FieldCondition(key="currentness", match=models.MatchAny(any=values)))
    return models.Filter(must=conditions)

"""session-memory Qdrant 미러 vs RetiredIndexBridge parity-soak 러너.

backfill 이후 parity 게이트를 위해 순수-계산 harness :func:`compare_recall` /
:func:`recall_parity_passes` (``qdrant_read_compare``)를 감싼다:

- ``primary_fetch``는 session-memory dataset에 대한 RetiredIndexBridge retrieve이며, 각 chunk가
  authority-join된다(즉 primary 쪽 자체가 권위 recall 집합이다).
- ``mirror_fetch``는 Qdrant 쿼리의 raw candidate를
  :class:`CouchDBProjectionStateAuthorityResolver`로 :func:`join_mirror_hits_to_authority`
  하여 권위에 결합한 것이다 -- 제품 read 경로가 쓰는 것과 동일한 projection-state join이라,
  세션이 여전히 PROJECTED이고 content_hash가 현재 투영된 body와 일치할 때만 미러 hit이 집계된다.

필수 non-emptiness 가드: :func:`compare_recall`는 primary top-k가 비면 공허하게
recall=1.0 / exact-match로 취급한다(``qdrant_read_compare.py:89-90``). 대부분 쿼리에서
RetiredIndexBridge가 아무것도 안 돌려주는 soak은 그래서 *거짓* green을 보고하게 된다. 이 러너는 primary
fetch가 cohort의 최소 ``min_nonempty_fraction`` 이상에서 >0 hit을 돌려주지 않으면 실행을
REJECT한다(``passed=False``, ``rejected_reason='insufficient_primary_coverage'``). parity
판정은 primary coverage가 그 하한을 넘을 때만 신뢰한다.
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
    """redaction-safe parity 결과(카운트/비율/플래그만)."""

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
    """raw 미러-candidate 쿼리를 감싸 hit이 authority-join되도록 한다.

    ``mirror_query(query) -> list[raw mirror hit dicts]`` (각 hit은 ``content_hash`` +
    ``session_id_hash`` 보유). 반환되는 fetcher는 모든 candidate를 CouchDB projection-state
    게이트로 resolve하고 resolve 안 되는 것은 버리므로, parity harness는 authority-join된
    미러 hit만 보게 된다.
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
    """필수 primary non-emptiness 가드와 함께 parity soak을 실행한다.

    두 fetcher 모두 이미 AUTHORITY-JOIN된 hit을 내야 한다(harness는 raw 미러 candidate를
    거부한다). primary coverage가 cohort의 ``min_nonempty_fraction`` 미만이면 판정은
    rejected다 -- 절대 green pass가 아니다.
    """

    cohort = list(queries or [])
    cohort_size = len(cohort)

    # primary_fetch를 메모이즈해, 아래 coverage probe와 compare_recall 내부 fetch가 쿼리당
    # (RetiredIndexBridge) primary를 두 번 호출하지 않게 한다.
    _primary_cache: dict[str, list[dict[str, Any]]] = {}

    def cached_primary_fetch(query: str) -> list[dict[str, Any]]:
        if query not in _primary_cache:
            _primary_cache[query] = list(primary_fetch(query) or [])
        return _primary_cache[query]

    # recall 수치를 신뢰하기 전에 primary coverage를 먼저 확인한다.
    nonempty_primary = 0
    for query in cohort:
        if cached_primary_fetch(query):
            nonempty_primary += 1

    fraction = (nonempty_primary / cohort_size) if cohort_size else 0.0

    # 빈 cohort 또는 primary coverage 부족 => reject(vacuous-recall 가드).
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

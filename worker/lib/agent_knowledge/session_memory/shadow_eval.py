from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..couchdb_source.document_model import SourceDocType, sha256_hash
from ..couchdb_source.session_memory_materializer import (
    MaterializedSessionMemory,
    materialize_session_memory,
)
from ..session_memory.segments import SessionSegment, build_segments


SCHEMA_VERSION = "session_memory_shadow_eval.v1"
MAX_QUERY_BYTES = 300
DEFAULT_PER_BUCKET = 60
DEFAULT_MAX_BYTES = 8_388_608
DEFAULT_MAX_SESSIONS_SCAN = 20_000
SEGMENT_MAX_BYTES = 4096
ALPHAS = (0.0, 0.0025, 0.005, 0.01, 0.02, 0.04)
ALPHA_KEYS = {
    0.0: "alpha_0",
    0.0025: "alpha_0_0025",
    0.005: "alpha_0_005",
    0.01: "alpha_0_01",
    0.02: "alpha_0_02",
    0.04: "alpha_0_04",
}
BUCKETS = (
    "lt_16k",
    "16k_64k",
    "64k_256k",
    "256k_1m",
    "1m_8m",
)
POSITIONS = ("head", "middle", "tail")
_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "configuration",
        "seed",
        "per_bucket",
        "max_bytes",
        "max_sessions_scan",
        "sampling",
        "scanned_sessions",
        "sampled_sessions",
        "excluded_oversize_sessions",
        "excluded_not_fully_materialized_sessions",
        "excluded_materialization_failures",
        "buckets",
        "eligible",
        "sampled",
        "metrics",
        "M0",
        "S4",
        "overall",
        "by_bucket",
        "by_position",
        "recall_at_1",
        "recall_at_5",
        "mrr",
        "query_count",
        "segment_counts",
        "p50",
        "p90",
        "max",
        "embedding_calls",
        "elapsed_seconds",
        *BUCKETS,
        *POSITIONS,
        *ALPHA_KEYS.values(),
    }
)
_ALLOWED_STRING_VALUES = frozenset({SCHEMA_VERSION})


class _Metrics:
    def __init__(self) -> None:
        self.overall: list[int | None] = []
        self.by_bucket: dict[str, list[int | None]] = {key: [] for key in BUCKETS}
        self.by_position: dict[str, list[int | None]] = {key: [] for key in POSITIONS}

    def add(self, *, rank: int | None, bucket: str, position: str) -> None:
        self.overall.append(rank)
        self.by_bucket[bucket].append(rank)
        self.by_position[position].append(rank)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": _summarize_ranks(self.overall),
            "by_bucket": {
                bucket: _summarize_ranks(self.by_bucket[bucket]) for bucket in BUCKETS
            },
            "by_position": {
                position: _summarize_ranks(self.by_position[position])
                for position in POSITIONS
            },
        }


def _summarize_ranks(ranks: Sequence[int | None]) -> dict[str, int | float]:
    count = len(ranks)
    if not count:
        return {
            "query_count": 0,
            "recall_at_1": 0.0,
            "recall_at_5": 0.0,
            "mrr": 0.0,
        }
    return {
        "query_count": count,
        "recall_at_1": sum(rank is not None and rank <= 1 for rank in ranks) / count,
        "recall_at_5": sum(rank is not None and rank <= 5 for rank in ranks) / count,
        "mrr": sum(1.0 / rank if rank is not None else 0.0 for rank in ranks) / count,
    }


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _bucket_for_size(size_bytes: int) -> str:
    if size_bytes < 16 * 1024:
        return "lt_16k"
    if size_bytes < 64 * 1024:
        return "16k_64k"
    if size_bytes < 256 * 1024:
        return "64k_256k"
    if size_bytes < 1024 * 1024:
        return "256k_1m"
    return "1m_8m"


def _positioned_views(
    views: Sequence[Any],
) -> tuple[tuple[int, str], ...]:
    count = len(views)
    if count == 0:
        return ()
    if count == 1:
        return ((0, "head"),)
    if count == 2:
        return ((0, "head"), (1, "tail"))
    indexes = (
        int(count * 0.1),
        int(count * 0.5),
        min(count - 1, int(count * 0.9)),
    )
    return tuple(zip(indexes, POSITIONS))


def _utf8_prefix(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _render_m0_body(
    materialized: MaterializedSessionMemory,
    views: Sequence[Any],
) -> str:
    lines = [
        f"# session-memory {materialized.provider} {materialized.project}",
        f"session_id_hash: {materialized.session_id_hash}",
        "",
        "## conversation",
    ]
    for view in views:
        lines.extend(("", str(view.text).rstrip()))
    lines.append("")
    lines.append("## tool_evidence_summary")
    for evidence_body in materialized.tool_evidence_bodies:
        lines.extend(("", str(evidence_body).rstrip()))
    return "\n".join(lines).rstrip() + "\n"


def _materialized_without_view(
    materialized: MaterializedSessionMemory,
    view_index: int,
) -> MaterializedSessionMemory:
    views = tuple(
        view
        for index, view in enumerate(materialized.conversation_views)
        if index != view_index
    )
    body = _render_m0_body(materialized, views)
    return replace(
        materialized,
        body=body,
        content_hash=sha256_hash(body),
        conversation_views=views,
    )


def _rank_for_target(
    scores: Mapping[str, float],
    target_session_id_hash: str,
) -> int | None:
    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    try:
        return ordered.index(target_session_id_hash) + 1
    except ValueError:
        return None


def _segment_count_percentile(counts: Sequence[int], percentile: float) -> float:
    if not counts:
        return 0.0
    ordered = sorted(counts)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return float(ordered[rank - 1])


def _validate_safe_report(value: Any) -> None:
    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key not in _ALLOWED_KEYS:
                    raise ValueError("shadow report contains an unapproved key")
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if isinstance(item, str):
            if item not in _ALLOWED_STRING_VALUES:
                raise ValueError("shadow report contains an unapproved string")
            return
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("shadow report contains a non-numeric value")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("shadow report contains a non-finite number")

    if not isinstance(value, dict):
        raise ValueError("shadow report must be an object")
    visit(value)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("shadow report schema version is invalid")


def run_shadow_eval(
    *,
    store: Any,
    embedder: Any,
    seed: int = 380,
    per_bucket: int = DEFAULT_PER_BUCKET,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_sessions_scan: int = DEFAULT_MAX_SESSIONS_SCAN,
    materializer: Callable[..., MaterializedSessionMemory] = materialize_session_memory,
) -> dict[str, Any]:
    if isinstance(per_bucket, bool) or per_bucket < 1:
        raise ValueError("per_bucket must be positive")
    if isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if isinstance(max_sessions_scan, bool) or max_sessions_scan < 1:
        raise ValueError("max_sessions_scan must be positive")

    started = time.monotonic()
    rng = random.Random(seed)
    reservoirs: dict[str, list[tuple[MaterializedSessionMemory, int]]] = {
        bucket: [] for bucket in BUCKETS
    }
    eligible_counts = {bucket: 0 for bucket in BUCKETS}
    seen_session_ids: set[str] = set()
    scanned_sessions = 0
    excluded_oversize = 0
    excluded_not_fully_materialized = 0
    excluded_materialization_failures = 0

    for session_document in store.iter_by_type(
        SourceDocType.TRANSCRIPT_SESSION,
        fields=["session_id_hash"],
        limit=max_sessions_scan,
    ):
        if scanned_sessions >= max_sessions_scan:
            break
        scanned_sessions += 1
        session_id_hash = str(session_document.get("session_id_hash") or "")
        if not session_id_hash or session_id_hash in seen_session_ids:
            continue
        seen_session_ids.add(session_id_hash)
        try:
            materialized = materializer(
                session_id_hash=session_id_hash,
                store=store,
            )
        except Exception:
            excluded_materialization_failures += 1
            continue
        if not materialized.fully_materialized:
            excluded_not_fully_materialized += 1
            continue
        size_bytes = len(materialized.body.encode("utf-8"))
        if size_bytes > max_bytes:
            excluded_oversize += 1
            continue
        bucket = _bucket_for_size(size_bytes)
        eligible_counts[bucket] += 1
        reservoir = reservoirs[bucket]
        if len(reservoir) < per_bucket:
            reservoir.append((materialized, size_bytes))
            continue
        replacement_index = rng.randrange(eligible_counts[bucket])
        if replacement_index < per_bucket:
            reservoir[replacement_index] = (materialized, size_bytes)

    sampled_by_bucket: dict[str, list[tuple[MaterializedSessionMemory, int]]] = {
        bucket: sorted(
            reservoirs[bucket],
            key=lambda entry: entry[0].session_id_hash,
        )
        for bucket in BUCKETS
    }
    sampled = [
        entry
        for bucket in BUCKETS
        for entry in sampled_by_bucket[bucket]
    ]
    original_m0_vectors: dict[str, list[float]] = {}
    original_s4_vectors: dict[str, tuple[list[list[float]], int]] = {}
    original_metadata: dict[str, tuple[MaterializedSessionMemory, int, str]] = {}
    segment_counts: list[int] = []
    embedding_calls = 0

    def embed(text: str) -> list[float]:
        nonlocal embedding_calls
        embedding_calls += 1
        vector = [float(value) for value in embedder.embed(text)]
        if not vector or any(not math.isfinite(value) for value in vector):
            raise ValueError("embedding provider returned an invalid vector")
        return vector

    for bucket in BUCKETS:
        for materialized, size_bytes in sampled_by_bucket[bucket]:
            session_id_hash = materialized.session_id_hash
            segments = (
                build_segments(materialized, max_bytes=SEGMENT_MAX_BYTES)
                if materialized.conversation_views
                else ()
            )
            segment_counts.append(len(segments))
            original_metadata[session_id_hash] = (
                materialized,
                size_bytes,
                bucket,
            )
            original_m0_vectors[session_id_hash] = embed(materialized.body)
            original_s4_vectors[session_id_hash] = (
                [embed(segment.text) for segment in segments],
                len(segments),
            )

    metrics: dict[str, _Metrics] = {
        "M0": _Metrics(),
        **{ALPHA_KEYS[alpha]: _Metrics() for alpha in ALPHAS},
    }
    query_count = 0
    candidate_ids = sorted(original_metadata)
    for target_session_id_hash in candidate_ids:
        materialized, _, bucket = original_metadata[target_session_id_hash]
        for view_index, position in _positioned_views(materialized.conversation_views):
            query_text = _utf8_prefix(
                str(materialized.conversation_views[view_index].text),
                MAX_QUERY_BYTES,
            )
            if not query_text:
                continue
            query_vector = embed(query_text)
            query_count += 1
            leave_one_out = _materialized_without_view(materialized, view_index)
            leave_one_out_m0_vector = embed(leave_one_out.body)
            leave_one_out_segments: tuple[SessionSegment, ...] = (
                build_segments(
                    leave_one_out,
                    max_bytes=SEGMENT_MAX_BYTES,
                )
                if leave_one_out.conversation_views
                else ()
            )
            leave_one_out_s4_vectors = [
                embed(segment.text) for segment in leave_one_out_segments
            ]

            m0_scores = {
                candidate_id: _cosine(
                    query_vector,
                    (
                        leave_one_out_m0_vector
                        if candidate_id == target_session_id_hash
                        else original_m0_vectors[candidate_id]
                    ),
                )
                for candidate_id in candidate_ids
            }
            metrics["M0"].add(
                rank=_rank_for_target(m0_scores, target_session_id_hash),
                bucket=bucket,
                position=position,
            )

            for alpha in ALPHAS:
                scores: dict[str, float] = {}
                for candidate_id in candidate_ids:
                    if candidate_id == target_session_id_hash:
                        vectors = leave_one_out_s4_vectors
                        segment_count = len(leave_one_out_segments)
                    else:
                        vectors, segment_count = original_s4_vectors[candidate_id]
                    if not vectors or segment_count < 1:
                        continue
                    max_similarity = max(
                        _cosine(query_vector, vector) for vector in vectors
                    )
                    scores[candidate_id] = max_similarity - alpha * math.log(
                        segment_count
                    )
                metrics[ALPHA_KEYS[alpha]].add(
                    rank=_rank_for_target(scores, target_session_id_hash),
                    bucket=bucket,
                    position=position,
                )

    segment_report = {
        "p50": _segment_count_percentile(segment_counts, 0.5),
        "p90": _segment_count_percentile(segment_counts, 0.9),
        "max": max(segment_counts, default=0),
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "configuration": {
            "seed": int(seed),
            "per_bucket": int(per_bucket),
            "max_bytes": int(max_bytes),
            "max_sessions_scan": int(max_sessions_scan),
        },
        "sampling": {
            "scanned_sessions": scanned_sessions,
            "sampled_sessions": len(sampled),
            "excluded_oversize_sessions": excluded_oversize,
            "excluded_not_fully_materialized_sessions": excluded_not_fully_materialized,
            "excluded_materialization_failures": excluded_materialization_failures,
            "buckets": {
                bucket: {
                    "eligible": eligible_counts[bucket],
                    "sampled": len(sampled_by_bucket[bucket]),
                }
                for bucket in BUCKETS
            },
        },
        "query_count": query_count,
        "metrics": {
            "M0": metrics["M0"].to_dict(),
            "S4": {
                ALPHA_KEYS[alpha]: metrics[ALPHA_KEYS[alpha]].to_dict()
                for alpha in ALPHAS
            },
        },
        "segment_counts": segment_report,
        "embedding_calls": embedding_calls,
        "elapsed_seconds": time.monotonic() - started,
    }
    _validate_safe_report(report)
    return report


def _build_source_store(environ: Mapping[str, str]) -> Any:
    from ..couchdb_source.couchdb_http_store import (
        CouchDBHttpSourceStore,
        basic_auth_header,
    )

    base_url = str(environ.get("COUCHDB_URL") or "").strip()
    if not base_url:
        raise ValueError("COUCHDB_URL is required")
    user = str(environ.get("COUCHDB_USER") or "")
    password = str(environ.get("COUCHDB_PASSWORD") or "")
    auth_header = basic_auth_header(user=user, password=password) if user else ""
    return CouchDBHttpSourceStore(
        base_url=base_url,
        db=str(environ.get("COUCHDB_DB") or "transcript_source"),
        auth_header=auth_header,
    )


def _build_embedding_provider(environ: Mapping[str, str]) -> Any:
    from ..rag_ingress.qdrant_embedding import build_openai_embedding_provider

    return build_openai_embedding_provider(environ=environ)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent_knowledge.session_memory.shadow_eval")
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-bucket", type=int, default=DEFAULT_PER_BUCKET)
    parser.add_argument("--seed", type=int, default=380)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--max-sessions-scan",
        type=int,
        default=DEFAULT_MAX_SESSIONS_SCAN,
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    store: Any | None = None,
    embedder: Any | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    env = environ if environ is not None else os.environ
    source_store = store
    provider = embedder
    owns_provider = provider is None
    try:
        if source_store is None:
            source_store = _build_source_store(env)
        if provider is None:
            provider = _build_embedding_provider(env)
        report = run_shadow_eval(
            store=source_store,
            embedder=provider,
            seed=args.seed,
            per_bucket=args.per_bucket,
            max_bytes=args.max_bytes,
            max_sessions_scan=args.max_sessions_scan,
        )
        _validate_safe_report(report)
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "sampled_sessions": report["sampling"]["sampled_sessions"],
                    "query_count": report["query_count"],
                    "embedding_calls": report["embedding_calls"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except ValueError as exc:
        if "shadow report" in str(exc) or "shadow report schema" in str(exc):
            return 2
        print("shadow_eval_failed", file=sys.stderr)
        return 1
    except Exception:
        print("shadow_eval_failed", file=sys.stderr)
        return 1
    finally:
        if owns_provider and provider is not None:
            closer = getattr(provider, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())

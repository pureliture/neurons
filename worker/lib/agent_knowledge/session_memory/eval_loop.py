"""LLM-brain eval loop: run enabled eval_queries and persist bounded audit rows.

The loop deliberately evaluates the existing read-side brain.query behavior and writes
only append-style eval_runs/retrieval_audit/context-pack audit rows when execute=True.
By default it does not call models, mutate MemoryCards, or touch graph/vector backends;
callers may inject an embedding-backed semantic_ranker for an explicit model lane.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .brain_query import run_brain_query_v2
from .brain_read_model import LegacyLedgerBrainReadModel

EVAL_LOOP_SCHEMA_VERSION = "llm_brain_eval_loop.v1"

QueryRunner = Callable[..., dict]


def _retention_disabled(retain_runs: int | None = None) -> dict:
    return {
        "enabled": False,
        "retain_runs": int(retain_runs or 0),
        "candidate_run_count": 0,
        "deleted_run_count": 0,
        "deleted_context_pack_count": 0,
        "deleted_context_pack_item_count": 0,
        "deleted_retrieval_audit_count": 0,
    }


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _count_by_ids(connection, table: str, column: str, values: Sequence[str]) -> int:
    if not values:
        return 0
    row = connection.execute(
        f"SELECT count(*) AS n FROM {table} WHERE {column} IN ({_placeholders(len(values))})",
        list(values),
    ).fetchone()
    return int(row["n"])


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"eval_run_{stamp}_{uuid.uuid4().hex[:12]}"


def _query_text(query: Mapping[str, Any]) -> str:
    terms = query.get("query_terms") or []
    if not isinstance(terms, Sequence) or isinstance(terms, (str, bytes)):
        return ""
    return " ".join(str(term).strip() for term in terms if str(term).strip())


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _result_items(response: Mapping[str, Any]) -> list[dict]:
    results = response.get("results")
    if isinstance(results, list) and results:
        return [dict(item) for item in results if isinstance(item, Mapping)]
    items: list[dict] = []
    for lane_name in ("current", "accepted"):
        lane = response.get(lane_name)
        if not isinstance(lane, list):
            continue
        for item in lane:
            if isinstance(item, Mapping):
                items.append(dict(item))
    return items


def _result_memory_ids(response: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in _result_items(response):
        memory_id = str(item.get("memory_id") or item.get("source_ref") or "")
        if memory_id:
            ids.append(memory_id)
    return _dedupe(ids)


def _score_query(*, expected_ids: Sequence[str], retrieved_ids: Sequence[str], min_recall: float, min_precision: float) -> dict:
    expected = set(str(value) for value in expected_ids if str(value))
    retrieved = set(str(value) for value in retrieved_ids if str(value))
    matched = expected & retrieved
    recall = 1.0 if not expected else len(matched) / len(expected)
    precision = 1.0 if not retrieved else len(matched) / len(retrieved)
    passed = recall >= float(min_recall) and precision >= float(min_precision)
    return {
        "expected_count": len(expected),
        "retrieved_count": len(retrieved),
        "matched_count": len(matched),
        "recall": recall,
        "precision": precision,
        "passed": passed,
    }


def _pack_items(response: Mapping[str, Any], *, run_id: str, query_id: str) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for rank, item in enumerate(_result_items(response)):
        memory_id = str(item.get("memory_id") or item.get("source_ref") or "")
        if not memory_id or memory_id in seen:
            continue
        seen.add(memory_id)
        items.append(
            {
                "kind": "memory_card",
                "memory_id": memory_id,
                # Do not persist raw MemoryCard summary/title in eval audit rows.
                "title": "",
                "summary": "",
                "score": item.get("score") if isinstance(item.get("score"), (int, float)) else None,
                "metadata": {
                    "schema_version": EVAL_LOOP_SCHEMA_VERSION,
                    "run_id": run_id,
                    "query_id": query_id,
                    "rank": rank,
                    "why_retrieved": str(item.get("why_retrieved") or ""),
                    "currentness": str(item.get("currentness") or ""),
                    "card_type": str(item.get("card_type") or ""),
                },
            }
        )
    return items


def _record_retrieval_audit(
    ledger,
    *,
    run_id: str,
    query: Mapping[str, Any],
    query_text: str,
    query_hash: str,
    response: Mapping[str, Any],
) -> None:
    pack_id = f"eval_pack_{uuid.uuid4().hex}"
    pack = {
        "pack_id": pack_id,
        "prompt_hash": _sha256_text(query_text),
        "items": _pack_items(response, run_id=run_id, query_id=str(query.get("query_id") or "")),
    }
    ledger.record_context_pack(
        pack,
        filters={
            "schema_version": EVAL_LOOP_SCHEMA_VERSION,
            "eval_run_id": run_id,
            "query_id": str(query.get("query_id") or ""),
            "project": str(query.get("project") or ""),
            "provider": str(query.get("provider") or ""),
            "k": int(query.get("k") or 0),
        },
        query_hash=query_hash,
        private_allowed=False,
    )


def _prune_eval_history(*, ledger, project: str | None, provider: str | None, retain_runs: int) -> dict:
    """Prune old eval-loop-owned audit rows after a successful append.

    Retention is scoped to an explicit project/provider pair and to context packs
    stamped with this eval-loop schema version. It never touches MemoryCards,
    eval_queries, graph/vector stores, or non-eval context packs.
    """

    retain = int(retain_runs or 0)
    if retain <= 0:
        return _retention_disabled(retain)
    if not project or not provider:
        raise ValueError("eval retention requires explicit project and provider")

    with ledger._connect() as connection:
        rows = connection.execute(
            """
            SELECT run_id
            FROM eval_runs
            WHERE project = ? AND provider = ?
            ORDER BY created_at DESC, run_id DESC
            """,
            (project, provider),
        ).fetchall()
        run_ids = [str(row["run_id"]) for row in rows]
        stale_run_ids = run_ids[retain:]
        stale_run_id_set = set(stale_run_ids)

        pack_ids: list[str] = []
        if stale_run_ids:
            pack_rows = connection.execute("SELECT pack_id, filters_json FROM context_packs").fetchall()
            for row in pack_rows:
                try:
                    filters = json.loads(str(row["filters_json"] or "{}"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(filters, dict):
                    continue
                if filters.get("schema_version") != EVAL_LOOP_SCHEMA_VERSION:
                    continue
                if str(filters.get("project") or "") != project or str(filters.get("provider") or "") != provider:
                    continue
                if str(filters.get("eval_run_id") or "") in stale_run_id_set:
                    pack_ids.append(str(row["pack_id"]))

        deleted_context_pack_item_count = _count_by_ids(connection, "context_pack_items", "pack_id", pack_ids)
        deleted_retrieval_audit_count = _count_by_ids(connection, "retrieval_audit", "pack_id", pack_ids)
        deleted_context_pack_count = _count_by_ids(connection, "context_packs", "pack_id", pack_ids)

        if pack_ids:
            placeholders = _placeholders(len(pack_ids))
            connection.execute(f"DELETE FROM context_pack_items WHERE pack_id IN ({placeholders})", pack_ids)
            connection.execute(f"DELETE FROM retrieval_audit WHERE pack_id IN ({placeholders})", pack_ids)
            connection.execute(f"DELETE FROM context_packs WHERE pack_id IN ({placeholders})", pack_ids)
        if stale_run_ids:
            connection.execute(
                f"DELETE FROM eval_runs WHERE run_id IN ({_placeholders(len(stale_run_ids))})",
                stale_run_ids,
            )

    return {
        "enabled": True,
        "retain_runs": retain,
        "candidate_run_count": len(run_ids),
        "deleted_run_count": len(stale_run_ids),
        "deleted_context_pack_count": deleted_context_pack_count,
        "deleted_context_pack_item_count": deleted_context_pack_item_count,
        "deleted_retrieval_audit_count": deleted_retrieval_audit_count,
    }


def _aggregate(per_query: list[dict]) -> dict:
    query_count = len(per_query)
    passed_count = sum(1 for item in per_query if item.get("passed"))
    failed_count = query_count - passed_count
    if query_count:
        avg_recall = sum(float(item.get("recall") or 0.0) for item in per_query) / query_count
        avg_precision = sum(float(item.get("precision") or 0.0) for item in per_query) / query_count
    else:
        avg_recall = 0.0
        avg_precision = 0.0
    return {
        "query_count": query_count,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "avg_recall": avg_recall,
        "avg_precision": avg_precision,
        "retrieved_count": sum(int(item.get("retrieved_count") or 0) for item in per_query),
        "matched_count": sum(int(item.get("matched_count") or 0) for item in per_query),
        "expected_count": sum(int(item.get("expected_count") or 0) for item in per_query),
        "per_query": per_query,
    }


def run_enabled_eval_queries(
    *,
    ledger,
    project: str | None = None,
    provider: str | None = None,
    limit: int | None = None,
    execute: bool = False,
    run_id: str | None = None,
    retain_runs: int = 0,
    semantic_ranker: Callable[..., list[dict]] | None = None,
    query_runner: QueryRunner = run_brain_query_v2,
) -> dict:
    """Run enabled eval_queries and optionally persist eval_runs/retrieval_audit.

    ``execute=False`` is a strict dry-run: no ledger writes are performed. ``execute=True``
    appends one eval_runs row and one retrieval_audit row per successfully executed query.
    The returned/stored metrics contain aggregate counts and query IDs only, not raw query
    text, summaries, or MemoryCard IDs.
    """

    queries = ledger.list_eval_queries(project=project, provider=provider, enabled_only=True)
    if limit is not None:
        queries = queries[: max(int(limit), 0)]
    effective_run_id = run_id or _new_run_id()
    read_model = LegacyLedgerBrainReadModel(ledger)
    per_query: list[dict] = []
    failures: list[dict] = []
    max_k = 0

    for query in queries:
        query_id = str(query.get("query_id") or "")
        query_text = _query_text(query)
        query_hash = str(query.get("query_hash") or _sha256_text(query_text))
        k = int(query.get("k") or 1)
        max_k = max(max_k, k)
        if not query_text:
            score = {
                "query_id": query_id,
                "expected_count": len(query.get("expected_memory_ids") or []),
                "retrieved_count": 0,
                "matched_count": 0,
                "recall": 0.0,
                "precision": 0.0,
                "passed": False,
                "error_type": "empty_query_terms",
            }
            per_query.append(score)
            failures.append({"query_id": query_id, "reason": "empty_query_terms"})
            continue
        try:
            response = query_runner(
                read_model=read_model,
                brain_id=f"/project/{query['project']}",
                query=query_text,
                query_terms=list(query.get("query_terms") or []),
                query_intent="eval",
                limit=k,
                semantic_ranker=semantic_ranker,
            )
            retrieved_ids = _result_memory_ids(response)
            score = _score_query(
                expected_ids=list(query.get("expected_memory_ids") or []),
                retrieved_ids=retrieved_ids,
                min_recall=float(query.get("min_recall") or 0.0),
                min_precision=float(query.get("min_precision") or 0.0),
            )
            score["query_id"] = query_id
            per_query.append(score)
            if not score["passed"]:
                failures.append(
                    {
                        "query_id": query_id,
                        "reason": "threshold_miss",
                        "recall": score["recall"],
                        "precision": score["precision"],
                        "expected_count": score["expected_count"],
                        "retrieved_count": score["retrieved_count"],
                    }
                )
            if execute:
                _record_retrieval_audit(
                    ledger,
                    run_id=effective_run_id,
                    query=query,
                    query_text=query_text,
                    query_hash=query_hash,
                    response=response,
                )
        except Exception as exc:  # pragma: no cover - defensive runtime reporting path
            score = {
                "query_id": query_id,
                "expected_count": len(query.get("expected_memory_ids") or []),
                "retrieved_count": 0,
                "matched_count": 0,
                "recall": 0.0,
                "precision": 0.0,
                "passed": False,
                "error_type": type(exc).__name__,
            }
            per_query.append(score)
            failures.append({"query_id": query_id, "reason": "query_error", "error_type": type(exc).__name__})

    metrics = _aggregate(per_query)
    evaluation_status = "no_queries" if not queries else "pass" if not failures else "fail"
    status = evaluation_status if execute else "dry_run"
    if execute:
        ledger.insert_eval_run(
            {
                "run_id": effective_run_id,
                "status": evaluation_status,
                "project": project or "",
                "provider": provider or "",
                "k": max_k,
                "query_count": metrics["query_count"],
                "metrics": metrics,
                "failures": failures,
                "network_used": semantic_ranker is not None,
                "mutation_performed": True,
            }
        )
        retention = _prune_eval_history(
            ledger=ledger,
            project=project,
            provider=provider,
            retain_runs=retain_runs,
        )
    else:
        retention = _retention_disabled(retain_runs)

    return {
        "schema_version": EVAL_LOOP_SCHEMA_VERSION,
        "status": status,
        "evaluation_status": evaluation_status,
        "run_id": effective_run_id,
        "execute": bool(execute),
        "mutation_performed": bool(execute),
        "network_used": semantic_ranker is not None,
        "project": project or "",
        "provider": provider or "",
        "metrics": metrics,
        "failures": failures,
        "retention": retention,
    }

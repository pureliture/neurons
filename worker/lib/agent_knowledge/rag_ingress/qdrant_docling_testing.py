"""Reusable in-memory fake of the qdrant-client surface used by the mirror PoC.

This is test/local support code, not a production path. It exists so that more
than one test module (and local smoke scripts) can exercise
:class:`QdrantDoclingMirrorAdapter` without installing the optional
``qdrant-client`` dependency and without re-implementing a private stub per test
file.

It implements exactly the client methods the adapter calls -- ``collection_exists``,
``create_collection``, ``upsert``, ``retrieve``, ``query_points``, ``delete`` --
and accepts the dict-shaped points / filters / selectors that the adapter emits
when the real ``qdrant_client`` package is absent (see ``_point_struct``,
``_target_profile_filter``, ``_points_selector`` in ``qdrant_docling_mirror``).
The scoring is a cosine-on-normalized-vectors dot product so query ordering is
deterministic for parity tests; it is not a real ANN index.
"""

from __future__ import annotations

import math
from typing import Any


def _point_field(point: Any, key: str) -> Any:
    if isinstance(point, dict):
        return point.get(key)
    return getattr(point, key, None)


def _selector_ids(points_selector: Any) -> list[Any]:
    """Extract point ids from any selector shape the adapter may emit.

    The adapter passes a plain ``list`` of ids when ``qdrant_client`` is absent,
    or a ``models.PointIdsList(points=[...])`` when it is installed. A dict
    ``{"points": [...]}`` (PointIdsList serialised) is also accepted.
    """

    if points_selector is None:
        return []
    if isinstance(points_selector, (list, tuple, set)):
        return list(points_selector)
    if isinstance(points_selector, dict):
        return list(points_selector.get("points") or [])
    points = getattr(points_selector, "points", None)
    if points is not None:
        return list(points)
    return []


def _filter_conditions(query_filter: Any) -> list[dict[str, Any]]:
    if query_filter is None:
        return []
    if isinstance(query_filter, dict):
        must = query_filter.get("must") or []
    else:
        must = list(getattr(query_filter, "must", None) or [])
    conditions: list[dict[str, Any]] = []
    for raw in must:
        if isinstance(raw, dict):
            key = raw.get("key")
            match = raw.get("match") or {}
            value = match.get("value") if isinstance(match, dict) else getattr(match, "value", None)
        else:
            key = getattr(raw, "key", None)
            match = getattr(raw, "match", None)
            value = getattr(match, "value", None)
        if key is not None:
            conditions.append({"key": str(key), "value": value})
    return conditions


def _payload_matches(payload: dict[str, Any], conditions: list[dict[str, Any]]) -> bool:
    for condition in conditions:
        if payload.get(condition["key"]) != condition["value"]:
            return False
    return True


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return dot / (left_norm * right_norm)


class InMemoryQdrantClient:
    """Dict-backed fake of the qdrant-client methods the mirror adapter uses."""

    def __init__(self) -> None:
        # collection_name -> {point_id: {"vector": [...], "payload": {...}}}
        self._collections: dict[str, dict[Any, dict[str, Any]]] = {}

    # -- collection lifecycle -------------------------------------------------
    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self._collections

    def create_collection(self, collection_name: str, vectors_config: Any = None) -> None:
        self._collections.setdefault(collection_name, {})

    # -- writes ---------------------------------------------------------------
    def upsert(self, *, collection_name: str, points: list[Any]) -> dict[str, Any]:
        store = self._collections.setdefault(collection_name, {})
        for point in points:
            point_id = _point_field(point, "id")
            vector = _point_field(point, "vector") or []
            payload = _point_field(point, "payload") or {}
            store[point_id] = {"vector": list(vector), "payload": dict(payload)}
        return {"status": "completed"}

    def delete(self, *, collection_name: str, points_selector: Any) -> dict[str, Any]:
        store = self._collections.get(collection_name, {})
        removed = 0
        for point_id in _selector_ids(points_selector):
            if point_id in store:
                del store[point_id]
                removed += 1
        return {"status": "completed", "removed": removed}

    # -- reads ----------------------------------------------------------------
    def retrieve(
        self,
        *,
        collection_name: str,
        ids: list[Any],
        with_payload: bool = True,
        with_vectors: bool = False,
    ) -> list[dict[str, Any]]:
        store = self._collections.get(collection_name, {})
        results: list[dict[str, Any]] = []
        for point_id in ids:
            record = store.get(point_id)
            if record is None:
                continue
            point: dict[str, Any] = {"id": point_id}
            if with_payload:
                point["payload"] = dict(record["payload"])
            if with_vectors:
                point["vector"] = list(record["vector"])
            results.append(point)
        return results

    def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int,
        query_filter: Any = None,
    ) -> dict[str, Any]:
        store = self._collections.get(collection_name, {})
        conditions = _filter_conditions(query_filter)
        scored: list[dict[str, Any]] = []
        for point_id, record in store.items():
            if not _payload_matches(record["payload"], conditions):
                continue
            scored.append(
                {
                    "id": point_id,
                    "score": _cosine(list(query), record["vector"]),
                    "payload": dict(record["payload"]),
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return {"points": scored[: max(1, int(limit))]}

    # -- test introspection ---------------------------------------------------
    def point_count(self, collection_name: str) -> int:
        return len(self._collections.get(collection_name, {}))


__all__ = ["InMemoryQdrantClient"]

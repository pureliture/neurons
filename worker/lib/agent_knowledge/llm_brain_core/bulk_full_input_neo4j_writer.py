"""Trial-only real Neo4j shadow writer (raw driver, isolated DB only)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from neo4j import GraphDatabase

from .bulk_full_input import is_shadow_group
from .bulk_semantic import BulkSemanticWriteReport

_LABEL = "TrialShadowEntity"
_REL = "TRIAL_SHADOW_REL"
_LEGACY = "TrialLegacyMarker"


def _uid(group: str, chunk_hash: str, kind: str, name: str) -> str:
    raw = f"{group}|{chunk_hash}|{kind}|{name}".encode("utf-8")
    return "shadow_ent_" + hashlib.sha256(raw).hexdigest()[:24]


def _receipt(uid: str, chunk_hash: str, source_id: str) -> str:
    return "receipt:" + hashlib.sha256(
        f"{uid}|{chunk_hash}|{source_id}".encode("utf-8")
    ).hexdigest()[:16]


def _authority_meta(payload: Any) -> dict[str, str]:
    sources = payload.get("authority_sources")
    first: Any = {}
    if isinstance(sources, (list, tuple)) and sources and isinstance(sources[0], Mapping):
        first = sources[0]
    return {
        "entity_type": str(payload.get("entity_type") or ""),
        "source_hash": str(payload.get("source_hash") or ""),
        "source_revision": str(payload.get("source_revision") or ""),
        "project": str(payload.get("project") or ""),
        "authority_memory_id": str(
            payload.get("authority_memory_id") or first.get("authority_memory_id") or ""
        ),
        "authority_content_hash": str(
            payload.get("content_hash") or first.get("content_hash") or ""
        ),
    }


def _endpoint_types(result: Any) -> dict[str, str]:
    names = set()
    for relation in result.relations:
        names.add(str(relation.source))
        names.add(str(relation.target))
    types: dict[str, str] = {}
    for name in names:
        matches = {str(entity.type) for entity in result.entities if str(entity.name) == name}
        if len(matches) > 1:
            raise RuntimeError(f"ambiguous relation endpoint: {name}")
        types[name] = next(iter(matches)) if matches else "Concept"
    return types


class Neo4jShadowWriter:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def write_batch(
        self, inputs: list[Any], extraction: Any, *, allow_empty_sessions: bool = False
    ) -> BulkSemanticWriteReport:
        by_key = extraction.by_session_key()
        entities = relations = 0
        chunk_receipts: dict[str, str] = {}
        for item in inputs:
            payload = item.episode.payload
            group = str(payload.get("graph_group_id") or "")
            if not group or not is_shadow_group(group):
                raise RuntimeError("shadow graph_group_id contract missing")
            if int(payload.get("fence_token") or 0) <= 0:
                raise RuntimeError("fence_token contract missing")
        with self._driver.session() as session:
            with session.begin_transaction() as tx:
                for item in inputs:
                    result = by_key[item.session_key]
                    payload = item.episode.payload
                    meta = _authority_meta(payload)
                    group = str(payload.get("graph_group_id") or "")
                    chunk_hash = str(payload.get("canonical_chunk_hash") or "")
                    source_id = str(item.episode.natural_id)
                    token = int(payload.get("fence_token") or 0)
                    fence_uid = _uid(group, chunk_hash, "fence", source_id)
                    row = tx.run(
                        "MERGE (f:TrialFence {uid: $uid}) "
                        "ON CREATE SET f.fence_token = $token "
                        "ON MATCH SET f.fence_token = CASE "
                        "WHEN f.fence_token < $token THEN $token ELSE f.fence_token END "
                        "RETURN f.fence_token AS token",
                        uid=fence_uid, token=token,
                    ).single()
                    if row is None or int(row["token"]) != token:
                        raise RuntimeError("graph fence rejected stale writer token")
                    for entity in result.entities:
                        uid = _uid(group, chunk_hash, entity.type, entity.name)
                        receipt = _receipt(uid, chunk_hash, source_id)
                        tx.run(
                            f"MERGE (n:{_LABEL} {{uid: $uid}}) "
                            "SET n.group_id = $group, n.source_id = $source_id, "
                            "n.chunk_key = $chunk_key, n.canonical_chunk_hash = $hash, "
                            "n.entity_name = $name, n.receipt = $receipt, "
                            "n.entity_type = $entity_type, n.project = $project, "
                            "n.source_hash = $source_hash, n.source_revision = $source_revision, "
                            "n.authority_memory_id = $authority_memory_id, "
                            "n.authority_content_hash = $authority_content_hash",
                            uid=uid, group=group, source_id=source_id,
                            chunk_key=item.session_key, hash=chunk_hash,
                            name=entity.name, receipt=receipt,
                            entity_type=meta["entity_type"] or str(entity.type),
                            project=meta["project"], source_hash=meta["source_hash"],
                            source_revision=meta["source_revision"],
                            authority_memory_id=meta["authority_memory_id"],
                            authority_content_hash=meta["authority_content_hash"],
                        )
                        entities += 1
                        chunk_receipts.setdefault(item.session_key, receipt)
                    endpoint_types = _endpoint_types(result)
                    for relation in result.relations:
                        src_name = str(relation.source)
                        dst_name = str(relation.target)
                        src_type = endpoint_types[src_name]
                        dst_type = endpoint_types[dst_name]
                        src = _uid(group, chunk_hash, src_type, src_name)
                        dst = _uid(group, chunk_hash, dst_type, dst_name)
                        rel_type = str(getattr(relation, "type", "") or "")
                        rel_uid = "shadow_rel_" + hashlib.sha256(
                            f"{group}|{chunk_hash}|{src_type}|{src_name}|"
                            f"{dst_type}|{dst_name}|{rel_type}|{relation.fact}".encode("utf-8")
                        ).hexdigest()[:16]
                        receipt = "receipt:" + hashlib.sha256(
                            f"{rel_uid}|{chunk_hash}|{source_id}".encode("utf-8")
                        ).hexdigest()[:16]
                        tx.run(
                            f"MERGE (a:{_LABEL} {{uid: $src}}) "
                            f"MERGE (b:{_LABEL} {{uid: $dst}}) "
                            f"MERGE (a)-[r:{_REL} {{uid: $rel_uid}}]->(b) "
                            "SET a.group_id = $group, a.chunk_key = $chunk_key, "
                            "a.entity_type = $src_type, a.entity_name = $src_name, "
                            "a.canonical_chunk_hash = $hash, a.receipt = $src_receipt, "
                            "b.group_id = $group, b.chunk_key = $chunk_key, "
                            "b.entity_type = $dst_type, b.entity_name = $dst_name, "
                            "b.canonical_chunk_hash = $hash, b.receipt = $dst_receipt, "
                            "r.group_id = $group, r.chunk_key = $chunk_key, "
                            "r.fact = $fact, r.relation_type = $relation_type, "
                            "r.canonical_chunk_hash = $hash, r.receipt = $receipt, "
                            "r.source_hash = $source_hash, r.source_revision = $source_revision, "
                            "r.authority_memory_id = $authority_memory_id, "
                            "r.authority_content_hash = $authority_content_hash",
                            src=src, dst=dst, rel_uid=rel_uid,
                            group=group, chunk_key=item.session_key,
                            src_type=src_type, dst_type=dst_type,
                            src_name=src_name, dst_name=dst_name,
                            src_receipt=_receipt(src, chunk_hash, source_id),
                            dst_receipt=_receipt(dst, chunk_hash, source_id),
                            hash=chunk_hash, receipt=receipt,
                            fact=str(relation.fact), relation_type=rel_type,
                            source_hash=meta["source_hash"],
                            source_revision=meta["source_revision"],
                            authority_memory_id=meta["authority_memory_id"],
                            authority_content_hash=meta["authority_content_hash"],
                        )
                        chunk_receipts.setdefault(item.session_key, receipt)
                        relations += 1
        return BulkSemanticWriteReport(
            projected=len(inputs),
            entities_written=entities,
            relations_written=relations,
            chunk_receipts=chunk_receipts,
            projected_natural_ids=tuple(item.episode.natural_id for item in inputs),
        )

    def verify_receipt(self, *, chunk_key: str, expected_hash: str) -> bool:
        with self._driver.session() as session:
            nodes = list(session.run(
                f"MATCH (n:{_LABEL} {{chunk_key: $k}}) "
                "RETURN n.canonical_chunk_hash AS h, n.receipt AS r",
                k=chunk_key,
            ))
            relations = list(session.run(
                f"MATCH ()-[r:{_REL} {{chunk_key: $k}}]->() "
                "RETURN r.canonical_chunk_hash AS h, r.receipt AS r",
                k=chunk_key,
            ))
        return (
            bool(nodes)
            and all(row["h"] == expected_hash and bool(row["r"]) for row in nodes)
            and all(row["h"] == expected_hash and bool(row["r"]) for row in relations)
        )

    def count_nodes(self, *, chunk_key: str) -> int:
        with self._driver.session() as session:
            row = session.run(
                f"MATCH (n:{_LABEL} {{chunk_key: $k}}) RETURN count(n) AS c", k=chunk_key
            ).single()
        return int(row["c"])

    def count_relations(self, *, chunk_key: str) -> int:
        with self._driver.session() as session:
            row = session.run(
                f"MATCH ()-[r:{_REL} {{chunk_key: $k}}]->() RETURN count(r) AS c",
                k=chunk_key,
            ).single()
        return int(row["c"])

    def set_legacy_marker(self, *, uid: str, marker: str) -> None:
        with self._driver.session() as session:
            session.run(
                f"MERGE (n:{_LEGACY} {{uid: $uid}}) SET n.marker = $marker",
                uid=uid, marker=marker,
            )

    def legacy_marker(self, *, uid: str) -> str:
        with self._driver.session() as session:
            row = session.run(
                f"MATCH (n:{_LEGACY} {{uid: $uid}}) RETURN n.marker AS m", uid=uid
            ).single()
        return str(row["m"]) if row else ""

    def delete_synthetic(self, *, chunk_key: str) -> None:
        with self._driver.session() as session:
            session.run(f"MATCH (n:{_LABEL} {{chunk_key: $k}}) DETACH DELETE n", k=chunk_key)
            session.run(f"MATCH ()-[r:{_REL} {{chunk_key: $k}}]->() DELETE r", k=chunk_key)

    def delete_legacy_marker(self, *, uid: str) -> None:
        with self._driver.session() as session:
            session.run(f"MATCH (n:{_LEGACY} {{uid: $uid}}) DELETE n", uid=uid)

    def close(self) -> None:
        self._driver.close()

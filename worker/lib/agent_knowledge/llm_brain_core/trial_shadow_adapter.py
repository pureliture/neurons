"""Trial-only read adapter: real Neo4j TrialShadowEntity -> resolver episodes.

Inert unless eligible shadow group IDs are explicitly supplied. Never registered
in production wiring; it exists so the real resolver/PG/MCP read path can be
exercised with isolated synthetic graph data.
"""

from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

from .models import GraphMemoryResult, OntologyEpisode


class TrialShadowGraphAdapter:
    """Read-only adapter over the trial shadow graph labels."""

    def __init__(self, uri: str, user: str, password: str, *,
                 eligible_shadow_groups: Any = ()) -> None:
        self._uri = str(uri)
        self._user = str(user)
        self._password = str(password)
        self._eligible = tuple(sorted({str(group) for group in eligible_shadow_groups}))

    def upsert_episode(self, episode: OntologyEpisode) -> str:
        return "skipped_disabled"

    def get_episodes_by_ids(self, episode_ids: Any, *, brain_id: str = "",
                            entity_types: Any = None) -> tuple[OntologyEpisode, ...]:
        return ()

    def search_context(self, *, brain_id: str, query: str, entity_types: Any = None,
                       limit: int = 10, eligible_shadow_groups: Any = None) -> GraphMemoryResult:
        groups = tuple(sorted({str(group) for group in (
            self._eligible if eligible_shadow_groups is None else eligible_shadow_groups
        )}))
        if not groups:
            return GraphMemoryResult(
                status="available", episodes=(), details=("shadow_groups_not_eligible",)
            )
        project = brain_id.removeprefix("/project/") if brain_id.startswith("/project/") else ""
        driver = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
        try:
            with driver.session() as session:
                rows = list(session.run(
                    "MATCH (n:TrialShadowEntity) "
                    "WHERE n.group_id IN $groups "
                    "AND n.authority_memory_id IS NOT NULL AND n.authority_memory_id <> '' "
                    "AND n.authority_content_hash IS NOT NULL AND n.authority_content_hash <> '' "
                    "AND ($project = '' OR n.project = $project) "
                    "RETURN n.uid AS uid, n.group_id AS group_id, "
                    "n.source_id AS source_id, n.entity_name AS entity_name, "
                    "n.entity_type AS entity_type, n.project AS project, "
                    "n.source_hash AS source_hash, n.source_revision AS source_revision, "
                    "n.authority_memory_id AS authority_memory_id, "
                    "n.authority_content_hash AS authority_content_hash, "
                    "n.chunk_key AS chunk_key, n.receipt AS receipt "
                    "ORDER BY n.uid LIMIT $limit",
                    groups=list(groups), project=project, limit=max(1, min(int(limit), 100)),
                ))
        finally:
            driver.close()
        episodes: list[OntologyEpisode] = []
        for row in rows:
            entity_type = str(row.get("entity_type") or "Concept")
            source_entity_types = [entity_type]
            sources = [{
                "authority_memory_id": str(row.get("authority_memory_id") or ""),
                "content_hash": str(row.get("authority_content_hash") or ""),
                "source_entity_types": source_entity_types,
                "source_revision": str(row.get("source_revision") or ""),
                "source_hash": str(row.get("source_hash") or ""),
            }]
            payload = {
                "authority_sources": sources,
                "authority_memory_id": str(row.get("authority_memory_id") or ""),
                "content_hash": str(row.get("authority_content_hash") or ""),
                "project": str(row.get("project") or ""),
                "entity_name": str(row.get("entity_name") or ""),
                "shadow_group": str(row.get("group_id") or ""),
                "chunk_key": str(row.get("chunk_key") or ""),
                "receipt": str(row.get("receipt") or ""),
                "source_revision": str(row.get("source_revision") or ""),
                "source_hash": str(row.get("source_hash") or ""),
                "source_entity_types": source_entity_types,
            }
            episodes.append(OntologyEpisode.from_payload(
                event_id="shadow:" + str(row.get("uid") or ""),
                entity_type=entity_type,
                natural_id=str(row.get("source_id") or row.get("uid") or ""),
                payload=payload,
            ))
        return GraphMemoryResult(status="available", episodes=tuple(episodes),
                                 details=("shadow_graph_read",))

    def read_relation_facts(self, *, limit: int = 100) -> tuple[dict[str, str], ...]:
        if not self._eligible:
            return ()
        driver = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
        try:
            with driver.session() as session:
                rows = list(session.run(
                    "MATCH (a:TrialShadowEntity)-[r:TRIAL_SHADOW_REL]->(b:TrialShadowEntity) "
                    "WHERE r.group_id IN $groups "
                    "RETURN r.uid AS uid, r.group_id AS group_id, "
                    "r.fact AS fact, r.relation_type AS relation_type, "
                    "r.source_revision AS source_revision, r.source_hash AS source_hash "
                    "ORDER BY r.uid LIMIT $limit",
                    groups=list(self._eligible), limit=max(1, min(int(limit), 100)),
                ))
        finally:
            driver.close()
        return tuple({
            "uid": str(row.get("uid") or ""),
            "group_id": str(row.get("group_id") or ""),
            "fact": str(row.get("fact") or ""),
            "relation_type": str(row.get("relation_type") or ""),
            "source_revision": str(row.get("source_revision") or ""),
            "source_hash": str(row.get("source_hash") or ""),
        } for row in rows)

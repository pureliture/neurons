"""Real Neo4j test against a disposable isolated local container only.

Uses raw neo4j driver in a test (not the DDL-capable production Neo4jDriver
wrapper). Skipped unless TRIAL_NEO4J_URI/TRIAL_NEO4J_PASSWORD are provided.
Synthetic nodes only; no production data, no data bodies, no DSN output.
"""

from __future__ import annotations

import os

import pytest
from neo4j import GraphDatabase

from agent_knowledge.llm_brain_core.bulk_full_input import (
    is_shadow_group,
    shadow_group_ids,
)
from agent_knowledge.llm_brain_core.graph_scope import graph_group_ids

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TRIAL_NEO4J_URI") and os.environ.get("TRIAL_NEO4J_PASSWORD")),
    reason="no isolated local Neo4j test database provided",
)

SCOPE = "/project/synthetic-project"
POLICY = "full-input.v1"
LABEL = "SyntheticShadowChunk"
LEGACY_LABEL = "SyntheticLegacyNode"


def test_real_neo4j_shadow_ids_are_idempotent_and_leave_legacy_untouched():
    driver = GraphDatabase.driver(
        os.environ["TRIAL_NEO4J_URI"],
        auth=("neo4j", os.environ["TRIAL_NEO4J_PASSWORD"]),
    )
    shadow = shadow_group_ids(SCOPE, POLICY)
    assert shadow and is_shadow_group(shadow[0])
    assert not set(shadow) & set(graph_group_ids(SCOPE))
    try:
        with driver.session() as session:
            session.run(
                f"MERGE (n:{LEGACY_LABEL} {{uid: $uid}}) SET n.marker = $marker",
                uid="synthetic-legacy", marker="legacy-original",
            )
            params = {
                "uid": "synthetic-shadow-chunk",
                "group": shadow[0],
                "hash": "sha256:" + "c" * 64,
            }
            for _ in range(2):
                session.run(
                    f"MERGE (n:{LABEL} {{uid: $uid}}) "
                    "SET n.group_id = $group, n.canonical_chunk_hash = $hash",
                    **params,
                )
            count = session.run(
                f"MATCH (n:{LABEL} {{uid: $uid}}) RETURN count(n) AS c", uid=params["uid"]
            ).single()["c"]
            assert count == 1
            shadow_marker = session.run(
                f"MATCH (n:{LABEL} {{uid: $uid}}) RETURN n.group_id AS g",
                uid=params["uid"],
            ).single()["g"]
            assert shadow_marker == shadow[0]
            legacy_marker = session.run(
                f"MATCH (n:{LEGACY_LABEL} {{uid: $uid}}) RETURN n.marker AS m",
                uid="synthetic-legacy",
            ).single()["m"]
            assert legacy_marker == "legacy-original"
    finally:
        with driver.session() as session:
            session.run(f"MATCH (n:{LABEL}) WHERE n.uid STARTS WITH 'synthetic-' DETACH DELETE n")
            session.run(f"MATCH (n:{LEGACY_LABEL}) WHERE n.uid STARTS WITH 'synthetic-' DETACH DELETE n")
        driver.close()

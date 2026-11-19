"""Contract: relation endpoints use extracted entity types; receipts are verified."""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from neo4j import GraphDatabase

from agent_knowledge.llm_brain_core.bulk_full_input_neo4j_writer import Neo4jShadowWriter
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticRelation,
    BulkSemanticSessionInput,
    BulkSemanticSessionResult,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode

URI = os.environ.get("TRIAL_NEO4J_URI", "")
PASSWORD = os.environ.get("TRIAL_NEO4J_PASSWORD", "")
pytestmark = pytest.mark.skipif(not (URI and PASSWORD), reason="isolated Neo4j required")


def _item(group: str, chunk_hash: str, key: str):
    payload = {
        "graph_group_id": group,
        "canonical_chunk_hash": chunk_hash,
        "fence_token": 1,
        "project": "relation-contract",
        "source_hash": "sha256:" + "a" * 64,
        "source_revision": "rev-1",
    }
    episode = OntologyEpisode.from_payload(
        event_id="ev-" + key, natural_id="nat-" + key,
        entity_type="Session", payload=payload,
    )
    return BulkSemanticSessionInput(session_key=key, episode=episode, text="synthetic")


def _extraction(key: str, entities, relations):
    return BulkSemanticExtractionResult((
        BulkSemanticSessionResult(session_key=key, entities=entities, relations=relations),
    ))


def _writer():
    return Neo4jShadowWriter(URI, "neo4j", PASSWORD)


def test_ambiguous_endpoint_name_fails_closed():
    group = "shadow_" + uuid.uuid4().hex[:12]
    chunk_hash = "sha256:" + "b" * 64
    key = "amb-" + uuid.uuid4().hex
    entities = (
        BulkSemanticEntity(name="Alice", type="Person", summary="synthetic"),
        BulkSemanticEntity(name="Alice", type="Concept", summary="synthetic"),
        BulkSemanticEntity(name="Target", type="Concept", summary="synthetic"),
    )
    relations = (BulkSemanticRelation(source="Alice", target="Target",
                                      type="supports", fact="ambiguous fact"),)
    writer = _writer()
    try:
        with pytest.raises(RuntimeError, match="ambiguous relation endpoint"):
            writer.write_batch([_item(group, chunk_hash, key)],
                               _extraction(key, entities, relations))
    finally:
        writer.close()


def test_typed_endpoints_idempotency_fact_and_verified_receipt():
    group = "shadow_" + uuid.uuid4().hex[:12]
    chunk_hash = "sha256:" + hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    key = "rel-" + uuid.uuid4().hex
    entities = (
        BulkSemanticEntity(name="Alice", type="Person", summary="synthetic"),
        BulkSemanticEntity(name="Target", type="Concept", summary="synthetic"),
    )
    relations = (BulkSemanticRelation(source="Alice", target="Target",
                                      type="supports", fact="typed fact"),)
    writer = _writer()
    try:
        report = writer.write_batch([_item(group, chunk_hash, key)],
                                    _extraction(key, entities, relations))
        assert report.relations_written == 1
        assert report.chunk_receipts.get(key)
        with GraphDatabase.driver(URI, auth=("neo4j", PASSWORD)) as driver:
            with driver.session() as session:
                rows = list(session.run(
                    "MATCH (a:TrialShadowEntity)-[r:TRIAL_SHADOW_REL]->(b:TrialShadowEntity) "
                    "WHERE r.chunk_key = $k RETURN a.entity_type AS src_type, "
                    "b.entity_type AS dst_type, r.fact AS fact, r.relation_type AS rel_type, "
                    "r.receipt AS receipt, r.canonical_chunk_hash AS hash, r.uid AS uid",
                    k=key,
                ))
            assert len(rows) == 1, rows
            assert rows[0]["src_type"] == "Person", rows[0]
            assert rows[0]["dst_type"] == "Concept", rows[0]
            assert rows[0]["fact"] == "typed fact"
            assert rows[0]["rel_type"] == "supports"
            assert rows[0]["receipt"]
            assert rows[0]["hash"] == chunk_hash
        assert writer.verify_receipt(chunk_key=key, expected_hash=chunk_hash) is True
        assert writer.verify_receipt(chunk_key=key, expected_hash="sha256:" + "0" * 64) is False
        writer.write_batch([_item(group, chunk_hash, key)],
                           _extraction(key, entities, relations))
        with GraphDatabase.driver(URI, auth=("neo4j", PASSWORD)) as driver:
            with driver.session() as session:
                count = session.run(
                    "MATCH ()-[r:TRIAL_SHADOW_REL {chunk_key: $k}]->() RETURN count(r) AS c",
                    k=key,
                ).single()["c"]
            assert count == 1, count
    finally:
        writer.delete_synthetic(chunk_key=key)
        writer.close()

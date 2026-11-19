"""TDD: real isolated Neo4j shadow writer with graph-side receipts."""

from __future__ import annotations

import os
import uuid

import pytest

from agent_knowledge.llm_brain_core.bulk_full_input_neo4j_writer import Neo4jShadowWriter
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticRelation,
    BulkSemanticSessionResult,
    BulkSemanticSessionInput,
)
from agent_knowledge.llm_brain_core.models import OntologyEpisode

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TRIAL_NEO4J_URI") and os.environ.get("TRIAL_NEO4J_PASSWORD")),
    reason="no local Neo4j",
)
PREFIX = uuid.uuid4().hex[:8]

def _input(key: str, group: str, chunk_hash: str) -> BulkSemanticSessionInput:
    episode = OntologyEpisode.from_payload(
        event_id="evt_" + PREFIX,
        natural_id="nat_" + PREFIX,
        entity_type="Session",
        payload={"project": PREFIX, "graph_group_id": group,
                 "canonical_chunk_hash": chunk_hash, "fence_token": 1},
    )
    return BulkSemanticSessionInput(session_key=key, episode=episode, text="synthetic")


def _extraction(item, *, entity_name="SyntheticEntity", relation=False):
    relations = ()
    if relation:
        relations = (BulkSemanticRelation(source=entity_name, target="SyntheticTarget",
                                          type="synthetic_rel", fact="synthetic fact"),)
    return BulkSemanticExtractionResult((
        BulkSemanticSessionResult(
            session_key=item.session_key,
            entities=(BulkSemanticEntity(name=entity_name, type="Concept", summary="synthetic"),),
            relations=relations,
        ),
    ))


def _writer():
    return Neo4jShadowWriter(
        os.environ["TRIAL_NEO4J_URI"], "neo4j", os.environ["TRIAL_NEO4J_PASSWORD"]
    )


def test_shadow_write_is_idempotent_and_receipt_verified():
    writer = _writer()
    group = "shadow_" + PREFIX
    chunk_hash = "sha256:" + "a" * 64
    item = _input(PREFIX + "-chunk", group, chunk_hash)
    try:
        for _ in range(2):
            report = writer.write_batch([item], _extraction(item))
            assert report.projected == 1
            assert report.entities_written == 1
        assert writer.verify_receipt(chunk_key=item.session_key, expected_hash=chunk_hash)
        count = writer.count_nodes(chunk_key=item.session_key)
        assert count == 1
    finally:
        writer.delete_synthetic(chunk_key=item.session_key)
        writer.close()


def test_legacy_node_marker_unchanged_and_distinct_hash_creates_distinct_node():
    writer = _writer()
    group = "shadow_" + PREFIX
    h1, h2 = "sha256:" + "b" * 64, "sha256:" + "c" * 64
    k1, k2 = PREFIX + "-k1", PREFIX + "-k2"
    writer.set_legacy_marker(uid=PREFIX + "-legacy", marker="legacy-original")
    try:
        i1, i2 = _input(k1, group, h1), _input(k2, group, h2)
        writer.write_batch([i1], _extraction(i1))
        writer.write_batch([i2], _extraction(i2))
        assert writer.count_nodes(chunk_key=k1) == 1
        assert writer.count_nodes(chunk_key=k2) == 1
        assert writer.legacy_marker(uid=PREFIX + "-legacy") == "legacy-original"
    finally:
        writer.delete_synthetic(chunk_key=k1)
        writer.delete_synthetic(chunk_key=k2)
        writer.delete_legacy_marker(uid=PREFIX + "-legacy")
        writer.close()


def test_relation_edge_written_idempotently():
    writer = _writer()
    group = "shadow_" + PREFIX
    h = "sha256:" + "d" * 64
    item = _input(PREFIX + "-rel", group, h)
    try:
        for _ in range(2):
            report = writer.write_batch([item], _extraction(item, relation=True))
            assert report.relations_written == 1
        assert writer.count_relations(chunk_key=item.session_key) == 1
    finally:
        writer.delete_synthetic(chunk_key=item.session_key)
        writer.close()

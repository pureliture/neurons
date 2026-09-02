from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.ledger import Ledger
from agent_knowledge.llm_brain_core.objects.object_packs import (
    _approval_board_item,
    _candidate_graph_hash,
    _object_edge_refs,
    _object_evidence_refs,
)
from agent_knowledge.llm_brain_core.slim_serializer import SlimSerializer
from agent_knowledge.mcp_jsonrpc import (
    handle_jsonrpc_message,
)
from agent_knowledge.session_memory.memory_card import (
    SHA256_HEX_PATTERN,
    _safe_evidence_refs,
    _validate_hash_list,
    build_memory_candidate,
    validate_content_hash,
    validate_judgment_basis_bundle,
    validate_memory_card_envelope,
    validate_typed_payload,
)
from agent_knowledge.session_memory.memory_miner import (
    build_memory_card_candidate_from_source_span,
)
from e2e.conftest import (
    InMemoryPostgresStore,
    MemoryCard,
    MemoryEdge,
    MockMCPServer,
    sha256_str,
)


def _create_real_service(tmp_path: Path) -> KnowledgeSearchService:
    private = tmp_path / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    ledger = Ledger(private / "challenger_m2_ledger.sqlite")
    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        allow_private_results=True,
    )


# =============================================================================
# Dimension 1: Provenance Hash Integrity (^sha256:[0-9a-f]{64}$)
# =============================================================================

class TestProvenanceHashIntegrity:
    """Adversarial stress-testing of hash regex verification across all models."""

    @pytest.mark.parametrize("valid_hex", [
        "0" * 64,
        "f" * 64,
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        hashlib.sha256(b"canonical_payload_test_data").hexdigest(),
        hashlib.sha256(b"").hexdigest(),
    ])
    def test_valid_sha256_hashes_accepted(self, valid_hex: str):
        valid_hash = f"sha256:{valid_hex}"
        assert SHA256_HEX_PATTERN.fullmatch(valid_hash) is not None
        assert validate_content_hash(valid_hash) == valid_hash

        # Validate in hash list
        _validate_hash_list([valid_hash, valid_hash], "test_field")

    @pytest.mark.parametrize("uppercase_hash", [
        "sha256:" + "A" * 64,
        "sha256:" + "F" * 64,
        "sha256:0123456789ABCDEF0123456789abcdef0123456789abcdef0123456789abcdef",
        "sha256:0123456789abcdef0123456789ABCDEF0123456789abcdef0123456789abcdef",
        "SHA256:" + "a" * 64,
        "Sha256:" + "a" * 64,
    ])
    def test_uppercase_hex_strictly_rejected(self, uppercase_hash: str):
        """Uppercase hex or uppercase prefix MUST be rejected to enforce canonical hashing."""
        assert SHA256_HEX_PATTERN.fullmatch(uppercase_hash) is None
        with pytest.raises(ValueError, match="sha256"):
            validate_content_hash(uppercase_hash)

        with pytest.raises(ValueError, match="sha256"):
            _validate_hash_list([uppercase_hash], "evidence_hashes")

    @pytest.mark.parametrize("non_hex_hash", [
        "sha256:" + "g" * 64,
        "sha256:" + "z" * 64,
        "sha256:" + "0123456789abcdef" * 3 + "xyz0123456789abc",
        "sha256:" + "0123456789abcdef" * 3 + "!@#$%^&*()_+1234",
        "sha256:--not-a-valid-hex-digest-value-at-all-strictly-forbidden-content--",
        "sha256:" + " " * 64,
        "sha256:" + "0" * 32 + " " + "0" * 31,
    ])
    def test_non_hex_characters_strictly_rejected(self, non_hex_hash: str):
        assert SHA256_HEX_PATTERN.fullmatch(non_hex_hash) is None
        with pytest.raises(ValueError, match="sha256"):
            validate_content_hash(non_hex_hash)

        with pytest.raises(ValueError, match="sha256"):
            _validate_hash_list([non_hex_hash], "evidence_hashes")

    @pytest.mark.parametrize("wrong_length_hash", [
        "sha256:",                                    # 0 chars (prefix only)
        "sha256:" + "a" * 1,                          # 1 char
        "sha256:" + "a" * 16,                         # 16 chars
        "sha256:" + "a" * 32,                         # 32 chars (MD5 length)
        "sha256:" + "a" * 63,                         # 63 chars (off-by-one under)
        "sha256:" + "a" * 65,                         # 65 chars (off-by-one over)
        "sha256:" + "a" * 128,                        # 128 chars (SHA-512 length)
        "sha256:" + "a" * 256,                        # 256 chars
    ])
    def test_hash_length_mismatches_strictly_rejected(self, wrong_length_hash: str):
        assert SHA256_HEX_PATTERN.fullmatch(wrong_length_hash) is None
        with pytest.raises(ValueError, match="sha256"):
            validate_content_hash(wrong_length_hash)

        with pytest.raises(ValueError, match="sha256"):
            _validate_hash_list([wrong_length_hash], "evidence_hashes")

    @pytest.mark.parametrize("bad_prefix_or_type", [
        "md5:d41d8cd98f00b204e9800998ecf8427e",
        "sha512:" + "a" * 128,
        "sha1:" + "a" * 40,
        "a" * 64,                                     # raw 64 hex without prefix
        "",                                            # empty string
        "   ",                                         # whitespace only
        "sha256",                                      # missing colon
        "sha256;0" * 64,                              # semicolon
        None,                                          # None type
        1234567890,                                    # int
        b"sha256:" + b"0" * 64,                        # raw bytes
    ])
    def test_bad_prefixes_and_types_strictly_rejected(self, bad_prefix_or_type: Any):
        if isinstance(bad_prefix_or_type, str):
            assert SHA256_HEX_PATTERN.fullmatch(bad_prefix_or_type) is None
        with pytest.raises((ValueError, TypeError)):
            validate_content_hash(bad_prefix_or_type)

    @pytest.mark.parametrize("padded_hash", [
        " sha256:" + "a" * 64,
        "sha256:" + "a" * 64 + " ",
        "sha256:" + "a" * 64 + "\n",
        "\tsha256:" + "a" * 64,
        "sha256:\n" + "a" * 64,
        "sha256:" + "a" * 32 + "\n" + "a" * 32,
    ])
    def test_whitespace_and_newline_padding_strictly_rejected(self, padded_hash: str):
        assert SHA256_HEX_PATTERN.fullmatch(padded_hash) is None
        with pytest.raises(ValueError, match="sha256"):
            validate_content_hash(padded_hash)

    def test_memory_card_envelope_with_invalid_evidence_hashes_fails(self):
        """MemoryCard envelope validation fails if any evidence_hash is malformed."""
        valid_card = {
            "memory_id": "mem_test_hash_01",
            "brain_id": "brain_1",
            "card_type": "decision",
            "scope": "project",
            "project": "neurons",
            "provider": "codex",
            "title": "Valid Title",
            "summary": "Valid summary",
            "render_text": "Valid render",
            "lifecycle_state": "candidate",
            "judgment_state": "none",
            "status": "candidate",
            "approval_state": "suggested",
            "governance_tier": "medium",
            "freshness": "current",
            "currentness": "current",
            "confidence": 0.95,
            "confidence_basis": "deterministic",
            "source_refs": [],
            "evidence_refs": [],
            "evidence_hashes": ["sha256:" + "0" * 64, "malformed_hash_xyz"],
            "derived_from": [],
            "supersedes": [],
            "superseded_by": [],
            "conflicts": [],
            "active_until": None,
            "content_hash": "sha256:" + "1" * 64,
            "typed_payload": {
                "decision": "Valid decision",
                "rationale": "Valid rationale",
                "alternatives": [],
                "consequence": "none",
                "authority_ref": "RFC-1",
            },
        }
        with pytest.raises(ValueError, match="evidence_hashes entries must be sha256"):
            validate_memory_card_envelope(valid_card)

    def test_safe_evidence_refs_rejects_malformed_hashes(self):
        """_safe_evidence_refs rejects evidence refs with malformed content_hash."""
        bad_refs = [
            {"knowledge_id": "k1", "content_hash": "sha256:" + "a" * 64},
            {"knowledge_id": "k2", "content_hash": "invalid_hash_string"},
        ]
        with pytest.raises(ValueError, match="memory evidence refs require knowledge_id and sha256 content_hash"):
            _safe_evidence_refs(bad_refs)


# =============================================================================
# Dimension 2: Cyclic DAG Traversal and Recursion Guards
# =============================================================================

class TestCyclicDAGTraversal:
    """Stress-test cycle detection, visited path tracking, and recursion bounds."""

    def test_direct_2_node_cycle_prevention(self):
        """A -> B -> A terminates without infinite recursion and records complete visited paths."""
        store = InMemoryPostgresStore()
        store.insert_card(MemoryCard("A", "neurons", "decision", "A", "Summary A", {}))
        store.insert_card(MemoryCard("B", "neurons", "decision", "B", "Summary B", {}))
        store.insert_edge(MemoryEdge(1, "A", "derived_from", "B", "sha256:edge1"))
        store.insert_edge(MemoryEdge(2, "B", "derived_from", "A", "sha256:edge2"))

        tree = store.recursive_dag_traversal("A", max_depth=5)
        # Hops: A->B (depth 1, visited [A]), B->A (depth 2, visited [A, B]), then cycle guard prevents re-traversing A
        assert len(tree) == 2
        assert tree[0]["src_id"] == "A" and tree[0]["dst_id"] == "B" and tree[0]["depth"] == 1
        assert tree[1]["src_id"] == "B" and tree[1]["dst_id"] == "A" and tree[1]["depth"] == 2
        assert tree[1]["visited_path"] == ["A", "B"]

    def test_3_node_cycle_prevention(self):
        """A -> B -> C -> A terminates safely after 3 hops without infinite recursion."""
        store = InMemoryPostgresStore()
        for node in ("A", "B", "C"):
            store.insert_card(MemoryCard(node, "neurons", "decision", node, f"Summary {node}", {}))
        store.insert_edge(MemoryEdge(1, "A", "derived_from", "B", "sha256:e1"))
        store.insert_edge(MemoryEdge(2, "B", "derived_from", "C", "sha256:e2"))
        store.insert_edge(MemoryEdge(3, "C", "derived_from", "A", "sha256:e3"))

        tree = store.recursive_dag_traversal("A", max_depth=5)
        # Hops: A->B (depth 1), B->C (depth 2), C->A (depth 3), then cycle guard blocks re-entering A
        assert len(tree) == 3
        assert [t["dst_id"] for t in tree] == ["B", "C", "A"]
        assert [t["depth"] for t in tree] == [1, 2, 3]
        assert tree[2]["visited_path"] == ["A", "B", "C"]

    def test_self_referential_loop_prevention(self):
        """Self-referential edge A -> A executes exactly once and does not loop."""
        store = InMemoryPostgresStore()
        store.insert_card(MemoryCard("A", "neurons", "decision", "A", "Summary A", {}))
        store.insert_edge(MemoryEdge(1, "A", "derived_from", "A", "sha256:self"))

        tree = store.recursive_dag_traversal("A", max_depth=5)
        # Hops: A->A at depth 1 (visited [A]), next hop from A is blocked because next_e.src_id 'A' is in visited [A]
        assert len(tree) == 1
        assert tree[0]["src_id"] == "A"
        assert tree[0]["dst_id"] == "A"
        assert tree[0]["depth"] == 1
        assert tree[0]["visited_path"] == ["A"]

    def test_complex_multi_cyclic_graph(self):
        """Complex web with multiple overlapping cycles and cross-edges terminates deterministically."""
        store = InMemoryPostgresStore()
        for n in ("A", "B", "C", "D", "E"):
            store.insert_card(MemoryCard(n, "neurons", "decision", n, f"Summary {n}", {}))

        store.insert_edge(MemoryEdge(1, "A", "derived_from", "B", "sha256:1"))
        store.insert_edge(MemoryEdge(2, "B", "derived_from", "C", "sha256:2"))
        store.insert_edge(MemoryEdge(3, "C", "derived_from", "A", "sha256:3"))  # Cycle 1 back to A
        store.insert_edge(MemoryEdge(4, "B", "derived_from", "D", "sha256:4"))  # Branch from B
        store.insert_edge(MemoryEdge(5, "D", "derived_from", "E", "sha256:5"))
        store.insert_edge(MemoryEdge(6, "E", "derived_from", "B", "sha256:6"))  # Cycle 2 back to B
        store.insert_edge(MemoryEdge(7, "C", "derived_from", "D", "sha256:7"))  # Cross edge
        store.insert_edge(MemoryEdge(8, "E", "derived_from", "A", "sha256:8"))  # Cross edge to A

        tree = store.recursive_dag_traversal("A", max_depth=5)
        assert len(tree) > 0
        assert len(tree) <= 20  # Strict finite bound
        for t in tree:
            assert len(t["visited_path"]) == len(set(t["visited_path"])), f"Duplicate in visited path: {t['visited_path']}"
            assert t["depth"] <= 5

    def test_depth_ceiling_guard_strictly_bounds_at_depth_limit(self):
        """Deep linear chain N1 -> N2 -> ... -> N20 strictly stops at depth = 5."""
        store = InMemoryPostgresStore()
        for i in range(1, 21):
            store.insert_card(MemoryCard(f"N{i}", "neurons", "decision", f"N{i}", "S", {}))
        for i in range(1, 20):
            store.insert_edge(MemoryEdge(i, f"N{i}", "derived_from", f"N{i+1}", f"sha256:{i:064x}"))

        tree = store.recursive_dag_traversal("N1", max_depth=5)
        assert len(tree) == 5
        assert [t["depth"] for t in tree] == [1, 2, 3, 4, 5]
        assert tree[-1]["dst_id"] == "N6"

        # Test custom max_depth=3
        tree_3 = store.recursive_dag_traversal("N1", max_depth=3)
        assert len(tree_3) == 3
        assert [t["depth"] for t in tree_3] == [1, 2, 3]

    def test_temporal_filtering_with_cyclic_dag(self):
        """Temporal point-in-time filtering correctly disables expired edges in a cyclic graph."""
        store = InMemoryPostgresStore()
        store.insert_card(MemoryCard("A", "neurons", "decision", "A", "S", {}))
        store.insert_card(MemoryCard("B", "neurons", "decision", "B", "S", {}))

        t_active_from = datetime(2026, 3, 1, tzinfo=timezone.utc)
        t_active_to = datetime(2026, 6, 1, tzinfo=timezone.utc)

        # A -> B is valid March to June
        store.insert_edge(MemoryEdge(1, "A", "supersedes", "B", "sha256:1", valid_from=t_active_from, valid_to=t_active_to))
        # B -> A is valid June to September
        store.insert_edge(MemoryEdge(2, "B", "supersedes", "A", "sha256:2", valid_from=t_active_to, valid_to=datetime(2026, 9, 1, tzinfo=timezone.utc)))

        # Query in April 2026: only A -> B is active
        tree_april = store.recursive_dag_traversal("A", as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
        assert len(tree_april) == 1
        assert tree_april[0]["src_id"] == "A" and tree_april[0]["dst_id"] == "B"

        # Query in July 2026 from A: A -> B is inactive, so 0 edges
        tree_july = store.recursive_dag_traversal("A", as_of=datetime(2026, 7, 1, tzinfo=timezone.utc))
        assert len(tree_july) == 0


# =============================================================================
# Dimension 3: Multi-Hop DAG Edge Ordering & Property Serialization
# =============================================================================

class TestMultiHopDAGEdgeOrderingAndSerialization:
    """Stress-test DAG relationship classification, ordering, and serializer properties."""

    def test_multi_hop_ancestor_chain_ordering(self):
        """Ancestry chain traversal maintains strict monotonically increasing depth order."""
        store = InMemoryPostgresStore()
        for i in range(1, 6):
            store.insert_card(MemoryCard(f"ancestor_{i}", "neurons", "decision", f"A{i}", "S", {}))
        for i in range(1, 5):
            store.insert_edge(MemoryEdge(i, f"ancestor_{i}", "derived_from", f"ancestor_{i+1}", f"sha256:{i:064x}"))

        tree = store.recursive_dag_traversal("ancestor_1", max_depth=5)
        depths = [t["depth"] for t in tree]
        assert depths == [1, 2, 3, 4]
        assert [t["src_id"] for t in tree] == ["ancestor_1", "ancestor_2", "ancestor_3", "ancestor_4"]
        assert [t["dst_id"] for t in tree] == ["ancestor_2", "ancestor_3", "ancestor_4", "ancestor_5"]

    def test_diamond_dag_convergence_ordering(self):
        """Diamond DAG (A -> B -> D, A -> C -> D) traverses both branches with deterministic convergence."""
        store = InMemoryPostgresStore()
        for node in ("A", "B", "C", "D"):
            store.insert_card(MemoryCard(node, "neurons", "decision", node, f"Summary {node}", {}))

        store.insert_edge(MemoryEdge(1, "A", "derived_from", "B", "sha256:1"))
        store.insert_edge(MemoryEdge(2, "A", "derived_from", "C", "sha256:2"))
        store.insert_edge(MemoryEdge(3, "B", "derived_from", "D", "sha256:3"))
        store.insert_edge(MemoryEdge(4, "C", "derived_from", "D", "sha256:4"))

        tree = store.recursive_dag_traversal("A", max_depth=5)
        assert len(tree) == 4
        assert [t["depth"] for t in tree] == [1, 1, 2, 2]
        d_nodes = [t for t in tree if t["dst_id"] == "D"]
        assert len(d_nodes) == 2
        assert {t["src_id"] for t in d_nodes} == {"B", "C"}

    def test_all_relationship_types_serialization_in_with_evidence(self):
        """SlimSerializer.serialize_with_evidence accurately preserves all DAG relationship types."""
        edges = [
            {"src_id": "card_0", "rel_type": "supersedes", "dst_id": "card_1", "provenance_hash": "sha256:" + "1" * 64},
            {"src_id": "card_0", "rel_type": "derived_from", "dst_id": "card_2", "provenance_hash": "sha256:" + "2" * 64},
            {"src_id": "card_0", "rel_type": "contradicts", "dst_id": "card_3", "provenance_hash": "sha256:" + "3" * 64},
            {"src_id": "card_0", "rel_type": "supports", "dst_id": "card_4", "provenance_hash": "sha256:" + "4" * 64},
        ]
        evidence_hashes = ["sha256:" + f"{i}" * 64 for i in range(1, 5)]

        payload = SlimSerializer.serialize_with_evidence(
            project="neurons",
            decisions=[{"id": "card_0", "title": "Decision 0", "typed_payload": {"decision": "Root"}}],
            preferences=[],
            guardrails=["guard_1"],
            edges=edges,
            evidence_hashes=evidence_hashes,
            source_refs=[{"locator": "docs/adr-0003.md", "span": "1-10"}],
        )

        assert payload["schema_version"] == "lbrain_evidence_context.v1"
        assert len(payload["edges"]) == 4
        rel_types = [e["rel_type"] for e in payload["edges"]]
        assert rel_types == ["supersedes", "derived_from", "contradicts", "supports"]

        for idx, edge in enumerate(payload["edges"]):
            assert edge["src_id"] == "card_0"
            assert edge["dst_id"] == f"card_{idx+1}"
            assert edge["provenance_hash"] == "sha256:" + f"{idx+1}" * 64

    def test_evidence_hashes_deterministic_dedup_preserving_order(self):
        """evidence_hashes deduplicates repeated hashes while strictly preserving first-seen order."""
        h1 = "sha256:" + "1" * 64
        h2 = "sha256:" + "2" * 64
        h3 = "sha256:" + "3" * 64
        input_hashes = [h1, h2, h1, h3, h2, h3, h1]

        payload = SlimSerializer.serialize_with_evidence(
            project="neurons",
            decisions=[],
            preferences=[],
            guardrails=[],
            edges=[],
            evidence_hashes=input_hashes,
            source_refs=[],
        )
        assert payload["evidence_hashes"] == [h1, h2, h3]

    def test_cyclic_edge_structure_serialized_without_infinite_recursion(self):
        """Passing cyclic edge data into serialize_with_evidence executes safely in sub-millisecond time."""
        cyclic_edges = [
            {"src_id": "A", "rel_type": "supersedes", "dst_id": "B", "provenance_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111"},
            {"src_id": "B", "rel_type": "supersedes", "dst_id": "A", "provenance_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222"},
            {"src_id": "A", "rel_type": "supports", "dst_id": "A", "provenance_hash": "sha256:3333333333333333333333333333333333333333333333333333333333333333"},
        ]
        payload = SlimSerializer.serialize_with_evidence(
            project="neurons",
            decisions=[],
            preferences=[],
            guardrails=[],
            edges=cyclic_edges,
            evidence_hashes=[],
            source_refs=[],
        )
        assert len(payload["edges"]) == 3
        assert payload["edges"][0]["src_id"] == "A"
        assert payload["edges"][1]["src_id"] == "B"
        assert payload["edges"][2]["src_id"] == "A" and payload["edges"][2]["dst_id"] == "A"


# =============================================================================
# Dimension 4: Object Packs Cyclic Resilience & Deterministic Hash Integrity
# =============================================================================

class TestObjectPacksCyclicAndProvenanceHardening:
    """Stress-test object_packs.py helper methods against cyclic edges and hash determinism."""

    def test_object_evidence_refs_cyclic_edges(self):
        """_object_evidence_refs extracts unique evidence refs even when edges contain circular references."""
        obj = {"object_id": "obj_A", "evidence_refs": ["ev_1", "ev_2"]}
        safe_edges = [
            {"from_object_id": "obj_A", "to_object_id": "obj_B", "evidence_refs": ["ev_2", "ev_3"]},
            {"from_object_id": "obj_B", "to_object_id": "obj_A", "evidence_refs": ["ev_3", "ev_4"]},
            {"from_object_id": "obj_A", "to_object_id": "obj_A", "evidence_refs": ["ev_1", "ev_5"]},  # Self loop
        ]
        refs = _object_evidence_refs(obj, safe_edges=safe_edges)
        assert refs == ["ev_1", "ev_2", "ev_3", "ev_4", "ev_5"]

    def test_object_edge_refs_cyclic_edges(self):
        """_object_edge_refs extracts edge IDs from cyclic edges without duplication or error."""
        obj = {"object_id": "node_X", "edge_refs": ["e_direct"]}
        safe_edges = [
            {"edge_id": "e_cycle_1", "from_object_id": "node_X", "to_object_id": "node_Y"},
            {"edge_id": "e_cycle_2", "from_object_id": "node_Y", "to_object_id": "node_X"},
            {"edge_id": "e_self", "from_object_id": "node_X", "to_object_id": "node_X"},
        ]
        refs = _object_edge_refs(obj, safe_edges=safe_edges)
        assert refs == ["e_cycle_1", "e_cycle_2", "e_direct", "e_self"]

    def test_candidate_graph_hash_deterministic_computation(self):
        """_candidate_graph_hash produces identical deterministic hash for same graph structure."""
        objects = [{"object_id": "O1", "title": "Obj 1"}]
        edges = [{"edge_id": "E1", "from_object_id": "O1", "to_object_id": "O1"}]
        evidence = [{"evidence_id": "EV1", "summary": "Ev 1"}]

        h1 = _candidate_graph_hash(objects, edges, evidence)
        h2 = _candidate_graph_hash(objects, edges, evidence)
        assert h1 == h2
        assert h1.startswith("sha256:")
        assert len(h1.split(":", 1)[1]) == 64

    def test_approval_board_item_cyclic_node_resilience(self):
        """_approval_board_item correctly processes an object connected to cyclic edges."""
        obj = {
            "object_id": "cand_cyclic_1",
            "object_type": "RepoDocument",
            "title": "Cyclic Doc",
            "authority_lane": "candidate",
            "review_state": "needs_review",
            "evidence_refs": ["ev_cand_1"],
            "edge_refs": [],
            "confidence": {"score": 0.8},
        }
        safe_edges = [
            {"edge_id": "e1", "from_object_id": "cand_cyclic_1", "to_object_id": "cand_cyclic_1", "evidence_refs": ["ev_loop"]},
        ]
        item = _approval_board_item(obj, safe_edges=safe_edges, reviewer_actions=("promote", "reject"))
        assert item["object_id"] == "cand_cyclic_1"
        assert item["editable"] is True
        assert "ev_loop" in item["evidence_refs"]
        assert "e1" in item["edge_refs"]


# =============================================================================
# Dimension 5: End-to-End MCP Surface Adversarial Stress Tests
# =============================================================================

class TestEndToEndMCPProvenanceAndDAGAdversarial:
    """Stress-test MCP tools with cyclic DAG topologies and tampered hash inputs."""

    def test_mcp_brain_resolve_with_evidence_on_cyclic_dag(self):
        """brain.resolve with response_mode='with_evidence' returns complete graph without loop."""
        store = InMemoryPostgresStore()
        # Seed 3 decisions in a cycle: D1 -> D2 -> D3 -> D1
        for i in range(1, 4):
            store.insert_card(
                MemoryCard(
                    memory_id=f"mem_d{i}",
                    project="neurons",
                    card_type="decision",
                    title=f"Decision {i}",
                    summary=f"Decision summary {i}",
                    typed_payload={"decision": f"Dec {i}", "rationale": f"Rat {i}"},
                    lifecycle_state="human_accepted",
                    authorization_status="active",
                    currentness="current",
                    content_hash=f"sha256:{i:064x}",
                    source_ref=[{"locator": f"docs/adr-000{i}.md"}],
                )
            )

        store.insert_edge(MemoryEdge(1, "mem_d1", "supersedes", "mem_d2", "sha256:" + "1" * 64))
        store.insert_edge(MemoryEdge(2, "mem_d2", "supersedes", "mem_d3", "sha256:" + "2" * 64))
        store.insert_edge(MemoryEdge(3, "mem_d3", "supersedes", "mem_d1", "sha256:" + "3" * 64))

        server = MockMCPServer(store)
        req = {
            "jsonrpc": "2.0",
            "id": 501,
            "method": "tools/call",
            "params": {
                "name": "brain.resolve",
                "arguments": {
                    "project": "neurons",
                    "mode": "context",
                    "response_mode": "with_evidence",
                },
            },
        }
        res = server.handle_public_request(req)
        assert "result" in res
        result = res["result"]
        assert result["schema_version"] == "lbrain_evidence_context.v1"
        assert len(result["decisions"]) == 3
        assert len(result["edges"]) == 3
        assert len(result["evidence_hashes"]) == 3

    @pytest.mark.parametrize("tampered_hash", [
        "not_a_sha256_hash",
        "sha256:12345",
        "sha256:" + "G" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "md5:d41d8cd98f00b204e9800998ecf8427e",
    ])
    def test_mcp_memory_candidate_create_tampered_hash_fails_closed(self, tmp_path: Path, tampered_hash: str):
        """memory_candidate_create rejects tampered/invalid content_hash fail-closed with -32602 on real service."""
        service = _create_real_service(tmp_path)
        req = {
            "jsonrpc": "2.0",
            "id": 502,
            "method": "tools/call",
            "params": {
                "name": "memory_candidate_create",
                "arguments": {
                    "card_type": "decision",
                    "project": "neurons",
                    "title": "Tampered Hash Test",
                    "summary": "Should be rejected",
                    "typed_payload": {"decision": "Reject this", "rationale": "r", "alternatives": [], "consequence": "c", "authority_ref": "a"},
                    "content_hash": tampered_hash,
                },
            },
        }
        res = handle_jsonrpc_message(req, service, surface="agent")
        assert "error" in res
        assert res["error"]["code"] == -32602

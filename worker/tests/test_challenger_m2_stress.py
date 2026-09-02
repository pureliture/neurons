from __future__ import annotations

import base64
import json
import random
import string
from typing import Any

import pytest

from agent_knowledge.llm_brain_core.slim_serializer import SlimSerializer


def _random_string(length: int, chars: str = string.ascii_letters + string.digits) -> str:
    return "".join(random.choices(chars, k=length))


def _random_unicode_string(length: int) -> str:
    # Mix of emojis, CJK, Arabic (RTL), ZWJ sequences, combining diacritics, Cyrillic
    unicode_samples = [
        "🔥", "🧠", "⚡", "🚀", "🎉", "👨‍👩‍👧‍👦", "🏳️‍🌈",  # Emojis & ZWJ
        "한글테스트", "안녕하세요", "인공지능",  # Korean Hangul
        "日本語テスト", "東京", "人工知能",  # Japanese Kanji/Kana
        "中文测试", "深度学习", "知识图谱",  # Chinese Hanzi
        "مرحبا بالعالم", "الذكاء الاصطناعي",  # Arabic RTL
        "Привет мир", "машинное обучение",  # Russian Cyrillic
        "éàçüöñ", "e\u0301", "a\u0308",  # Combining diacritics
        "\u200b\u200c\u200d\ufeff",  # Zero-width spaces & joiners
    ]
    result = []
    current_len = 0
    while current_len < length:
        sample = random.choice(unicode_samples)
        result.append(sample)
        current_len += len(sample)
    return "".join(result)[:length]


class TestAdversarialPayloadBudget:
    """Adversarial payload budget testing for SlimSerializer.

    Spec requirement: SlimSerializer.serialize_slim MUST NEVER exceed 3072 bytes (3.0 KB)
    and always produce valid JSON across all input vectors.
    """

    def test_100_random_massive_cards_hard_limit(self):
        """Generate 100+ random massive cards with multi-MB strings, assert NEVER > 3072 bytes."""
        random.seed(42)
        decisions = []
        for i in range(120):
            huge_decision = _random_string(50000)
            huge_rationale = _random_string(30000)
            huge_title = _random_string(5000)
            decisions.append({
                "memory_id": f"mem_massive_{i}_{_random_string(32)}",
                "title": f"Decision {i}: {huge_title}",
                "typed_payload": {
                    "decision": huge_decision,
                    "rationale": huge_rationale,
                    "nested_deep": {"level1": {"level2": {"data": _random_string(10000)}}},
                },
                "currentness": "current",
                "content_hash": f"sha256:{_random_string(64, '0123456789abcdef')}",
            })

        for limit in [1, 5, 10, 20, 50]:
            payload = SlimSerializer.serialize_slim(
                project="neurons-massive-test",
                decisions=decisions,
                preferences=[],
                guardrails=["guardrail_1", "guardrail_2"],
                limit=limit,
            )
            raw_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            assert raw_bytes <= 3072, f"Payload size {raw_bytes} exceeds 3072 bytes limit (limit={limit})"
            dumped = json.dumps(payload, ensure_ascii=False)
            reloaded = json.loads(dumped)
            assert reloaded["schema_version"] == "lbrain_slim_context.v1"
            assert isinstance(reloaded["decisions"], list)

    def test_100_massive_unicode_cards_glyph_resilience(self):
        """Test with massive multi-byte UTF-8 glyphs, emojis, ZWJ, CJK, RTL."""
        random.seed(1337)
        decisions = []
        for i in range(100):
            unicode_decision = _random_unicode_string(20000)
            unicode_rationale = _random_unicode_string(15000)
            unicode_title = _random_unicode_string(3000)
            decisions.append({
                "memory_id": f"mem_unicode_{i}",
                "title": f"Unicode Card {i} " + unicode_title,
                "typed_payload": {
                    "decision": unicode_decision,
                    "rationale": unicode_rationale,
                },
                "currentness": "current",
                "content_hash": f"sha256:{i:064x}",
            })

        payload = SlimSerializer.serialize_slim(
            project="neurons-unicode-stress",
            decisions=decisions,
            preferences=[],
            guardrails=["한국어_가드레일_⚡", "日本語_制約_🔥"],
            limit=5,
        )
        raw_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert raw_bytes <= 3072, f"Unicode payload size {raw_bytes} exceeds 3072 bytes"
        dumped = json.dumps(payload, ensure_ascii=False)
        reloaded = json.loads(dumped)
        assert reloaded["schema_version"] == "lbrain_slim_context.v1"

    def test_deep_nested_and_malformed_typed_payloads(self):
        """Test with deeply nested dictionaries, lists, boolean/null values, raw json strings."""
        nested_obj: dict[str, Any] = {"leaf": "deep_data"}
        for depth in range(50):
            nested_obj = {"depth": depth, "next": nested_obj, "array": [depth, depth * 2]}

        decisions = [
            {
                "memory_id": f"mem_deep_{i}",
                "title": f"Deep Nested Card {i}",
                "typed_payload": {
                    "decision": "Nested structure decision",
                    "rationale": "Nested structure rationale",
                    "deep_tree": nested_obj,
                    "weird_types": [None, True, False, 1e10, {"nested": ["a", "b"]}],
                },
                "content_hash": f"sha256:{i:064x}",
            }
            for i in range(10)
        ]

        payload = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=decisions,
            preferences=[],
            guardrails=[],
            limit=5,
        )
        raw_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert raw_bytes <= 3072
        assert json.loads(json.dumps(payload, ensure_ascii=False))

    def test_single_monster_card_truncation(self):
        """Test a single card with 10MB decision, 10MB rationale, 10MB title."""
        monster_card = [
            {
                "memory_id": "mem_monster",
                "title": "M" * 1000000,
                "typed_payload": {
                    "decision": "D" * 5000000,
                    "rationale": "R" * 5000000,
                },
                "content_hash": "sha256:" + "0" * 64,
            }
        ]
        payload = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=monster_card,
            preferences=[],
            guardrails=[],
            limit=1,
        )
        raw_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert raw_bytes <= 3072, f"Monster card payload size {raw_bytes} exceeds 3072 bytes"
        dumped = json.dumps(payload, ensure_ascii=False)
        reloaded = json.loads(dumped)
        assert len(reloaded["decisions"]) == 1
        assert reloaded["decisions"][0]["id"] == "mem_monster"

    def test_massive_recent_context_truncation_when_decisions_empty(self):
        """Test hard limit budgeting when recent_context itself is 50KB and decisions list is empty."""
        massive_context = "Recent context entry line " * 2000  # ~50 KB
        payload = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=[],
            preferences=[],
            guardrails=[],
            recent_context=massive_context,
            limit=1,
        )
        raw_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert raw_bytes <= 3072, f"Massive context (empty decisions) payload size {raw_bytes} exceeds 3072 bytes"
        assert json.loads(json.dumps(payload, ensure_ascii=False))

    def test_massive_preferences_budget_containment(self):
        """Test hard limit budgeting when preferences contains 50 massive items."""
        preferences = [
            {"title": f"Pref {i}", "rule": "A" * 200, "scope": "testing"}
            for i in range(50)
        ]
        payload = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=[],
            preferences=preferences,
            guardrails=[],
        )
        raw_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert raw_bytes <= 3072, f"Massive preferences payload size {raw_bytes} exceeds 3072 bytes"

    def test_massive_guardrails_budget_containment(self):
        """Test hard limit budgeting when guardrails contains 50 massive strings."""
        guardrails = [f"guardrail_{i}_" + "X" * 150 for i in range(50)]
        payload = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=[],
            preferences=[],
            guardrails=guardrails,
        )
        raw_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert raw_bytes <= 3072, f"Massive guardrails payload size {raw_bytes} exceeds 3072 bytes"


class TestAdversarialPagination:
    """Adversarial pagination testing for SlimSerializer."""

    def test_corrupted_and_malformed_cursors(self):
        """Test non-base64, invalid padding, arbitrary binary, and corrupted cursors."""
        decisions = [
            {"id": f"d_{i}", "title": f"D{i}", "typed_payload": {"decision": f"Dec {i}"}}
            for i in range(10)
        ]

        corrupted_cursors = [
            "not_base_64_at_all!!@@##",
            "YWJj===",  # invalid base64 padding
            "====",
            "A",
            "AA",
            "AAA",
            "",
            "   ",
            "\n\t\r",
            base64.b64encode(b"").decode("utf-8"),  # empty base64
            base64.b64encode(b"offset:").decode("utf-8"),  # missing offset value
            base64.b64encode(b"offset:abc").decode("utf-8"),  # non-integer offset
            base64.b64encode(b"offset:12.34").decode("utf-8"),  # float offset
            base64.b64encode(b"offset:NaN").decode("utf-8"),
            base64.b64encode(b"offset:null").decode("utf-8"),
            base64.b64encode(b"offset:None").decode("utf-8"),
            base64.b64encode(b"offset:{}").decode("utf-8"),
            base64.b64encode(b"offset:[]").decode("utf-8"),
            base64.b64encode(b"\x00\xff\xfe\x01\x02").decode("latin1"),  # binary junk
            "offset:5",  # raw unencoded string
            "5",  # raw unencoded number string
            "{'cursor': 5}",  # raw dict
        ]

        for bad_cursor in corrupted_cursors:
            payload = SlimSerializer.serialize_slim(
                project="neurons",
                decisions=decisions,
                preferences=[],
                guardrails=[],
                limit=3,
                cursor=bad_cursor,
            )
            assert payload["schema_version"] == "lbrain_slim_context.v1"
            assert len(payload["decisions"]) == 3
            assert payload["decisions"][0]["id"] == "d_0"
            assert payload["has_more"] is True
            assert payload["next_cursor"] is not None

    def test_out_of_bounds_and_large_offsets(self):
        """Test out-of-bounds, far beyond total items cursor offsets."""
        decisions = [{"id": f"d_{i}", "title": f"D{i}", "typed_payload": {}} for i in range(5)]

        large_offsets = [5, 6, 10, 100, 1000000, 999999999999999]
        for off in large_offsets:
            cursor = base64.b64encode(f"offset:{off}".encode("utf-8")).decode("utf-8")
            payload = SlimSerializer.serialize_slim(
                project="neurons",
                decisions=decisions,
                preferences=[],
                guardrails=[],
                limit=3,
                cursor=cursor,
            )
            assert len(payload["decisions"]) == 0
            assert payload["has_more"] is False
            assert payload["next_cursor"] is None

    def test_negative_offset_cursor_resilience(self):
        """Test negative offsets in cursor (e.g. offset:-1, offset:-10).

        Must normalize negative start_idx to 0 and NOT return has_more=True with 0 items
        or negative next_cursor offsets.
        """
        decisions = [{"id": f"d_{i}", "title": f"D{i}", "typed_payload": {}} for i in range(5)]
        negative_offsets = [-1, -5, -10, -100]

        for neg_off in negative_offsets:
            cursor = base64.b64encode(f"offset:{neg_off}".encode("utf-8")).decode("utf-8")
            payload = SlimSerializer.serialize_slim(
                project="neurons",
                decisions=decisions,
                preferences=[],
                guardrails=[],
                limit=3,
                cursor=cursor,
            )
            assert payload["schema_version"] == "lbrain_slim_context.v1"
            # When normalized to 0, limit=3 must return 3 decisions starting from d_0
            assert len(payload["decisions"]) == 3, f"Expected 3 decisions with normalized start_idx for offset {neg_off}, got {len(payload['decisions'])}"
            assert payload["decisions"][0]["id"] == "d_0"
            if payload["next_cursor"]:
                decoded = base64.b64decode(payload["next_cursor"]).decode("utf-8")
                offset_val = int(decoded.split(":")[-1])
                assert offset_val >= 0, f"Next cursor offset must be non-negative, got {offset_val}"

    def test_non_positive_limit_handling(self):
        """Test edge cases with limit <= 0 (limit=0, limit=-5)."""
        decisions = [{"id": f"d_{i}", "title": f"D{i}", "typed_payload": {}} for i in range(5)]
        for non_pos_limit in [0, -1, -5]:
            payload = SlimSerializer.serialize_slim(
                project="neurons",
                decisions=decisions,
                preferences=[],
                guardrails=[],
                limit=non_pos_limit,
            )
            # Should not claim has_more=True with next_cursor=offset:0 creating infinite loops
            if payload.get("next_cursor"):
                decoded = base64.b64decode(payload["next_cursor"]).decode("utf-8")
                offset_val = int(decoded.split(":")[-1])
                assert offset_val > 0, f"next_cursor offset must advance (> 0), got {offset_val}"

    def test_end_to_end_deterministic_pagination_traversal(self):
        """Traverse 100 items with different page sizes (limit=1, 7, 23, 50).

        Assert all items retrieved exactly once in deterministic order without infinite loop.
        """
        total_count = 100
        all_decisions = [
            {
                "id": f"decision_item_{i:03d}",
                "title": f"Title {i}",
                "typed_payload": {"decision": f"Decision body {i}"},
                "content_hash": f"sha256:{i:064x}",
            }
            for i in range(total_count)
        ]

        for page_size in [1, 7, 23, 50]:
            collected_ids: list[str] = []
            cursor = None
            page_count = 0
            max_pages = total_count + 10

            while page_count < max_pages:
                page = SlimSerializer.serialize_slim(
                    project="neurons",
                    decisions=all_decisions,
                    preferences=[],
                    guardrails=[],
                    limit=page_size,
                    cursor=cursor,
                )
                page_count += 1
                for d in page["decisions"]:
                    collected_ids.append(d["id"])

                if not page["has_more"]:
                    assert page["next_cursor"] is None
                    break
                cursor = page["next_cursor"]
                assert cursor is not None

            assert len(collected_ids) == total_count, (
                f"Expected {total_count} items with page_size={page_size}, got {len(collected_ids)}"
            )
            expected_ids = [f"decision_item_{i:03d}" for i in range(total_count)]
            assert collected_ids == expected_ids, f"Mismatch in order/content with page_size={page_size}"


class TestZeroDecisionsPreferencesEdgeCases:
    """Boundary testing for empty context and zero decisions/preferences."""

    def test_empty_context_serialization_under_200_bytes(self):
        """Verify empty context serialization stays strictly <= 200 bytes."""
        payload = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=[],
            preferences=[],
            guardrails=[],
            recent_context="",
            gaps=[],
        )
        raw_json = json.dumps(payload, ensure_ascii=False)
        raw_bytes = len(raw_json.encode("utf-8"))
        assert raw_bytes <= 200, f"Empty context size {raw_bytes} exceeds 200 bytes limit: '{raw_json}'"

        # Check required schema fields
        assert payload["schema_version"] == "lbrain_slim_context.v1"
        assert payload["project"] == "neurons"
        assert payload["decisions"] == []
        assert payload["preferences"] == []
        assert payload["active_guardrails"] == []
        assert payload["gaps"] == []
        assert payload["has_more"] is False
        assert payload["next_cursor"] is None

    def test_none_defaults_and_minimal_fields(self):
        """Test with None / default arguments for gaps, cursor, recent_context."""
        payload = SlimSerializer.serialize_slim(
            project="n",
            decisions=[],
            preferences=[],
            guardrails=[],
            recent_context="",
            gaps=None,
            cursor=None,
        )
        raw_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert raw_bytes <= 200, f"Minimal context size {raw_bytes} exceeds 200 bytes limit"
        assert payload["gaps"] == []

    def test_single_item_contexts(self):
        """Test minimal payloads with exactly 1 item in each category."""
        # 1 decision only
        p1 = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=[{"id": "d1", "title": "T1", "typed_payload": {"decision": "D1"}}],
            preferences=[],
            guardrails=[],
        )
        assert len(p1["decisions"]) == 1
        assert len(json.dumps(p1, ensure_ascii=False).encode("utf-8")) <= 400

        # 1 preference only
        p2 = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=[],
            preferences=[{"title": "P1", "rule": "R1", "scope": "general"}],
            guardrails=[],
        )
        assert len(p2["preferences"]) == 1
        assert len(json.dumps(p2, ensure_ascii=False).encode("utf-8")) <= 350

        # 1 guardrail only
        p3 = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=[],
            preferences=[],
            guardrails=["no_destructive_writes"],
        )
        assert len(p3["active_guardrails"]) == 1
        assert len(json.dumps(p3, ensure_ascii=False).encode("utf-8")) <= 300


class TestWithEvidenceStress:
    """Adversarial testing for serialize_with_evidence."""

    def test_dedup_and_massive_evidence_hashes(self):
        """Test deduplication of 1000+ hashes preserving deterministic order."""
        raw_hashes = [f"sha256:hash_{i % 25:04d}" for i in range(1000)]
        edges = [
            {
                "src_id": f"node_{i}",
                "rel_type": "supports",
                "dst_id": f"node_{i+1}",
                "provenance_hash": f"sha256:prov_{i:04d}",
            }
            for i in range(20)
        ]
        source_refs = [{"locator": f"repo/file_{i}.py", "span": f"lines {i}-{i+10}"} for i in range(10)]

        payload = SlimSerializer.serialize_with_evidence(
            project="neurons",
            decisions=[],
            preferences=[],
            guardrails=[],
            edges=edges,
            evidence_hashes=raw_hashes,
            source_refs=source_refs,
        )
        assert payload["schema_version"] == "lbrain_evidence_context.v1"
        assert len(payload["evidence_hashes"]) == 25
        assert len(payload["edges"]) == 20
        assert len(payload["source_refs"]) == 10


class TestChallengerRound2AdvancedAdversarial:
    """Comprehensive Round 2 Adversarial Stress Testing Suite for Milestone 2."""

    def test_massive_gaps_payload_budget_containment(self):
        """Adversarial vector: 50 massive gaps (1000 chars each) with empty decisions."""
        gaps = [f"gap_{i}_" + _random_unicode_string(1000) for i in range(50)]
        payload = SlimSerializer.serialize_slim(
            project="neurons",
            decisions=[],
            preferences=[],
            guardrails=[],
            gaps=gaps,
        )
        raw_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        assert raw_bytes <= 3072, f"Massive gaps payload size {raw_bytes} exceeds 3072 bytes"
        assert json.loads(json.dumps(payload, ensure_ascii=False))

    def test_all_components_massive_combined_pressure(self):
        """Adversarial vector: Massive decisions + preferences + guardrails + gaps + context all together."""
        random.seed(999)
        decisions = [
            {
                "id": f"d_{i}",
                "title": _random_unicode_string(500),
                "typed_payload": {
                    "decision": _random_unicode_string(5000),
                    "rationale": _random_unicode_string(5000),
                },
                "content_hash": f"sha256:{i:064x}",
            }
            for i in range(20)
        ]
        preferences = [
            {"title": _random_unicode_string(100), "rule": _random_unicode_string(1000), "scope": "testing"}
            for i in range(20)
        ]
        guardrails = [_random_unicode_string(500) for _ in range(20)]
        gaps = [_random_unicode_string(500) for _ in range(20)]
        recent_context = _random_unicode_string(10000)

        for limit in [1, 3, 5, 10]:
            payload = SlimSerializer.serialize_slim(
                project="neurons-all-components-massive",
                decisions=decisions,
                preferences=preferences,
                guardrails=guardrails,
                recent_context=recent_context,
                gaps=gaps,
                limit=limit,
            )
            raw_json = json.dumps(payload, ensure_ascii=False)
            raw_bytes = len(raw_json.encode("utf-8"))
            assert raw_bytes <= 3072, f"Combined massive payload {raw_bytes} exceeds 3072 bytes (limit={limit})"
            reloaded = json.loads(raw_json)
            assert reloaded["schema_version"] == "lbrain_slim_context.v1"

    def test_fuzzing_1000_randomized_inputs(self):
        """Fuzz testing: 1,000 iterations with completely randomized inputs, limits, and cursor states."""
        random.seed(2026)
        for iteration in range(1000):
            num_decisions = random.randint(0, 15)
            num_prefs = random.randint(0, 10)
            num_guardrails = random.randint(0, 10)
            num_gaps = random.randint(0, 5)

            decisions = [
                {
                    "id": f"id_{i}_{random.randint(0, 9999)}",
                    "title": _random_unicode_string(random.randint(0, 200)),
                    "typed_payload": {
                        "decision": _random_unicode_string(random.randint(0, 1000)),
                        "rationale": _random_unicode_string(random.randint(0, 1000)),
                    },
                    "currentness": random.choice(["current", "superseded", "stale", None]),
                    "content_hash": f"sha256:{random.randint(0, 2**64):064x}",
                }
                for i in range(num_decisions)
            ]

            preferences = [
                {
                    "title": _random_unicode_string(random.randint(0, 100)),
                    "rule": _random_unicode_string(random.randint(0, 500)),
                    "scope": random.choice(["general", "repo", "security", None]),
                }
                for i in range(num_prefs)
            ]

            guardrails = [_random_unicode_string(random.randint(0, 200)) for _ in range(num_guardrails)]
            gaps = [_random_unicode_string(random.randint(0, 100)) for _ in range(num_gaps)] if random.random() > 0.3 else None
            recent_context = _random_unicode_string(random.randint(0, 2000)) if random.random() > 0.2 else None
            limit = random.randint(-5, 30)

            cursor_choice = random.choice([
                None,
                "",
                "invalid_b64",
                base64.b64encode(f"offset:{random.randint(-100, 100)}".encode("utf-8")).decode("utf-8"),
                base64.b64encode(f"{random.randint(-100, 100)}".encode("utf-8")).decode("utf-8"),
                base64.b64encode(b"gibberish").decode("utf-8"),
            ])

            payload = SlimSerializer.serialize_slim(
                project=f"fuzz_proj_{random.randint(0, 100)}",
                decisions=decisions,
                preferences=preferences,
                guardrails=guardrails,
                recent_context=recent_context,
                gaps=gaps,
                limit=limit,
                cursor=cursor_choice,
            )

            raw_json = json.dumps(payload, ensure_ascii=False)
            raw_bytes = len(raw_json.encode("utf-8"))
            assert raw_bytes <= 3072, f"Fuzz iteration {iteration} exceeded 3072 bytes: got {raw_bytes}"
            parsed = json.loads(raw_json)
            assert parsed["schema_version"] == "lbrain_slim_context.v1"
            assert isinstance(parsed["decisions"], list)
            assert isinstance(parsed["preferences"], list)
            assert isinstance(parsed["active_guardrails"], list)
            assert isinstance(parsed["gaps"], list)
            assert isinstance(parsed["has_more"], bool)

    def test_with_evidence_deep_graph_fuzzing(self):
        """Test serialize_with_evidence with random cyclic/acyclic edges, dirty hashes, complex source refs."""
        random.seed(777)
        for iteration in range(200):
            num_edges = random.randint(0, 30)
            edges = [
                {
                    "src_id": f"node_{random.randint(0, 10)}",
                    "rel_type": random.choice(["supports", "supersedes", "refines", "violates", ""]),
                    "dst_id": f"node_{random.randint(0, 10)}",
                    "provenance_hash": f"sha256:{random.randint(0, 2**64):064x}",
                }
                for _ in range(num_edges)
            ]

            num_hashes = random.randint(0, 50)
            hashes = [
                f"sha256:{random.randint(0, 10):064x}" if random.random() > 0.2 else "   "
                for _ in range(num_hashes)
            ]

            num_refs = random.randint(0, 20)
            refs = []
            for _ in range(num_refs):
                if random.random() > 0.5:
                    refs.append({"locator": f"file_{random.randint(0, 10)}.py", "line": random.randint(1, 100)})
                else:
                    refs.append(f"file_{random.randint(0, 10)}.py:span")

            payload = SlimSerializer.serialize_with_evidence(
                project="evidence_fuzz",
                decisions=[],
                preferences=[],
                guardrails=[],
                edges=edges,
                evidence_hashes=hashes,
                source_refs=refs,
            )

            assert payload["schema_version"] == "lbrain_evidence_context.v1"
            assert isinstance(payload["edges"], list)
            assert isinstance(payload["evidence_hashes"], list)
            assert isinstance(payload["source_refs"], list)
            for h in payload["evidence_hashes"]:
                assert h.strip() != ""


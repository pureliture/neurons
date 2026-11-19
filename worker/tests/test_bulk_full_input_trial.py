"""TDD suite for the isolated bulk full-input trial. Synthetic data only.

These tests target a module that does not exist yet; the first run must be RED.
No network, no real DB, no real data, no production paths.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_knowledge.llm_brain_core.bulk_full_input import (
    DuplicateRejected,
    FencingRejected,
    ModelPolicy,
    PaidModelRejected,
    PrivacyGateRejected,
    ChunkTooLarge,
    make_full_chunk_session_input,
    SourceInvalid,
    FullInputBuilder,
    InMemoryChunkState,
    assert_free_only,
    choose_fallback,
    eligible_reader_group_ids,
    is_shadow_group,
    needs_reprocess,
    privacy_gate,
    shadow_group_ids,
    unaffected_ordinals,
)
from agent_knowledge.llm_brain_core.graph_first_resolver import GraphFirstResolver
from agent_knowledge.llm_brain_core.graph_scope import graph_group_ids


def _doc(index: int, body: str) -> dict:
    return {
        "_id": f"synthetic-member-{index}",
        "turn_start_index": index,
        "turn_end_index": index,
        "part_index": 0,
        "char_start": 0,
        "body": f"turn_start_index: {index}\npart_index: 0\n\n{body}",
    }


def _build(docs, *, policy="full-input.v1", overlap=0):
    return FullInputBuilder(
        docs,
        session_hash="synthetic-session",
        source_hash="synthetic-source",
        policy_version=policy,
        run_id="synthetic-run",
        overlap_bytes=overlap,
    )


# --- byte coverage / UTF-8 / chunk relations --------------------------------

def test_basic_ranges_cover_canonical_bytes_exactly_once():
    docs = [_doc(i, f"chunk-{i}-payload") for i in range(4)]
    builder = _build(docs)
    plan = builder.plan
    assert plan.total_bytes == len(builder.canonical_bytes)
    ranges = [r for chunk in plan.chunks for r in chunk.basic_ranges]
    assert ranges == sorted(ranges, key=lambda r: r.start)
    cursor = 0
    pieces = []
    for span in ranges:
        assert span.start == cursor
        pieces.append(builder.canonical_bytes[span.start:span.end])
        cursor = span.end
    assert cursor == plan.total_bytes
    assert b"".join(pieces) == builder.canonical_bytes


def test_front_middle_end_all_present_in_full_input():
    docs = [
        _doc(0, "FRONT_SENTINEL" + "A" * 5000),
        _doc(1, "MIDDLE_SENTINEL" + "B" * 5000),
        _doc(2, "C" * 5000 + "END_SENTINEL"),
    ]
    builder = _build(docs)
    assert b"FRONT_SENTINEL" in builder.canonical_bytes
    assert b"MIDDLE_SENTINEL" in builder.canonical_bytes
    assert b"END_SENTINEL" in builder.canonical_bytes
    assert len(builder.plan.chunks) == 3


def test_korean_and_emoji_ranges_end_on_utf8_boundaries():
    docs = [_doc(0, "한글" * 1000), _doc(1, "😀" * 700 + "끝"), _doc(2, "마지막")]
    builder = _build(docs)
    for chunk in builder.plan.chunks:
        for span in chunk.basic_ranges:
            piece = builder.canonical_bytes[span.start:span.end]
            assert piece.decode("utf-8")
    assert "😀".encode("utf-8") in builder.canonical_bytes
    assert "끝".encode("utf-8") in builder.canonical_bytes


def test_overlap_is_recorded_as_context_without_breaking_basic_coverage():
    docs = [_doc(0, "A" * 200), _doc(1, "B" * 200)]
    builder = _build(docs, overlap=8)
    plan = builder.plan
    assert plan.chunks[1].overlap_ranges
    assert plan.chunks[0].overlap_ranges == ()
    basic = [r for c in plan.chunks for r in c.basic_ranges]
    assert [(r.start, r.end) for r in basic] == [(0, 200), (200, 401)]
    overlap = plan.chunks[1].overlap_ranges[0]
    assert (overlap.start, overlap.end) == (192, 200)
    assert builder.render(1).startswith(builder.canonical_bytes[overlap.start:overlap.end])


def _flatten(ranges):
    return [i for r in ranges for i in range(r.start, r.end)]


def test_overlapping_or_conflicting_member_coordinates_are_source_invalid():
    docs = [_doc(0, "first"), _doc(0, "conflicting-second")]
    with pytest.raises(SourceInvalid):
        _build(docs)


def test_empty_input_has_explicit_zero_byte_plan():
    builder = _build([_doc(0, "")])
    assert builder.plan.total_bytes == 0
    assert len(builder.plan.chunks) == 1


# --- policy version / completion selection ----------------------------------

def test_existing_completion_with_old_policy_is_reselected():
    assert needs_reprocess(
        completed_policy_version="legacy-unknown-coverage",
        completed_source_hash="synthetic-source",
        completed_map_digest="any",
        planned_policy_version="full-input.v1",
        planned_source_hash="synthetic-source",
        planned_map_digest="any",
        all_chunks_verified=True,
    ) is True


def test_same_policy_and_full_verification_skips():
    assert needs_reprocess(
        completed_policy_version="full-input.v1",
        completed_source_hash="synthetic-source",
        completed_map_digest="digest-1",
        planned_policy_version="full-input.v1",
        planned_source_hash="synthetic-source",
        planned_map_digest="digest-1",
        all_chunks_verified=True,
    ) is False


def test_partial_completion_and_source_change_are_reprocessed():
    base = dict(
        completed_policy_version="full-input.v1",
        completed_source_hash="synthetic-source",
        completed_map_digest="digest-1",
        planned_policy_version="full-input.v1",
        planned_source_hash="synthetic-source",
        planned_map_digest="digest-1",
    )
    assert needs_reprocess(**base, all_chunks_verified=False) is True
    assert needs_reprocess(**{**base, "completed_source_hash": "older"}, all_chunks_verified=True) is True
    assert needs_reprocess(**{**base, "completed_map_digest": "older"}, all_chunks_verified=True) is True


# --- interruption / dedupe / fencing ----------------------------------------

def test_resume_after_interruption_reuses_completed_chunks_without_duplicate_writes():
    docs = [_doc(i, f"payload-{i}") for i in range(3)]
    builder = _build(docs)
    state = InMemoryChunkState()
    keys = [c.key for c in builder.plan.chunks]
    token = state.claim(keys[0], owner="worker-1", lease_expiry=100)
    state.complete(keys[0], token=token, output_digest="out-0", write_receipt="receipt-0")
    state.claim(keys[1], owner="worker-1", lease_expiry=100)

    pending = [c.key for c in builder.plan.chunks if not state.is_complete(c.key)]
    assert pending == keys[1:]
    for key, digest in zip(pending, ("out-1", "out-2")):
        token = state.claim(key, owner="worker-2", lease_expiry=200)
        state.complete(key, token=token, output_digest=digest, write_receipt=f"receipt-{digest}")
    assert state.write_count(keys[0]) == 1
    assert all(state.is_complete(key) for key in keys)
    assert state.all_verified(keys)


def test_stale_fencing_token_cannot_complete_after_reclaim():
    state = InMemoryChunkState()
    key = "chunk-key"
    token1 = state.claim(key, owner="worker-1", lease_expiry=10)
    token2 = state.claim(key, owner="worker-2", lease_expiry=20)
    assert token2 > token1
    with pytest.raises(FencingRejected):
        state.complete(key, token=token1, output_digest="stale", write_receipt="receipt")


def test_same_receipt_is_idempotent_but_changed_output_is_rejected():
    state = InMemoryChunkState()
    key = "chunk-key"
    token = state.claim(key, owner="worker-1", lease_expiry=10)
    state.complete(key, token=token, output_digest="out-1", write_receipt="receipt-1")
    fresh = state.claim(key, owner="worker-1", lease_expiry=20)
    state.complete(key, token=fresh, output_digest="out-1", write_receipt="receipt-1")
    assert state.write_count(key) == 1
    later = state.claim(key, owner="worker-1", lease_expiry=30)
    with pytest.raises(DuplicateRejected):
        state.complete(key, token=later, output_digest="out-2", write_receipt="receipt-2")


# --- source revision change --------------------------------------------------

def test_append_reuses_prior_chunks_but_front_edit_invalidates_them():
    old = _build([_doc(0, "alpha"), _doc(1, "beta")]).plan
    appended = _build([_doc(0, "alpha"), _doc(1, "beta"), _doc(2, "gamma")]).plan
    edited = _build([_doc(0, "ALPHA"), _doc(1, "beta")]).plan
    assert unaffected_ordinals(old, appended) == (0, 1)
    assert unaffected_ordinals(old, edited) == ()


# --- shadow isolation and reader selection ----------------------------------

def test_shadow_group_is_disjoint_from_current_and_legacy_groups():
    scope = "/project/synthetic"
    shadow = shadow_group_ids(scope, "full-input.v1")
    assert shadow
    assert not set(shadow) & set(graph_group_ids(scope))
    assert all(is_shadow_group(group) for group in shadow)


def test_reader_excludes_shadow_until_explicitly_eligible():
    scope = "/project/synthetic"
    default = eligible_reader_group_ids(scope, "full-input.v1", eligible=False)
    enabled = eligible_reader_group_ids(scope, "full-input.v1", eligible=True)
    assert default == graph_group_ids(scope)
    assert set(enabled) - set(default) == set(shadow_group_ids(scope, "full-input.v1"))


def test_real_graph_first_join_accepts_verified_shadow_grounding_and_rejects_mismatch():
    class _Store:
        def list_authorized_cards(self, **kwargs):
            return [{"memory_id": "synthetic-card", "content_hash": "synthetic-hash",
                     "currentness": "current"}]

    resolver = GraphFirstResolver.__new__(GraphFirstResolver)
    resolver._store = _Store()
    request = SimpleNamespace(project="synthetic", as_of=None)
    episode = SimpleNamespace(
        entity_type="GraphFact",
        payload={
            "authority_sources": [
                {"authority_memory_id": "synthetic-card", "content_hash": "synthetic-hash"}
            ],
            "source_entity_types": ["Entity"],
        },
        valid_from=None,
        valid_to=None,
        currentness=None,
    )
    cards, status = resolver._join_graph_candidates(request, (episode,))
    assert status == "verified"
    assert len(cards) == 1

    bad = SimpleNamespace(
        entity_type="GraphFact",
        payload={
            "authority_sources": [
                {"authority_memory_id": "synthetic-card", "content_hash": "changed"}
            ],
            "source_entity_types": ["Entity"],
        },
        valid_from=None,
        valid_to=None,
        currentness=None,
    )
    cards, status = resolver._join_graph_candidates(request, (bad,))
    assert cards == []
    assert status == "mismatch"


def test_real_join_never_exposes_session_non_card_fact():
    resolver = GraphFirstResolver.__new__(GraphFirstResolver)
    resolver._store = SimpleNamespace(list_authorized_cards=lambda **kwargs: [])
    episode = SimpleNamespace(
        entity_type="Session",
        payload={"authority_memory_id": "synthetic-card", "content_hash": "synthetic-hash"},
    )
    cards, _status = resolver._join_graph_candidates(
        SimpleNamespace(project="synthetic", as_of=None), (episode,)
    )
    assert cards == []


# --- privacy gate and free-only fail-closed ---------------------------------

def test_privacy_gate_blocks_synthetic_raw_marker_and_missing_redaction_flag():
    with pytest.raises(PrivacyGateRejected):
        privacy_gate(b"public-prefix RAW-PII-SYNTHETIC",
                     forbidden_markers=(b"RAW-PII-SYNTHETIC",), all_redacted=True)
    with pytest.raises(PrivacyGateRejected):
        privacy_gate(b"public-prefix", forbidden_markers=(), all_redacted=False)
    privacy_gate(b"public-prefix", forbidden_markers=(), all_redacted=True)


def test_free_only_policy_rejects_paid_unknown_and_wrong_endpoint():
    free = ModelPolicy(provider="synthetic-provider", model="free-model",
                       endpoint_class="approved_free", price_status="free_confirmed")
    assert_free_only(free, frozenset({"free-model"}))
    for policy in (
        ModelPolicy("synthetic-provider", "paid-model", "approved_free", "paid"),
        ModelPolicy("synthetic-provider", "free-model", "approved_free", "unknown"),
        ModelPolicy("synthetic-provider", "free-model", "unapproved", "free_confirmed"),
        ModelPolicy("synthetic-provider", "other-free", "approved_free", "free_confirmed"),
    ):
        with pytest.raises(PaidModelRejected):
            assert_free_only(policy, frozenset({"free-model"}))


def test_fallback_selection_never_chooses_paid_candidate():
    candidates = (
        ModelPolicy("p", "paid", "approved_free", "paid"),
        ModelPolicy("p", "free-b", "approved_free", "free_confirmed"),
        ModelPolicy("p", "unknown", "approved_free", "unknown"),
    )
    assert choose_fallback(candidates, frozenset({"free-b"})).model == "free-b"
    assert choose_fallback(candidates, frozenset()) is None


def test_opencode_go_identity_rejects_vertex_and_model_only_claim():
    from agent_knowledge.llm_brain_core.bulk_full_input import (
        PaidModelRejected,
        ProviderIdentityRejected,
        enforce_opencode_go_hy4_flash_free,
    )
    from agent_knowledge.llm_brain_core.bulk_semantic import (
        OpenAICompatibleBulkSemanticExtractor,
    )

    allowlist = frozenset({"hy4-flash-free"})
    good = OpenAICompatibleBulkSemanticExtractor(
        base_url="https://opencode.ai/zen/go/v1",
        model="hy4-flash-free",
        post_fn=lambda *args, **kwargs: '{"sessions": []}',
    )
    enforce_opencode_go_hy4_flash_free(good, allowlist=allowlist)

    vertex = OpenAICompatibleBulkSemanticExtractor(
        base_url="https://vertex.example.invalid/v1",
        model="hy4-flash-free",
        post_fn=lambda *args, **kwargs: '{"sessions": []}',
    )
    with pytest.raises(ProviderIdentityRejected):
        enforce_opencode_go_hy4_flash_free(vertex, allowlist=allowlist)

    wrong_model = OpenAICompatibleBulkSemanticExtractor(
        base_url="https://opencode.ai/zen/go/v1",
        model="paid-model",
        post_fn=lambda *args, **kwargs: '{"sessions": []}',
    )
    with pytest.raises(PaidModelRejected):
        enforce_opencode_go_hy4_flash_free(wrong_model, allowlist=allowlist)



def test_full_chunk_input_preserves_front_middle_end_without_compaction():
    body = "FRONT" + "A" * 3000 + "MIDDLE" + "B" * 3000 + "END"
    builder = _build([_doc(0, body)])
    text = make_full_chunk_session_input(builder=builder, ordinal=0, max_bytes=20000)
    assert "FRONT" in text and "MIDDLE" in text and "END" in text
    assert "[...]" not in text


def test_oversize_chunk_fails_closed_instead_of_silent_truncation():
    builder = _build([_doc(0, "A" * 500)])
    with pytest.raises(ChunkTooLarge):
        make_full_chunk_session_input(builder=builder, ordinal=0, max_bytes=100)


def test_privacy_gate_runs_before_model_input_is_rendered():
    builder = _build([_doc(0, "public " + "RAW-PII-SYNTHETIC")])
    with pytest.raises(PrivacyGateRejected):
        make_full_chunk_session_input(
            builder=builder,
            ordinal=0,
            max_bytes=2000,
            forbidden_markers=(b"RAW-PII-SYNTHETIC",),
        )

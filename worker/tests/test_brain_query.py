from __future__ import annotations

from agent_knowledge.session_memory.brain_query import (
    project_from_brain_id,
    run_brain_query,
    run_brain_query_v2,
)


class _EmptyReadModel:
    def get_card_meta(self, card_id):
        return None

    def list_recent_cards(self, *, project, limit):
        return []

    def list_project_card_counts(self):
        return []


# --- 파라미터 검증 / 에러 contract ---


def test_project_from_brain_id():
    assert project_from_brain_id("/project/workspace-x") == "workspace-x"
    assert project_from_brain_id("/task/t1") is None
    assert project_from_brain_id("") is None
    assert project_from_brain_id("/project/") is None


def test_unsupported_brain_id_error():
    result = run_brain_query(read_model=_EmptyReadModel(), brain_id="/task/t1", query="q")
    assert result["error"]["code"] == "unsupported_brain_id"
    assert result["results"] == []


def test_mode_not_implemented_error():
    result = run_brain_query(
        read_model=_EmptyReadModel(), brain_id="/project/p", query="q", mode="archive"
    )
    assert result["error"]["code"] == "mode_not_implemented"


def test_reserved_params_fail_closed():
    for kwargs in ({"time": "2026"}, {"privacy": "private"}):
        result = run_brain_query(
            read_model=_EmptyReadModel(), brain_id="/project/p", query="q", **kwargs
        )
        assert result["error"]["code"] == "param_not_implemented"


def test_empty_query_error():
    result = run_brain_query(read_model=_EmptyReadModel(), brain_id="/project/p", query="  ")
    assert result["error"]["code"] == "invalid_query"


def test_limit_clamped_to_bounds():
    # limit 0/음수/99는 에러가 아니라 1..10으로 clamp (기존 tool들의 1-10 cap 관례)
    for limit in (0, -3, 99):
        result = run_brain_query(
            read_model=_EmptyReadModel(), brain_id="/project/p", query="q", limit=limit
        )
        assert "error" not in result


def test_empty_project_brain_id_is_unsupported():
    result = run_brain_query(read_model=_EmptyReadModel(), brain_id="/project/", query="q")
    assert result["error"]["code"] == "unsupported_brain_id"


def test_query_normalization_whitespace_and_600_cap():
    from agent_knowledge.session_memory.brain_query import _normalize_query

    assert _normalize_query("  a \n\t b  ") == "a b"
    assert len(_normalize_query("x" * 700)) == 600  # MAX_QUERY_CHARS 절사


# --- dedup ---


def _hit(tag, message_type="semantic", score=None, content="c"):
    return {
        "kind": "native_memory",
        "session_tag": tag,
        "brain_id": "/project/p",
        "message_type": message_type,
        "content": content,
        "score": score,
        "tier": "low",
    }


def test_dedupe_prefers_non_raw_then_score():
    from agent_knowledge.session_memory.brain_query import dedupe_native_hits

    hits = [
        _hit("mem:a", message_type="raw", score=0.9),
        _hit("mem:a", message_type="semantic", score=0.5),
        _hit("mem:b", message_type="semantic", score=0.3),
        _hit("mem:b", message_type="procedural", score=0.7),
    ]
    deduped = dedupe_native_hits(hits)
    assert [h["session_tag"] for h in deduped] == ["mem:a", "mem:b"]
    assert deduped[0]["message_type"] == "semantic"  # non-raw 우선 (score 0.9 raw 탈락)
    assert deduped[1]["score"] == 0.7  # non-raw끼리는 score 최고


def test_dedupe_score_none_is_lowest_and_keeps_input_order():
    from agent_knowledge.session_memory.brain_query import dedupe_native_hits

    hits = [
        _hit("mem:a", score=None),
        _hit("mem:a", score=0.1),
        _hit("mem:c", score=None),
        _hit("mem:c", score=None, content="second-tie-loses"),
    ]
    deduped = dedupe_native_hits(hits)
    assert deduped[0]["score"] == 0.1
    # 완전 동률(tie)은 입력 순서 첫 항목 유지
    assert deduped[1]["content"] == "c"


# --- envelope 빌더 ---


def _card(**over):
    card = {
        "memory_id": "mem_abc",
        "card_type": "procedural_rule",
        "project": "p",
        "state": "active",
        "approved_at": "2026-06-10T00:00:00+00:00",
        "supersedes": "",
        "summary": "uv run을 쓴다",
    }
    card.update(over)
    return card


def test_envelope_has_all_six_ux_signals():
    from agent_knowledge.session_memory.brain_query import build_card_envelope

    env = build_card_envelope(
        brain_id="/project/p", card=_card(), why="semantic_match(score=0.8)", demote=False
    )
    for field in (
        "brain_id", "result_type", "summary", "why_retrieved", "source_ref",
        "observed_at", "freshness", "approval_state", "privacy", "confidence", "conflicts",
    ):
        assert field in env, field
    assert env["result_type"] == "memory_card"
    assert env["source_ref"] == "mem_abc"
    assert env["observed_at"] == "2026-06-10T00:00:00+00:00"  # Phase 1 = approved_at
    assert env["freshness"] == "current"
    assert env["approval_state"] == "approved"
    assert env["privacy"] == "redacted"
    assert env["conflicts"] == []


def test_confidence_rules():
    from agent_knowledge.session_memory.brain_query import build_card_envelope

    # high-risk(명시 승인) → high, 강등 시 medium
    high = build_card_envelope(
        brain_id="/project/p", card=_card(card_type="user_preference"), why="w", demote=False
    )
    assert high["confidence"] == "high"
    demoted = build_card_envelope(
        brain_id="/project/p", card=_card(card_type="user_preference"), why="w", demote=True
    )
    assert demoted["confidence"] == "medium"
    # low-risk 자동승인 → medium, 강등 시 low
    med = build_card_envelope(brain_id="/project/p", card=_card(), why="w", demote=False)
    assert med["confidence"] == "medium"
    med_demoted = build_card_envelope(brain_id="/project/p", card=_card(), why="w", demote=True)
    assert med_demoted["confidence"] == "low"
    # 미지정/알 수 없는 card_type → low (fail-closed), 강등해도 low 유지
    unknown = build_card_envelope(
        brain_id="/project/p", card=_card(card_type="mystery"), why="w", demote=False
    )
    assert unknown["confidence"] == "low"


def test_conflicts_from_supersedes():
    from agent_knowledge.session_memory.brain_query import build_card_envelope

    env = build_card_envelope(
        brain_id="/project/p", card=_card(supersedes="mem_old"), why="w", demote=False
    )
    assert env["conflicts"] == [{"superseded": "mem_old"}]


def test_summary_is_redacted_and_bounded():
    from agent_knowledge.session_memory.brain_query import build_card_envelope
    from agent_knowledge.session_memory.transcript_model import (
        MAX_TRANSCRIPT_SNIPPET_CHARS,
        TRUNCATED_TEXT_MARKER,
    )

    # strict redactor(redact_and_bound_evidence_text)는 일반 /Users/ 경로를 마스킹한다.
    # 입력을 MAX_TRANSCRIPT_SNIPPET_CHARS 초과로 만들어 절사도 실증한다.
    env = build_card_envelope(
        brain_id="/project/p", card=_card(summary="path /Users/example/secret " + "x" * 1100),
        why="w", demote=False,
    )
    assert "/Users/example" not in env["summary"]
    assert len(env["summary"]) <= MAX_TRANSCRIPT_SNIPPET_CHARS
    assert TRUNCATED_TEXT_MARKER in env["summary"]


def test_public_card_redacts_title_and_confidence_basis():
    # defense-in-depth: 이미 저장된 legacy card 의 title/confidence_basis 에 사적 경로가
    # 들어 있어도 brain_query 읽기 표면으로 raw 가 새지 않는다.
    from agent_knowledge.session_memory.brain_query import _normalize_query_memory_card

    env = _normalize_query_memory_card(
        brain_id="/project/p",
        card=_card(
            title="/Users/example/.ssh/id_rsa",
            confidence_basis="proof at /Volumes/usb/secret",
        ),
    )
    assert "/Users/example" not in env["title"]
    assert "/Volumes/usb" not in env["confidence_basis"]


def test_session_tag_included_when_hit_present():
    from agent_knowledge.session_memory.brain_query import build_card_envelope

    env = build_card_envelope(
        brain_id="/project/p", card=_card(), why="w", demote=False,
        hit={"session_tag": "mem:mem_abc"},
    )
    assert env["session_tag"] == "mem:mem_abc"


# --- run_brain_query: semantic 경로 ---


class _CardReadModel:
    def __init__(self, cards):
        self._cards = {c["memory_id"]: c for c in cards}
        self.recent_calls = []

    def get_card_meta(self, card_id):
        return self._cards.get(card_id)

    def list_recent_cards(self, *, project, limit):
        self.recent_calls.append((project, limit))
        return [c for c in self._cards.values() if c.get("project") == project][:limit]

    def list_project_card_counts(self):
        return []


class _AcceptedCardReadModel(_CardReadModel):
    def __init__(self, cards):
        super().__init__(cards)
        self.accepted_calls = []

    def list_accepted_cards(self, *, project, limit):
        self.accepted_calls.append((project, limit))
        return [c for c in self._cards.values() if c.get("project") == project][:limit]


def test_semantic_path_builds_envelopes_and_audit():
    cards = [_card(memory_id="mem_a"), _card(memory_id="mem_b", card_type="user_preference")]
    read_model = _CardReadModel(cards)

    def semantic(query, brain_id):
        assert brain_id == "/project/p"
        return [
            _hit("mem:mem_a", score=0.9),
            _hit("mem:mem_a", message_type="raw", score=0.95),  # dedup 대상
            _hit("mem:mem_b", score=0.4),
        ]

    result = run_brain_query(
        read_model=read_model, semantic_recall=semantic, brain_id="/project/p", query="q"
    )
    assert "error" not in result
    assert [e["source_ref"] for e in result["results"]] == ["mem_a", "mem_b"]
    assert result["results"][0]["why_retrieved"] == "semantic_match(score=0.9)"
    assert result["results"][0]["confidence"] == "medium"  # low-risk, 강등 없음
    assert result["results"][1]["confidence"] == "high"
    assert result["audit"]["path"] == "native_semantic"
    assert result["audit"]["native_memory_bound"] is True
    assert result["audit"]["dropped_hits"] == 0
    assert result["audit"]["query_hash"]


def test_semantic_hit_without_card_is_dropped_fail_closed():
    read_model = _CardReadModel([_card(memory_id="mem_a")])

    def semantic(query, brain_id):
        return [_hit("mem:mem_a", score=0.9), _hit("mem:mem_ghost", score=0.8)]

    result = run_brain_query(
        read_model=read_model, semantic_recall=semantic, brain_id="/project/p", query="q"
    )
    assert [e["source_ref"] for e in result["results"]] == ["mem_a"]
    assert result["audit"]["dropped_hits"] == 1


def test_semantic_hit_with_inactive_card_is_dropped():
    read_model = _CardReadModel([_card(memory_id="mem_a", state="superseded")])

    def semantic(query, brain_id):
        return [_hit("mem:mem_a", score=0.9)]

    result = run_brain_query(
        read_model=read_model, semantic_recall=semantic, brain_id="/project/p", query="q"
    )
    assert result["results"] == []
    assert result["audit"]["dropped_hits"] == 1


def test_score_none_demotes_confidence():
    read_model = _CardReadModel([_card(memory_id="mem_a", card_type="user_preference")])

    def semantic(query, brain_id):
        return [_hit("mem:mem_a", score=None)]

    result = run_brain_query(
        read_model=read_model, semantic_recall=semantic, brain_id="/project/p", query="q"
    )
    env = result["results"][0]
    assert env["why_retrieved"] == "semantic_match(score=none)"
    assert env["confidence"] == "medium"  # high에서 강등


def test_limit_applies_after_dedup():
    cards = [_card(memory_id=f"mem_{i}") for i in range(5)]
    read_model = _CardReadModel(cards)

    def semantic(query, brain_id):
        return [_hit(f"mem:mem_{i}", score=0.5) for i in range(5)]

    result = run_brain_query(
        read_model=read_model, semantic_recall=semantic, brain_id="/project/p", query="q", limit=2
    )
    assert len(result["results"]) == 2


# --- run_brain_query: ledger fallback ---


def test_unbound_native_falls_back_to_ledger_recent():
    read_model = _CardReadModel([_card(memory_id="mem_a", card_type="user_preference")])
    result = run_brain_query(read_model=read_model, brain_id="/project/p", query="q")
    assert result["audit"]["path"] == "ledger_fallback"
    assert result["audit"]["native_memory_bound"] is False
    env = result["results"][0]
    assert env["why_retrieved"] == "ledger_recent"
    assert env["confidence"] == "medium"  # high에서 강등
    assert "session_tag" not in env


def test_semantic_empty_falls_back_to_ledger():
    read_model = _CardReadModel([_card(memory_id="mem_a")])
    result = run_brain_query(
        read_model=read_model, semantic_recall=lambda q, b: [], brain_id="/project/p", query="q"
    )
    assert result["audit"]["path"] == "native_empty_fallback"
    assert [e["source_ref"] for e in result["results"]] == ["mem_a"]


def test_semantic_error_falls_back_to_ledger():
    read_model = _CardReadModel([_card(memory_id="mem_a")])

    def broken(query, brain_id):
        raise RuntimeError("retired_index_bridge down")

    result = run_brain_query(
        read_model=read_model, semantic_recall=broken, brain_id="/project/p", query="q"
    )
    assert result["audit"]["path"] == "native_error_fallback"
    assert [e["source_ref"] for e in result["results"]] == ["mem_a"]


def test_fallback_skips_non_active_cards():
    read_model = _CardReadModel(
        [_card(memory_id="mem_a"), _card(memory_id="mem_b", state="disabled")]
    )
    result = run_brain_query(read_model=read_model, brain_id="/project/p", query="q")
    assert [e["source_ref"] for e in result["results"]] == ["mem_a"]


def test_zero_results_is_normal_response():
    result = run_brain_query(read_model=_EmptyReadModel(), brain_id="/project/p", query="q")
    assert result["results"] == []
    assert "error" not in result


def test_non_list_semantic_return_is_fail_closed_empty():
    # 계약 외 반환(None/dict/str)은 raise 없이 native_empty_fallback로 흡수
    read_model = _CardReadModel([_card(memory_id="mem_a")])
    for bogus in (None, {"k": "v"}, "abc"):
        result = run_brain_query(
            read_model=read_model, semantic_recall=lambda q, b, v=bogus: v,
            brain_id="/project/p", query="q",
        )
        assert result["audit"]["path"] == "native_empty_fallback"
        assert [e["source_ref"] for e in result["results"]] == ["mem_a"]


def test_limit_cutoff_keeps_top_score_regardless_of_input_order():
    # upstream이 score 역순으로 줘도 컷오프는 상위 score를 보존
    cards = [_card(memory_id="mem_low"), _card(memory_id="mem_high")]
    read_model = _CardReadModel(cards)

    def semantic(query, brain_id):
        return [_hit("mem:mem_low", score=0.1), _hit("mem:mem_high", score=0.99)]

    result = run_brain_query(
        read_model=read_model, semantic_recall=semantic,
        brain_id="/project/p", query="q", limit=1,
    )
    assert [e["source_ref"] for e in result["results"]] == ["mem_high"]


def test_brain_query_v2_ranks_accepted_cards_by_query_terms_before_limit_cutoff():
    # Regression for eval quality: v2 must not return only the most recent accepted
    # cards before query ranking, because older but exact expected cards then miss recall.
    read_model = _AcceptedCardReadModel(
        [
            _card(memory_id="mem_recent_noise", summary="general operating note"),
            _card(memory_id="mem_exact_expected", summary="needle ranking eval target"),
        ]
    )

    result = run_brain_query_v2(
        read_model=read_model,
        brain_id="/project/p",
        query="needle ranking eval target",
        query_intent="eval",
        limit=1,
    )

    assert [item["memory_id"] for item in result["results"]] == ["mem_exact_expected"]
    assert read_model.accepted_calls == [("p", 50)]


def test_brain_query_v2_does_not_fill_limit_with_weak_lexical_matches():
    # Precision regression: if the exact expected card is the only strong lexical
    # match, v2 should not pad the context with weakly-related accepted cards.
    read_model = _AcceptedCardReadModel(
        [
            _card(memory_id="mem_recent_weak", summary="needle unrelated note"),
            _card(memory_id="mem_recent_noise", summary="general operating note"),
            _card(memory_id="mem_exact_expected", summary="needle ranking eval target"),
        ]
    )

    result = run_brain_query_v2(
        read_model=read_model,
        brain_id="/project/p",
        query="needle ranking eval target",
        query_intent="eval",
        limit=5,
    )

    assert [item["memory_id"] for item in result["results"]] == ["mem_exact_expected"]


def test_brain_query_v2_uses_eval_query_terms_to_drop_single_phrase_noise():
    # A card that only matches one broad phrase such as "live mutation" should not
    # be returned when the eval query also names more specific phrases.
    read_model = _AcceptedCardReadModel(
        [
            _card(
                memory_id="mem_broad_live_mutation_noise",
                card_type="decision",
                summary="OCI app-plane context mentions live mutation but not the gate policy.",
            ),
            _card(
                memory_id="mem_expected_gate",
                card_type="decision",
                summary="Dry-run work must not perform live mutation without separate approval gates.",
            ),
        ]
    )

    result = run_brain_query_v2(
        read_model=read_model,
        brain_id="/project/p",
        query="neurons live mutation approval gate dry-run",
        query_terms=["neurons", "live mutation", "approval gate", "dry-run"],
        query_intent="eval",
        limit=5,
    )

    assert [item["memory_id"] for item in result["results"]] == ["mem_expected_gate"]


def test_brain_query_v2_keeps_token_covered_expected_cards_when_only_one_phrase_matches():
    # If only one card has a single phrase match, do not use that weak signal to
    # drop another expected card with strong token coverage.
    read_model = _AcceptedCardReadModel(
        [
            _card(
                memory_id="mem_phrase_expected",
                summary="neurons authoritative MemoryCard accepted current",
            ),
            _card(
                memory_id="mem_token_expected",
                summary="MemoryCard accepted evidence current",
            ),
            _card(memory_id="mem_noise", summary="neurons generic status"),
        ]
    )

    result = run_brain_query_v2(
        read_model=read_model,
        brain_id="/project/p",
        query="neurons authoritative MemoryCard accepted current",
        query_terms=["neurons", "authoritative", "MemoryCard", "accepted current"],
        query_intent="eval",
        limit=5,
    )

    assert [item["memory_id"] for item in result["results"]] == [
        "mem_phrase_expected",
        "mem_token_expected",
    ]


def test_brain_query_v2_uses_injected_semantic_ranker_for_eval_candidates():
    read_model = _AcceptedCardReadModel(
        [
            _card(memory_id="mem_low_vector", summary="needle ranking eval target"),
            _card(memory_id="mem_high_vector", summary="needle ranking eval target"),
        ]
    )
    calls = []

    def semantic_ranker(**kwargs):
        calls.append(kwargs)
        ranked = []
        for card in kwargs["cards"]:
            copy = dict(card)
            copy["_semantic_score"] = 0.99 if card["memory_id"] == "mem_high_vector" else 0.10
            ranked.append(copy)
        return sorted(ranked, key=lambda item: item["_semantic_score"], reverse=True)

    result = run_brain_query_v2(
        read_model=read_model,
        brain_id="/project/p",
        query="needle ranking eval target",
        query_terms=["needle ranking", "eval target"],
        query_intent="eval",
        limit=1,
        semantic_ranker=semantic_ranker,
    )

    assert calls and calls[0]["query"] == "needle ranking eval target"
    assert len(calls[0]["cards"]) == 2
    assert [item["memory_id"] for item in result["results"]] == ["mem_high_vector"]
    assert result["audit"]["semantic_ranker_used"] is True


# --- brain.resolve ---


class _CountReadModel(_EmptyReadModel):
    def list_project_card_counts(self):
        return [("workspace-index-advisor", 3), ("workspace-stocks", 1), ("", 2)]


def test_resolve_lists_project_brain_ids_with_counts():
    from agent_knowledge.session_memory.brain_query import resolve_brain_ids

    result = resolve_brain_ids(read_model=_CountReadModel())
    assert result["candidates"] == [
        {"brain_id": "/project/workspace-index-advisor", "kind": "project", "card_count": 3, "hint": ""},
        {"brain_id": "/project/workspace-stocks", "kind": "project", "card_count": 1, "hint": ""},
    ]  # 빈 project는 제외


def test_resolve_query_substring_match():
    from agent_knowledge.session_memory.brain_query import resolve_brain_ids

    result = resolve_brain_ids(read_model=_CountReadModel(), query="stocks")
    assert [c["brain_id"] for c in result["candidates"]] == ["/project/workspace-stocks"]


def test_resolve_no_match_returns_empty():
    from agent_knowledge.session_memory.brain_query import resolve_brain_ids

    result = resolve_brain_ids(read_model=_CountReadModel(), query="zzz")
    assert result["candidates"] == []

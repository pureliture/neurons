import json

from agent_knowledge.session_memory.memory_miner import FakeMemoryMiner, LlmMemoryMiner, build_ragflow_completion_fn


PROJECT = "workspace-ragflow-advisor"


def _chunk(text, *, knowledge_id="kn_x", content_hash="sha256:x"):
    return {
        "knowledge_id": knowledge_id,
        "content_hash": content_hash,
        "provider": "claude",
        "project": PROJECT,
        "redacted_text": text,
    }


def test_llm_memory_miner_parses_json_array_into_typed_candidates():
    captured = {}

    def fake_completion(messages):
        captured["messages"] = messages
        return json.dumps(
            [
                {"type": "project_decision", "statement": "Keep RAGFlow core unmodified."},
                {"type": "procedural_rule", "statement": "Approval before live mutation."},
            ]
        )

    candidates = LlmMemoryMiner(completion_fn=fake_completion).mine_chunk(_chunk("some session text"))

    assert [c["candidate_type"] for c in candidates] == ["project_decision", "procedural_rule"]
    assert all(c["approval_state"] == "pending" for c in candidates)
    assert all(
        c["evidence_refs"] == [{"knowledge_id": "kn_x", "content_hash": "sha256:x"}] for c in candidates
    )
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][1]["role"] == "user"
    assert "some session text" in captured["messages"][1]["content"]


def test_llm_memory_miner_repeats_json_directive_in_user_message():
    # Live finding: RAGFlow native chat/completions dilutes a system-only JSON
    # directive (deepseek returned prose). Reinforcing it in the user turn makes
    # JSON output reliable, so the user message must carry the directive too.
    captured = {}

    def fake_completion(messages):
        captured["user"] = messages[1]["content"]
        return "[]"

    LlmMemoryMiner(completion_fn=fake_completion).mine_chunk(_chunk("raw session body"))

    user_content = captured["user"]
    assert "raw session body" in user_content
    assert "JSON" in user_content
    assert user_content != "raw session body"


def test_build_ragflow_completion_fn_wires_client_chat_completion():
    calls = {}

    class FakeClient:
        def chat_completion(self, messages, *, llm_id="", stream=False):
            calls["messages"] = messages
            calls["llm_id"] = llm_id
            return json.dumps([{"type": "project_decision", "statement": "Wired through RAGFlow."}])

    completion_fn = build_ragflow_completion_fn(FakeClient())
    candidates = LlmMemoryMiner(completion_fn=completion_fn).mine_chunk(_chunk("body"))

    assert [c["candidate_type"] for c in candidates] == ["project_decision"]
    assert calls["messages"][0]["role"] == "system"
    assert calls["llm_id"] == ""


def test_build_ragflow_completion_fn_passes_llm_id_when_set():
    seen = {}

    class FakeClient:
        def chat_completion(self, messages, *, llm_id="", stream=False):
            seen["llm_id"] = llm_id
            return "[]"

    build_ragflow_completion_fn(FakeClient(), llm_id="m@F")([{"role": "user", "content": "x"}])

    assert seen["llm_id"] == "m@F"


def test_llm_memory_miner_normalizes_short_type_labels_to_canonical():
    # Live deepseek-v4-flash emits short category labels (decision/preference/risk)
    # instead of the canonical enum. Normalize rather than drop.
    def fake_completion(messages):
        return json.dumps(
            [
                {"type": "decision", "statement": "Runtime store lives in the server container."},
                {"type": "preference", "statement": "Source code goes to graphify."},
                {"type": "risk", "statement": "Splitting the ledger god class is too costly."},
                {"type": "totally_unknown", "statement": "drop me"},
            ]
        )

    candidates = LlmMemoryMiner(completion_fn=fake_completion).mine_chunk(_chunk("text"))

    assert [c["candidate_type"] for c in candidates] == [
        "project_decision",
        "user_preference",
        "risk_or_constraint",
    ]


def test_llm_memory_miner_strips_code_fence_wrapping():
    def fake_completion(messages):
        return "```json\n[{\"type\": \"semantic_fact\", \"statement\": \"Embedding runs on Mac mini.\"}]\n```"

    candidates = LlmMemoryMiner(completion_fn=fake_completion).mine_chunk(_chunk("text"))

    assert [c["candidate_type"] for c in candidates] == ["semantic_fact"]


def test_llm_memory_miner_returns_empty_on_invalid_json_without_raising():
    candidates = LlmMemoryMiner(completion_fn=lambda messages: "sorry, no JSON here").mine_chunk(_chunk("text"))

    assert candidates == []


def test_llm_memory_miner_drops_unsupported_or_empty_candidates_fail_closed():
    def fake_completion(messages):
        return json.dumps(
            [
                {"type": "not_a_real_type", "statement": "ignored"},
                {"type": "project_decision", "statement": "   "},
                {"type": "tool_skill", "statement": "Use uv for Python."},
            ]
        )

    candidates = LlmMemoryMiner(completion_fn=fake_completion).mine_chunk(_chunk("text"))

    assert [c["candidate_type"] for c in candidates] == ["tool_skill"]


def test_llm_memory_miner_respects_max_candidates_cap():
    def fake_completion(messages):
        return json.dumps([{"type": "semantic_fact", "statement": f"fact {i}"} for i in range(10)])

    candidates = LlmMemoryMiner(completion_fn=fake_completion, max_candidates=3).mine_chunk(_chunk("text"))

    assert len(candidates) == 3


def test_llm_memory_miner_batch_dry_run_emits_candidates_without_live_write():
    calls = {"completion": 0}

    def fake_completion(messages):
        calls["completion"] += 1
        return json.dumps([{"type": "project_decision", "statement": "Whole runtime store lives in the container."}])

    miner = LlmMemoryMiner(completion_fn=fake_completion)
    docs = [_chunk("a", knowledge_id="kn_a", content_hash="sha256:a"), _chunk("b", knowledge_id="kn_b", content_hash="sha256:b")]

    all_candidates = [c for doc in docs for c in miner.mine_chunk(doc)]

    assert len(all_candidates) == 2
    assert calls["completion"] == 2
    # dry-run: miner only calls the injected completion_fn, never a RAGFlow dataset write client.
    assert all(c["approval_state"] == "pending" for c in all_candidates)


def test_llm_memory_miner_bounds_statement_via_build_memory_candidate():
    long_statement = "decision " * 100  # > 240 chars

    def fake_completion(messages):
        return json.dumps([{"type": "project_decision", "statement": long_statement}])

    candidates = LlmMemoryMiner(completion_fn=fake_completion).mine_chunk(_chunk("text"))

    assert len(candidates) == 1
    assert len(candidates[0]["statement"]) <= 240


def test_fake_memory_miner_extracts_bounded_candidates_from_transcript_chunk():
    chunk = {
        "knowledge_id": "kn_chunk",
        "content_hash": "sha256:chunk",
        "provider": "claude",
        "project": PROJECT,
        "redacted_text": "\n".join(
            [
                "Preference: User wants implementation and runtime verification progress separated.",
                "Decision: Keep RAGFlow core unmodified.",
                "Rule: Do not run live scheduler mutation without approval.",
                "Ignored raw body: " + ("A" * 5000),
            ]
        ),
    }

    candidates = FakeMemoryMiner(max_candidates=3).mine_chunk(chunk)

    assert [candidate["candidate_type"] for candidate in candidates] == [
        "user_preference",
        "project_decision",
        "procedural_rule",
    ]
    assert all(candidate["approval_state"] == "pending" for candidate in candidates)
    assert all(len(candidate["statement"]) <= 240 for candidate in candidates)
    assert all(candidate["evidence_refs"] == [{"knowledge_id": "kn_chunk", "content_hash": "sha256:chunk"}] for candidate in candidates)
    serialized = json.dumps(candidates, sort_keys=True)
    assert "AAAAA" not in serialized
    assert "raw body" not in serialized.lower()


def test_fake_memory_miner_marks_sensitive_profile_candidates_manual_only():
    chunk = {
        "knowledge_id": "kn_profile",
        "content_hash": "sha256:profile",
        "provider": "claude",
        "project": PROJECT,
        "redacted_text": "Profile: User lives in a private location and prefers Korean answers.",
    }

    candidates = FakeMemoryMiner(max_candidates=5).mine_chunk(chunk)

    assert candidates[0]["candidate_type"] == "user_preference"
    assert candidates[0]["sensitivity"] == "profile_changing"
    assert candidates[0]["requires_manual_approval"] is True

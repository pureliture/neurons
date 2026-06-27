from agent_knowledge.llm_brain_core.repo_style_profile import repo_style_profile_from_memory_cards

from test_context_authority_pack import _card


def test_repo_style_profile_links_claims_to_files_commits_sessions_and_repo_scope():
    style = _card(
        "mem_style",
        "repo_style",
        "Tests should use uv run pytest",
        {
            "claim": "Python worker tests use uv run pytest.",
            "repo_scope": "neurons/worker",
            "files": ["worker/tests/test_context_authority_pack.py"],
            "commits": ["commit:abc123"],
            "sessions": ["session:turn-10994"],
            "reason": "Observed repeated project workflow evidence.",
        },
    )
    preference = _card(
        "mem_pref",
        "preference",
        "Korean response preference",
        {"preference": "자연어 응답은 한국어로 작성한다.", "applies_to": "natural_language_response"},
    )
    historical = _card(
        "mem_old_code",
        "decision",
        "Old code happened to use a pattern",
        {"decision": "Old code happened to use a pattern once."},
    )

    profile = repo_style_profile_from_memory_cards(
        [style, preference, historical],
        repository="neurons",
    )

    assert profile == {
        "schema_version": "repo_style_profile.v1",
        "repository": "neurons",
        "claims": [
            {
                "memory_id": "mem_style",
                "claim": "Python worker tests use uv run pytest.",
                "repo_scope": "neurons/worker",
                "reason": "Observed repeated project workflow evidence.",
                "confidence": 0.9,
                "files": ["worker/tests/test_context_authority_pack.py"],
                "commits": ["commit:abc123"],
                "sessions": ["session:turn-10994"],
                "evidence_refs": ["mem_style", "worker/tests/test_context_authority_pack.py", "commit:abc123", "session:turn-10994"],
            }
        ],
        "ignored_inputs": [
            {"memory_id": "mem_pref", "reason": "user_preference_not_repo_style"},
            {"memory_id": "mem_old_code", "reason": "insufficient_style_authority"},
        ],
    }

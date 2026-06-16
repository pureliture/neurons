from __future__ import annotations

from agent_knowledge.couchdb_source.project_authority import (
    ProjectAuthorityInput,
    ProjectAuthoritySource,
    resolve_project,
)


def test_capture_metadata_is_authoritative() -> None:
    res = resolve_project(
        ProjectAuthorityInput(
            capture_metadata_project="neurons",
            provider_source_path="/Users/dev/Projects/dendrite/x.jsonl",
        )
    )
    assert res.project == "neurons"
    assert res.source == ProjectAuthoritySource.CAPTURE_METADATA
    # capture metadata wins; lower-tier disagreement is noted but not ambiguous
    assert res.ambiguous is False
    assert "tier_conflict" in res.notes
    assert res.eligible_for_retirement is True


def test_provider_path_resolves_when_no_capture_metadata() -> None:
    res = resolve_project(
        ProjectAuthorityInput(provider_source_path="/Users/dev/Projects/neurons/.git/x")
    )
    assert res.project == "neurons"
    assert res.source == ProjectAuthoritySource.PROVIDER_PATH
    assert res.ambiguous is False


def test_conflicting_nonauthoritative_signals_are_ambiguous() -> None:
    res = resolve_project(
        ProjectAuthorityInput(
            provider_source_path="/Users/dev/Projects/neurons/x",
            cwd="/Users/dev/Projects/dendrite",
        )
    )
    assert res.ambiguous is True
    assert res.eligible_for_retirement is False
    assert set(res.candidates) == {"neurons", "dendrite"}
    assert res.project == "neurons"  # highest present tier still wins as label


def test_server_inference_only_when_no_direct_signal() -> None:
    res = resolve_project(
        ProjectAuthorityInput(),
        server_inference=lambda: "inferred-project",
    )
    assert res.project == "inferred-project"
    assert res.source == ProjectAuthoritySource.SERVER_INFERENCE
    assert res.ambiguous is False
    assert "server_inference_only" in res.notes


def test_no_signal_is_unresolved_and_excluded() -> None:
    res = resolve_project(ProjectAuthorityInput())
    assert res.project == ""
    assert res.source == ProjectAuthoritySource.UNRESOLVED
    assert res.ambiguous is True
    assert res.eligible_for_retirement is False


def test_ragflow_mismatch_is_reported_not_trusted() -> None:
    res = resolve_project(
        ProjectAuthorityInput(
            capture_metadata_project="neurons",
            ragflow_project_hint="wrong-project",
        )
    )
    assert res.project == "neurons"
    assert res.ragflow_mismatch is True
    assert "ragflow_project_mismatch" in res.notes


def test_authority_block_is_public_safe() -> None:
    res = resolve_project(
        ProjectAuthorityInput(provider_source_path="/Users/dev/Projects/neurons/x")
    )
    block = res.to_authority_block()
    # only canonical labels leave; no raw path
    assert "/Users/" not in str(block)
    assert block["project"] == "neurons"
    assert block["eligible_for_retirement"] is True

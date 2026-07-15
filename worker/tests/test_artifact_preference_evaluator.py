from __future__ import annotations

import asyncio
import copy
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_knowledge.llm_brain_core.objects import artifact_preference_evaluator
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.cli import main
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import dispatch_tool_call
from agent_knowledge.llm_brain_core.objects.artifact_preference_evaluator import (
    artifact_preference_application_receipt_is_valid,
)
from agent_knowledge.llm_brain_core.objects.post_deploy_mcp_capture import (
    collect_source_to_candidate_post_deploy_mcp_capture,
)
from agent_knowledge.llm_brain_core.objects.runtime_readiness import (
    build_source_to_candidate_runtime_post_deploy_capture_packet,
    build_source_to_candidate_runtime_post_deploy_capture_readiness_report,
)
from agent_knowledge.public_safe_util import hash_payload


PROJECT = "neurons"
REPOSITORY = "pureliture/neurons"
BRANCH = "main"
TARGET_OBJECT_ID = "ko:ArtifactPreference:p7-html-review-density"
PROPOSAL_ID = "proposal:p7-html-review-density"
DECISION_ID = "decision:p7-html-review-density"
SOURCE_CONTENT_HASH = "sha256:" + "a" * 64


def _service_with_current_artifact_preference(tmp_path: Path) -> KnowledgeSearchService:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    ledger = Ledger(private / "ledger.sqlite")
    proposed_object = {
        "schema_version": "artifact_preference_proposed_object_snapshot.v1",
        "object_id": TARGET_OBJECT_ID,
        "object_type": "ArtifactPreference",
        "scope": {"project": PROJECT},
        "title": "Evidence-dense HTML review artifacts",
        "summary": "Prefer HTML review artifacts with objects, relationships, evidence, and gate status.",
        "content_hash": SOURCE_CONTENT_HASH,
        "evidence_refs": ["memory:p7-html-review-density"],
        "source_refs": [],
        "confidence": {"score": 0.98, "basis": "Reviewed preference authority."},
        "privacy_class": "public_safe",
        "payload": {
            "preference": "Prefer evidence-dense HTML review artifacts.",
            "scope": "project",
            "applies_to": "html_review_artifact",
            "reason": "Review artifacts should expose decision evidence.",
            "exceptions": [],
            "explicitness": "explicit",
            "repeated_count": 1,
            "confirmation_status": "confirmed",
            "currentness": "current",
            "artifact_memory_kind": "preference",
            "source_memory_id": "memory:p7-html-review-density",
            "raw_return_capability": "denied",
        },
    }
    ledger.upsert_object_review_proposal(
        {
            "schema_version": "object_review_proposal.v1",
            "proposal_id": PROPOSAL_ID,
            "proposal_type": "propose_current",
            "target_object_id": TARGET_OBJECT_ID,
            "object_type": "ArtifactPreference",
            "project": PROJECT,
            "ledger_scope": "production",
            "status": "needs_review",
            "reason": "Reviewed artifact preference is ready for authority.",
            "evidence_refs": ["memory:p7-html-review-density"],
            "proposer": "codex",
            "proposed_object": proposed_object,
        }
    )
    ledger.commit_object_authority_decision(
        {
            "schema_version": "authority_decision.v1",
            "decision_id": DECISION_ID,
            "proposal_id": PROPOSAL_ID,
            "target_object_id": TARGET_OBJECT_ID,
            "project": PROJECT,
            "ledger_scope": "production",
            "decision_type": "accept_current",
            "previous_authority_lane": "proposal_only",
            "new_authority_lane": "accepted_current",
            "decision_reason": "Promote reviewed artifact preference.",
            "evidence_refs": ["memory:p7-html-review-density"],
        }
    )
    ledger.upsert_llm_brain_memory_card(
        {
            "memory_id": "mem_artifact_preference_p7_html_review_density",
            "brain_id": f"/project/{PROJECT}",
            "card_type": "preference",
            "scope": "project",
            "project": PROJECT,
            "provider": "codex",
            "title": proposed_object["title"],
            "summary": proposed_object["summary"],
            "render_text": proposed_object["summary"],
            "lifecycle_state": "human_accepted",
            "judgment_state": "none",
            "status": "accepted",
            "approval_state": "approved",
            "governance_tier": "medium",
            "freshness": "current",
            "currentness": "current",
            "confidence": 0.98,
            "confidence_basis": "Reviewed preference authority.",
            "source_refs": [],
            "evidence_refs": ["memory:p7-html-review-density"],
            "evidence_hashes": [SOURCE_CONTENT_HASH],
            "derived_from": [TARGET_OBJECT_ID, PROPOSAL_ID],
            "supersedes": [],
            "superseded_by": [],
            "conflicts": [],
            "active_until": "",
            "approved_by": "redacted",
            "approved_at": "2026-07-15T00:00:00+00:00",
            "typed_payload": {
                "preference": proposed_object["payload"]["preference"],
                "explicitness": "explicit",
                "repeated_count": 1,
                "confirmation_status": "confirmed",
                "applies_to": "html_review_artifact",
                "reason": proposed_object["payload"]["reason"],
                "exceptions": [],
                "target_object_id": TARGET_OBJECT_ID,
                "source_object_type": "ArtifactPreference",
                "source_content_hash": SOURCE_CONTENT_HASH,
                "authority_proposal_id": PROPOSAL_ID,
                "authority_decision_id": DECISION_ID,
            },
        }
    )
    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
    )


def _valid_artifact_input() -> dict:
    summary = "Deployed HTML review evidence exposes objects, relationships, evidence, and gate status."
    metrics = {
        "object_count": 2,
        "relationship_count": 1,
        "evidence_count": 2,
        "gate_status_count": 1,
        "hidden_gap_count": 0,
        "protected_content_count": 0,
    }
    evidence_refs = ["route:html-preference", "context:style-preference"]
    return {
        "repository": REPOSITORY,
        "branch": BRANCH,
        "project": PROJECT,
        "artifact_type": "html_review_artifact",
        "summary": summary,
        "artifact_fingerprint": hash_payload(
            {
                "artifact_type": "html_review_artifact",
                "summary": summary,
                "metrics": metrics,
                "evidence_refs": evidence_refs,
            }
        ),
        "metrics": metrics,
        "evidence_refs": evidence_refs,
        "consumer": "codex",
    }


def test_evaluate_current_artifact_preference_returns_deterministic_bound_receipt(
    tmp_path: Path,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()

    first = service.brain_artifact_preference_evaluate(**arguments)
    second = service.brain_artifact_preference_evaluate(**arguments)

    assert first == second
    assert first["schema_version"] == "artifact_preference_application_receipt.v1"
    assert first["status"] == "PASS"
    assert first["applied"] is True
    assert first["production_mutation_performed"] is False
    assert first["preference_binding"] == {
        "target_object_id": TARGET_OBJECT_ID,
        "project": PROJECT,
        "memory_id": "mem_artifact_preference_p7_html_review_density",
        "card_content_hash": service.ledger.list_llm_brain_memory_cards(
            project=PROJECT,
            accepted_only=True,
            limit=1,
        )[0]["content_hash"],
        "source_content_hash": SOURCE_CONTENT_HASH,
        "proposal_id": PROPOSAL_ID,
        "decision_id": DECISION_ID,
        "authority_lane": "accepted_current",
    }
    assert first["artifact_binding"]["repository_hash"] == hash_payload(REPOSITORY)
    assert first["artifact_binding"]["branch_hash"] == hash_payload(BRANCH)
    assert first["artifact_binding"]["artifact_fingerprint"] == arguments["artifact_fingerprint"]
    assert first["application_result"] == {
        "evaluator_profile": "html_review_evidence_density_v1",
        "outcome": "pass",
        "passed_rules": [
            "object_count_at_least_one",
            "relationship_count_at_least_one",
            "evidence_count_at_least_one",
            "gate_status_count_at_least_one",
            "hidden_gap_count_zero",
            "protected_content_count_zero",
        ],
        "failed_rules": [],
    }
    assert first["consumer_surface"] == {
        "tool": "brain_artifact_preference_evaluate",
        "version": "v1",
        "consumer": "codex",
    }
    assert first["receipt_hash"] == hash_payload(
        {
            "preference_binding": first["preference_binding"],
            "artifact_binding": first["artifact_binding"],
            "application_result": first["application_result"],
            "consumer_surface": first["consumer_surface"],
        }
    )


def test_artifact_preference_evaluate_cli_uses_explicit_project_and_same_receipt(
    tmp_path: Path,
    capsys,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()

    code = main(
        [
            "artifact-preference-evaluate",
            "--ledger",
            str(service.ledger.path),
            "--repository",
            arguments["repository"],
            "--branch",
            arguments["branch"],
            "--project",
            arguments["project"],
            "--artifact-type",
            arguments["artifact_type"],
            "--summary",
            arguments["summary"],
            "--artifact-fingerprint",
            arguments["artifact_fingerprint"],
            *[
                item
                for key, value in arguments["metrics"].items()
                for item in ("--metric", f"{key}={value}")
            ],
            *[
                item
                for ref in arguments["evidence_refs"]
                for item in ("--evidence-ref", ref)
            ],
            "--consumer",
            arguments["consumer"],
        ]
    )
    receipt = json.loads(capsys.readouterr().out)

    assert code == 0
    assert receipt == service.brain_artifact_preference_evaluate(**arguments)


def _empty_service(tmp_path: Path) -> KnowledgeSearchService:
    private = tmp_path / "empty-private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    return KnowledgeSearchService(
        ledger=Ledger(private / "ledger.sqlite"),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
    )


def _add_second_current_artifact_preference(service: KnowledgeSearchService) -> None:
    proposal = copy.deepcopy(service.ledger.get_object_review_proposal(PROPOSAL_ID))
    target_object_id = "ko:ArtifactPreference:p7-html-review-density-second"
    proposal_id = "proposal:p7-html-review-density-second"
    decision_id = "decision:p7-html-review-density-second"
    proposal.update(
        {
            "proposal_id": proposal_id,
            "target_object_id": target_object_id,
            "status": "needs_review",
        }
    )
    proposal.pop("decision_id", None)
    proposal["proposed_object"]["object_id"] = target_object_id
    service.ledger.upsert_object_review_proposal(proposal)
    service.ledger.commit_object_authority_decision(
        {
            "schema_version": "authority_decision.v1",
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "target_object_id": target_object_id,
            "project": PROJECT,
            "ledger_scope": "production",
            "decision_type": "accept_current",
            "previous_authority_lane": "proposal_only",
            "new_authority_lane": "accepted_current",
            "decision_reason": "Promote second reviewed artifact preference.",
            "evidence_refs": ["memory:p7-html-review-density-second"],
        }
    )
    card = copy.deepcopy(
        service.ledger.list_llm_brain_memory_cards(
            project=PROJECT,
            accepted_only=True,
            limit=20,
        )[0]
    )
    card.update(
        {
            "memory_id": "mem_artifact_preference_p7_html_review_density_second",
            "content_hash": "",
            "card_hash": "",
        }
    )
    card["typed_payload"].update(
        {
            "target_object_id": target_object_id,
            "authority_proposal_id": proposal_id,
            "authority_decision_id": decision_id,
        }
    )
    service.ledger.upsert_llm_brain_memory_card(card)


def _rewrite_current_card(service: KnowledgeSearchService, mutate) -> None:
    card = service.ledger.list_llm_brain_memory_cards(
        project=PROJECT,
        accepted_only=True,
        limit=20,
    )[0]
    mutate(card)
    card.pop("content_hash", None)
    card.pop("card_hash", None)
    service.ledger.upsert_llm_brain_memory_card(card)


def _add_unrelated_accepted_current_cards(
    service: KnowledgeSearchService,
    *,
    count: int,
) -> None:
    template = service.ledger.list_llm_brain_memory_cards(
        project=PROJECT,
        accepted_only=True,
        limit=1,
    )[0]
    for index in range(count):
        card = copy.deepcopy(template)
        card["memory_id"] = f"mem_000_unrelated_preference_{index:03d}"
        card["typed_payload"]["source_object_type"] = "RepoStyle"
        card["typed_payload"]["target_object_id"] = f"ko:RepoStyle:unrelated-{index:03d}"
        card.pop("content_hash", None)
        card.pop("card_hash", None)
        service.ledger.upsert_llm_brain_memory_card(card)


def test_evaluator_fails_closed_when_no_accepted_current_preference(tmp_path: Path):
    receipt = _empty_service(tmp_path).brain_artifact_preference_evaluate(
        **_valid_artifact_input()
    )

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["accepted_current_artifact_preference_missing"]
    assert receipt["gaps"] == []
    assert receipt["application_result"]["outcome"] == "not_evaluated"


def test_artifact_preference_evaluate_cli_exits_nonzero_when_no_current_preference(
    tmp_path: Path,
    capsys,
):
    service = _empty_service(tmp_path)
    arguments = _valid_artifact_input()

    code = main(
        [
            "artifact-preference-evaluate",
            "--ledger",
            str(service.ledger.path),
            "--repository",
            arguments["repository"],
            "--branch",
            arguments["branch"],
            "--project",
            arguments["project"],
            "--artifact-type",
            arguments["artifact_type"],
            "--summary",
            arguments["summary"],
            "--artifact-fingerprint",
            arguments["artifact_fingerprint"],
            *[
                item
                for key, value in arguments["metrics"].items()
                for item in ("--metric", f"{key}={value}")
            ],
            *[
                item
                for ref in arguments["evidence_refs"]
                for item in ("--evidence-ref", ref)
            ],
            "--consumer",
            arguments["consumer"],
        ]
    )
    receipt = json.loads(capsys.readouterr().out)

    assert code == 1
    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False


def test_evaluator_fails_closed_for_multiple_accepted_current_preferences(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)
    _add_second_current_artifact_preference(service)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["multiple_accepted_current_artifact_preferences"]


def test_evaluator_fails_closed_for_stale_preference(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)
    _rewrite_current_card(service, lambda card: card.update({"currentness": "stale"}))

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["artifact_preference_not_current"]


def test_evaluator_reports_unsupported_profile_when_only_other_applies_to_exists(
    tmp_path: Path,
):
    service = _service_with_current_artifact_preference(tmp_path)
    _rewrite_current_card(
        service,
        lambda card: card["typed_payload"].update(
            {"applies_to": "markdown_review_artifact"}
        ),
    )

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["unsupported_artifact_preference_profile"]


def test_evaluator_reports_missing_when_only_malformed_target_object_type_exists(
    tmp_path: Path,
):
    service = _service_with_current_artifact_preference(tmp_path)
    _rewrite_current_card(
        service,
        lambda card: card["typed_payload"].update(
            {"target_object_id": "ko:RepoStyle:malformed"}
        ),
    )

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["accepted_current_artifact_preference_missing"]


def test_evaluator_reports_unsupported_when_newer_malformed_card_precedes_other_profile(
    tmp_path: Path,
):
    service = _service_with_current_artifact_preference(tmp_path)
    _rewrite_current_card(
        service,
        lambda card: card["typed_payload"].update(
            {"applies_to": "markdown_review_artifact"}
        ),
    )
    valid_other_profile = service.ledger.list_llm_brain_memory_cards(
        project=PROJECT,
        accepted_only=True,
        limit=1,
    )[0]
    malformed = copy.deepcopy(valid_other_profile)
    malformed["memory_id"] = "mem_000_malformed_artifact_preference"
    malformed["approved_at"] = "2027-01-01T00:00:00+00:00"
    malformed["typed_payload"]["target_object_id"] = "ko:RepoStyle:malformed"
    malformed.pop("content_hash", None)
    malformed.pop("card_hash", None)
    service.ledger.upsert_llm_brain_memory_card(malformed)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["unsupported_artifact_preference_profile"]


@pytest.mark.parametrize(
    ("mutation", "expected_failure"),
    [
        (
            lambda card: card["typed_payload"].update(
                {"source_content_hash": "sha256:" + "b" * 64}
            ),
            "artifact_preference_proposal_lineage_mismatch",
        ),
        (
            lambda card: card["typed_payload"].update(
                {"evaluator_profile": "unsupported_profile"}
            ),
            "unsupported_artifact_preference_profile",
        ),
    ],
)
def test_evaluator_fails_closed_for_lineage_or_profile_tamper(
    tmp_path: Path,
    mutation,
    expected_failure: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    _rewrite_current_card(service, mutation)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert expected_failure in receipt["failures"]


def test_evaluator_detects_authority_decision_drift_during_evaluation(
    tmp_path: Path,
    monkeypatch,
):
    service = _service_with_current_artifact_preference(tmp_path)
    original = service.ledger.list_object_authority_decisions
    call_count = 0

    def drifting_decisions(**kwargs):
        nonlocal call_count
        call_count += 1
        decisions = original(**kwargs)
        if call_count > 1:
            decisions[0]["new_authority_lane"] = "accepted_non_current"
        return decisions

    monkeypatch.setattr(
        service.ledger,
        "list_object_authority_decisions",
        drifting_decisions,
    )

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["authority_decision_drift_during_evaluation"]


def test_evaluator_fails_closed_when_card_scan_is_saturated(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)
    template = service.ledger.list_llm_brain_memory_cards(
        project=PROJECT,
        accepted_only=True,
        limit=1,
    )[0]
    for index in range(100):
        card = copy.deepcopy(template)
        card["memory_id"] = f"mem_artifact_preference_archived_{index:03d}"
        card["currentness"] = "superseded"
        card["superseded_by"] = [template["memory_id"]]
        card.pop("content_hash", None)
        card.pop("card_hash", None)
        service.ledger.upsert_llm_brain_memory_card(card)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == [
        "accepted_current_artifact_preference_scan_saturated"
    ]


def test_evaluator_ignores_200_unrelated_accepted_current_cards(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)
    _add_unrelated_accepted_current_cards(service, count=200)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "PASS"
    assert receipt["applied"] is True
    assert receipt["preference_binding"]["target_object_id"] == TARGET_OBJECT_ID


def test_evaluator_does_not_miss_second_current_hidden_after_100_cards(
    tmp_path: Path,
):
    service = _service_with_current_artifact_preference(tmp_path)
    _add_second_current_artifact_preference(service)
    template = service.ledger.list_llm_brain_memory_cards(
        project=PROJECT,
        accepted_only=True,
        limit=1,
    )[0]
    for index in range(100):
        card = copy.deepcopy(template)
        card["memory_id"] = f"mem_artifact_preference_archived_{index:03d}"
        card["currentness"] = "superseded"
        card["superseded_by"] = [template["memory_id"]]
        card.pop("content_hash", None)
        card.pop("card_hash", None)
        service.ledger.upsert_llm_brain_memory_card(card)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == [
        "accepted_current_artifact_preference_scan_saturated"
    ]


def test_evaluator_fails_closed_when_malformed_card_hides_second_current(
    tmp_path: Path,
):
    service = _service_with_current_artifact_preference(tmp_path)
    _add_second_current_artifact_preference(service)
    template = service.ledger.list_llm_brain_memory_cards(
        project=PROJECT,
        accepted_only=True,
        limit=1,
    )[0]
    for index in range(99):
        card = copy.deepcopy(template)
        card["memory_id"] = f"mem_artifact_preference_archived_{index:03d}"
        card["currentness"] = "superseded"
        card["superseded_by"] = [template["memory_id"]]
        card.pop("content_hash", None)
        card.pop("card_hash", None)
        service.ledger.upsert_llm_brain_memory_card(card)
    malformed = copy.deepcopy(template)
    malformed["memory_id"] = "mem_artifact_preference_archived_malformed"
    malformed["typed_payload"]["target_object_id"] = "ko:RepoStyle:malformed"
    malformed.pop("content_hash", None)
    malformed.pop("card_hash", None)
    service.ledger.upsert_llm_brain_memory_card(malformed)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == [
        "accepted_current_artifact_preference_scan_saturated"
    ]


def test_evaluator_detects_concurrent_second_current_insertion(
    tmp_path: Path,
    monkeypatch,
):
    service = _service_with_current_artifact_preference(tmp_path)
    original = service.ledger.list_llm_brain_memory_cards
    initial_cards = original(
        project=PROJECT,
        accepted_only=True,
        current_only=False,
        limit=101,
    )
    second = copy.deepcopy(initial_cards[0])
    second["memory_id"] = "mem_artifact_preference_concurrent_second"
    second["typed_payload"]["target_object_id"] = (
        "ko:ArtifactPreference:p7-html-review-density-concurrent"
    )
    call_count = 0

    def concurrent_cards(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return copy.deepcopy(initial_cards)
        return [*copy.deepcopy(initial_cards), copy.deepcopy(second)]

    monkeypatch.setattr(service.ledger, "list_llm_brain_memory_cards", concurrent_cards)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["authority_decision_drift_during_evaluation"]


def test_evaluator_detects_canonical_card_change_during_evaluation(
    tmp_path: Path,
    monkeypatch,
):
    service = _service_with_current_artifact_preference(tmp_path)
    original = service.ledger.list_llm_brain_memory_cards
    initial_cards = original(
        project=PROJECT,
        accepted_only=True,
        current_only=False,
        limit=101,
    )
    changed_cards = copy.deepcopy(initial_cards)
    changed_cards[0]["summary"] = "Changed canonical card summary."
    changed_cards[0].pop("content_hash", None)
    changed_cards[0].pop("card_hash", None)
    changed_hash = hash_payload(changed_cards[0])
    changed_cards[0]["content_hash"] = changed_hash
    changed_cards[0]["card_hash"] = changed_hash
    call_count = 0

    def changing_cards(**kwargs):
        nonlocal call_count
        call_count += 1
        return copy.deepcopy(initial_cards if call_count == 1 else changed_cards)

    monkeypatch.setattr(service.ledger, "list_llm_brain_memory_cards", changing_cards)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["authority_decision_drift_during_evaluation"]


def test_evaluator_rejects_card_content_mutation_without_canonical_hash_update(
    tmp_path: Path,
    monkeypatch,
):
    service = _service_with_current_artifact_preference(tmp_path)
    original = service.ledger.list_llm_brain_memory_cards

    def tampered_cards(**kwargs):
        cards = original(**kwargs)
        cards[0]["summary"] = "Mutated canonical card content."
        return cards

    monkeypatch.setattr(service.ledger, "list_llm_brain_memory_cards", tampered_cards)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert "canonical_artifact_preference_content_hash_mismatch" in receipt["failures"]


@pytest.mark.parametrize("hash_field", ["content_hash", "card_hash"])
def test_evaluator_rejects_canonical_card_hash_tamper(
    tmp_path: Path,
    monkeypatch,
    hash_field: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    original = service.ledger.list_llm_brain_memory_cards

    def tampered_cards(**kwargs):
        cards = original(**kwargs)
        cards[0][hash_field] = "sha256:" + "f" * 64
        return cards

    monkeypatch.setattr(service.ledger, "list_llm_brain_memory_cards", tampered_cards)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert "canonical_artifact_preference_content_hash_mismatch" in receipt["failures"]


def test_evaluator_does_not_expose_raw_external_id_from_canonical_binding(
    tmp_path: Path,
    monkeypatch,
):
    service = _service_with_current_artifact_preference(tmp_path)
    original = service.ledger.list_llm_brain_memory_cards

    def unsafe_cards(**kwargs):
        cards = original(**kwargs)
        cards[0]["memory_id"] = "dataset_id:raw-external"
        cards[0].pop("content_hash", None)
        cards[0].pop("card_hash", None)
        content_hash = hash_payload(cards[0])
        cards[0]["content_hash"] = content_hash
        cards[0]["card_hash"] = content_hash
        return cards

    monkeypatch.setattr(service.ledger, "list_llm_brain_memory_cards", unsafe_cards)

    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())
    serialized = json.dumps(receipt, sort_keys=True)

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is False
    assert receipt["failures"] == ["canonical_artifact_preference_binding_invalid"]
    assert "dataset_id" not in serialized
    assert "raw-external" not in serialized


def test_evaluator_reports_rule_failure_without_fabricating_pass(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["metrics"]["object_count"] = 0
    arguments["artifact_fingerprint"] = hash_payload(
        {
            "artifact_type": arguments["artifact_type"],
            "summary": arguments["summary"],
            "metrics": arguments["metrics"],
            "evidence_refs": arguments["evidence_refs"],
        }
    )

    receipt = service.brain_artifact_preference_evaluate(**arguments)

    assert receipt["status"] == "FAIL"
    assert receipt["applied"] is True
    assert receipt["application_result"]["outcome"] == "fail"
    assert receipt["application_result"]["failed_rules"] == [
        "object_count_at_least_one"
    ]
    assert "object_count_at_least_one" not in receipt["application_result"]["passed_rules"]


def test_evaluator_allows_safe_percent_encoding_in_public_text(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["branch"] = "feature%2Fartifact-review"
    arguments["summary"] = "Evidence%20dense HTML review artifact."
    arguments["artifact_fingerprint"] = hash_payload(
        {
            "artifact_type": arguments["artifact_type"],
            "summary": arguments["summary"],
            "metrics": arguments["metrics"],
            "evidence_refs": arguments["evidence_refs"],
        }
    )

    receipt = service.brain_artifact_preference_evaluate(**arguments)

    assert receipt["status"] == "PASS"
    assert receipt["applied"] is True


@pytest.mark.parametrize(
    ("field", "encoded_value", "protected_fragment"),
    [
        ("summary", "%2FUsers%2Fexample%2Fprivate%20artifact", "/Users/"),
        ("summary", "API_KEY%3Dprivate-value", "private-value"),
        ("branch", "%5C%5Cinternal-host%5Cprivate-share", "internal-host"),
        ("summary", "document_id%3Draw-external", "raw-external"),
    ],
)
def test_evaluator_rejects_percent_encoded_protected_consumer_input_without_echo(
    tmp_path: Path,
    field: str,
    encoded_value: str,
    protected_fragment: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments[field] = encoded_value
    if field == "summary":
        arguments["artifact_fingerprint"] = hash_payload(
            {
                "artifact_type": arguments["artifact_type"],
                "summary": arguments["summary"],
                "metrics": arguments["metrics"],
                "evidence_refs": arguments["evidence_refs"],
            }
        )

    with pytest.raises(ValueError) as error:
        service.brain_artifact_preference_evaluate(**arguments)

    assert encoded_value not in str(error.value)
    assert protected_fragment not in str(error.value)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda arguments: arguments["metrics"].update({"unknown_count": 1}),
        lambda arguments: arguments["metrics"].update({"object_count": True}),
        lambda arguments: arguments["metrics"].update({"object_count": -1}),
        lambda arguments: arguments["metrics"].update({"object_count": 1_000_001}),
        lambda arguments: arguments["metrics"].update({"evidence_count": 1}),
        lambda arguments: arguments.update({"artifact_fingerprint": "sha256:" + "0" * 64}),
        lambda arguments: arguments.update({"summary": "/Users/example/private artifact"}),
        lambda arguments: arguments["evidence_refs"].append("dataset_id:raw-external"),
        lambda arguments: arguments.update({"artifact_type": "pdf_review_artifact"}),
    ],
)
def test_evaluator_rejects_invalid_or_protected_consumer_input(
    tmp_path: Path,
    mutation,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    mutation(arguments)

    with pytest.raises(ValueError):
        service.brain_artifact_preference_evaluate(**arguments)


def test_mcp_evaluator_rejects_protected_authority_field_injection(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = {**_valid_artifact_input(), "object_id": TARGET_OBJECT_ID}

    with pytest.raises(ValueError):
        dispatch_tool_call(
            {
                "name": "brain_artifact_preference_evaluate",
                "arguments": arguments,
            },
            service,
        )


def test_evaluator_performs_no_ledger_mutation(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)

    def snapshot() -> dict:
        return {
            "cards": service.ledger.list_llm_brain_memory_cards(project=PROJECT, limit=100),
            "proposals": service.ledger.list_object_review_proposals(project=PROJECT, limit=100),
            "decisions": service.ledger.list_object_authority_decisions(project=PROJECT, limit=100),
            "state": service.ledger.get_object_authority_state(TARGET_OBJECT_ID),
        }

    before = snapshot()
    receipt = service.brain_artifact_preference_evaluate(**_valid_artifact_input())

    assert receipt["production_mutation_performed"] is False
    assert snapshot() == before


@pytest.mark.parametrize(
    ("path", "field", "value"),
    [
        ((), "dataset_id", "raw-external"),
        (("preference_binding",), "document_id", "raw-external"),
        (("artifact_binding",), "dataset_id", "raw-external"),
        (("application_result",), "document_id", "raw-external"),
        (("consumer_surface",), "dataset_id", "raw-external"),
    ],
)
def test_receipt_validator_rejects_extra_top_level_or_nested_protected_fields(
    tmp_path: Path,
    path: tuple[str, ...],
    field: str,
    value: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    target = receipt
    for key in path:
        target = target[key]
    target[field] = value
    receipt["receipt_hash"] = hash_payload(
        {
            "preference_binding": receipt["preference_binding"],
            "artifact_binding": receipt["artifact_binding"],
            "application_result": receipt["application_result"],
            "consumer_surface": receipt["consumer_surface"],
        }
    )

    assert artifact_preference_application_receipt_is_valid(receipt) is False


@pytest.mark.parametrize(
    ("section", "field", "marker"),
    [
        ("preference_binding", "memory_id", "mem-safe document_id=raw-external"),
        ("preference_binding", "proposal_id", "proposal:safe dataset_id:raw-external"),
    ],
)
def test_receipt_validator_rejects_raw_external_id_marker_in_allowed_string_after_rehash(
    tmp_path: Path,
    section: str,
    field: str,
    marker: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    receipt[section][field] = marker
    receipt["receipt_hash"] = hash_payload(
        {
            "preference_binding": receipt["preference_binding"],
            "artifact_binding": receipt["artifact_binding"],
            "application_result": receipt["application_result"],
            "consumer_surface": receipt["consumer_surface"],
        }
    )

    assert artifact_preference_application_receipt_is_valid(receipt) is False


@pytest.mark.parametrize(
    "marker",
    [
        "mem-safe document_id : raw-external",
        "mem-safe documentId: raw-external",
        "mem-safe document.id: raw-external",
        "mem-safe document-id: raw-external",
        "mem-safe document id: raw-external",
        "mem-safe document_id%20%3A%20raw-external",
        "mem-safe document_id%2520%253A%2520raw-external",
        "mem-safe document_id%252520%25253A%252520raw-external",
        "mem-safe document_id%25252520%2525253A%25252520raw-external",
    ],
)
def test_receipt_validator_rejects_normalized_raw_external_id_marker_after_rehash(
    tmp_path: Path,
    marker: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    receipt["preference_binding"]["memory_id"] = marker
    receipt["receipt_hash"] = hash_payload(
        {
            "preference_binding": receipt["preference_binding"],
            "artifact_binding": receipt["artifact_binding"],
            "application_result": receipt["application_result"],
            "consumer_surface": receipt["consumer_surface"],
        }
    )

    assert artifact_preference_application_receipt_is_valid(receipt) is False


@pytest.mark.parametrize(
    "marker",
    [
        "mem-safe-%2FUsers%2Fexample%2Fprivate",
        "mem-safe-API_KEY%3Dprivate-value",
        "mem-safe-%5C%5Cinternal-host%5Cprivate-share",
        "mem-safe-document_id%3Draw-external",
    ],
)
def test_receipt_validator_rejects_percent_encoded_protected_string_after_rehash(
    tmp_path: Path,
    marker: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    receipt["preference_binding"]["memory_id"] = marker
    receipt["receipt_hash"] = hash_payload(
        {
            "preference_binding": receipt["preference_binding"],
            "artifact_binding": receipt["artifact_binding"],
            "application_result": receipt["application_result"],
            "consumer_surface": receipt["consumer_surface"],
        }
    )

    assert artifact_preference_application_receipt_is_valid(receipt) is False


@pytest.mark.parametrize("marker", ["mem-safe%20value", "mem-safe%2Fvalue"])
def test_receipt_validator_allows_ordinary_percent_encoding_after_rehash(
    tmp_path: Path,
    marker: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    receipt["preference_binding"]["memory_id"] = marker
    receipt["receipt_hash"] = hash_payload(
        {
            "preference_binding": receipt["preference_binding"],
            "artifact_binding": receipt["artifact_binding"],
            "application_result": receipt["application_result"],
            "consumer_surface": receipt["consumer_surface"],
        }
    )

    assert artifact_preference_application_receipt_is_valid(receipt) is True


def test_receipt_validator_fails_closed_after_bounded_percent_decode(
    tmp_path: Path,
    monkeypatch,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    decode_calls = 0

    def never_stable(value: str) -> str:
        nonlocal decode_calls
        decode_calls += 1
        return value + "%20"

    monkeypatch.setattr(artifact_preference_evaluator, "unquote", never_stable)

    assert artifact_preference_application_receipt_is_valid(receipt) is False
    assert (
        decode_calls
        == artifact_preference_evaluator._PUBLIC_SAFE_PERCENT_DECODE_MAX_PASSES
    )


def test_post_deploy_collector_recalculates_actual_service_artifact_review(
    tmp_path: Path,
    capsys,
):
    service = _service_with_current_artifact_preference(tmp_path)
    initial = service.brain_source_to_candidate_runtime_readiness(
        collect_shadow_evidence=True,
        evidence_collection_mode="post_deploy_read_only_smoke",
        evidence_collection_network_used=True,
        repository=REPOSITORY,
        branch=BRANCH,
        project=PROJECT,
        consumer="codex",
    )
    assert initial["preference_artifact_memory"]["artifact_review_check"]["status"] == "failed"

    class _ServiceBackedSession:
        async def initialize(self):
            return None

        async def list_tools(self):
            names = [
                "brain_source_to_candidate_runtime_readiness",
                "brain_context_resolve",
                "brain_objects_query",
                "brain_artifact_preference_evaluate",
            ]
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])

        async def call_tool(self, name: str, arguments: dict):
            result = dispatch_tool_call(
                {"name": name, "arguments": arguments},
                service,
            )
            return SimpleNamespace(
                isError=False,
                structuredContent=result["structuredContent"],
            )

    @asynccontextmanager
    async def session_factory(_mcp_url: str):
        yield _ServiceBackedSession()

    artifact_input = _valid_artifact_input()
    descriptor = {
        key: artifact_input[key]
        for key in (
            "artifact_type",
            "summary",
            "artifact_fingerprint",
            "metrics",
            "evidence_refs",
        )
    }
    capture = asyncio.run(
        collect_source_to_candidate_post_deploy_mcp_capture(
            mcp_url="https://mcp.example.test/mcp",
            repository=REPOSITORY,
            branch=BRANCH,
            project=PROJECT,
            expected_commit="verified-commit",
            deployed_identity={
                "contains_expected_commit": True,
                "identity_source": "redacted_artifact_identity_summary",
            },
            artifact_descriptor=descriptor,
            session_factory=session_factory,
        )
    )

    assert capture["preference_artifact_memory"]["artifact_review_check"]["status"] == "pass"
    assert capture["artifact_preference_application_receipt"]["status"] == "PASS"
    assert capture["production_mutation_performed"] is False

    direct_report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=capture,
        expected_commit="verified-commit",
    )
    direct_claim = next(
        claim
        for claim in direct_report["claims"]
        if claim["claim_id"] == "live.preference_artifact.memory"
    )
    assert direct_claim["status"] == "validated"

    replayed_capture = json.loads(json.dumps(capture))
    replay_report = build_source_to_candidate_runtime_post_deploy_capture_readiness_report(
        captured_evidence=replayed_capture,
        expected_commit="verified-commit",
    )
    replay_claim = next(
        claim
        for claim in replay_report["claims"]
        if claim["claim_id"] == "live.preference_artifact.memory"
    )
    assert replay_claim["status"] == "not_validated"
    assert "preference_artifact_collector_capability_missing" in replay_claim["gaps"]

    service_capture_report = service.brain_source_to_candidate_runtime_readiness(
        post_deploy_capture=capture,
        expected_commit="verified-commit",
    )
    service_capture_claim = next(
        claim
        for claim in service_capture_report["claims"]
        if claim["claim_id"] == "live.preference_artifact.memory"
    )
    assert service_capture_claim["status"] == "not_validated"
    assert "preference_artifact_collector_capability_missing" in service_capture_claim["gaps"]

    collected_packet = build_source_to_candidate_runtime_post_deploy_capture_packet(
        captured_evidence=capture,
    )
    service_live_report = service.brain_source_to_candidate_runtime_readiness(
        live_evidence=collected_packet,
        expected_commit="verified-commit",
    )
    service_live_claim = next(
        claim
        for claim in service_live_report["claims"]
        if claim["claim_id"] == "live.preference_artifact.memory"
    )
    assert service_live_claim["status"] == "not_validated"
    assert "preference_artifact_collector_capability_missing" in service_live_claim["gaps"]

    replay_file = tmp_path / "post-deploy-capture.json"
    replay_file.write_text(json.dumps(capture), encoding="utf-8")
    assert (
        main(
            [
                "source-to-candidate-runtime-readiness",
                "--post-deploy-capture-file",
                str(replay_file),
                "--expected-commit",
                "verified-commit",
            ]
        )
        == 0
    )
    replay_cli_report = json.loads(capsys.readouterr().out)
    replay_cli_claim = next(
        claim
        for claim in replay_cli_report["claims"]
        if claim["claim_id"] == "live.preference_artifact.memory"
    )
    assert replay_cli_report["status"] == "PASS_WITH_GAPS"
    assert replay_cli_claim["status"] == "not_validated"
    assert "preference_artifact_collector_capability_missing" in replay_cli_claim["gaps"]


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (
            "preference_binding",
            "memory_id",
            "mem_artifact_preference_tampered",
        ),
        ("preference_binding", "card_content_hash", "sha256:" + "f" * 64),
        ("artifact_binding", "repository_hash", hash_payload("other/repository")),
        ("artifact_binding", "branch_hash", hash_payload("other-branch")),
    ],
)
def test_receipt_validator_rejects_bound_value_tamper(
    tmp_path: Path,
    section: str,
    field: str,
    value: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    receipt[section][field] = value

    assert artifact_preference_application_receipt_is_valid(receipt) is False


def test_receipt_validator_rejects_protected_key_at_any_depth(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    receipt["preference_binding"]["project"] = {
        "nested": {"dataset_id": "raw-external"}
    }
    receipt["receipt_hash"] = hash_payload(
        {
            "preference_binding": receipt["preference_binding"],
            "artifact_binding": receipt["artifact_binding"],
            "application_result": receipt["application_result"],
            "consumer_surface": receipt["consumer_surface"],
        }
    )

    assert artifact_preference_application_receipt_is_valid(receipt) is False


def test_receipt_validator_rejects_non_scalar_exact_field_value(tmp_path: Path):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    receipt["preference_binding"]["project"] = {"nested": "neurons"}
    receipt["receipt_hash"] = hash_payload(
        {
            "preference_binding": receipt["preference_binding"],
            "artifact_binding": receipt["artifact_binding"],
            "application_result": receipt["application_result"],
            "consumer_surface": receipt["consumer_surface"],
        }
    )

    assert artifact_preference_application_receipt_is_valid(receipt) is False


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ((), "gaps"),
        (("preference_binding",), "memory_id"),
        (("artifact_binding",), "repository_hash"),
        (("application_result",), "outcome"),
        (("consumer_surface",), "version"),
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "unknown"])
def test_receipt_validator_requires_exact_fields_at_every_schema_level(
    tmp_path: Path,
    path: tuple[str, ...],
    field: str,
    mutation: str,
):
    service = _service_with_current_artifact_preference(tmp_path)
    arguments = _valid_artifact_input()
    arguments["consumer"] = "post_deploy_mcp_capture"
    receipt = service.brain_artifact_preference_evaluate(**arguments)
    target = receipt
    for key in path:
        target = target[key]
    if mutation == "missing":
        target.pop(field)
    else:
        target["unexpected"] = "value"
    receipt["receipt_hash"] = hash_payload(
        {
            "preference_binding": receipt["preference_binding"],
            "artifact_binding": receipt["artifact_binding"],
            "application_result": receipt["application_result"],
            "consumer_surface": receipt["consumer_surface"],
        }
    )

    assert artifact_preference_application_receipt_is_valid(receipt) is False

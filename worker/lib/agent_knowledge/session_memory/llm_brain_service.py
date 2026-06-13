from __future__ import annotations

from typing import Any, Mapping

from .memory_evaluation import apply_auto_acceptance_plan
from .memory_promotion import human_approve_memory_card_candidate
from .ragflow_projection import build_projection_job, execute_projection_job


class LLMBrainMemoryService:
    """Integration boundary for canonical LLM-brain ledger writes."""

    def __init__(self, ledger):
        self.ledger = ledger

    def accept_human_approved_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        approved_by: str,
        decision_id: str,
        artifact_id: str = "",
        user_reason: str | None = None,
        timestamp: str | None = None,
    ) -> dict:
        promotion = human_approve_memory_card_candidate(
            candidate,
            approved_by=approved_by,
            decision_id=decision_id,
            artifact_id=artifact_id,
            user_reason=user_reason,
            timestamp=timestamp,
        )
        accepted_card = self.ledger.upsert_llm_brain_memory_card(promotion["accepted_card"])
        feedback_record = self.ledger.upsert_llm_brain_feedback_record(promotion["feedback_record"])
        return {
            "schema_version": "llm_brain_human_acceptance_commit.v1",
            "promotion_path": "human_approval",
            "canonical_write_performed": True,
            "accepted_card": accepted_card,
            "feedback_record": feedback_record,
        }

    def accept_auto_policy_candidate(
        self,
        candidate: Mapping[str, Any],
        evaluation: Mapping[str, Any],
        *,
        operator_approval_ref: str,
    ) -> dict:
        application = apply_auto_acceptance_plan(
            candidate,
            evaluation,
            allow_auto_accept=True,
            operator_approval_ref=operator_approval_ref,
        )
        if application["status"] != "auto_accepted":
            return {
                "schema_version": "llm_brain_auto_acceptance_commit.v1",
                "canonical_write_performed": False,
                "application": application,
            }
        accepted_card = self.ledger.upsert_llm_brain_memory_card(application["accepted_card"])
        return {
            "schema_version": "llm_brain_auto_acceptance_commit.v1",
            "promotion_path": "auto_policy",
            "canonical_write_performed": True,
            "accepted_card": accepted_card,
            "application": application,
        }

    def enqueue_projection_for_card(self, card: Mapping[str, Any]) -> dict:
        job = build_projection_job(card)
        stored_job = self.ledger.upsert_llm_brain_projection_job(job)
        return {
            "schema_version": "llm_brain_projection_enqueue_commit.v1",
            "projection_job_write_performed": True,
            "job": stored_job,
        }

    def execute_projection_job(
        self,
        job: Mapping[str, Any],
        *,
        client: Any,
        allow_write: bool,
        approval_record: Mapping[str, Any] | None = None,
    ) -> dict:
        executable_job = dict(job)
        if approval_record is not None:
            executable_job["approval_record"] = dict(approval_record)
        result = execute_projection_job(executable_job, client=client, allow_write=allow_write)
        updated_job = dict(executable_job)
        updated_job["status"] = str(result.get("status") or updated_job.get("status") or "")
        updated_job["attempt_count"] = int(updated_job.get("attempt_count") or 0) + 1
        updated_job["last_result"] = result
        stored_job = self.ledger.upsert_llm_brain_projection_job(updated_job)
        return {
            "schema_version": "llm_brain_projection_execution_commit.v1",
            "projection_job_write_performed": True,
            "result": result,
            "job": stored_job,
        }

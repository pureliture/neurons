from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .memory_evaluation import apply_auto_acceptance_plan, classify_candidate_block_reason
from .memory_promotion import commit_supersession, human_approve_memory_card_candidate
from .index_projection import build_projection_job, execute_projection_job


def _build_pg_card(card: Mapping[str, Any], *, active: bool = True):
    """Convert a card envelope dict to a PostgreSQL MemoryCard dataclass.

    Accepted cards must have authorization_status='active' so they pass the
    _authority_filter. The envelope uses source_refs (plural) but MemoryCard
    uses source_ref (singular).
    """
    from ..postgres_store.pgvector_store import MemoryCard

    valid_from = card.get("valid_from")
    if isinstance(valid_from, str):
        valid_from = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
    elif valid_from is None:
        valid_from = datetime.now(timezone.utc)

    valid_to = card.get("valid_to")
    if isinstance(valid_to, str):
        valid_to = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))

    return MemoryCard(
        memory_id=str(card["memory_id"]),
        project=str(card["project"]),
        card_type=str(card["card_type"]),
        title=str(card.get("title") or ""),
        summary=str(card.get("summary") or ""),
        typed_payload=dict(card.get("typed_payload") or {}),
        lifecycle_state=str(card.get("lifecycle_state") or "candidate"),
        authorization_status="active" if active else "disabled",
        currentness=str(card.get("currentness") or "current"),
        confidence=float(card.get("confidence") or 0.0),
        valid_from=valid_from,
        valid_to=valid_to,
        content_hash=str(card.get("content_hash") or ""),
        source_ref=list(card.get("source_refs") or []),
    )


class LLMBrainMemoryService:
    """Integration boundary for canonical LLM-brain ledger writes."""

    def __init__(self, ledger, *, pgvector_store=None):
        self.ledger = ledger
        self.pgvector_store = pgvector_store

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
        if self.pgvector_store is not None:
            def transform(current, target):
                if str(current.get('lifecycle_state')) not in ('candidate', 'needs_review', 'suggested_accept'):
                    raise ValueError('only pending steward candidate may be approved')
                block_reason = classify_candidate_block_reason(current)
                if block_reason:
                    raise ValueError(f'candidate blocked from approval: {block_reason}')
                promoted = human_approve_memory_card_candidate(current, approved_by=approved_by,
                    decision_id=decision_id, artifact_id=artifact_id, user_reason=user_reason, timestamp=timestamp)
                return promoted['accepted_card'], None
            changed = self.pgvector_store.steward_decision(decision_id=decision_id, memory_id=str(candidate['memory_id']),
                content_hash=str(candidate['content_hash']), action='approve', actor=approved_by, transform=transform)
            return {'schema_version': 'llm_brain_human_acceptance_commit.v1', 'promotion_path': 'human_approval',
                    'canonical_write_performed': True, 'accepted_card': changed['card']}
        block_reason = classify_candidate_block_reason(candidate)
        if block_reason:
            raise ValueError(f"candidate blocked from auto-accept: {block_reason}")
        promotion = human_approve_memory_card_candidate(
            candidate,
            approved_by=approved_by,
            decision_id=decision_id,
            artifact_id=artifact_id,
            user_reason=user_reason,
            timestamp=timestamp,
        )
        # card + audit 를 한 트랜잭션으로 묶어 부분 커밋을 막는다(#49).
        with self.ledger._transaction() as tx:
            accepted_card = tx.upsert_llm_brain_memory_card(promotion["accepted_card"])
            feedback_record = tx.upsert_llm_brain_feedback_record(promotion["feedback_record"])
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
        if self.pgvector_store is not None:
            application = apply_auto_acceptance_plan(candidate, evaluation, allow_auto_accept=True,
                operator_approval_ref=operator_approval_ref)
            if application['status'] != 'auto_accepted':
                return {'schema_version': 'llm_brain_auto_acceptance_commit.v1',
                        'canonical_write_performed': False, 'application': application}
            def transform(current, target):
                if current.get('lifecycle_state') not in ('candidate', 'needs_review', 'suggested_accept'):
                    raise ValueError('only pending steward candidate may be auto-accepted')
                evaluated = apply_auto_acceptance_plan(current, evaluation, allow_auto_accept=True,
                    operator_approval_ref=operator_approval_ref)
                if evaluated['status'] != 'auto_accepted':
                    raise ValueError('steward auto acceptance no longer eligible')
                return evaluated['accepted_card'], None
            changed = self.pgvector_store.steward_decision(decision_id=operator_approval_ref,
                memory_id=str(candidate['memory_id']), content_hash=str(candidate['content_hash']),
                action='auto_accept', actor=operator_approval_ref, transform=transform)
            return {'schema_version': 'llm_brain_auto_acceptance_commit.v1', 'promotion_path': 'auto_policy',
                    'canonical_write_performed': True, 'accepted_card': changed['card']}
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

    def supersede_accepted_card(
        self,
        *,
        old_card: Mapping[str, Any],
        new_candidate: Mapping[str, Any],
        approved_by: str,
        decision_id: str,
        timestamp: str | None = None,
    ) -> dict:
        """Accept a new current card and atomically demote the old card it replaces.

        The new candidate is accepted via the human-approval path (the only accept
        primitive that runs at cold start), then the old card is re-written to
        currentness=superseded so it leaves both current and accepted recall lanes.
        """
        if self.pgvector_store is not None:
            def transform(current, old):
                if current.get('lifecycle_state') not in ('candidate', 'needs_review', 'suggested_accept'):
                    raise ValueError('supersede proposal already committed')
                if old is None or old.get('currentness') != 'current' or old.get('approval_state') not in ('approved', 'auto_accepted'):
                    raise ValueError('supersede target is not accepted and current')
                if current.get('steward_target_memory_id') != old['memory_id']:
                    raise ValueError('supersede target mismatch')
                promoted = human_approve_memory_card_candidate(current, approved_by=approved_by,
                    decision_id=decision_id, timestamp=timestamp)['accepted_card']
                demoted = commit_supersession(old, superseded_by=promoted['memory_id'], timestamp=timestamp)
                return promoted, demoted
            changed = self.pgvector_store.steward_decision(decision_id=decision_id,
                memory_id=str(new_candidate['memory_id']), content_hash=str(new_candidate['content_hash']),
                action='supersede', actor=approved_by, target_memory_id=str(old_card['memory_id']), transform=transform)
            return {'schema_version': 'llm_brain_supersession_commit.v1', 'canonical_write_performed': True,
                    'new_card': changed['card'], 'superseded_card': changed['target']}
        promotion = human_approve_memory_card_candidate(
            new_candidate,
            approved_by=approved_by,
            decision_id=decision_id,
            timestamp=timestamp,
        )
        # new accept + audit + old demote 를 한 트랜잭션으로 묶는다. 중간 실패 시 전부 rollback(#49).
        with self.ledger._transaction() as tx:
            new_card = tx.upsert_llm_brain_memory_card(promotion["accepted_card"])
            feedback_record = tx.upsert_llm_brain_feedback_record(promotion["feedback_record"])
            demoted = commit_supersession(
                old_card,
                superseded_by=new_card["memory_id"],
                timestamp=timestamp,
            )
            superseded_card = tx.upsert_llm_brain_memory_card(demoted)
        return {
            "schema_version": "llm_brain_supersession_commit.v1",
            "canonical_write_performed": True,
            "new_card": new_card,
            "superseded_card": superseded_card,
            "feedback_record": feedback_record,
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

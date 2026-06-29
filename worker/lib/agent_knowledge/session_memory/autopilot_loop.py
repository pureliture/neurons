"""Autopilot curation cycle orchestrator (steps 2-3 of the autopilot loop).

Given already-mined candidates, classify each through the B-core safety subset,
auto-accept clean candidates via the human-approval path (the only accept primitive
that runs at cold start), demote any card a supersede detector flags as replaced, and
route blocked candidates to needs_review (out of the canonical accepted set).

Mining (step 1) is deliberately upstream and injected as ``candidates`` so this core
is fixture-testable without a live RetiredIndexBridge/transcript-memory seam. The detector
(supersede detection, step 2) is injected so its algorithm can evolve independently.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from .llm_brain_service import LLMBrainMemoryService
from .memory_evaluation import classify_candidate_block_reason
from .memory_promotion import mark_candidate_needs_review


SupersedeDetector = Callable[[Mapping[str, Any], Any], Mapping[str, Any] | None]


def run_autopilot_cycle(
    *,
    candidates: Sequence[Mapping[str, Any]],
    ledger: Any,
    refresh_watermark: str,
    approved_by: str = "autopilot",
    supersede_detector: SupersedeDetector | None = None,
    projection_client: Any | None = None,
    timestamp: str | None = None,
) -> dict:
    service = LLMBrainMemoryService(ledger)
    accepted: list[dict] = []
    needs_review: list[dict] = []
    superseded: list[dict] = []
    projected_count = 0

    for candidate in candidates:
        decision_id = f"auto:{refresh_watermark}:{candidate.get('memory_id', '')}"
        block_reason = classify_candidate_block_reason(candidate)
        if block_reason:
            review = mark_candidate_needs_review(
                candidate,
                reason=block_reason,
                decision_id=decision_id,
                conflict_state="conflict" if block_reason == "conflict" else "none",
                timestamp=timestamp,
            )
            needs_review.append(review["review_card"])
            continue

        old_card = supersede_detector(candidate, ledger) if supersede_detector else None
        if old_card:
            committed = service.supersede_accepted_card(
                old_card=old_card,
                new_candidate=candidate,
                approved_by=approved_by,
                decision_id=decision_id,
                timestamp=timestamp,
            )
            accepted.append(committed["new_card"])
            superseded.append(committed["superseded_card"])
            # Project the new current card AND re-project the demoted card (currentness=superseded)
            # so the RetiredIndexBridge mirror demotes too (design step 4).
            projected_count += _project_cards(
                service, projection_client, [committed["new_card"], committed["superseded_card"]]
            )
        else:
            committed = service.accept_human_approved_candidate(
                candidate,
                approved_by=approved_by,
                decision_id=decision_id,
                timestamp=timestamp,
            )
            accepted.append(committed["accepted_card"])
            projected_count += _project_cards(service, projection_client, [committed["accepted_card"]])

    return {
        "schema_version": "llm_brain_autopilot_cycle.v1",
        "refresh_watermark": refresh_watermark,
        "accepted": accepted,
        "needs_review": needs_review,
        "superseded": superseded,
        "projected_count": projected_count,
    }


def _autopilot_projection_approval(job: Mapping[str, Any]) -> dict:
    # Self-minted projection approval under standing pre-approval. dry_run_status='dry_run'
    # is required by execute_projection_job even on a live write (means "a dry-run preceded
    # this", not "no-op") — see index_projection. Forbidden ops are unaffected.
    return {
        "approved": True,
        "operation": "index_projection_write",
        "idempotency_key": job["idempotency_key"],
        "dry_run_status": "dry_run",
        "approved_by": "autopilot",
    }


def _project_cards(service: Any, projection_client: Any | None, cards: Sequence[Mapping[str, Any]]) -> int:
    if projection_client is None:
        return 0
    count = 0
    for card in cards:
        queued = service.enqueue_projection_for_card(card)
        executed = service.execute_projection_job(
            queued["job"],
            client=projection_client,
            allow_write=True,
            approval_record=_autopilot_projection_approval(queued["job"]),
        )
        if (executed.get("result") or {}).get("status") == "projected":
            count += 1
    return count

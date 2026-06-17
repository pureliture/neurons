from __future__ import annotations

from .memory_card import build_memory_card


class CurationService:
    def __init__(self, ledger):
        self.ledger = ledger

    def add_candidate(self, candidate: dict) -> dict:
        return self.ledger.upsert_memory_candidate(candidate)

    def approve(self, candidate_id: str, *, approved_by: str, supersedes: str = "") -> dict:
        if supersedes:
            raise ValueError("use supersede for auditable memory replacement")
        candidate = self.ledger.get_memory_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"unknown memory candidate: {candidate_id}")
        card = build_memory_card(candidate, approved_by=approved_by, supersedes=supersedes)
        stored = self.ledger.upsert_memory_card(card)
        self.ledger.add_memory_card_evidence(card["memory_id"], candidate["evidence_refs"])
        self.ledger.update_memory_candidate_state(candidate_id, "approved", reviewed_by=approved_by)
        if candidate["candidate_type"] == "user_preference":
            self.ledger.upsert_profile_fact(
                memory_id=card["memory_id"],
                project=card["project"],
                fact_type=card["card_type"],
                content_hash=card["content_hash"],
                state=card["state"],
            )
        return stored

    def reject(self, candidate_id: str, *, reviewed_by: str, reason: str) -> dict:
        return self.ledger.update_memory_candidate_state(candidate_id, "rejected", reviewed_by=reviewed_by, reason=reason)

    def disable(self, memory_id: str, *, reviewed_by: str, reason: str) -> dict:
        card = self.ledger.update_memory_card_state(memory_id, "disabled", reviewed_by=reviewed_by, reason=reason)
        if card["card_type"] == "user_preference":
            self.ledger.upsert_profile_fact(
                memory_id=memory_id,
                project=card["project"],
                fact_type=card["card_type"],
                content_hash=card["content_hash"],
                state="disabled",
            )
        return card

    def supersede(self, old_memory_id: str, candidate_id: str, *, approved_by: str, reason: str) -> dict:
        candidate = self.ledger.get_memory_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"unknown memory candidate: {candidate_id}")
        card = build_memory_card(candidate, approved_by=approved_by, supersedes=old_memory_id)
        old_card = self.ledger.update_memory_card_state(old_memory_id, "superseded", reviewed_by=approved_by, reason=reason)
        if old_card["card_type"] == "user_preference":
            self.ledger.upsert_profile_fact(
                memory_id=old_memory_id,
                project=old_card["project"],
                fact_type=old_card["card_type"],
                content_hash=old_card["content_hash"],
                state="superseded",
            )
        stored = self.ledger.upsert_memory_card(card)
        self.ledger.add_memory_card_evidence(card["memory_id"], candidate["evidence_refs"])
        self.ledger.update_memory_candidate_state(candidate_id, "approved", reviewed_by=approved_by, reason=reason)
        if candidate["candidate_type"] == "user_preference":
            self.ledger.upsert_profile_fact(
                memory_id=card["memory_id"],
                project=card["project"],
                fact_type=card["card_type"],
                content_hash=card["content_hash"],
                state=card["state"],
            )
        return stored

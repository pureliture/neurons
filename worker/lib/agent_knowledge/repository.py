from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol


class MemoryCurationRepository(Protocol):
    """Use-case port for curation-owned approval writes."""

    def approve_candidate(
        self,
        candidate: Mapping[str, Any],
        card: Mapping[str, Any],
        *,
        approved_by: str,
    ) -> Mapping[str, Any]: ...


class LedgerMemoryCurationRepository:
    """Ledger-backed repository for the first M2 curation caller migration."""

    def __init__(self, ledger):
        self._ledger = ledger

    def approve_candidate(
        self,
        candidate: Mapping[str, Any],
        card: Mapping[str, Any],
        *,
        approved_by: str,
    ) -> Mapping[str, Any]:
        transaction_factory = getattr(self._ledger, "_transaction", None)
        if transaction_factory is None:
            raise RuntimeError("LedgerMemoryCurationRepository requires Ledger._transaction")
        with transaction_factory() as transaction:
            return self._approve_on(transaction, candidate, card, approved_by=approved_by)

    @staticmethod
    def _approve_on(
        transaction,
        candidate: Mapping[str, Any],
        card: Mapping[str, Any],
        *,
        approved_by: str,
    ) -> Mapping[str, Any]:
        card_payload = dict(card)
        stored = transaction.upsert_memory_card(card_payload)
        memory_id = str(card_payload["memory_id"])
        transaction.add_memory_card_evidence(memory_id, list(candidate["evidence_refs"]))
        transaction.update_memory_candidate_state(
            str(candidate["candidate_id"]),
            "approved",
            reviewed_by=approved_by,
        )
        if candidate["candidate_type"] == "user_preference":
            transaction.upsert_profile_fact(
                memory_id=memory_id,
                project=str(card_payload["project"]),
                fact_type=str(card_payload["card_type"]),
                content_hash=str(card_payload["content_hash"]),
                state=str(card_payload.get("state", "active")),
            )
        return stored


class _MemoryCardRepositoryCandidate(Protocol):
    """MemoryCard lifecycle data를 다루는 repository port 후보."""

    def get_by_id(self, memory_id: str) -> Mapping[str, Any] | None: ...

    def get_state(self, memory_id: str) -> str | None: ...


class _SessionRepositoryCandidate(Protocol):
    """Transcript session과 chunk 조회를 다루는 repository port 후보."""

    def get_session(self, session_id_hash: str) -> Mapping[str, Any] | None: ...

    def iter_chunks(self, session_id_hash: str) -> Iterable[Mapping[str, Any]]: ...


class _TranscriptRepositoryCandidate(Protocol):
    """Transcript lookup index와 raw transcript metadata를 다루는 repository port 후보."""


class _KnowledgeItemRepositoryCandidate(Protocol):
    """Base knowledge item lifecycle을 다루는 repository port 후보."""

    def get_by_id(self, knowledge_id: str) -> Mapping[str, Any] | None: ...

    def update_status(self, knowledge_id: str, status: str) -> None: ...


class _UnitOfWorkCandidate(Protocol):
    """Future transaction boundary candidate; not a public M2 contract."""

    memory_cards: _MemoryCardRepositoryCandidate
    sessions: _SessionRepositoryCandidate
    transcripts: _TranscriptRepositoryCandidate
    knowledge_items: _KnowledgeItemRepositoryCandidate

    def __enter__(self) -> _UnitOfWorkCandidate: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


_MEMORY_CURATION_METHOD_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "table": "memory_candidates",
        "method": "upsert_memory_candidate",
        "current_owner": "Ledger.MemoryPromotionMixin",
        "candidate_port": "MemoryCurationRepository",
        "migration_action": "candidate_port_only",
    },
    {
        "table": "memory_candidates",
        "method": "update_memory_candidate_state",
        "current_owner": "Ledger.MemoryPromotionMixin",
        "candidate_port": "MemoryCurationRepository",
        "migration_action": "candidate_port_only",
    },
    {
        "table": "memory_cards",
        "method": "upsert_memory_card",
        "current_owner": "Ledger.NativeMemoryMixin",
        "candidate_port": "MemoryCurationRepository",
        "migration_action": "candidate_port_only",
    },
    {
        "table": "memory_card_evidence",
        "method": "add_memory_card_evidence",
        "current_owner": "Ledger.NativeMemoryMixin",
        "candidate_port": "MemoryCurationRepository",
        "migration_action": "candidate_port_only",
    },
    {
        "table": "profile_facts",
        "method": "upsert_profile_fact",
        "current_owner": "Ledger.NativeMemoryMixin",
        "candidate_port": "MemoryCurationRepository",
        "migration_action": "candidate_port_only",
    },
)


def repository_candidate_method_matrix() -> list[dict[str, Any]]:
    """Return the M2 repository extraction candidate method matrix."""

    return [dict(row) for row in _MEMORY_CURATION_METHOD_MATRIX]


def build_repository_extraction_plan() -> dict[str, Any]:
    """Build the M2 extraction plan for the first migrated curation caller."""

    return {
        "schema_version": "agent_knowledge_repository_extraction_plan.v1",
        "milestone": "M2",
        "mode": "first_caller_migration",
        "first_candidate": {
            "name": "memory_curation",
            "port": "MemoryCurationRepository",
            "adapter": "LedgerMemoryCurationRepository",
            "activation_state": "active_for_curation_approve",
            "public_import_contract": False,
            "protocol_definition_stable": False,
            "tables": [
                "memory_candidates",
                "memory_cards",
                "memory_card_evidence",
                "profile_facts",
            ],
            "method_matrix": repository_candidate_method_matrix(),
        },
        "first_migrated_caller": {
            "caller": "CurationService.approve",
            "repository": "LedgerMemoryCurationRepository",
            "rollback_guard": "Ledger._transaction",
        },
        "next_multi_write_candidate": {
            "caller": "CurationService.supersede",
            "reason": "old_card_demote_plus_new_card_approval_multi_write",
            "status": "not_migrated_in_m2_first_caller",
            "transaction_safe_claimed": False,
        },
        "caller_migration_order": [
            {
                "caller": "CurationService.approve",
                "reason": "multi_write_transaction_target",
                "rollback_guard": "Ledger._transaction",
            },
            {
                "caller": "CurationService.reject",
                "reason": "single_candidate_state_write",
                "rollback_guard": "existing_behavior_fixture",
            },
            {
                "caller": "CurationService.disable",
                "reason": "memory_card_state_transition",
                "rollback_guard": "existing_behavior_fixture",
            },
            {
                "caller": "CurationService.supersede",
                "reason": "follow_on_multi_write_candidate",
                "rollback_guard": "future_transaction_candidate",
            },
        ],
        "rollback_guard": {
            "transaction_seam": "Ledger._transaction",
            "public_unit_of_work_activated": False,
            "fixtures": [
                "tests/test_ledger_transaction.py",
                "tests/test_curation.py",
            ],
        },
        "public_compatibility_gate": {
            "public_api_break_allowed": False,
            "ledger_public_methods_preserved": True,
            "fixtures": [
                "tests/test_curation.py",
                "tests/test_ledger_core.py",
                "tests/test_db_adapter.py",
            ],
        },
        "abort_criteria": [
            "public API break would be required",
            "existing ledger.* callers would need mass migration",
            "rollback guard would require exposing ledger.transaction() before approval",
        ],
    }

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol


class MemoryCurationRepository(Protocol):
    """M2 first repository candidate for curation-owned memory writes."""

    def upsert_memory_candidate(self, candidate: dict) -> Mapping[str, Any]: ...

    def update_memory_candidate_state(
        self,
        candidate_id: str,
        state: str,
        *,
        reviewed_by: str = "",
        reason: str = "",
    ) -> Mapping[str, Any]: ...

    def upsert_memory_card(self, card: dict) -> Mapping[str, Any]: ...

    def add_memory_card_evidence(self, memory_id: str, evidence_refs: list[dict]) -> None: ...

    def upsert_profile_fact(
        self,
        *,
        memory_id: str,
        project: str,
        fact_type: str,
        content_hash: str,
        state: str,
    ) -> None: ...


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
    """Build the M2 readiness plan without activating a public migration."""

    return {
        "schema_version": "agent_knowledge_repository_extraction_plan.v1",
        "milestone": "M2",
        "mode": "readiness_only",
        "first_candidate": {
            "name": "memory_curation",
            "port": "MemoryCurationRepository",
            "activation_state": "readiness_only",
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

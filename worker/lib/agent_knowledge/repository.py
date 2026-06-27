from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol


class MemoryCardRepository(Protocol):
    """MemoryCard lifecycle data를 다루는 repository port 후보."""

    def get_by_id(self, memory_id: str) -> Mapping[str, Any] | None: ...

    def get_state(self, memory_id: str) -> str | None: ...


class SessionRepository(Protocol):
    """Transcript session과 chunk 조회를 다루는 repository port 후보."""

    def get_session(self, session_id_hash: str) -> Mapping[str, Any] | None: ...

    def iter_chunks(self, session_id_hash: str) -> Iterable[Mapping[str, Any]]: ...


class TranscriptRepository(Protocol):
    """Transcript lookup index와 raw transcript metadata를 다루는 repository port 후보."""


class KnowledgeItemRepository(Protocol):
    """Base knowledge item lifecycle을 다루는 repository port 후보."""

    def get_by_id(self, knowledge_id: str) -> Mapping[str, Any] | None: ...

    def update_status(self, knowledge_id: str, status: str) -> None: ...


class UnitOfWork(Protocol):
    """Transaction boundary와 domain repository 접근을 묶는 port 후보."""

    memory_cards: MemoryCardRepository
    sessions: SessionRepository
    transcripts: TranscriptRepository
    knowledge_items: KnowledgeItemRepository

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

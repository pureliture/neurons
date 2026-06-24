from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .ledger import Ledger
from .session_memory.transcript_model import MAX_TRANSCRIPT_SNIPPET_CHARS, redact_and_bound_text


@dataclass(frozen=True)
class TurnRange:
    start: int
    end: int


@dataclass(frozen=True)
class ConversationChunkDetails:
    chunk_id: str
    session_id_hash: str
    turn_range: TurnRange
    snippet: str
    source_status: str
    redaction_version: int | str


@dataclass(frozen=True)
class MemoryProvenance:
    dataset: str
    ragflow_document_id: str


@dataclass(frozen=True)
class MemorySearchResultItem:
    knowledge_id: str
    result_type: str
    title: str
    domain: str
    project: str
    provider: str
    summary: str
    score: float | None
    currentness: str
    provenance: MemoryProvenance
    conversation_chunk: ConversationChunkDetails | None = None


@dataclass(frozen=True)
class MemorySearchResponse:
    results: list[MemorySearchResultItem]


@dataclass(frozen=True)
class MemorySearchQuery:
    query: str
    filters: dict | None = None
    limit: int = 10
    include_private: bool = False


class AuthorizedMemoryReader(Protocol):
    def read(self, query: MemorySearchQuery) -> MemorySearchResponse:
        """ledger가 승인한 public-safe memory 결과만 반환한다."""


class MemoryReadPipeline:
    def __init__(
        self,
        *,
        ledger: Ledger,
        ragflow,
        dataset_ids: list[str],
        allow_private_results: bool = False,
    ):
        self.ledger = ledger
        self.ragflow = ragflow
        self.dataset_ids = dataset_ids
        self.allow_private_results = bool(allow_private_results)

    def read(self, query: MemorySearchQuery) -> MemorySearchResponse:
        bounded_limit = max(1, int(query.limit))
        chunks = self.ragflow.retrieve(
            query.query,
            self.dataset_ids,
            filters=query.filters,
            limit=bounded_limit,
        )
        results: list[MemorySearchResultItem] = []
        private_allowed = bool(query.include_private and self.allow_private_results)
        for chunk in chunks:
            document_id = str(chunk.get("document_id") or chunk.get("doc_id") or "")
            if not document_id:
                continue
            item = self.ledger.authorize_document(
                document_id,
                filters=query.filters or {},
                include_private=private_allowed,
            )
            if item is None:
                continue
            provenance = MemoryProvenance(
                dataset=str(chunk.get("kb_id") or chunk.get("dataset_id") or item["ragflow_dataset_id"]),
                ragflow_document_id=item["ragflow_document_id"],
            )
            conversation_chunk_details = None
            if item["type"] == "conversation_chunk":
                conversation_chunk = self.ledger.get_conversation_chunk_by_document(document_id)
                if conversation_chunk is None:
                    continue
                conversation_chunk_details = ConversationChunkDetails(
                    chunk_id=conversation_chunk["chunk_id"],
                    session_id_hash=conversation_chunk["session_id_hash"],
                    turn_range=TurnRange(
                        start=conversation_chunk["turn_start_index"],
                        end=conversation_chunk["turn_end_index"],
                    ),
                    snippet=redact_and_bound_text(
                        str(chunk.get("content") or ""),
                        MAX_TRANSCRIPT_SNIPPET_CHARS,
                    ),
                    source_status=conversation_chunk["source_status"],
                    redaction_version=conversation_chunk["redaction_version"],
                )
            result_item = MemorySearchResultItem(
                knowledge_id=item["knowledge_id"],
                result_type=item["type"],
                title=item["title"],
                domain=item["domain"],
                project=item["project"],
                provider=item["provider"],
                summary=item["summary"],
                score=chunk.get("score"),
                currentness="server_authorized",
                provenance=provenance,
                conversation_chunk=conversation_chunk_details,
            )
            results.append(result_item)
        sliced_results = results[:bounded_limit]
        return MemorySearchResponse(results=sliced_results)

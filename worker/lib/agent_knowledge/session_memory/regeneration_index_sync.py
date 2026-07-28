from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ..ledger import SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from ..temp_upload import secure_upload_payload


_RESUMABLE_STATUSES = frozenset(
    {
        "uploaded_unparsed",
        "metadata_applied",
        "parse_requested",
        "indexing",
        "index_timeout",
    }
)
_INDEXED_REUSE_STATUSES = frozenset({"indexed", "active"})


@dataclass(frozen=True)
class SessionMemoryCoverageRecord:
    source_content_hash: str
    source_window_hash: str
    redaction_version: str
    turn_start_index: int
    turn_end_index: int


@dataclass(frozen=True)
class SessionMemoryIndexSyncRequest:
    """계획된 session-memory 문서의 index 동기화에 필요한 입력값."""

    ledger: object
    retired_index_bridge: object
    dataset_id: str
    runtime_dir: str | Path
    packed: object
    planned: Mapping[str, object]
    content_hash: str
    provider: str
    project: str
    session_id_hash: str
    coverage: Mapping[str, int]
    coverage_records: tuple[SessionMemoryCoverageRecord, ...]
    max_poll_attempts: int
    poll_interval_seconds: float
    sleep_func: Callable[[float], None]


@dataclass(frozen=True)
class SessionMemoryIndexSyncResult:
    planned: dict[str, object]
    mutation_performed: bool
    document_id: str = ""


class SessionMemoryIndexSyncExecutor:
    """기존 session-memory index 쓰기와 재개 상태 전이를 실행한다."""

    def execute(self, request: SessionMemoryIndexSyncRequest) -> SessionMemoryIndexSyncResult:
        ledger = request.ledger
        retired_index_bridge = request.retired_index_bridge
        if not ledger or not retired_index_bridge or not request.dataset_id:
            raise ValueError("session-memory sync requires ledger, retired_index_bridge, and dataset_id")

        packed = request.packed
        planned = dict(request.planned)
        knowledge_id = packed.metadata["knowledge_id"]
        existing = ledger.get_by_knowledge_id(knowledge_id)
        if existing is None:
            existing = ledger.get_by_content_hash(request.content_hash)
        existing_status = str((existing or {}).get("status") or "")
        existing_document_id = str((existing or {}).get("index_document_id") or "")
        existing_same_content = bool(existing and existing.get("content_hash") == request.content_hash)
        if existing_same_content:
            knowledge_id = str(existing.get("knowledge_id") or knowledge_id)
            packed.metadata["knowledge_id"] = knowledge_id
            planned["knowledge_id"] = knowledge_id

        if existing_same_content and existing_status in _INDEXED_REUSE_STATUSES:
            self._restore_coverage(request, knowledge_id)
            return SessionMemoryIndexSyncResult(planned=planned, mutation_performed=False)

        can_resume_existing_document = (
            existing_same_content
            and bool(existing_document_id)
            and existing_status in _RESUMABLE_STATUSES
        )
        if not can_resume_existing_document:
            stored = ledger.upsert_session_memory(
                knowledge_id=knowledge_id,
                content_hash=request.content_hash,
                provider=request.provider,
                project=request.project,
                session_id_hash=request.session_id_hash,
                title=packed.title,
                summary=packed.metadata.get("summary", ""),
                evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
                source_manifest_hash=planned["source_manifest_hash"],
                source_chunk_count=planned["source_chunk_count"],
                coverage_status=_coverage_status(request.coverage),
                coverage_gap_count=request.coverage["gap_count"],
                coverage_duplicate_count=request.coverage["duplicate_count"],
            )
            knowledge_id = str(stored.get("knowledge_id") or knowledge_id)
            packed.metadata["knowledge_id"] = knowledge_id
            planned["knowledge_id"] = knowledge_id

        self._restore_coverage(request, knowledge_id)
        document_id = self._upload_or_resume(
            request,
            knowledge_id=knowledge_id,
            existing_status=existing_status,
            existing_document_id=existing_document_id,
            can_resume_existing_document=can_resume_existing_document,
        )
        self._poll_until_indexed(request, knowledge_id=knowledge_id, document_id=document_id)
        return SessionMemoryIndexSyncResult(
            planned=planned,
            mutation_performed=True,
            document_id=document_id,
        )

    @staticmethod
    def _restore_coverage(request: SessionMemoryIndexSyncRequest, knowledge_id: str) -> None:
        for coverage_record in request.coverage_records:
            request.ledger.record_session_memory_coverage(
                active_knowledge_id=knowledge_id,
                source_content_hash=coverage_record.source_content_hash,
                source_window_hash=coverage_record.source_window_hash,
                derived_content_hash=request.content_hash,
                redaction_version=coverage_record.redaction_version,
                turn_start_index=coverage_record.turn_start_index,
                turn_end_index=coverage_record.turn_end_index,
            )

    @staticmethod
    def _upload_or_resume(
        request: SessionMemoryIndexSyncRequest,
        *,
        knowledge_id: str,
        existing_status: str,
        existing_document_id: str,
        can_resume_existing_document: bool,
    ) -> str:
        if can_resume_existing_document:
            return SessionMemoryIndexSyncExecutor._resume_existing_document(
                request,
                knowledge_id=knowledge_id,
                existing_status=existing_status,
                existing_document_id=existing_document_id,
            )
        return SessionMemoryIndexSyncExecutor._upload_new_document(request, knowledge_id=knowledge_id)

    @staticmethod
    def _resume_existing_document(
        request: SessionMemoryIndexSyncRequest,
        *,
        knowledge_id: str,
        existing_status: str,
        existing_document_id: str,
    ) -> str:
        if existing_status == "uploaded_unparsed":
            request.retired_index_bridge.update_metadata(
                request.dataset_id,
                existing_document_id,
                request.packed.metadata,
            )
            request.ledger.mark_metadata_applied(knowledge_id)
            request.retired_index_bridge.request_parse(request.dataset_id, [existing_document_id])
            request.ledger.mark_parse_requested(knowledge_id)
        elif existing_status == "metadata_applied":
            request.retired_index_bridge.request_parse(request.dataset_id, [existing_document_id])
            request.ledger.mark_parse_requested(knowledge_id)
        return existing_document_id

    @staticmethod
    def _upload_new_document(request: SessionMemoryIndexSyncRequest, *, knowledge_id: str) -> str:
        ledger = request.ledger
        retired_index_bridge = request.retired_index_bridge
        with secure_upload_payload(request.runtime_dir, request.packed.body) as upload_path:
            upload = retired_index_bridge.upload_document(
                request.dataset_id,
                upload_path.read_text(encoding="utf-8"),
                filename=request.packed.filename,
            )
        document_id = upload["document_id"]
        ledger.mark_uploaded(
            knowledge_id,
            dataset_id=request.dataset_id,
            document_id=document_id,
            run=upload["run"],
        )
        retired_index_bridge.update_metadata(request.dataset_id, document_id, request.packed.metadata)
        ledger.mark_metadata_applied(knowledge_id)
        retired_index_bridge.request_parse(request.dataset_id, [document_id])
        ledger.mark_parse_requested(knowledge_id)
        return document_id

    @staticmethod
    def _poll_until_indexed(
        request: SessionMemoryIndexSyncRequest,
        *,
        knowledge_id: str,
        document_id: str,
    ) -> None:
        last_run = "TIMEOUT"
        last_progress = 0
        for attempt in range(request.max_poll_attempts):
            status = request.retired_index_bridge.get_document_status(request.dataset_id, document_id)
            run = status["run"]
            if run == "DONE":
                request.ledger.mark_indexed(knowledge_id, run=run)
                return
            if run == "FAIL":
                request.ledger.mark_parse_failed(knowledge_id, run=run)
                raise RuntimeError(f"parse failed for {knowledge_id}")
            last_run = run or "RUNNING"
            last_progress = status.get("progress", 0)
            request.ledger.mark_indexing(knowledge_id, run=last_run, progress=last_progress)
            if request.poll_interval_seconds > 0 and attempt + 1 < request.max_poll_attempts:
                request.sleep_func(request.poll_interval_seconds)
        request.ledger.mark_index_timeout(knowledge_id, run=last_run, progress=last_progress)
        raise RuntimeError(f"index timeout for {knowledge_id}")


def _coverage_status(coverage: Mapping[str, int]) -> str:
    if coverage["gap_count"] == 0 and coverage["duplicate_count"] == 0:
        return "complete"
    return "incomplete"

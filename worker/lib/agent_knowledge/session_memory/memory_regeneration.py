from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from ..document_envelope import build_agent_id, build_document_filename
from ..ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from ..redaction import redact_public_ingress_text
from .transcript_model import MAX_PACKED_TRANSCRIPT_BODY_CHARS, REDACTION_VERSION, bound_text
from .transcript_packer import PackedTranscriptDocument
from .transcript_ingest import build_transcript_chunks
from .transcript_parsers import extract_tool_evidence, parse_transcript_source
SESSION_MEMORY_KIND = "session_memory"
SESSION_RECAP_KIND = "session_recap"
TASK_SUMMARY_KIND = "task_summary"
APPROVED_MEMORY_CARD_KIND = "approved_memory_card"
PROJECT_CONTEXT_SNAPSHOT_KIND = "project_context_snapshot"
SESSION_MEMORY_SOT_KIND = SESSION_MEMORY_KIND  # deprecated compatibility alias
SESSION_MEMORY_DATASET_ROLE = "session-memory"
PROJECT_MEMORY_DATASET_ROLE = "project-memory"
DEFAULT_SESSION_MEMORY_TARGET_PROFILE = "ragflow-session-memory"
DEFAULT_PROJECT_MEMORY_TARGET_PROFILE = "ragflow-project-memory"
MEMORY_REGENERATION_REPORT_SCHEMA_VERSION = "agent_knowledge_memory_regeneration_report.v1"
MEMORY_REGENERATION_SESSION_MEMORY_VERSION = "session-memory.v1"
MEMORY_REGENERATION_SOT_VERSION = MEMORY_REGENERATION_SESSION_MEMORY_VERSION  # deprecated compatibility alias
SESSION_RECAP_VERSION = "session-recap.v1"
PROJECT_MEMORY_REGENERATION_SUMMARY_VERSION = "deterministic-project-memory.v1"
DERIVED_MEMORY_BODY_VERSION = "retrieval-first-sections.v1"
SESSION_MEMORY_BODY_VERSION = "retrieval-first-full-source-sections-with-source-identity.v3"
SESSION_MEMORY_BODY_VERSION_WITH_EVIDENCE = "retrieval-first-full-source-sections-with-source-identity-and-tool-evidence.v4"
MAX_SESSION_MEMORY_EVIDENCE_TEXT_CHARS = 1024
LEGACY_SESSION_SUMMARY_PIPELINE_REMOVED_MESSAGE = (
    "legacy session_summary pipeline has been removed; use SessionMemoryRegenerationRunner"
)
SOURCE_CHUNK_PROVENANCE_SAMPLE_LIMIT = 20
PROJECT_SOURCE_CHUNK_PROVENANCE_SAMPLE_LIMIT = 10
SOURCE_SESSION_PROVENANCE_SAMPLE_LIMIT = 20
SESSION_MEMORY_SOURCE_MANIFEST_ALGORITHM = "source_content_hash|source_window_hash.sorted.v1"
SESSION_MEMORY_SOT_SOURCE_MANIFEST_ALGORITHM = SESSION_MEMORY_SOURCE_MANIFEST_ALGORITHM  # deprecated compatibility alias
_SESSION_MEMORY_CHUNK_HEADER_LABELS = (
    "session_id_hash",
    "source_locator_hash",
    "turn_start_index",
    "turn_end_index",
    "turn_part_index",
    "turn_part_count",
    "part_index",
    "part_count",
    "char_start",
    "char_end",
    "content_hash",
    "knowledge_id",
    "chunk_id",
    "dataset_id",
    "dataset_ref",
    "datasetId",
    "dataset_ids",
    "document_id",
    "document_ref",
    "documentId",
    "document_ids",
    "token",
    "access_token",
    "api_key",
)
_SESSION_MEMORY_HEADER_LINE_RE = re.compile(
    rf"^\s*(?:{'|'.join(re.escape(label) for label in _SESSION_MEMORY_CHUNK_HEADER_LABELS)})\s*[:=]\s*.*$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class TranscriptMemoryChunkRecord:
    knowledge_id: str
    chunk_id: str
    session_id_hash: str
    provider: str
    project: str
    turn_start_index: int
    turn_end_index: int
    observed_at_start: str
    observed_at_end: str
    content_hash: str
    redacted_text: str
    source_status: str
    redaction_version: str = REDACTION_VERSION
    part_index: int = 1
    part_count: int = 1
    char_start: int = 0
    char_end: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "redacted_text", redact_public_ingress_text(self.redacted_text))


class TranscriptMemorySource(Protocol):
    def list_conversation_chunks(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[TranscriptMemoryChunkRecord]:
        ...


class FixtureTranscriptMemorySource:
    def __init__(self, chunks: Iterable[TranscriptMemoryChunkRecord], *, tool_evidence: Iterable | None = None):
        self._chunks = list(chunks)
        self._tool_evidence = list(tool_evidence or [])

    def list_conversation_chunks(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[TranscriptMemoryChunkRecord]:
        return [
            chunk
            for chunk in self._chunks
            if (project is None or chunk.project == project)
            and (provider is None or chunk.provider == provider)
            and (session_id_hash is None or chunk.session_id_hash == session_id_hash)
        ]

    def list_tool_evidence_summaries(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list:
        return [
            item
            for item in self._tool_evidence
            if session_id_hash is None or _evidence_field(item, "session_id_hash") in ("", session_id_hash)
        ]


class RagflowTranscriptMemorySource:
    """Read redacted transcript-memory records from a read-only RAGFlow adapter."""

    def __init__(self, ragflow):
        self.ragflow = ragflow

    def list_conversation_chunks(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[TranscriptMemoryChunkRecord]:
        if not hasattr(self.ragflow, "list_transcript_memory_chunks"):
            raise ValueError("ragflow transcript-memory read adapter is required")
        records = self.ragflow.list_transcript_memory_chunks(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
        )
        return [_chunk_record_from_ragflow(record) for record in records]


class RagflowRetrievalTranscriptMemorySource:
    """Read RAGFlow retrieval hits and resolve every candidate through the local ledger."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        ragflow,
        dataset_ids: list[str],
        query: str,
        limit: int = 10,
    ):
        self.ledger = ledger
        self.ragflow = ragflow
        self.dataset_ids = list(dataset_ids)
        self.query = query
        self.limit = limit
        self.last_read_report: dict = {}

    def list_conversation_chunks(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[TranscriptMemoryChunkRecord]:
        filters = {
            "project": project or "",
            "provider": provider or "",
            "domain": "agent_memory",
            "type": "conversation_chunk",
        }
        filters = {key: value for key, value in filters.items() if value}
        chunks = self.ragflow.retrieve(self.query, self.dataset_ids, filters=filters, limit=self.limit)
        records: list[TranscriptMemoryChunkRecord] = []
        unauthorized_candidate_count = 0
        dataset_mismatch_count = 0
        for chunk in chunks or []:
            document_id = chunk.get("document_id") or chunk.get("doc_id")
            if not document_id:
                unauthorized_candidate_count += 1
                continue
            item = self.ledger.authorize_document(str(document_id), filters=filters, include_private=True)
            if item is None or item.get("type") != "conversation_chunk":
                unauthorized_candidate_count += 1
                continue
            candidate_dataset = chunk.get("kb_id") or chunk.get("dataset_id")
            if candidate_dataset and item.get("ragflow_dataset_id") and candidate_dataset != item["ragflow_dataset_id"]:
                dataset_mismatch_count += 1
                continue
            conversation_chunk = self.ledger.get_conversation_chunk_by_document(str(document_id))
            if conversation_chunk is None:
                unauthorized_candidate_count += 1
                continue
            if session_id_hash and conversation_chunk["session_id_hash"] != session_id_hash:
                unauthorized_candidate_count += 1
                continue
            records.append(_chunk_record_from_ledger(conversation_chunk, str(chunk.get("content") or "")))
        self.last_read_report = {
            "source": "ragflow_retrieval",
            "query_hash": _sha256_content(self.query),
            "dataset_ids_hash": _sha256_content("|".join(self.dataset_ids)),
            "candidate_count": len(chunks or []),
            "authorized_chunk_count": len(records),
            "unauthorized_candidate_count": unauthorized_candidate_count,
            "dataset_mismatch_count": dataset_mismatch_count,
            "network_used": True,
            "mutation_performed": False,
            "ragflow_write_performed": False,
            "raw_query_printed": False,
            "raw_chunk_content_printed": False,
            "raw_ragflow_ids_printed": False,
        }
        return records
class LedgerTranscriptMemorySource:
    """Read the local status mirror for already indexed RAGFlow transcript chunks."""

    def __init__(self, ledger: Ledger, *, densify_indexed_windows: bool = True):
        self.ledger = ledger
        self.densify_indexed_windows = densify_indexed_windows

    def list_conversation_chunks(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[TranscriptMemoryChunkRecord]:
        turn_records = self._list_turn_records(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
        )
        if turn_records:
            return turn_records
        rows = self.ledger.list_indexed_transcript_chunks(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
        )
        records = [TranscriptMemoryChunkRecord(**row) for row in rows]
        if self.densify_indexed_windows:
            return _densify_source_windows(records)
        return records

    def _list_turn_records(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[TranscriptMemoryChunkRecord]:
        if not session_id_hash:
            return []
        session = self.ledger.get_transcript_session(session_id_hash)
        if not session:
            return []
        if project and session.get("project") != project:
            return []
        if provider and session.get("provider") != provider:
            return []
        turns = self.ledger.list_transcript_turns(session_id_hash)
        records: list[TranscriptMemoryChunkRecord] = []
        for ordinal, turn in enumerate(turns, start=1):
            provider_turn_index = int(turn["turn_index"])
            role = str(turn.get("role") or "unknown")
            redacted_text = str(turn.get("redacted_text") or "")
            turn_hash = str(turn.get("turn_id_hash") or _sha256_content(f"{session_id_hash}:{provider_turn_index}:{role}"))
            fragment = _hash_fragment(turn_hash, 16)
            observed_at = str(turn.get("observed_at") or session.get("started_at") or "")
            records.append(
                TranscriptMemoryChunkRecord(
                    knowledge_id=f"source_transcript_turn_{fragment}",
                    chunk_id=f"turn_{ordinal:06d}_{fragment}",
                    session_id_hash=session_id_hash,
                    provider=str(session["provider"]),
                    project=str(session["project"]),
                    turn_start_index=ordinal,
                    turn_end_index=ordinal,
                    observed_at_start=observed_at,
                    observed_at_end=observed_at,
                    content_hash=_sha256_content(f"{turn_hash}\n{role}\n{redacted_text}"),
                    redacted_text=f"{role}: {redacted_text}",
                    source_status="indexed_transcript_turn",
                    redaction_version=REDACTION_VERSION,
                )
            )
        return records

    def list_tool_evidence_summaries(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[dict]:
        return self.ledger.list_tool_evidence_summaries(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
        )


def _densify_source_windows(records: list[TranscriptMemoryChunkRecord]) -> list[TranscriptMemoryChunkRecord]:
    dense_records: list[TranscriptMemoryChunkRecord] = []
    for ordinal, record in enumerate(records, start=1):
        data = record.__dict__.copy()
        data["turn_start_index"] = ordinal
        data["turn_end_index"] = ordinal
        data["part_index"] = 1
        data["part_count"] = 1
        data["char_start"] = 0
        data["char_end"] = len(record.redacted_text or "")
        dense_records.append(TranscriptMemoryChunkRecord(**data))
    return dense_records


class SessionFileTranscriptMemorySource:
    """Read session-memory source directly from private provider transcript files."""

    def __init__(
        self,
        *,
        source_files: Iterable[Path | str] | None = None,
        source_roots: Iterable[Path | str] | None = None,
    ):
        self.source_files = tuple(Path(path) for path in (source_files or ()))
        self.source_roots = tuple(Path(path) for path in (source_roots or ()))
        self._loaded = False
        self._chunks: list[TranscriptMemoryChunkRecord] = []
        self._tool_evidence: list = []
        self.last_read_report: dict = {}

    def list_conversation_chunks(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[TranscriptMemoryChunkRecord]:
        self._load(project=project or "", provider=provider or "")
        return [
            chunk
            for chunk in self._chunks
            if (project is None or chunk.project == project)
            and (provider is None or chunk.provider == provider)
            and (session_id_hash is None or chunk.session_id_hash == session_id_hash)
        ]

    def list_tool_evidence_summaries(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list:
        self._load(project=project or "", provider=provider or "")
        return [
            item
            for item in self._tool_evidence
            if (project is None or _evidence_field(item, "project") == project)
            and (provider is None or _evidence_field(item, "provider") == provider)
            and (session_id_hash is None or _evidence_field(item, "session_id_hash") == session_id_hash)
        ]

    def _load(self, *, project: str, provider: str) -> None:
        if self._loaded:
            return
        if not provider:
            raise ValueError("actual session file source requires provider")
        if not project:
            raise ValueError("actual session file source requires project")
        candidate_paths = _unique_session_source_paths(self.source_files, self.source_roots)
        if not candidate_paths:
            raise ValueError("actual session file source requires at least one readable source file")
        parsed_session_count = 0
        skipped_source_count = 0
        parser_error_count = 0
        chunks: list[TranscriptMemoryChunkRecord] = []
        evidence: list = []
        for path in candidate_paths:
            try:
                source_locator_hash = _sha256_content(str(path))
                parsed = parse_transcript_source(provider, path, project=project, source_locator_hash=source_locator_hash)
                parsed_session_count += 1
                chunks.extend(_transcript_memory_records_from_parsed_source(parsed))
                evidence.extend(extract_tool_evidence(provider, path, project=project, source_locator_hash=source_locator_hash))
            except ValueError:
                parser_error_count += 1
                skipped_source_count += 1
        self._chunks = chunks
        self._tool_evidence = evidence
        self._loaded = True
        self.last_read_report = {
            "source": "actual_session_file",
            "source_file_count": len(candidate_paths),
            "parsed_session_count": parsed_session_count,
            "skipped_source_count": skipped_source_count,
            "parser_error_count": parser_error_count,
            "conversation_chunk_count": len(chunks),
            "tool_evidence_count": len(evidence),
            "network_used": False,
            "mutation_performed": False,
            "ragflow_write_performed": False,
            "raw_source_paths_printed": False,
            "raw_chunk_content_printed": False,
        }


def _unique_session_source_paths(
    source_files: Iterable[Path],
    source_roots: Iterable[Path],
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for source_file in source_files:
        if source_file.is_file() and not source_file.is_symlink():
            paths.append(source_file)
    for source_root in source_roots:
        if source_root.is_file() and not source_root.is_symlink():
            paths.append(source_root)
            continue
        if not source_root.is_dir() or source_root.is_symlink():
            continue
        paths.extend(path for path in source_root.rglob("*.jsonl") if path.is_file() and not path.is_symlink())
    unique: dict[str, Path] = {}
    for path in paths:
        unique[str(path.resolve())] = path
    return tuple(unique[key] for key in sorted(unique))


def _transcript_memory_records_from_parsed_source(parsed) -> list[TranscriptMemoryChunkRecord]:
    turns_by_index = {turn.turn_index: turn for turn in parsed.turns}
    records: list[TranscriptMemoryChunkRecord] = []
    for chunk in build_transcript_chunks(parsed):
        window_turns = [
            turns_by_index[index]
            for index in range(chunk.turn_start_index, chunk.turn_end_index + 1)
            if index in turns_by_index
        ]
        observed = [turn.observed_at for turn in window_turns if turn.observed_at]
        records.append(
            TranscriptMemoryChunkRecord(
                knowledge_id=f"source_session_chunk_{_hash_fragment(chunk.chunk_id, 16)}",
                chunk_id=chunk.chunk_id,
                session_id_hash=chunk.session_id_hash,
                provider=chunk.provider,
                project=chunk.project,
                turn_start_index=chunk.turn_start_index,
                turn_end_index=chunk.turn_end_index,
                observed_at_start=observed[0] if observed else parsed.session.started_at,
                observed_at_end=observed[-1] if observed else parsed.session.ended_at or parsed.session.started_at,
                content_hash=chunk.content_hash,
                redacted_text=chunk.redacted_text,
                source_status=chunk.source_status,
                redaction_version=chunk.redaction_version,
                part_index=chunk.part_index,
                part_count=chunk.part_count,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
            )
        )
    return records


@dataclass(frozen=True)
class SessionChunkGroup:
    session_id_hash: str
    provider: str
    project: str
    chunks: tuple[TranscriptMemoryChunkRecord, ...]

    @property
    def turn_start_index(self) -> int:
        return min(chunk.turn_start_index for chunk in self.chunks)

    @property
    def turn_end_index(self) -> int:
        return max(chunk.turn_end_index for chunk in self.chunks)

    @property
    def observed_at_start(self) -> str:
        return self.chunks[0].observed_at_start

    @property
    def observed_at_end(self) -> str:
        return self.chunks[-1].observed_at_end


@dataclass(frozen=True)
class ProjectChunkGroup:
    provider: str
    project: str
    chunks: tuple[TranscriptMemoryChunkRecord, ...]

    @property
    def session_id_hashes(self) -> tuple[str, ...]:
        return tuple(sorted({chunk.session_id_hash for chunk in self.chunks}))

    @property
    def turn_start_index(self) -> int:
        return min(chunk.turn_start_index for chunk in self.chunks)

    @property
    def turn_end_index(self) -> int:
        return max(chunk.turn_end_index for chunk in self.chunks)

    @property
    def observed_at_start(self) -> str:
        return self.chunks[0].observed_at_start

    @property
    def observed_at_end(self) -> str:
        return self.chunks[-1].observed_at_end


class SessionMemoryRegenerationRunner:
    def __init__(
        self,
        *,
        source: TranscriptMemorySource,
        target_profile: str = DEFAULT_SESSION_MEMORY_TARGET_PROFILE,
        ledger: Ledger | None = None,
        sync: bool = False,
        ragflow=None,
        dataset_id: str = "",
        runtime_dir: str | Path = "",
        max_poll_attempts: int = 60,
        poll_interval_seconds: float = 1.0,
        sleep_func=None,
    ):
        import time

        self.source = source
        self.target_profile = target_profile
        self.ledger = ledger
        self.sync = sync
        self.ragflow = ragflow
        self.dataset_id = dataset_id
        self.runtime_dir = runtime_dir
        self.max_poll_attempts = max_poll_attempts
        self.poll_interval_seconds = poll_interval_seconds
        self.sleep_func = sleep_func or time.sleep

    def _list_tool_evidence_for(self, group: SessionChunkGroup) -> list:
        lister = getattr(self.source, "list_tool_evidence_summaries", None)
        if not callable(lister):
            return []
        return lister(
            project=group.project,
            provider=group.provider,
            session_id_hash=group.session_id_hash,
        )

    def run(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> dict:
        chunks = self.source.list_conversation_chunks(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
        )
        source_report = getattr(self.source, "last_read_report", {})
        groups = _group_by_session(chunks)
        duplicate_count = 0
        gap_count = 0
        deduplicated_source_chunk_count = 0
        subsumed_overlap_count = 0
        would_write_session_memory = []
        skipped_sessions: list[dict] = []
        mutated = False

        for group in groups:
            canonical_group, canonicalization = _canonical_session_group_for_memory(group)
            deduplicated_source_chunk_count += canonicalization["dropped_source_chunk_count"]
            subsumed_overlap_count += canonicalization["subsumed_overlap_count"]
            coverage = _coverage_report(canonical_group.chunks)
            duplicate_count += coverage["duplicate_count"]
            gap_count += coverage["gap_count"]
            invalid_turn_window_count = sum(
                1
                for chunk in canonical_group.chunks
                if chunk.turn_start_index <= 0 or chunk.turn_end_index < chunk.turn_start_index
            )
            if invalid_turn_window_count:
                skipped_sessions.append(
                    {
                        "reason": "invalid_turn_window",
                        "session_id_fragment": _hash_fragment(group.session_id_hash, 12),
                        "source_chunk_count": len(group.chunks),
                        "canonical_source_chunk_count": len(canonical_group.chunks),
                        "deduplicated_source_chunk_count": canonicalization["dropped_source_chunk_count"],
                        "subsumed_overlap_count": canonicalization["subsumed_overlap_count"],
                        "coverage_gap_count": coverage["gap_count"],
                        "coverage_duplicate_count": coverage["duplicate_count"],
                        "invalid_turn_window_count": invalid_turn_window_count,
                    }
                )
                continue
            if self.sync and (coverage["gap_count"] or coverage["duplicate_count"]):
                skipped_sessions.append(
                    {
                        "reason": "coverage_incomplete_before_upload",
                        "session_id_fragment": _hash_fragment(group.session_id_hash, 12),
                        "source_chunk_count": len(group.chunks),
                        "canonical_source_chunk_count": len(canonical_group.chunks),
                        "deduplicated_source_chunk_count": canonicalization["dropped_source_chunk_count"],
                        "subsumed_overlap_count": canonicalization["subsumed_overlap_count"],
                        "coverage_gap_count": coverage["gap_count"],
                        "coverage_duplicate_count": coverage["duplicate_count"],
                    }
                )
                continue
            evidence = self._list_tool_evidence_for(canonical_group)
            packed = pack_session_memory_document(canonical_group, evidence=evidence)
            content_hash = _sha256_content(packed.body)
            planned = _planned_session_memory_record(
                group=canonical_group,
                packed=packed,
                content_hash=content_hash,
                target_profile=self.target_profile,
                coverage=coverage,
                canonicalization=canonicalization,
            )

            if not self.sync:
                would_write_session_memory.append(planned)
                continue

            if not self.ledger or not self.ragflow or not self.dataset_id:
                raise ValueError("session-memory sync requires ledger, ragflow, and dataset_id")

            knowledge_id = packed.metadata["knowledge_id"]
            existing = self.ledger.get_by_knowledge_id(knowledge_id)
            if existing is None:
                existing = self.ledger.get_by_content_hash(content_hash)
            existing_status = str((existing or {}).get("status") or "")
            existing_document_id = str((existing or {}).get("ragflow_document_id") or "")
            existing_same_content = bool(existing and existing.get("content_hash") == content_hash)
            if existing_same_content:
                knowledge_id = str(existing.get("knowledge_id") or knowledge_id)
                packed.metadata["knowledge_id"] = knowledge_id
                planned["knowledge_id"] = knowledge_id
            if existing_same_content and existing_status in ("indexed", "active"):
                for chunk in _normalize_session_chunks_for_memory(canonical_group.chunks):
                    self.ledger.record_session_memory_coverage(
                        active_knowledge_id=knowledge_id,
                        source_content_hash=chunk.content_hash,
                        source_window_hash=_session_memory_source_window_hash(chunk),
                        derived_content_hash=content_hash,
                        redaction_version=chunk.redaction_version,
                        turn_start_index=chunk.turn_start_index,
                        turn_end_index=chunk.turn_end_index,
                    )
                would_write_session_memory.append(planned)
                continue

            resume_existing_document = (
                existing_same_content
                and bool(existing_document_id)
                and existing_status in {"uploaded_unparsed", "metadata_applied", "parse_requested", "indexing", "index_timeout"}
            )
            if not resume_existing_document:
                stored = self.ledger.upsert_session_memory(
                    knowledge_id=knowledge_id,
                    content_hash=content_hash,
                    provider=canonical_group.provider,
                    project=canonical_group.project,
                    session_id_hash=canonical_group.session_id_hash,
                    title=packed.title,
                    summary=packed.metadata.get("summary", ""),
                    evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
                    source_manifest_hash=planned["source_manifest_hash"],
                    source_chunk_count=planned["source_chunk_count"],
                    coverage_status=(
                        "complete"
                        if coverage["gap_count"] == 0 and coverage["duplicate_count"] == 0
                        else "incomplete"
                    ),
                    coverage_gap_count=coverage["gap_count"],
                    coverage_duplicate_count=coverage["duplicate_count"],
                )
                knowledge_id = str(stored.get("knowledge_id") or knowledge_id)
                packed.metadata["knowledge_id"] = knowledge_id
                planned["knowledge_id"] = knowledge_id
            mutated = True

            for chunk in _normalize_session_chunks_for_memory(canonical_group.chunks):
                self.ledger.record_session_memory_coverage(
                    active_knowledge_id=knowledge_id,
                    source_content_hash=chunk.content_hash,
                    source_window_hash=_session_memory_source_window_hash(chunk),
                    derived_content_hash=content_hash,
                    redaction_version=chunk.redaction_version,
                    turn_start_index=chunk.turn_start_index,
                    turn_end_index=chunk.turn_end_index,
                )

            if resume_existing_document:
                document_id = existing_document_id
                if existing_status == "uploaded_unparsed":
                    self.ragflow.update_metadata(self.dataset_id, document_id, packed.metadata)
                    self.ragflow.request_parse(self.dataset_id, [document_id])
                    self.ledger.mark_parse_requested(knowledge_id)
                elif existing_status == "metadata_applied":
                    self.ragflow.request_parse(self.dataset_id, [document_id])
                    self.ledger.mark_parse_requested(knowledge_id)
            else:
                from ..temp_upload import secure_upload_payload

                with secure_upload_payload(self.runtime_dir, packed.body) as upload_path:
                    upload = self.ragflow.upload_document(
                        self.dataset_id,
                        upload_path.read_text(encoding="utf-8"),
                        filename=packed.filename,
                    )
                document_id = upload["document_id"]
                self.ledger.mark_uploaded(knowledge_id, dataset_id=self.dataset_id, document_id=document_id, run=upload["run"])
                self.ragflow.update_metadata(self.dataset_id, document_id, packed.metadata)
                self.ragflow.request_parse(self.dataset_id, [document_id])
                self.ledger.mark_parse_requested(knowledge_id)

            last_run = "TIMEOUT"
            last_progress = 0
            indexed = False
            for attempt in range(self.max_poll_attempts):
                status = self.ragflow.get_document_status(self.dataset_id, document_id)
                run = status["run"]
                if run == "DONE":
                    self.ledger.mark_indexed(knowledge_id, run=run)
                    indexed = True
                    break
                if run == "FAIL":
                    self.ledger.mark_parse_failed(knowledge_id, run=run)
                    raise RuntimeError(f"parse failed for {knowledge_id}")
                last_run = run or "RUNNING"
                last_progress = status.get("progress", 0)
                self.ledger.mark_indexing(knowledge_id, run=last_run, progress=last_progress)
                if self.poll_interval_seconds > 0 and attempt + 1 < self.max_poll_attempts:
                    self.sleep_func(self.poll_interval_seconds)
            if not indexed:
                self.ledger.mark_index_timeout(knowledge_id, run=last_run, progress=last_progress)
                raise RuntimeError(f"index timeout for {knowledge_id}")

            would_write_session_memory.append({**planned, "document_id": document_id})

        return {
            "schema_version": MEMORY_REGENERATION_REPORT_SCHEMA_VERSION,
            "mode": "sync" if self.sync else "dry_run",
            "network_used": bool(source_report.get("network_used")) or self.sync,
            "mutation_performed": mutated,
            "ragflow_write_performed": mutated,
            "datasetRole": SESSION_MEMORY_DATASET_ROLE,
            "targetProfile": self.target_profile,
            "kind": SESSION_MEMORY_KIND,
            "source_report": source_report,
            "sessions_seen": len(groups),
            "memory_documents_planned": len(would_write_session_memory),
            "gap_count": gap_count,
            "duplicate_count": duplicate_count,
            "deduplicated_source_chunk_count": deduplicated_source_chunk_count,
            "subsumed_overlap_count": subsumed_overlap_count,
            "skipped_sessions": skipped_sessions,
            "would_write_session_memory": would_write_session_memory,
        }


class SessionMemoryBulkDryRunRunner:
    def __init__(
        self,
        *,
        source: TranscriptMemorySource,
        target_profile: str = DEFAULT_SESSION_MEMORY_TARGET_PROFILE,
        sample_limit: int = 20,
        max_sessions: int = 0,
    ):
        self.source = source
        self.target_profile = target_profile
        self.sample_limit = max(int(sample_limit), 0)
        self.max_sessions = max(int(max_sessions), 0)

    def run(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
    ) -> dict:
        chunks = self.source.list_conversation_chunks(
            project=project,
            provider=provider,
            session_id_hash=None,
        )
        source_report = getattr(self.source, "last_read_report", {})
        groups = _group_by_session(chunks)
        sessions_available = len(groups)
        if self.max_sessions:
            groups = groups[: self.max_sessions]

        classification_counts = {"ready": 0, "deduped": 0, "quarantined": 0, "skipped": 0}
        session_samples: list[dict] = []
        gap_count = 0
        duplicate_count = 0
        input_source_chunk_count = 0
        canonical_source_chunk_count = 0
        deduplicated_source_chunk_count = 0
        subsumed_overlap_count = 0
        exact_duplicate_count = 0

        for group in groups:
            canonical_group, canonicalization = _canonical_session_group_for_memory(group)
            coverage = _coverage_report(canonical_group.chunks)
            input_source_chunk_count += canonicalization["input_source_chunk_count"]
            canonical_source_chunk_count += canonicalization["canonical_source_chunk_count"]
            deduplicated_source_chunk_count += canonicalization["dropped_source_chunk_count"]
            subsumed_overlap_count += canonicalization["subsumed_overlap_count"]
            exact_duplicate_count += canonicalization["exact_duplicate_count"]
            gap_count += coverage["gap_count"]
            duplicate_count += coverage["duplicate_count"]

            if coverage["duplicate_count"]:
                classification = "quarantined"
            elif coverage["gap_count"]:
                classification = "skipped"
            elif canonicalization["dropped_source_chunk_count"]:
                classification = "deduped"
            else:
                classification = "ready"
            classification_counts[classification] += 1

            if len(session_samples) < self.sample_limit:
                session_samples.append(
                    {
                        "classification": classification,
                        "session_id_fragment": _hash_fragment(group.session_id_hash, 12),
                        "turn_range": {
                            "start": canonical_group.turn_start_index,
                            "end": canonical_group.turn_end_index,
                        },
                        "input_source_chunk_count": canonicalization["input_source_chunk_count"],
                        "canonical_source_chunk_count": canonicalization["canonical_source_chunk_count"],
                        "deduplicated_source_chunk_count": canonicalization["dropped_source_chunk_count"],
                        "exact_duplicate_count": canonicalization["exact_duplicate_count"],
                        "subsumed_overlap_count": canonicalization["subsumed_overlap_count"],
                        "coverage_gap_count": coverage["gap_count"],
                        "coverage_duplicate_count": coverage["duplicate_count"],
                    }
                )

        planned_count = classification_counts["ready"] + classification_counts["deduped"]
        return {
            "schema_version": "agent_knowledge_session_memory_bulk_dry_run.v1",
            "mode": "bulk_dry_run",
            "network_used": bool(source_report.get("network_used")),
            "mutation_performed": False,
            "ragflow_write_performed": False,
            "datasetRole": SESSION_MEMORY_DATASET_ROLE,
            "targetProfile": self.target_profile,
            "kind": SESSION_MEMORY_KIND,
            "source_report": source_report,
            "sessions_available": sessions_available,
            "sessions_seen": len(groups),
            "memory_documents_planned": planned_count,
            "ready_session_count": classification_counts["ready"],
            "deduped_session_count": classification_counts["deduped"],
            "quarantined_session_count": classification_counts["quarantined"],
            "skipped_session_count": classification_counts["skipped"],
            "classification_counts": classification_counts,
            "gap_count": gap_count,
            "duplicate_count": duplicate_count,
            "input_source_chunk_count": input_source_chunk_count,
            "canonical_source_chunk_count": canonical_source_chunk_count,
            "deduplicated_source_chunk_count": deduplicated_source_chunk_count,
            "exact_duplicate_count": exact_duplicate_count,
            "subsumed_overlap_count": subsumed_overlap_count,
            "sample_limit": self.sample_limit,
            "session_samples": session_samples,
        }


class SessionRecapRegenerationRunner:
    def __init__(
        self,
        *,
        source: TranscriptMemorySource,
        ledger: Ledger | None = None,
        enqueue_sink=None,
        enqueue: bool = False,
        target_profile: str = DEFAULT_SESSION_MEMORY_TARGET_PROFILE,
    ):
        self.source = source
        self.ledger = ledger
        self.enqueue_sink = enqueue_sink
        self.enqueue = enqueue
        self.target_profile = target_profile

    def run(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> dict:
        if self.enqueue and self.enqueue_sink is None:
            raise ValueError("session recap regeneration enqueue mode requires an enqueue sink")
        chunks = self.source.list_conversation_chunks(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
        )
        source_report = getattr(self.source, "last_read_report", {})
        groups = _group_by_session(chunks)
        duplicate_count = 0
        gap_count = 0
        would_enqueue = []
        enqueued = []
        skipped = {"coverage_gap": 0, "duplicate": 0}

        for group in groups:
            coverage = _coverage_report(group.chunks)
            duplicate_count += coverage["duplicate_count"]
            gap_count += coverage["gap_count"]
            if coverage["gap_count"]:
                skipped["coverage_gap"] += 1
                continue
            packed = pack_session_recap_document(group)
            content_hash = _sha256_content(packed.body)
            existing = self.ledger.get_by_content_hash(content_hash) if self.ledger else None
            if _existing_derived_memory_blocks_enqueue(existing, kind=SESSION_RECAP_KIND, enqueue=self.enqueue):
                skipped["duplicate"] += 1
                continue
            planned = _planned_session_recap_record(
                group=group,
                packed=packed,
                content_hash=content_hash,
                target_profile=self.target_profile,
                coverage=coverage,
            )
            if not self.enqueue:
                would_enqueue.append(planned)
                continue
            if self.ledger is not None:
                self.ledger.upsert_session_recap(
                    knowledge_id=packed.metadata["knowledge_id"],
                    content_hash=content_hash,
                    provider=group.provider,
                    project=group.project,
                    session_id_hash=group.session_id_hash,
                    title=packed.title,
                    summary=_bounded_session_recap_summary(group),
                    evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
                    coverage_status="complete",
                    coverage_gap_count=coverage["gap_count"],
                    coverage_duplicate_count=coverage["duplicate_count"],
                )
            enqueue_result = self.enqueue_sink.enqueue_document(
                source=_queue_source(group),
                packed=packed,
                content_hash=content_hash,
                target_profile=self.target_profile,
                kind=packed.kind,
                idempotency_key=_session_recap_idempotency_key(group.session_id_hash, content_hash),
            )
            job_id = str(enqueue_result.get("job_id") or "")
            if self.ledger is not None:
                self.ledger.mark_enqueued(
                    packed.metadata["knowledge_id"],
                    target_profile=self.target_profile,
                    job_id=job_id,
                    run="QUEUED",
                )
            enqueued.append({**planned, "job_id": job_id, "status": str(enqueue_result.get("status") or "queued")})

        skipped = {key: value for key, value in skipped.items() if value}
        return {
            "schema_version": MEMORY_REGENERATION_REPORT_SCHEMA_VERSION,
            "mode": "enqueue" if self.enqueue else "dry_run",
            "network_used": bool(self.enqueue) or bool(source_report.get("network_used")),
            "mutation_performed": bool(self.enqueue),
            "datasetRole": SESSION_MEMORY_DATASET_ROLE,
            "targetProfile": self.target_profile,
            "kind": SESSION_RECAP_KIND,
            "source_report": source_report,
            "sessions_seen": len(groups),
            "recaps_planned": len(would_enqueue) + len(enqueued),
            "skipped": skipped,
            "gap_count": gap_count,
            "duplicate_count": duplicate_count,
            "would_enqueue": would_enqueue,
            "enqueued": enqueued,
        }

    @classmethod
    def reconcile_indexed_recaps(
        cls,
        *,
        ledger: Ledger,
        limit: int = 50,
    ) -> dict:
        indexed = _indexed_session_recaps(ledger, limit=limit)
        return {
            "schema_version": "agent_knowledge_session_recap_reconciler.v1",
            "mode": "apply",
            "datasetRole": SESSION_MEMORY_DATASET_ROLE,
            "kind": SESSION_RECAP_KIND,
            "examined_count": len(indexed),
            "indexed_count": len(indexed),
            "promoted_count": 0,
            "blocked_count": 0,
            "mutation_performed": False,
            "network_used": False,
            "promoted": [],
            "blocked": [],
        }

    @classmethod
    def mark_ragflow_done_session_recaps(
        cls,
        *,
        ledger: Ledger,
        ragflow,
        dataset_id: str,
        limit: int = 50,
        max_pages: int = 20,
        page_size: int = 200,
    ) -> dict:
        queued = _queued_session_recaps(ledger, limit=limit)
        content_hashes = {row["content_hash"] for row in queued}
        done_docs_by_hash: dict[str, dict] = {}
        pages_scanned = 0
        for page in range(1, max(int(max_pages), 1) + 1):
            docs = ragflow.list_documents(dataset_id, page=page, page_size=page_size)
            pages_scanned += 1
            if not docs:
                break
            for doc in docs:
                meta = doc.get("meta_fields") or {}
                content_hash = str(meta.get("content_hash") or "")
                document_kind = str(meta.get("type") or meta.get("result_type") or "")
                if (
                    content_hash in content_hashes
                    and document_kind == SESSION_RECAP_KIND
                    and str(doc.get("run") or "") == "DONE"
                ):
                    done_docs_by_hash[content_hash] = doc
            if len(done_docs_by_hash) == len(content_hashes):
                break
        indexed_count = 0
        for row in queued:
            doc = done_docs_by_hash.get(row["content_hash"])
            document_id = str((doc or {}).get("id") or "")
            if not document_id:
                continue
            ledger.mark_uploaded(row["knowledge_id"], dataset_id=dataset_id, document_id=document_id, run="DONE")
            ledger.mark_indexed(row["knowledge_id"], run="DONE")
            indexed_count += 1
        return {
            "schema_version": "agent_knowledge_session_recap_ragflow_done_reconciler.v1",
            "checked_queued_count": len(queued),
            "ragflow_indexed_count": indexed_count,
            "ragflow_missing_count": len(queued) - indexed_count,
            "ragflow_pages_scanned": pages_scanned,
            "network_used": True,
            "mutation_performed": bool(indexed_count),
            "raw_ragflow_ids_printed": False,
        }


class ProjectMemoryRegenerationRunner:
    def __init__(
        self,
        *,
        source: TranscriptMemorySource,
        ledger: Ledger | None = None,
        enqueue_sink=None,
        enqueue: bool = False,
        target_profile: str = DEFAULT_PROJECT_MEMORY_TARGET_PROFILE,
    ):
        self.source = source
        self.ledger = ledger
        self.enqueue_sink = enqueue_sink
        self.enqueue = enqueue
        self.target_profile = target_profile

    def run(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> dict:
        if self.enqueue and self.enqueue_sink is None:
            raise ValueError("project memory regeneration enqueue mode requires an enqueue sink")
        chunks = self.source.list_conversation_chunks(
            project=project,
            provider=provider,
            session_id_hash=session_id_hash,
        )
        source_report = getattr(self.source, "last_read_report", {})
        groups = _group_by_project(chunks)
        would_enqueue = []
        enqueued = []
        skipped = {"duplicate": 0}

        for group in groups:
            packed = pack_project_memory_document(group)
            content_hash = _sha256_content(packed.body)
            existing = self.ledger.get_by_content_hash(content_hash) if self.ledger else None
            if _existing_derived_memory_blocks_enqueue(existing, kind=PROJECT_CONTEXT_SNAPSHOT_KIND, enqueue=self.enqueue):
                skipped["duplicate"] += 1
                continue
            planned = _planned_project_record(
                group=group,
                packed=packed,
                content_hash=content_hash,
                target_profile=self.target_profile,
            )
            if not self.enqueue:
                would_enqueue.append(planned)
                continue
            if self.ledger is not None:
                self.ledger.upsert_prepared(
                    knowledge_id=packed.metadata["knowledge_id"],
                    content_hash=content_hash,
                    provider=group.provider,
                    project=group.project,
                    domain="agent_memory",
                    type=PROJECT_CONTEXT_SNAPSHOT_KIND,
                    title=packed.title,
                    summary=_bounded_project_summary(group),
                    privacy_level="private",
                    evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
                )
            enqueue_result = self.enqueue_sink.enqueue_document(
                source=_queue_source(group),
                packed=packed,
                content_hash=content_hash,
                target_profile=self.target_profile,
                kind=packed.kind,
                idempotency_key=_project_idempotency_key(group.provider, group.project, content_hash),
            )
            job_id = str(enqueue_result.get("job_id") or "")
            if self.ledger is not None:
                self.ledger.mark_enqueued(
                    packed.metadata["knowledge_id"],
                    target_profile=self.target_profile,
                    job_id=job_id,
                    run="QUEUED",
                )
            enqueued.append({**planned, "job_id": job_id, "status": str(enqueue_result.get("status") or "queued")})

        skipped = {key: value for key, value in skipped.items() if value}
        return {
            "schema_version": MEMORY_REGENERATION_REPORT_SCHEMA_VERSION,
            "mode": "enqueue" if self.enqueue else "dry_run",
            "network_used": bool(self.enqueue) or bool(source_report.get("network_used")),
            "mutation_performed": bool(self.enqueue),
            "datasetRole": PROJECT_MEMORY_DATASET_ROLE,
            "targetProfile": self.target_profile,
            "kind": PROJECT_CONTEXT_SNAPSHOT_KIND,
            "source_report": source_report,
            "projects_seen": len(groups),
            "snapshots_planned": len(would_enqueue) + len(enqueued),
            "skipped": skipped,
            "would_enqueue": would_enqueue,
            "enqueued": enqueued,
        }

    @classmethod
    def process_dirty_projects(
        cls,
        *,
        ledger: Ledger,
        enqueue_sink=None,
        enqueue: bool = False,
        target_profile: str = DEFAULT_PROJECT_MEMORY_TARGET_PROFILE,
        limit: int = 10,
        quiet_period_seconds: int = 60,
    ) -> dict:
        if enqueue and enqueue_sink is None:
            raise ValueError("dirty project memory enqueue mode requires an enqueue sink")
        dirty_projects = ledger.list_dirty_project_memory(
            limit=limit,
            quiet_period_seconds=quiet_period_seconds,
        )
        processed = []
        failed = []
        for dirty in dirty_projects:
            try:
                report = cls(
                    source=LedgerTranscriptMemorySource(ledger),
                    ledger=ledger,
                    enqueue_sink=enqueue_sink,
                    enqueue=enqueue,
                    target_profile=target_profile,
                ).run(
                    project=dirty["project"],
                    provider=dirty["provider"],
                )
                enqueued = report.get("enqueued") or []
                if enqueue and enqueued:
                    first = enqueued[0]
                    ledger.mark_dirty_project_memory_enqueued(
                        provider=dirty["provider"],
                        project=dirty["project"],
                        snapshot_knowledge_id=str(first.get("knowledge_id") or ""),
                        ingress_job_id=str(first.get("job_id") or ""),
                    )
                elif enqueue and report.get("skipped", {}).get("duplicate"):
                    ledger.mark_dirty_project_memory_skipped(
                        provider=dirty["provider"],
                        project=dirty["project"],
                        reason="duplicate",
                    )
                processed.append(
                    {
                        "provider": dirty["provider"],
                        "project": dirty["project"],
                        "status": "processed",
                        "snapshots_planned": int(report.get("snapshots_planned") or 0),
                        "enqueued_count": len(enqueued),
                        "skipped": dict(report.get("skipped") or {}),
                    }
                )
            except Exception as exc:
                if enqueue:
                    ledger.mark_dirty_project_memory_failed(
                        provider=dirty["provider"],
                        project=dirty["project"],
                        error_class=type(exc).__name__,
                    )
                failed.append(
                    {
                        "provider": dirty["provider"],
                        "project": dirty["project"],
                        "error_class": type(exc).__name__,
                    }
                )
        return {
            "schema_version": "agent_knowledge_project_memory_dirty_processor.v1",
            "mode": "enqueue" if enqueue else "dry_run",
            "datasetRole": PROJECT_MEMORY_DATASET_ROLE,
            "targetProfile": target_profile,
            "kind": PROJECT_CONTEXT_SNAPSHOT_KIND,
            "dirty_projects_seen": len(dirty_projects),
            "processed_count": len(processed),
            "enqueued_count": sum(item["enqueued_count"] for item in processed),
            "failed_count": len(failed),
            "mutation_performed": bool(enqueue),
            "processed": processed,
            "failed": failed,
        }

    @classmethod
    def reconcile_indexed_projects(
        cls,
        *,
        ledger: Ledger,
        limit: int = 50,
    ) -> dict:
        candidates = ledger.list_project_memory_indexed_candidates(limit=limit)
        promoted = []
        blocked = []
        for candidate in candidates:
            try:
                active = ledger.promote_project_memory_snapshot(candidate["knowledge_id"])
                ledger.mark_dirty_project_memory_promoted(
                    provider=candidate["provider"],
                    project=candidate["project"],
                    snapshot_knowledge_id=candidate["knowledge_id"],
                )
                promoted.append(
                    {
                        "provider": candidate["provider"],
                        "project": candidate["project"],
                        "active_knowledge_id": active["active_knowledge_id"],
                        "active_content_hash": active["active_content_hash"],
                    }
                )
            except ValueError as exc:
                blocked.append(
                    {
                        "provider": candidate.get("provider", ""),
                        "project": candidate.get("project", ""),
                        "knowledge_id": candidate.get("knowledge_id", ""),
                        "error_class": type(exc).__name__,
                    }
                )
        return {
            "schema_version": "agent_knowledge_project_memory_reconciler.v1",
            "mode": "apply",
            "datasetRole": PROJECT_MEMORY_DATASET_ROLE,
            "kind": PROJECT_CONTEXT_SNAPSHOT_KIND,
            "examined_count": len(candidates),
            "promoted_count": len(promoted),
            "blocked_count": len(blocked),
            "mutation_performed": bool(promoted),
            "network_used": False,
            "promoted": promoted,
            "blocked": blocked,
        }

    @classmethod
    def mark_ragflow_done_project_snapshots(
        cls,
        *,
        ledger: Ledger,
        ragflow,
        dataset_id: str,
        limit: int = 50,
        max_pages: int = 20,
        page_size: int = 200,
    ) -> dict:
        queued = _queued_project_memory_snapshots(ledger, limit=limit)
        content_hashes = {row["content_hash"] for row in queued}
        done_docs_by_hash: dict[str, dict] = {}
        pages_scanned = 0
        for page in range(1, max(int(max_pages), 1) + 1):
            docs = ragflow.list_documents(dataset_id, page=page, page_size=page_size)
            pages_scanned += 1
            if not docs:
                break
            for doc in docs:
                meta = doc.get("meta_fields") or {}
                content_hash = str(meta.get("content_hash") or "")
                if content_hash in content_hashes and str(doc.get("run") or "") == "DONE":
                    done_docs_by_hash[content_hash] = doc
            if len(done_docs_by_hash) == len(content_hashes):
                break
        indexed_count = 0
        for row in queued:
            doc = done_docs_by_hash.get(row["content_hash"])
            document_id = str((doc or {}).get("id") or "")
            if not document_id:
                continue
            ledger.mark_uploaded(row["knowledge_id"], dataset_id=dataset_id, document_id=document_id, run="DONE")
            ledger.mark_indexed(row["knowledge_id"], run="DONE")
            indexed_count += 1
        return {
            "schema_version": "agent_knowledge_project_memory_ragflow_done_reconciler.v1",
            "checked_queued_count": len(queued),
            "ragflow_indexed_count": indexed_count,
            "ragflow_missing_count": len(queued) - indexed_count,
            "ragflow_pages_scanned": pages_scanned,
            "network_used": True,
            "mutation_performed": bool(indexed_count),
            "raw_ragflow_ids_printed": False,
        }
def pack_session_memory_document(
    group: SessionChunkGroup,
    *,
    evidence: list | None = None,
) -> PackedTranscriptDocument:
    evidence = list(evidence or [])
    knowledge_id = _knowledge_id_for_session_memory(group, evidence=evidence)
    chunks = _normalize_session_chunks_for_memory(group.chunks)
    if not chunks:
        raise ValueError("session-memory packer requires at least one chunk")
    turn_start_index = min(chunk.turn_start_index for chunk in chunks)
    turn_end_index = max(chunk.turn_end_index for chunk in chunks)
    observed_at_start = chunks[0].observed_at_start
    observed_at_end = chunks[-1].observed_at_end
    evidence_lines = _sectioned_tool_evidence_lines(evidence)
    content_lines = [
        "# Session Memory",
        "",
        "## Source Identity",
        "",
        f"- provider: {group.provider}",
        f"- project: {group.project}",
        f"- session_id_hash_fragment: {_hash_fragment(group.session_id_hash, 12)}",
        f"- turn_range: {turn_start_index}-{turn_end_index}",
        f"- source_chunk_count: {len(chunks)}",
        "",
        "## Transcript",
        "",
        *_sectioned_session_memory_lines(group),
        *evidence_lines,
    ]
    content_body = "\n".join(content_lines) + "\n"
    _assert_session_memory_not_truncated(content_body)
    _assert_session_memory_covers_sources(content_body, chunks)
    metadata = _session_memory_metadata(
        group,
        knowledge_id,
        turn_start_index=turn_start_index,
        turn_end_index=turn_end_index,
        evidence_count=len(evidence),
    )
    body = _render_retrieval_first_document(metadata, content_body, max_chars=None, include_result_type=False)
    filename = build_document_filename(
        kind=SESSION_MEMORY_KIND,
        provider=group.provider,
        project=group.project,
        session_id_hash=group.session_id_hash,
        turn_start_index=turn_start_index,
        turn_end_index=turn_end_index,
        observed_at_start=observed_at_start,
        content=content_body,
    )
    return PackedTranscriptDocument(
        kind=SESSION_MEMORY_KIND,
        title=f"{group.provider} session memory {turn_start_index}-{turn_end_index}",
        body=body,
        metadata=metadata,
        filename=filename,
    )


def pack_session_recap_document(group: SessionChunkGroup) -> PackedTranscriptDocument:
    knowledge_id = _knowledge_id_for_session_recap(group)
    content_lines = [
        "# Session Recap",
        "",
        "Session Recap v1",
        "",
        *_sectioned_session_recap_lines(group),
        "",
        "## Appendix: Source Coverage",
        "",
        f"- provider: {group.provider}",
        f"- project: {group.project}",
        f"- session_id_hash_fragment: {_hash_fragment(group.session_id_hash, 12)}",
        f"- turn_range: {group.turn_start_index}-{group.turn_end_index}",
        f"- source_chunk_count: {len(group.chunks)}",
        f"- recap_version: {SESSION_RECAP_VERSION}",
        "- currentness: generated_session_recap",
        "",
        "## Appendix: Source Chunks",
    ]
    for chunk in group.chunks:
        content_lines.append(
            "- "
            f"knowledge_id: {chunk.knowledge_id}; "
            f"chunk_id: {chunk.chunk_id}; "
            f"content_hash_fragment: {_hash_fragment(chunk.content_hash, 12)}; "
            f"turn_range: {chunk.turn_start_index}-{chunk.turn_end_index}"
        )

    content_body = "\n".join(content_lines) + "\n"
    metadata = _session_recap_metadata(group, knowledge_id)
    body = _render_retrieval_first_document(metadata, content_body)
    filename = build_document_filename(
        kind=SESSION_RECAP_KIND,
        provider=group.provider,
        project=group.project,
        session_id_hash=group.session_id_hash,
        turn_start_index=group.turn_start_index,
        turn_end_index=group.turn_end_index,
        observed_at_start=group.observed_at_start,
        content=content_body,
    )
    return PackedTranscriptDocument(
        kind=SESSION_RECAP_KIND,
        title=f"{group.provider} session recap {group.turn_start_index}-{group.turn_end_index}",
        body=body,
        metadata=metadata,
        filename=filename,
    )


def pack_project_memory_document(group: ProjectChunkGroup) -> PackedTranscriptDocument:
    knowledge_id = _knowledge_id_for_project_memory(group)
    session_id_hashes = group.session_id_hashes
    chunks_by_session: dict[str, list[TranscriptMemoryChunkRecord]] = {}
    for chunk in group.chunks:
        chunks_by_session.setdefault(chunk.session_id_hash, []).append(chunk)
    content_lines = [
        "# Project Memory Snapshot",
        "",
        *_sectioned_project_memory_lines(group),
        "",
        "## Appendix: Context",
        "",
        f"- provider: {group.provider}",
        f"- project: {group.project}",
        f"- source_session_count: {len(session_id_hashes)}",
        f"- source_chunk_count: {len(group.chunks)}",
        f"- turn_range: {group.turn_start_index}-{group.turn_end_index}",
        f"- summary_version: {PROJECT_MEMORY_REGENERATION_SUMMARY_VERSION}",
        "- currentness: active_project_memory_snapshot",
        "",
        "## Appendix: Source Sessions",
    ]
    for session_id_hash in session_id_hashes:
        session_chunks = chunks_by_session[session_id_hash]
        content_lines.append(
            "- "
            f"session_id_hash: {session_id_hash}; "
            f"source_chunk_count: {len(session_chunks)}; "
            f"turn_range: {min(chunk.turn_start_index for chunk in session_chunks)}-"
            f"{max(chunk.turn_end_index for chunk in session_chunks)}"
        )

    content_body = bound_text("\n".join(content_lines) + "\n", MAX_PACKED_TRANSCRIPT_BODY_CHARS)
    metadata = _project_memory_metadata(group, knowledge_id)
    body = _render_retrieval_first_document(metadata, content_body)
    filename = build_document_filename(
        kind=PROJECT_CONTEXT_SNAPSHOT_KIND,
        provider=group.provider,
        project=group.project,
        session_id_hash=_hash_fragment("|".join(session_id_hashes), 16),
        turn_start_index=group.turn_start_index,
        turn_end_index=group.turn_end_index,
        observed_at_start=group.observed_at_start,
        content=content_body,
    )
    return PackedTranscriptDocument(
        kind=PROJECT_CONTEXT_SNAPSHOT_KIND,
        title=f"{group.provider} project memory {group.project}",
        body=body,
        metadata=metadata,
        filename=filename,
    )


def _group_by_session(chunks: list[TranscriptMemoryChunkRecord]) -> list[SessionChunkGroup]:
    grouped: dict[tuple[str, str, str], list[TranscriptMemoryChunkRecord]] = {}
    for chunk in chunks:
        grouped.setdefault((chunk.session_id_hash, chunk.provider, chunk.project), []).append(chunk)
    groups = []
    for (session_id_hash, provider, project), items in grouped.items():
        ordered = tuple(sorted(items, key=_session_chunk_sort_key))
        groups.append(
            SessionChunkGroup(
                session_id_hash=session_id_hash,
                provider=provider,
                project=project,
                chunks=ordered,
            )
        )
    return sorted(groups, key=lambda item: (item.project, item.provider, item.session_id_hash))


def _group_by_project(chunks: list[TranscriptMemoryChunkRecord]) -> list[ProjectChunkGroup]:
    grouped: dict[tuple[str, str], list[TranscriptMemoryChunkRecord]] = {}
    for chunk in chunks:
        grouped.setdefault((chunk.provider, chunk.project), []).append(chunk)
    groups = []
    for (provider, project), items in grouped.items():
        ordered = tuple(
            sorted(
                items,
                key=lambda item: (
                    item.project,
                    item.provider,
                    item.session_id_hash,
                    item.turn_start_index,
                    item.turn_end_index,
                    item.chunk_id,
                ),
            )
        )
        groups.append(ProjectChunkGroup(provider=provider, project=project, chunks=ordered))
    return sorted(groups, key=lambda item: (item.project, item.provider))
def _queued_session_recaps(ledger: Ledger, *, limit: int) -> list[dict]:
    with ledger._connect() as connection:
        rows = connection.execute(
            """
            SELECT knowledge_id, content_hash
            FROM knowledge_items
            WHERE type = 'session_recap'
              AND status = 'queued'
              AND ingress_target_profile = ?
              AND content_hash != ''
            ORDER BY queued_at ASC, updated_at ASC
            LIMIT ?
            """,
            (DEFAULT_SESSION_MEMORY_TARGET_PROFILE, max(int(limit), 1)),
        ).fetchall()
    return [dict(row) for row in rows]


def _indexed_session_recaps(ledger: Ledger, *, limit: int) -> list[dict]:
    with ledger._connect() as connection:
        rows = connection.execute(
            """
            SELECT knowledge_id, content_hash
            FROM knowledge_items
            WHERE type = 'session_recap'
              AND status = 'indexed'
              AND ragflow_dataset_id != ''
              AND ragflow_document_id != ''
            ORDER BY indexed_at DESC, updated_at DESC
            LIMIT ?
            """,
            (max(int(limit), 1),),
        ).fetchall()
    return [dict(row) for row in rows]


def _queued_project_memory_snapshots(ledger: Ledger, *, limit: int) -> list[dict]:
    with ledger._connect() as connection:
        rows = connection.execute(
            """
            SELECT knowledge_id, content_hash
            FROM knowledge_items
            WHERE type = 'project_context_snapshot'
              AND status = 'queued'
              AND ingress_target_profile = ?
              AND content_hash != ''
            ORDER BY queued_at ASC, updated_at ASC
            LIMIT ?
            """,
            (DEFAULT_PROJECT_MEMORY_TARGET_PROFILE, max(int(limit), 1)),
        ).fetchall()
    return [dict(row) for row in rows]


def _chunk_record_from_ragflow(record: dict) -> TranscriptMemoryChunkRecord:
    metadata = dict(record.get("metadata") or {})
    if metadata.get("result_type") != "conversation_chunk" and metadata.get("type") != "conversation_chunk":
        raise ValueError("ragflow transcript-memory record must be a conversation_chunk")
    redacted_text = str(record.get("body") or record.get("content") or "")
    return TranscriptMemoryChunkRecord(
        knowledge_id=str(metadata["knowledge_id"]),
        chunk_id=str(metadata["chunk_id"]),
        session_id_hash=str(metadata["session_id_hash"]),
        provider=str(metadata["provider"]),
        project=str(metadata["project"]),
        turn_start_index=int(metadata["turn_start_index"]),
        turn_end_index=int(metadata["turn_end_index"]),
        observed_at_start=str(metadata.get("observed_at_start") or ""),
        observed_at_end=str(metadata.get("observed_at_end") or ""),
        content_hash=str(metadata.get("content_hash") or record.get("content_hash") or ""),
        redacted_text=redacted_text,
        source_status=str(metadata.get("source_status") or "indexed_transcript_memory"),
        redaction_version=str(metadata.get("redaction_version") or REDACTION_VERSION),
        part_index=int(metadata.get("part_index") or 1),
        part_count=int(metadata.get("part_count") or 1),
        char_start=int(metadata.get("char_start") or 0),
        char_end=int(metadata.get("char_end") or 0),
    )


def _chunk_record_from_ledger(record: dict, retrieval_content: str = "") -> TranscriptMemoryChunkRecord:
    return TranscriptMemoryChunkRecord(
        knowledge_id=str(record["knowledge_id"]),
        chunk_id=str(record["chunk_id"]),
        session_id_hash=str(record["session_id_hash"]),
        provider=str(record["provider"]),
        project=str(record["project"]),
        turn_start_index=int(record["turn_start_index"]),
        turn_end_index=int(record["turn_end_index"]),
        observed_at_start=str(record.get("observed_at_start") or record.get("created_at") or ""),
        observed_at_end=str(record.get("observed_at_end") or record.get("updated_at") or ""),
        content_hash=str(record.get("content_hash") or ""),
        redacted_text=str(retrieval_content or record.get("redacted_text") or ""),
        source_status=str(record.get("source_status") or "indexed_transcript_memory"),
        redaction_version=str(record.get("redaction_version") or REDACTION_VERSION),
    )
def _ragflow_document_sort_key(doc: dict) -> tuple[str, str]:
    return (str(doc.get("update_time") or doc.get("update_date") or doc.get("create_time") or ""), str(doc.get("name") or ""))


def _coverage_report(chunks: tuple[TranscriptMemoryChunkRecord, ...]) -> dict:
    duplicate_count = 0
    gap_count = 0
    seen_chunks = set()
    chunks_by_window: dict[tuple[int, int], list[TranscriptMemoryChunkRecord]] = {}
    for chunk in _normalize_session_chunks_for_memory(chunks):
        source_key = (chunk.chunk_id, chunk.content_hash)
        if source_key in seen_chunks:
            duplicate_count += 1
        seen_chunks.add(source_key)
        chunks_by_window.setdefault((chunk.turn_start_index, chunk.turn_end_index), []).append(chunk)

    for window_chunks in chunks_by_window.values():
        if any(chunk.part_count > 1 for chunk in window_chunks):
            if not _is_complete_multipart_window(window_chunks):
                gap_count += 1
        elif len(window_chunks) > 1:
            duplicate_count += 1

    previous_end: int | None = None
    for turn_start_index, turn_end_index in sorted(chunks_by_window):
        if previous_end is not None and turn_start_index <= previous_end:
            duplicate_count += 1
        if previous_end is not None and turn_start_index > previous_end + 1:
            gap_count += 1
        previous_end = max(previous_end or turn_end_index, turn_end_index)
    return {"duplicate_count": duplicate_count, "gap_count": gap_count}


def _canonical_session_group_for_memory(group: SessionChunkGroup) -> tuple[SessionChunkGroup, dict]:
    chunks, report = _canonicalize_session_chunks_for_memory(group.chunks)
    return (
        SessionChunkGroup(
            session_id_hash=group.session_id_hash,
            provider=group.provider,
            project=group.project,
            chunks=chunks,
        ),
        report,
    )


def _canonicalize_session_chunks_for_memory(
    chunks: tuple[TranscriptMemoryChunkRecord, ...] | Iterable[TranscriptMemoryChunkRecord],
) -> tuple[tuple[TranscriptMemoryChunkRecord, ...], dict]:
    normalized = _normalize_session_chunks_for_memory(chunks)
    deduped: list[TranscriptMemoryChunkRecord] = []
    seen_exact_sources: set[tuple[object, ...]] = set()
    exact_duplicate_count = 0
    for chunk in normalized:
        source_key = (
            chunk.content_hash,
            chunk.turn_start_index,
            chunk.turn_end_index,
            int(chunk.part_index or 1),
            int(chunk.part_count or 1),
            int(chunk.char_start or 0),
            int(chunk.char_end or 0),
            chunk.redaction_version,
        )
        if source_key in seen_exact_sources:
            exact_duplicate_count += 1
            continue
        seen_exact_sources.add(source_key)
        deduped.append(chunk)

    sanitized_text_by_index = {
        index: _sanitize_session_memory_chunk_text(chunk.redacted_text).strip()
        for index, chunk in enumerate(deduped)
    }
    subsumed_indexes: set[int] = set()
    for container_index, container in enumerate(deduped):
        container_text = sanitized_text_by_index[container_index]
        if not container_text:
            continue
        for candidate_index, candidate in enumerate(deduped):
            if container_index == candidate_index or candidate_index in subsumed_indexes:
                continue
            if not _chunk_turn_window_strictly_contains(container, candidate):
                continue
            candidate_text = sanitized_text_by_index[candidate_index]
            if candidate_text and candidate_text in container_text:
                subsumed_indexes.add(candidate_index)

    canonical = tuple(
        chunk
        for index, chunk in enumerate(deduped)
        if index not in subsumed_indexes
    )
    dropped_source_chunk_count = len(normalized) - len(canonical)
    return canonical, {
        "input_source_chunk_count": len(normalized),
        "canonical_source_chunk_count": len(canonical),
        "exact_duplicate_count": exact_duplicate_count,
        "subsumed_overlap_count": len(subsumed_indexes),
        "dropped_source_chunk_count": dropped_source_chunk_count,
    }


def _chunk_turn_window_strictly_contains(
    container: TranscriptMemoryChunkRecord,
    candidate: TranscriptMemoryChunkRecord,
) -> bool:
    container_start = int(container.turn_start_index)
    container_end = int(container.turn_end_index)
    candidate_start = int(candidate.turn_start_index)
    candidate_end = int(candidate.turn_end_index)
    return (
        (container_start, container_end) != (candidate_start, candidate_end)
        and container_start <= candidate_start
        and container_end >= candidate_end
    )


def _existing_derived_memory_blocks_enqueue(existing: dict | None, *, kind: str, enqueue: bool) -> bool:
    if not existing or existing.get("type") != kind:
        return False
    status = existing.get("status")
    if status == "disabled":
        return False
    return not (enqueue and status == "prepared")
def _session_memory_metadata(
    group: SessionChunkGroup,
    knowledge_id: str,
    *,
    turn_start_index: int | None = None,
    turn_end_index: int | None = None,
    evidence_count: int = 0,
) -> dict:
    chunks = _normalize_session_chunks_for_memory(group.chunks)
    if turn_start_index is None:
        turn_start_index = min(chunk.turn_start_index for chunk in chunks)
    if turn_end_index is None:
        turn_end_index = max(chunk.turn_end_index for chunk in chunks)
    source_manifest_hash = _session_memory_source_manifest_hash(chunks)
    body_version = SESSION_MEMORY_BODY_VERSION_WITH_EVIDENCE if evidence_count else SESSION_MEMORY_BODY_VERSION
    provenance_source_kind = "conversation_chunk+tool_evidence_summary" if evidence_count else "conversation_chunk"
    return {
        "schema_version": "agent_knowledge_document.v2",
        "result_type": SESSION_MEMORY_KIND,
        "dataset_role": SESSION_MEMORY_DATASET_ROLE,
        "knowledge_id": knowledge_id,
        "provider": group.provider,
        "project": group.project,
        "agent_id": build_agent_id(provider=group.provider, producer="memory-regeneration"),
        "session_id_hash": group.session_id_hash,
        "source_locator_hash": "derived-from-ragflow-transcript-memory",
        "chunk_id": f"session_memory_{_hash_fragment(group.session_id_hash, 16)}",
        "turn_start_index": turn_start_index,
        "turn_end_index": turn_end_index,
        "source_turn_range": f"{turn_start_index}-{turn_end_index}",
        "observed_at_start": chunks[0].observed_at_start if chunks else "",
        "observed_at_end": chunks[-1].observed_at_end if chunks else "",
        "privacy_level": "private",
        "redaction_version": REDACTION_VERSION,
        "parser_version": MEMORY_REGENERATION_SESSION_MEMORY_VERSION,
        "body_version": body_version,
        "tool_evidence_count": evidence_count,
        "source_status": "derived_from_indexed_transcript_memory",
        "evidence_status": SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        "domain": "agent_memory",
        "type": SESSION_MEMORY_KIND,
        "source_chunk_count": len(chunks),
        "source_role": "transcript-memory",
        "source_manifest_hash": source_manifest_hash,
        "source_manifest_algorithm": SESSION_MEMORY_SOURCE_MANIFEST_ALGORITHM,
        "provenance_producer": "memory-regeneration-runner",
        "provenance_status": SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        "provenance_ledger_contract": "agent_knowledge_ledger.v3",
        "provenance_source_target_profile": "ragflow-transcript-memory",
        "provenance_source_kind": provenance_source_kind,
        "retrieval_tags": ",".join([group.provider, group.project, SESSION_MEMORY_DATASET_ROLE, SESSION_MEMORY_KIND]),
        "retention_policy": "private_indefinite_until_superseded_or_disabled",
        "retention_supersedes": "",
    }


def _session_recap_metadata(group: SessionChunkGroup, knowledge_id: str) -> dict:
    return {
        "schema_version": "agent_knowledge_document.v2",
        "result_type": SESSION_RECAP_KIND,
        "dataset_role": SESSION_MEMORY_DATASET_ROLE,
        "knowledge_id": knowledge_id,
        "provider": group.provider,
        "project": group.project,
        "agent_id": build_agent_id(provider=group.provider, producer="session-recap"),
        "session_id_hash": group.session_id_hash,
        "source_locator_hash": "derived-from-ragflow-transcript-memory",
        "chunk_id": f"recap_{_hash_fragment(group.session_id_hash, 16)}",
        "turn_start_index": group.turn_start_index,
        "turn_end_index": group.turn_end_index,
        "observed_at_start": group.observed_at_start,
        "observed_at_end": group.observed_at_end,
        "privacy_level": "private",
        "redaction_version": REDACTION_VERSION,
        "parser_version": SESSION_RECAP_VERSION,
        "source_status": "derived_from_indexed_transcript_memory",
        "evidence_status": SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        "domain": "agent_memory",
        "type": SESSION_RECAP_KIND,
        "provenance": {
            "producer": "memory-regeneration-runner",
            "status": SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
            "ledger_contract": "agent_knowledge_ledger.v3",
            "source_target_profile": "ragflow-transcript-memory",
            "source_kind": "conversation_chunk",
            **_source_chunk_provenance(group.chunks),
        },
        "retrieval_hints": {
            "questions": [],
            "tags": [
                group.provider,
                group.project,
                SESSION_MEMORY_DATASET_ROLE,
                SESSION_RECAP_KIND,
            ],
        },
        "retention": {
            "policy": "private_indefinite_until_superseded_or_disabled",
            "supersedes": "",
        },
    }


def _project_memory_metadata(group: ProjectChunkGroup, knowledge_id: str) -> dict:
    return {
        "schema_version": "agent_knowledge_document.v2",
        "result_type": PROJECT_CONTEXT_SNAPSHOT_KIND,
        "dataset_role": PROJECT_MEMORY_DATASET_ROLE,
        "knowledge_id": knowledge_id,
        "provider": group.provider,
        "project": group.project,
        "agent_id": build_agent_id(provider=group.provider, producer="project-memory"),
        "project_key_hash": _sha256_content("|".join([group.provider, group.project])),
        "source_locator_hash": "derived-from-session-memory-and-transcript-memory",
        "chunk_id": f"project_{_hash_fragment(group.project, 16)}",
        "turn_start_index": group.turn_start_index,
        "turn_end_index": group.turn_end_index,
        "observed_at_start": group.observed_at_start,
        "observed_at_end": group.observed_at_end,
        "privacy_level": "private",
        "redaction_version": REDACTION_VERSION,
        "parser_version": PROJECT_MEMORY_REGENERATION_SUMMARY_VERSION,
        "source_status": "derived_from_indexed_transcript_memory",
        "domain": "agent_memory",
        "type": PROJECT_CONTEXT_SNAPSHOT_KIND,
        "provenance": {
            "producer": "memory-regeneration-runner",
            "ledger_contract": "agent_knowledge_ledger.v3",
            "source_target_profile": "ragflow-transcript-memory",
            "source_kind": "conversation_chunk",
            **_source_session_provenance(group.session_id_hashes),
            **_source_chunk_provenance(
                group.chunks,
                include_session_id_hash=True,
                sample_limit=PROJECT_SOURCE_CHUNK_PROVENANCE_SAMPLE_LIMIT,
            ),
        },
        "retrieval_hints": {
            "questions": [],
            "tags": [
                group.provider,
                group.project,
                PROJECT_MEMORY_DATASET_ROLE,
                PROJECT_CONTEXT_SNAPSHOT_KIND,
            ],
        },
        "retention": {
            "policy": "private_indefinite_until_superseded_or_disabled",
            "supersedes": "",
        },
    }


def _source_session_provenance(session_id_hashes: tuple[str, ...]) -> dict:
    sample = _bounded_source_session_sample(session_id_hashes)
    return {
        "source_session_count": len(session_id_hashes),
        "source_session_manifest_hash": _sha256_content("\n".join(session_id_hashes)),
        "source_sessions_sample_limit": SOURCE_SESSION_PROVENANCE_SAMPLE_LIMIT,
        "source_sessions_truncated": len(session_id_hashes) > len(sample),
        "source_sessions_sample": list(sample),
    }


def _bounded_source_session_sample(session_id_hashes: tuple[str, ...]) -> tuple[str, ...]:
    if len(session_id_hashes) <= SOURCE_SESSION_PROVENANCE_SAMPLE_LIMIT:
        return session_id_hashes
    head_count = SOURCE_SESSION_PROVENANCE_SAMPLE_LIMIT // 2
    tail_count = SOURCE_SESSION_PROVENANCE_SAMPLE_LIMIT - head_count
    return (*session_id_hashes[:head_count], *session_id_hashes[-tail_count:])


def _source_chunk_provenance(
    chunks: tuple[TranscriptMemoryChunkRecord, ...],
    *,
    include_session_id_hash: bool = False,
    sample_limit: int = SOURCE_CHUNK_PROVENANCE_SAMPLE_LIMIT,
) -> dict:
    material_lines = [
        "|".join(
            [
                chunk.knowledge_id,
                chunk.chunk_id,
                chunk.content_hash,
                str(chunk.turn_start_index),
                str(chunk.turn_end_index),
            ]
        )
        for chunk in chunks
    ]
    sample = _bounded_source_chunk_sample(chunks, sample_limit=sample_limit)
    return {
        "source_chunk_count": len(chunks),
        "source_chunk_manifest_hash": _sha256_content("\n".join(material_lines)),
        "source_chunks_sample_limit": sample_limit,
        "source_chunks_truncated": len(chunks) > len(sample),
        "source_chunks": [
            _source_chunk_projection(chunk, include_session_id_hash=include_session_id_hash)
            for chunk in sample
        ],
    }


def _bounded_source_chunk_sample(
    chunks: tuple[TranscriptMemoryChunkRecord, ...],
    *,
    sample_limit: int = SOURCE_CHUNK_PROVENANCE_SAMPLE_LIMIT,
) -> tuple[TranscriptMemoryChunkRecord, ...]:
    if len(chunks) <= sample_limit:
        return chunks
    head_count = sample_limit // 2
    tail_count = sample_limit - head_count
    return (*chunks[:head_count], *chunks[-tail_count:])


def _source_chunk_projection(chunk: TranscriptMemoryChunkRecord, *, include_session_id_hash: bool = False) -> dict:
    projection = {
        "knowledge_id": chunk.knowledge_id,
        "chunk_id": chunk.chunk_id,
        "content_hash": chunk.content_hash,
        "turn_start_index": chunk.turn_start_index,
        "turn_end_index": chunk.turn_end_index,
    }
    if include_session_id_hash:
        projection["session_id_hash"] = chunk.session_id_hash
    return projection


def _session_memory_source_window_hash(chunk: TranscriptMemoryChunkRecord) -> str:
    material = "|".join(
        [
            "session_memory_source_window.v1",
            str(chunk.content_hash),
            str(chunk.turn_start_index),
            str(chunk.turn_end_index),
            str(chunk.redaction_version or REDACTION_VERSION),
        ]
    )
    return _sha256_content(material)


def _session_memory_source_manifest_pairs(
    chunks: tuple[TranscriptMemoryChunkRecord, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple((str(chunk.content_hash), _session_memory_source_window_hash(chunk)) for chunk in chunks)


def _session_memory_source_manifest_hash(chunks: tuple[TranscriptMemoryChunkRecord, ...]) -> str:
    pairs = _session_memory_source_manifest_pairs(chunks)
    material = "\n".join("|".join(pair) for pair in sorted(pairs))
    return _sha256_content(material)


def _render_retrieval_first_document(
    metadata: dict,
    content_body: str,
    *,
    max_chars: int | None = MAX_PACKED_TRANSCRIPT_BODY_CHARS,
    include_result_type: bool = True,
) -> str:
    front_matter_lines = ["---", f"schema_version: {metadata['schema_version']}"]
    if include_result_type:
        front_matter_lines.append(f"result_type: {metadata['result_type']}")
    front_matter_lines.extend(["---", ""])
    front_matter = "\n".join(front_matter_lines)
    document = front_matter + content_body.rstrip() + "\n"
    if max_chars is None:
        return document
    return bound_text(document, max_chars)


def _sanitize_session_memory_chunk_text(raw_text: str) -> str:
    text = str(raw_text)
    text = "\n".join(line for line in text.splitlines() if not _SESSION_MEMORY_HEADER_LINE_RE.match(line))
    text = re.sub(
        r"\b(?:session_id_hash|source_locator_hash|turn_start_index|turn_end_index|turn_part_index|turn_part_count|part_index|part_count|char_start|char_end|content_hash|knowledge_id|chunk_id|dataset_id|dataset_ref|datasetId|dataset_ids|document_id|document_ref|documentId|document_ids|token|access_token|api_key)"
        r"\s*[:=]\s*[^\s,;\]\)\n]+",
        "<redacted:private-field>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:ds_[A-Za-z0-9_-]+|doc_[A-Za-z0-9_-]+|kn_[A-Za-z0-9_-]+|chunk_[A-Za-z0-9_-]+)\b",
        "<redacted:private-field>",
        text,
    )
    return redact_public_ingress_text(text)


def _assert_session_memory_not_truncated(body: str) -> None:
    if "[truncated]" in str(body).lower():
        import warnings
        warnings.warn(
            "session-memory body contains [truncated] marker from provider source; "
            "proceeding because the build pipeline does not truncate",
            stacklevel=2,
        )


def _assert_session_memory_covers_sources(body: str, chunks) -> None:
    """Marker-independent no-loss guard.

    The `[truncated]` marker only catches clamps that left a marker. This
    asserts every source chunk's sanitized text is present in the packed body
    (whitespace-insensitive), so a future packer-side drop or marker-less clamp
    fails at pack time instead of silently shipping a GC-unsafe document.
    """
    haystack = " ".join(str(body or "").split())
    for chunk in chunks:
        needle = " ".join(
            _sanitize_session_memory_chunk_text(str(getattr(chunk, "redacted_text", "") or "")).split()
        )
        if needle and needle not in haystack:
            raise ValueError("session-memory body dropped a source chunk; refusing GC-unsafe migration")


def _session_memory_chunk_lines(chunk: TranscriptMemoryChunkRecord, source_window_index: int) -> list[str]:
    section_title = f"### Source Window {source_window_index}: turns {chunk.turn_start_index}-{chunk.turn_end_index}"
    body = _sanitize_session_memory_chunk_text(chunk.redacted_text)
    return [
        section_title,
        "",
        body if body else "- No retrievable content.",
        "",
    ]


def _session_chunk_sort_key(chunk: TranscriptMemoryChunkRecord) -> tuple[int, int, int, int, int, str]:
    return (
        int(chunk.turn_start_index),
        int(chunk.turn_end_index),
        int(chunk.part_index or 1),
        int(chunk.char_start or 0),
        int(chunk.char_end or 0),
        str(chunk.chunk_id),
    )


def _is_complete_multipart_window(chunks: list[TranscriptMemoryChunkRecord]) -> bool:
    if not chunks:
        return False
    part_counts = {int(chunk.part_count or 1) for chunk in chunks}
    if len(part_counts) != 1:
        return False
    part_count = part_counts.pop()
    if part_count <= 1 or len(chunks) != part_count:
        return False
    part_indices = [int(chunk.part_index or 1) for chunk in chunks]
    if sorted(part_indices) != list(range(1, part_count + 1)):
        return False
    previous_char_end: int | None = None
    for chunk in sorted(chunks, key=_session_chunk_sort_key):
        char_start = int(chunk.char_start or 0)
        char_end = int(chunk.char_end or 0)
        if char_end and char_end < char_start:
            return False
        if previous_char_end is not None and char_start < previous_char_end:
            return False
        previous_char_end = char_end
    return True


def _normalize_session_chunks_for_memory(
    chunks: tuple[TranscriptMemoryChunkRecord, ...] | TranscriptMemoryChunkRecord | Iterable[TranscriptMemoryChunkRecord],
) -> tuple[TranscriptMemoryChunkRecord, ...]:
    if isinstance(chunks, tuple):
        ordered = chunks
    elif isinstance(chunks, TranscriptMemoryChunkRecord):
        ordered = (chunks,)
    else:
        ordered = tuple(chunks)
    return tuple(sorted(ordered, key=_session_chunk_sort_key))


def _sectioned_session_memory_lines(group: SessionChunkGroup) -> list[str]:
    lines: list[str] = []
    chunks = _normalize_session_chunks_for_memory(group.chunks)
    for index, chunk in enumerate(chunks, start=1):
        lines.extend(_session_memory_chunk_lines(chunk, index))
    return lines


def _evidence_field(item, key: str) -> str:
    if isinstance(item, dict):
        value = item.get(key, "")
    else:
        value = getattr(item, key, "")
    return value if value is not None else ""


def _sectioned_tool_evidence_lines(evidence: list) -> list[str]:
    """Render redacted tool-evidence records into a session_memory v3 section.

    Re-applies the public-ingress redactor as defence in depth even though the
    evidence records are already redacted at the source layer.
    """
    if not evidence:
        return []
    ordered = sorted(evidence, key=lambda item: int(_evidence_field(item, "evidence_index") or 0))
    lines: list[str] = ["", "## Tool Evidence", ""]
    for item in ordered:
        category = _evidence_field(item, "category")
        outcome = _evidence_field(item, "outcome")
        index = _evidence_field(item, "evidence_index")
        command = redact_public_ingress_text(str(_evidence_field(item, "command_summary")))
        summary = redact_public_ingress_text(str(_evidence_field(item, "redacted_summary")))
        lines.extend(
            [
                f"### {index} {category}/{outcome}",
                f"- command: {command}",
                f"- result: {summary}",
                "",
            ]
        )
    return lines


def _first_matching_snippets(
    group: SessionChunkGroup | ProjectChunkGroup,
    keywords: tuple[str, ...],
    *,
    limit: int,
) -> list[str]:
    matches: list[str] = []
    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    for chunk in group.chunks:
        text = _recap_source_text(chunk)
        if not text:
            continue
        lowered = text.lower()
        if lowered_keywords and not any(keyword in lowered for keyword in lowered_keywords):
            continue
        matches.append(bound_text(text.replace("\n", " "), 280))
        if len(matches) >= limit:
            break
    return matches
def _recap_source_text(chunk: TranscriptMemoryChunkRecord) -> str:
    return redact_public_ingress_text(chunk.redacted_text).strip()
def _bullets_or_fallback(snippets: list[str], fallback: str) -> list[str]:
    if snippets:
        return [f"- {snippet}" for snippet in snippets]
    return [f"- {fallback}"]
def _sectioned_session_recap_lines(group: SessionChunkGroup) -> list[str]:
    outcome = _first_matching_snippets(group, ("outcome", "implemented", "fixed", "completed", "결과", "완료"), limit=1)
    decisions = _first_matching_snippets(group, ("decision", "decided", "keep", "policy", "결정", "유지"), limit=1)
    touched = _first_matching_snippets(group, ("file", "runtime", "touched", "modified", "path", "파일", "런타임"), limit=1)
    verification = _first_matching_snippets(group, ("verification", "verified", "pytest", "test", "검증", "테스트"), limit=1)
    followups = _first_matching_snippets(group, ("follow-up", "follow-ups", "next", "todo", "후속"), limit=1)
    goal = _first_matching_snippets(group, ("goal", "objective", "목표"), limit=1)
    work = _first_matching_snippets(group, ("work completed", "work performed", "implemented", "added", "changed", "작업"), limit=1)
    current_state = _first_matching_snippets(group, ("current state", "current runtime state", "state", "상태", "현재"), limit=1)
    risks = _first_matching_snippets(group, ("open risks", "risk", "blocked", "approval", "리스크", "승인"), limit=1)
    evidence = [
        (
            f"Source transcript coverage: {len(group.chunks)} chunks, turns "
            f"{group.turn_start_index}-{group.turn_end_index}, session fragment {_hash_fragment(group.session_id_hash, 12)}."
        )
    ]
    return [
        "## Outcome",
        "",
        *_bullets_or_fallback(outcome, _first_general_recap_bullet(group, "No outcome detected; review source summary before relying on this recap.")),
        "",
        "## Decisions",
        "",
        *_bullets_or_fallback(decisions, "No durable decision detected in source summary."),
        "",
        "## Files / Runtime touched",
        "",
        *_bullets_or_fallback(touched, "No file or runtime touch detected in source summary."),
        "",
        "## Verification",
        "",
        *_bullets_or_fallback(verification, "No verification evidence detected in source summary."),
        "",
        "## Follow-ups",
        "",
        *_bullets_or_fallback(followups, "No follow-up detected in source summary."),
        "",
        "## Goal",
        "",
        *_bullets_or_fallback(goal, "No separate goal detected; use Outcome as the handoff anchor."),
        "",
        "## Work Completed",
        "",
        *_bullets_or_fallback(work, _first_general_recap_bullet(group, "No separate work-completed line detected in source summary.")),
        "",
        "## Current State",
        "",
        *_bullets_or_fallback(current_state, _first_general_recap_bullet(group, "No current state detected in source summary.")),
        "",
        "## Risks / Caveats",
        "",
        *_bullets_or_fallback(risks, "No risk or caveat detected in source summary."),
        "",
        "## Evidence Pointers",
        "",
        *[f"- {snippet}" for snippet in evidence],
    ]


def _first_general_recap_bullet(group: SessionChunkGroup, fallback: str) -> str:
    for chunk in group.chunks:
        text = _recap_source_text(chunk)
        if text:
            return bound_text(text.replace("\n", " "), 280)
    return fallback


def _sectioned_project_memory_lines(group: ProjectChunkGroup) -> list[str]:
    runtime_state = _first_matching_snippets(
        group,
        ("current runtime state", "runtime", "healthz", "route", "현재", "상태"),
        limit=3,
    )
    dataset_shape = _first_matching_snippets(
        group,
        ("dataset shape", "project-memory", "session-memory", "docs", "active snapshots", "데이터셋"),
        limit=3,
    )
    routes = _first_matching_snippets(
        group,
        ("active routes", "memory-regeneration", "output project-memory", "direct lookup", "route", "경로"),
        limit=3,
    )
    decisions = _first_matching_snippets(
        group,
        ("recent decisions", "decision", "keep", "policy", "결정"),
        limit=3,
    )
    risks = _first_matching_snippets(
        group,
        ("open risks", "follow-up", "risk", "todo", "approval", "리스크", "후속"),
        limit=3,
    )
    identity = (
        f"{group.project} project_context_snapshot for {group.provider}/{group.project}; "
        f"{len(group.session_id_hashes)} source sessions and {len(group.chunks)} transcript chunks "
        f"cover turns {group.turn_start_index}-{group.turn_end_index}."
    )
    dataset_fallback = (
        f"project-memory emits one active project_context_snapshot document per project key; "
        f"current filename remains {group.project}.md."
    )
    dataset_shape_lines = [identity, *dataset_shape] if dataset_shape else [dataset_fallback]
    return [
        "## Current Runtime State",
        "",
        *_bullets_or_fallback(runtime_state, identity),
        "",
        "## Dataset Shape",
        "",
        *[f"- {snippet}" for snippet in dataset_shape_lines],
        "",
        "## Active Routes",
        "",
        *_bullets_or_fallback(routes, "project-memory writes only through memory-regeneration --output project-memory."),
        "",
        "## Recent Decisions",
        "",
        *_bullets_or_fallback(decisions, "No explicit recent decision line detected in the redacted source chunks."),
        "",
        "## Open Risks / Follow-ups",
        "",
        *_bullets_or_fallback(risks, "No explicit open risk or follow-up line detected in the redacted source chunks."),
    ]
def _bounded_session_recap_summary(group: SessionChunkGroup) -> str:
    return bound_text(
        redact_public_ingress_text(
            "\n".join(["# Session Recap", "", "Session Recap v1", "", *_sectioned_session_recap_lines(group)]) + "\n"
        ),
        2200,
    )


def _bounded_project_summary(group: ProjectChunkGroup) -> str:
    snippets = []
    for chunk in group.chunks[:5]:
        snippets.append(
            bound_text(
                redact_public_ingress_text(
                    f"Session {chunk.session_id_hash} turns {chunk.turn_start_index}-{chunk.turn_end_index}: "
                    f"{chunk.redacted_text}"
                ),
                500,
            )
        )
    joined = "\n".join(f"- {snippet}" for snippet in snippets)
    return bound_text(
        redact_public_ingress_text(
            (
                f"Deterministic project memory snapshot for {group.provider}/{group.project}: "
                f"{len(group.session_id_hashes)} source sessions and {len(group.chunks)} redacted transcript chunks "
                f"cover turns {group.turn_start_index}-{group.turn_end_index}.\n{joined}"
            )
        ),
        2200,
    )


def _planned_session_memory_record(
    *,
    group: SessionChunkGroup,
    packed: PackedTranscriptDocument,
    content_hash: str,
    target_profile: str,
    coverage: dict,
    canonicalization: dict | None = None,
) -> dict:
    idempotency_key = _idempotency_key_for_session_memory(group.session_id_hash, content_hash)
    canonicalization = canonicalization or {
        "input_source_chunk_count": len(group.chunks),
        "canonical_source_chunk_count": len(group.chunks),
        "exact_duplicate_count": 0,
        "subsumed_overlap_count": 0,
        "dropped_source_chunk_count": 0,
    }
    return {
        "memory_id_fragment": _hash_fragment(packed.metadata["knowledge_id"], 16),
        "session_id_fragment": _hash_fragment(group.session_id_hash, 12),
        "provider": group.provider,
        "project": group.project,
        "datasetRole": SESSION_MEMORY_DATASET_ROLE,
        "targetProfile": target_profile,
        "kind": SESSION_MEMORY_KIND,
        "contentHash": content_hash,
        "tool_evidence_count": packed.metadata.get("tool_evidence_count", 0),
        "idempotency_key_hash": _sha256_content(idempotency_key),
        "turn_range": {"start": packed.metadata["turn_start_index"], "end": packed.metadata["turn_end_index"]},
        "observed_at_start": packed.metadata["observed_at_start"],
        "observed_at_end": packed.metadata["observed_at_end"],
        "source_chunk_count": len(group.chunks),
        "input_source_chunk_count": canonicalization["input_source_chunk_count"],
        "canonical_source_chunk_count": canonicalization["canonical_source_chunk_count"],
        "deduplicated_source_chunk_count": canonicalization["dropped_source_chunk_count"],
        "exact_duplicate_count": canonicalization["exact_duplicate_count"],
        "subsumed_overlap_count": canonicalization["subsumed_overlap_count"],
        "source_manifest_hash": packed.metadata["source_manifest_hash"],
        "coverage_readiness_status": (
            "ready_for_upload"
            if coverage["gap_count"] == 0 and coverage["duplicate_count"] == 0
            else "blocked_coverage"
        ),
        "gap_count": coverage["gap_count"],
        "duplicate_count": coverage["duplicate_count"],
    }


def _planned_session_recap_record(
    *,
    group: SessionChunkGroup,
    packed: PackedTranscriptDocument,
    content_hash: str,
    target_profile: str,
    coverage: dict,
) -> dict:
    return {
        "knowledge_id": packed.metadata["knowledge_id"],
        "session_id_hash": group.session_id_hash,
        "provider": group.provider,
        "project": group.project,
        "datasetRole": SESSION_MEMORY_DATASET_ROLE,
        "targetProfile": target_profile,
        "kind": SESSION_RECAP_KIND,
        "contentHash": content_hash,
        "idempotencyKey": _session_recap_idempotency_key(group.session_id_hash, content_hash),
        "turn_range": {"start": group.turn_start_index, "end": group.turn_end_index},
        "observed_at_start": group.observed_at_start,
        "observed_at_end": group.observed_at_end,
        "source_chunk_count": len(group.chunks),
        "gap_count": coverage["gap_count"],
        "duplicate_count": coverage["duplicate_count"],
    }


def _planned_project_record(
    *,
    group: ProjectChunkGroup,
    packed: PackedTranscriptDocument,
    content_hash: str,
    target_profile: str,
) -> dict:
    return {
        "knowledge_id": packed.metadata["knowledge_id"],
        "provider": group.provider,
        "project": group.project,
        "datasetRole": PROJECT_MEMORY_DATASET_ROLE,
        "targetProfile": target_profile,
        "kind": PROJECT_CONTEXT_SNAPSHOT_KIND,
        "contentHash": content_hash,
        "idempotencyKey": _project_idempotency_key(group.provider, group.project, content_hash),
        "turn_range": {"start": group.turn_start_index, "end": group.turn_end_index},
        "observed_at_start": group.observed_at_start,
        "observed_at_end": group.observed_at_end,
        "source_session_count": len(group.session_id_hashes),
        "source_chunk_count": len(group.chunks),
    }


def _queue_source(group: SessionChunkGroup) -> dict[str, str]:
    return {
        "host": "mac_mini",
        "producer": "memory-regeneration-runner",
        "provider": group.provider,
        "project": group.project,
    }


def _knowledge_id_for_session_memory(group: SessionChunkGroup, *, evidence: list | None = None) -> str:
    chunks = _normalize_session_chunks_for_memory(group.chunks)
    evidence = sorted(evidence or [], key=lambda item: int(_evidence_field(item, "evidence_index") or 0))
    body_version = SESSION_MEMORY_BODY_VERSION_WITH_EVIDENCE if evidence else SESSION_MEMORY_BODY_VERSION
    evidence_identity = [
        "|".join(
            [
                str(_evidence_field(item, "evidence_index")),
                str(_evidence_field(item, "evidence_id_hash") or _evidence_field(item, "content_hash")),
                str(_evidence_field(item, "category")),
                str(_evidence_field(item, "outcome")),
            ]
        )
        for item in evidence
    ]
    seed = "|".join(
        [
            SESSION_MEMORY_KIND,
            MEMORY_REGENERATION_SESSION_MEMORY_VERSION,
            body_version,
            group.session_id_hash,
            group.provider,
            group.project,
            _session_memory_source_manifest_hash(_normalize_session_chunks_for_memory(group.chunks)),
            *[chunk.content_hash for chunk in chunks],
            *evidence_identity,
        ]
    )
    return "kn_session_memory_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _knowledge_id_for_session_recap(group: SessionChunkGroup) -> str:
    seed = "|".join(
        [
            SESSION_RECAP_KIND,
            SESSION_RECAP_VERSION,
            DERIVED_MEMORY_BODY_VERSION,
            group.session_id_hash,
            group.provider,
            group.project,
            *[chunk.content_hash for chunk in group.chunks],
        ]
    )
    return "kn_session_recap_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _knowledge_id_for_project_memory(group: ProjectChunkGroup) -> str:
    seed = "|".join(
        [
            PROJECT_CONTEXT_SNAPSHOT_KIND,
            PROJECT_MEMORY_REGENERATION_SUMMARY_VERSION,
            DERIVED_MEMORY_BODY_VERSION,
            group.provider,
            group.project,
            *[chunk.content_hash for chunk in group.chunks],
        ]
    )
    return "kn_project_memory_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
def _idempotency_key_for_session_memory(session_id_hash: str, content_hash: str) -> str:
    return f"{SESSION_MEMORY_KIND}:{session_id_hash}:{content_hash}"


def _session_recap_idempotency_key(session_id_hash: str, content_hash: str) -> str:
    return f"{SESSION_RECAP_KIND}:{session_id_hash}:{content_hash}"


def _project_idempotency_key(provider: str, project: str, content_hash: str) -> str:
    return f"{PROJECT_CONTEXT_SNAPSHOT_KIND}:{provider}:{project}:{content_hash}"


def _sha256_content(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_fragment(value: str, length: int) -> str:
    if ":" in value:
        value = value.split(":", 1)[1]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]

# Deprecated compatibility aliases for pre-terminology-unification callers.
SessionMemorySotRegenerationRunner = SessionMemoryRegenerationRunner
SessionMemorySotBulkDryRunRunner = SessionMemoryBulkDryRunRunner
pack_session_sot_document = pack_session_memory_document
_knowledge_id_for_session_sot = _knowledge_id_for_session_memory
_idempotency_key_for_session_sot = _idempotency_key_for_session_memory

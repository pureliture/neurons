from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from .transcript_model import REDACTION_VERSION


TRANSCRIPT_MEMORY_GC_OPERATION = "memory_regeneration_gc_transcript_memory_disable"
TRANSCRIPT_MEMORY_GC_REENABLE_OPERATION = "memory_regeneration_gc_transcript_memory_reenable"
TRANSCRIPT_MEMORY_GC_SCHEMA_VERSION = "agent_knowledge_transcript_memory_gc.v1"
TRANSCRIPT_MEMORY_GC_ALLOWED_RETENTION_POLICIES: frozenset[str] = frozenset({"private_indefinite_until_disabled"})

CANDIDATE_SCOPE_EXACT_COVERAGE = "exact-coverage"
CANDIDATE_SCOPE_SESSION_SEARCH = "session-search-surface"
CANDIDATE_SCOPES = (CANDIDATE_SCOPE_EXACT_COVERAGE, CANDIDATE_SCOPE_SESSION_SEARCH)

TRANSCRIPT_MEMORY_GC_CONTRACTS = {
    CANDIDATE_SCOPE_EXACT_COVERAGE: "active_session_memory_authorized_complete_coverage",
    CANDIDATE_SCOPE_SESSION_SEARCH: "active_session_memory_authorized_complete_session_search_surface",
}

EMPTY_COVERED_SOURCE_WINDOW_HASH_SQL = "'' AS covered_source_window_hash"
EDGE_COVERED_SOURCE_WINDOW_HASH_SQL = "edge.source_window_hash AS covered_source_window_hash"

BASE_CANDIDATE_SELECT_SQL = """
                SELECT
                    source.knowledge_id,
                    source.content_hash,
                    source.provider,
                    source.project,
                    tc.session_id_hash,
                    source.ragflow_dataset_id,
                    source.ragflow_document_id,
                    source.indexed_at,
                    source.updated_at,
                    source.metadata_json,
                    tc.redacted_text,
                    tc.turn_start_index,
                    tc.turn_end_index,
                    tc.redaction_version,
                    active.knowledge_id AS active_knowledge_id,
                    active.ragflow_document_id AS active_document_id,
                    {covered_source_window_hash}
                FROM knowledge_items source
                JOIN transcript_chunks tc
                  ON tc.knowledge_id = source.knowledge_id
                JOIN dirty_session_memory dirty
                  ON dirty.session_id_hash = tc.session_id_hash
                 AND dirty.provider = tc.provider
                 AND dirty.project = tc.project
                JOIN session_memory_active_snapshots snapshot
                  ON snapshot.session_id_hash = tc.session_id_hash
                JOIN knowledge_items active
                  ON active.knowledge_id = snapshot.active_knowledge_id
                 AND active.provider = tc.provider
                 AND active.project = tc.project
                {coverage_join}
                WHERE source.type = 'conversation_chunk'
                  AND source.status = 'indexed'
                  AND source.authorization_status = 'active'
                  AND source.disabled_at = ''
                  AND source.ragflow_dataset_id = ?
                  AND source.ragflow_document_id != ''
                  AND coalesce(nullif(source.indexed_at, ''), nullif(source.updated_at, '')) != ''
                  AND coalesce(nullif(source.indexed_at, ''), nullif(source.updated_at, '')) <= ?
                  AND dirty.status = 'promoted'
                  AND coalesce(nullif(snapshot.updated_at, ''), nullif(snapshot.activated_at, '')) != ''
                  AND julianday(
                        replace(
                            coalesce(nullif(snapshot.updated_at, ''), nullif(snapshot.activated_at, '')),
                            'Z',
                            '+00:00'
                        )
                      )
                      >= julianday(replace(dirty.dirty_at, 'Z', '+00:00'))
                  AND active.type = 'session_memory'
                  AND active.status IN ('indexed', 'active')
                  AND active.authorization_status = 'active'
                  AND active.disabled_at = ''
                  AND active.evidence_status = ?
                  AND active.coverage_status = 'complete'
                  AND active.coverage_gap_count = 0
                  AND active.coverage_duplicate_count = 0
                  AND active.ragflow_document_id != ''
                ORDER BY source.indexed_at ASC, source.updated_at ASC, source.knowledge_id ASC
                """

EXACT_COVERAGE_JOIN_SQL = """
                JOIN session_memory_coverage_edges edge
                  ON edge.active_knowledge_id = active.knowledge_id
                 AND edge.source_content_hash = source.content_hash
                """


@dataclass(frozen=True)
class TranscriptMemoryGcConfig:
    ledger_path: Path
    dataset_id: str
    ragflow_url: str
    session_memory_dataset_id: str = ""
    candidate_scope: str = CANDIDATE_SCOPE_EXACT_COVERAGE
    max_items: int = 25
    min_indexed_age_seconds: int = 86400
    execute_disable: bool = False
    verify_search_surface: bool = False
    retrieval_limit: int = 10
    declared_dataset_role: str = ""
    declared_retention_policy: str = ""

    def declared_policy_input(self) -> str:
        return (self.declared_retention_policy or self.declared_dataset_role or "").strip()


@dataclass(frozen=True)
class CandidateQuery:
    sql: str
    params: tuple[str, str, str]
    requires_window_hash: bool


@dataclass(frozen=True)
class DisableResult:
    attempted_count: int = 0
    disabled_count: int = 0
    failed_count: int = 0
    failed_error_class: str = ""


class TranscriptMemoryGcRunner:
    def __init__(self, *, config: TranscriptMemoryGcConfig, token: str = ""):
        self.config = config
        self.token = token

    def run(self) -> dict:
        declared = self.config.declared_policy_input()
        if declared and _resolve_retention_policy(declared) not in TRANSCRIPT_MEMORY_GC_ALLOWED_RETENTION_POLICIES:
            return _blocked_retention_policy_report(self.config)
        ledger = Ledger(self.config.ledger_path)
        candidates = self._list_candidates(ledger)
        selected = candidates[: _positive_int(self.config.max_items)]
        search_surface = _empty_search_surface_report(enabled=bool(self.config.verify_search_surface))
        disable_rows = _disable_rows(selected, search_surface)
        if self.config.execute_disable:
            disable_result = DisableResult(failed_error_class="live_execution_not_vendored")
            return _build_report(
                config=self.config,
                candidates=candidates,
                selected=selected,
                disable_rows=disable_rows,
                disable_result=disable_result,
                search_surface=search_surface,
                status="blocked_live_execution",
            )
        return _build_report(
            config=self.config,
            candidates=candidates,
            selected=selected,
            disable_rows=disable_rows,
            disable_result=DisableResult(),
            search_surface=search_surface,
        )

    def _list_candidates(self, ledger: Ledger) -> list[dict]:
        query = _candidate_query(self.config)
        with ledger._connect() as connection:
            rows = connection.execute(query.sql, query.params).fetchall()
        return self._filter_candidate_rows(ledger, rows, requires_window_hash=query.requires_window_hash)

    def _filter_candidate_rows(self, ledger: Ledger, rows, *, requires_window_hash: bool) -> list[dict]:
        candidates: list[dict] = []
        active_cache: dict[str, bool] = {}
        seen_sources: set[str] = set()
        for item in rows:
            row = dict(item)
            knowledge_id = str(row.get("knowledge_id") or "")
            if knowledge_id in seen_sources:
                continue
            if not self._candidate_is_safe(ledger, row, active_cache, requires_window_hash=requires_window_hash):
                continue
            seen_sources.add(knowledge_id)
            candidates.append(row)
        return candidates

    def _candidate_is_safe(
        self,
        ledger: Ledger,
        row: dict,
        active_cache: dict[str, bool],
        *,
        requires_window_hash: bool,
    ) -> bool:
        knowledge_id = str(row.get("knowledge_id") or "")
        if not knowledge_id or _is_gc_disabled(row):
            return False
        active_knowledge_id = str(row.get("active_knowledge_id") or "")
        if not _active_replacement_is_authorized(ledger, active_knowledge_id, active_cache):
            return False
        if not requires_window_hash:
            return True
        return str(row.get("covered_source_window_hash") or "") == _source_window_hash(row)


def _candidate_query(config: TranscriptMemoryGcConfig) -> CandidateQuery:
    cutoff = _cutoff_iso(config.min_indexed_age_seconds)
    params = (config.dataset_id, cutoff, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS)
    if config.candidate_scope == CANDIDATE_SCOPE_EXACT_COVERAGE:
        return CandidateQuery(
            sql=_candidate_sql(
                coverage_join=EXACT_COVERAGE_JOIN_SQL,
                covered_source_window_hash=EDGE_COVERED_SOURCE_WINDOW_HASH_SQL,
            ),
            params=params,
            requires_window_hash=True,
        )
    if config.candidate_scope == CANDIDATE_SCOPE_SESSION_SEARCH:
        return CandidateQuery(
            sql=_candidate_sql(
                coverage_join="",
                covered_source_window_hash=EMPTY_COVERED_SOURCE_WINDOW_HASH_SQL,
            ),
            params=params,
            requires_window_hash=False,
        )
    raise ValueError(f"unsupported candidate scope: {config.candidate_scope}")


def _candidate_sql(*, coverage_join: str, covered_source_window_hash: str) -> str:
    return BASE_CANDIDATE_SELECT_SQL.format(
        coverage_join=coverage_join,
        covered_source_window_hash=covered_source_window_hash,
    )


def _cutoff_iso(min_indexed_age_seconds: int) -> str:
    age = max(int(min_indexed_age_seconds), 0)
    return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()


def _positive_int(value: int) -> int:
    return max(int(value), 1)


def _metadata_dict(row: dict) -> dict:
    try:
        metadata = json.loads(str(row.get("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _replacement_contract(candidate_scope: str) -> str:
    return TRANSCRIPT_MEMORY_GC_CONTRACTS.get(
        candidate_scope,
        TRANSCRIPT_MEMORY_GC_CONTRACTS[CANDIDATE_SCOPE_EXACT_COVERAGE],
    )


def _empty_search_surface_report(*, enabled: bool) -> dict:
    return {
        "enabled": enabled,
        "scope": "session_memory_dataset_search" if enabled else "",
        "checked_count": 0,
        "passed_count": 0,
        "failed_count": 0,
        "query_attempt_count": 0,
        "network_used": False,
        "raw_query_printed": False,
        "raw_chunk_content_printed": False,
        "raw_ragflow_ids_printed": False,
    }


def _is_gc_disabled(row: dict) -> bool:
    gc = _metadata_dict(row).get("transcript_memory_gc")
    return isinstance(gc, dict) and gc.get("status") in {"disabled", "deleted"}


def _source_window_hash(row: dict) -> str:
    material = "|".join(
        [
            "session_memory_source_window.v1",
            str(row.get("content_hash") or ""),
            str(int(row.get("turn_start_index") or 0)),
            str(int(row.get("turn_end_index") or 0)),
            str(row.get("redaction_version") or REDACTION_VERSION),
        ]
    )
    return _sha256_content(material)


def _active_replacement_is_authorized(ledger: Ledger, knowledge_id: str, cache: dict[str, bool]) -> bool:
    item = ledger.get_by_knowledge_id(knowledge_id)
    if not item or not item.get("ragflow_document_id"):
        return False
    if not ledger._session_memory_coverage_edges_are_complete(item):
        return False
    if knowledge_id not in cache:
        cache[knowledge_id] = ledger.authorize_document(str(item.get("ragflow_document_id") or "")) is not None
    return cache[knowledge_id]


def _disable_rows(rows: list[dict], search_surface: dict) -> list[dict]:
    if int(search_surface.get("failed_count") or 0):
        return []
    if not search_surface.get("enabled"):
        return list(rows)
    return [row for row in rows if row.get("_search_surface_passed") is True]


def _build_report(
    *,
    config: TranscriptMemoryGcConfig,
    candidates: list[dict],
    selected: list[dict],
    disable_rows: list[dict],
    disable_result: DisableResult,
    search_surface: dict,
    status: str | None = None,
) -> dict:
    return {
        "schema_version": TRANSCRIPT_MEMORY_GC_SCHEMA_VERSION,
        "status": status or _run_status(search_surface, disable_result),
        "mode": "execute_disable" if config.execute_disable else "dry_run",
        "retention_policy_enforced": bool(config.declared_policy_input()),
        "candidate_scope": config.candidate_scope,
        "replacement_contract": _replacement_contract(config.candidate_scope),
        "eligible_count": len(candidates),
        "selected_count": len(selected),
        "disable_selected_count": len(disable_rows),
        "attempted_count": disable_result.attempted_count,
        "disabled_count": disable_result.disabled_count,
        "failed_count": disable_result.failed_count,
        "failed_error_class": disable_result.failed_error_class,
        "search_surface_verification": search_surface,
        "mutation_performed": False,
        "network_used": False,
        "raw_ids_printed": False,
        "hard_delete_performed": False,
    }


def _run_status(search_surface: dict, disable_result: DisableResult) -> str:
    if disable_result.failed_count:
        return "partial_failed"
    if int(search_surface.get("failed_count") or 0):
        return "blocked_search_surface_failed"
    return "ok"


def _blocked_retention_policy_report(config: TranscriptMemoryGcConfig) -> dict:
    return {
        "schema_version": TRANSCRIPT_MEMORY_GC_SCHEMA_VERSION,
        "status": "blocked_retention_policy",
        "mode": "execute_disable" if config.execute_disable else "dry_run",
        "retention_policy_enforced": True,
        "candidate_scope": config.candidate_scope,
        "replacement_contract": _replacement_contract(config.candidate_scope),
        "eligible_count": 0,
        "selected_count": 0,
        "disable_selected_count": 0,
        "attempted_count": 0,
        "disabled_count": 0,
        "failed_count": 0,
        "failed_error_class": "",
        "search_surface_verification": _empty_search_surface_report(enabled=False),
        "mutation_performed": False,
        "network_used": False,
        "raw_ids_printed": False,
        "hard_delete_performed": False,
    }


def _resolve_retention_policy(value: str) -> str:
    normalized = value.strip()
    aliases = {
        "transcript-memory": "private_indefinite_until_disabled",
        "episodic_conversation": "private_indefinite_until_disabled",
        "tool_evidence_summary": "private_indefinite_until_disabled",
    }
    return aliases.get(normalized, normalized)


def _sha256_content(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

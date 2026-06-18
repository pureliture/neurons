from __future__ import annotations

from __future__ import annotations
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from .db_adapter import ClosingSqliteConnection, SqliteLedgerDbAdapter

from .db_adapter import ClosingSqliteConnection, SqliteLedgerDbAdapter
from .ledger_base import *  # noqa: F401,F403 (상수/helper re-export 호환)
from .ledger_ingress_mixin import IngressStatusMixin
from .ledger_gc_safety_mixin import GcSafetyMixin
from .ledger_memory_promotion_mixin import MemoryPromotionMixin
from .ledger_native_memory_mixin import NativeMemoryMixin


class Ledger(
    IngressStatusMixin, GcSafetyMixin, MemoryPromotionMixin, NativeMemoryMixin,
):
    def __init__(self, path: Path | str, *, read_only: bool = False, db_adapter=None):
        self.path = Path(path)
        self.read_only = bool(read_only)
        self._temp_dir: Path | None = None
        # B: DB 엔진 접근 seam. None이면 현행 SQLite 어댑터를 lazy 생성(behavior-preserving).
        self._db_adapter = db_adapter
        # C cutover switch: 명시 어댑터가 없고 NEURON_LEDGER_PG_DSN 이 설정돼 있으면 PostgreSQL
        # 엔진을 기본으로 쓴다(엔진 flip = 환경변수 하나). 미설정이면 SQLite(기존 동작 그대로).
        if self._db_adapter is None:
            _pg_dsn = os.environ.get("NEURON_LEDGER_PG_DSN", "")
            if _pg_dsn:
                from .postgres_db_adapter import PostgresLedgerDbAdapter

                self._db_adapter = PostgresLedgerDbAdapter(_pg_dsn)
        # C: 파일 기반 엔진(SQLite)만 파일 권한 준비/하드닝을 한다. 서버형(Postgres)은 skip.
        file_backed = (
            True if self._db_adapter is None else getattr(self._db_adapter, "is_file_backed", True)
        )
        if not self.read_only:
            if file_backed:
                self._prepare_parent_directory()
            self._initialize()
            if file_backed:
                for p in self.path.parent.glob(f"{self.path.name}*"):
                    try:
                        os.chmod(p, 0o600)
                    except OSError:
                        pass
            return
        self.path = self._snapshot_read_only_copy(self.path)

    @classmethod
    def open_read_only(cls, path: Path | str) -> "Ledger":
        if not Path(path).exists():
            raise ValueError(f"ledger path does not exist: {path}")
        return cls(path, read_only=True)

    def __del__(self) -> None:
        if self._temp_dir is not None:
            try:
                shutil.rmtree(self._temp_dir)
            except OSError:
                pass

    def _snapshot_read_only_copy(self, source_path: Path) -> Path:
        if not source_path.exists():
            raise ValueError(f"ledger path does not exist: {source_path}")
        snapshot_dir = Path(tempfile.mkdtemp(prefix="agent-knowledge-ledger-ro-"))
        self._temp_dir = snapshot_dir
        for source_file in source_path.parent.glob(f"{source_path.name}*"):
            shutil.copy2(source_file, snapshot_dir / source_file.name)
        return snapshot_dir / source_path.name

    def _prepare_parent_directory(self) -> None:
        parent = self.path.parent
        if parent.is_symlink():
            raise ValueError("ledger parent must not be a symlink")
        existed = parent.exists()
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not existed:
            os.chmod(parent, 0o700)
            return
        mode = parent.stat().st_mode & 0o777
        if mode & 0o077:
            raise ValueError("ledger parent must be private")

    def _connect(self, *, configure_journal: bool = False) -> sqlite3.Connection:
        # B: 연결 생성을 ILedgerCoreDbAdapter 경계 뒤로. 기본은 현행 SQLite 어댑터(동작
        # 동일). C에서 PostgreSQL 어댑터를 주입하면 이 한 점에서 엔진이 바뀐다.
        if self._db_adapter is None:
            self._db_adapter = SqliteLedgerDbAdapter(self.path, read_only=self.read_only)
        return self._db_adapter.connect(configure_journal=configure_journal)

    def _initialize(self) -> None:
        with self._connect(configure_journal=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_items (
                    knowledge_id TEXT PRIMARY KEY,
                    content_hash TEXT UNIQUE NOT NULL,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    type TEXT NOT NULL,
                    session_id_hash TEXT DEFAULT '',
                    observed_at TEXT DEFAULT '',
                    ingested_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '',
                    privacy_level TEXT NOT NULL DEFAULT 'normal',
                    ragflow_dataset_id TEXT DEFAULT '',
                    ragflow_document_id TEXT DEFAULT '',
                    ingress_target_profile TEXT DEFAULT '',
                    ingress_job_id TEXT DEFAULT '',
                    queued_at TEXT DEFAULT '',
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_event_ids TEXT DEFAULT '',
                    redaction_version TEXT DEFAULT 'redaction.v1',
                    confidence TEXT DEFAULT 'medium',
                    supersedes TEXT DEFAULT '',
                    valid_until TEXT DEFAULT '',
                    evidence_status TEXT DEFAULT 'historical',
                    coverage_status TEXT DEFAULT '',
                    coverage_gap_count INTEGER DEFAULT 0,
                    coverage_duplicate_count INTEGER DEFAULT 0,
                    source_manifest_hash TEXT DEFAULT '',
                    source_chunk_count INTEGER DEFAULT 0,
                    ragflow_run TEXT DEFAULT '',
                    ragflow_progress REAL DEFAULT 0,
                    indexed_at TEXT DEFAULT '',
                    disabled_at TEXT DEFAULT '',
                    authorization_status TEXT DEFAULT 'active',
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingest_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    knowledge_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_class TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS ragflow_datasets (
                    logical_name TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    embedding_model TEXT DEFAULT '',
                    chunk_method TEXT DEFAULT '',
                    metadata_policy_version TEXT DEFAULT '',
                    contract_version TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    disabled_at TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS transcript_sessions (
                    session_id_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    started_at TEXT DEFAULT '',
                    ended_at TEXT DEFAULT '',
                    source_status TEXT NOT NULL DEFAULT 'source_unproven',
                    source_locator_hash TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcript_turns (
                    turn_id_hash TEXT PRIMARY KEY,
                    session_id_hash TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    observed_at TEXT DEFAULT '',
                    redacted_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcript_tool_events (
                    tool_event_id_hash TEXT PRIMARY KEY,
                    turn_id_hash TEXT NOT NULL,
                    event_index INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    redacted_summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcript_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    knowledge_id TEXT NOT NULL UNIQUE,
                    session_id_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    turn_start_index INTEGER NOT NULL,
                    turn_end_index INTEGER NOT NULL,
                    part_index INTEGER NOT NULL DEFAULT 1,
                    part_count INTEGER NOT NULL DEFAULT 1,
                    char_start INTEGER NOT NULL DEFAULT 0,
                    char_end INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL,
                    redacted_text TEXT NOT NULL,
                    source_status TEXT NOT NULL,
                    redaction_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transcript_validation_files (
                    legacy_document_id_hash TEXT NOT NULL,
                    validation_dataset_id TEXT NOT NULL,
                    source_dataset_id_hash TEXT NOT NULL,
                    source_locator_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    turn_start_index INTEGER NOT NULL,
                    turn_end_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    validation_document_ids_json TEXT NOT NULL,
                    validation_knowledge_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(legacy_document_id_hash, validation_dataset_id)
                );
                CREATE TABLE IF NOT EXISTS provider_source_contracts (
                    provider TEXT PRIMARY KEY,
                    contract_id TEXT NOT NULL,
                    provider_version TEXT NOT NULL,
                    installed_version_evidence TEXT DEFAULT '',
                    hook_event TEXT DEFAULT '',
                    source_locator_field TEXT DEFAULT '',
                    parser_version TEXT DEFAULT '',
                    native_parser_status TEXT DEFAULT '',
                    privacy_redaction_status TEXT DEFAULT '',
                    verification_status TEXT NOT NULL,
                    source_status TEXT NOT NULL,
                    hook_install_status TEXT NOT NULL,
                    rollback_state TEXT DEFAULT '',
                    evidence_hash TEXT NOT NULL,
                    redacted_evidence_ref TEXT DEFAULT '',
                    raw_prompt_policy TEXT DEFAULT '',
                    unsupported_reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backfill_sources (
                    source_id TEXT PRIMARY KEY,
                    raw_source_path TEXT NOT NULL,
                    source_path_hash TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_contract_status TEXT DEFAULT '',
                    source_contract_status TEXT DEFAULT '',
                    parser_status TEXT DEFAULT '',
                    inventory_status TEXT NOT NULL,
                    quarantine_reason TEXT DEFAULT '',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduler_runs (
                    run_id TEXT PRIMARY KEY,
                    scheduler_id TEXT NOT NULL,
                    command_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT '',
                    error_class TEXT DEFAULT '',
                    argv_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    candidate_type TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    sensitivity TEXT NOT NULL,
                    requires_manual_approval INTEGER NOT NULL,
                    approval_state TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT DEFAULT '',
                    reviewed_by TEXT DEFAULT '',
                    review_reason TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS memory_cards (
                    memory_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    card_type TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    supersedes TEXT DEFAULT '',
                    disabled_at TEXT DEFAULT '',
                    disabled_by TEXT DEFAULT '',
                    disable_reason TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS memory_card_evidence (
                    memory_id TEXT NOT NULL,
                    knowledge_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY(memory_id, knowledge_id)
                );
                CREATE TABLE IF NOT EXISTS llm_brain_memory_cards (
                    memory_id TEXT PRIMARY KEY,
                    brain_id TEXT NOT NULL,
                    card_type TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    judgment_state TEXT NOT NULL,
                    approval_state TEXT NOT NULL,
                    currentness TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    accepted_at TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_brain_feedback_records (
                    feedback_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    repo_id TEXT NOT NULL,
                    final_status TEXT NOT NULL,
                    user_action TEXT NOT NULL,
                    conflict_state TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_brain_projection_jobs (
                    job_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    job_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS llm_brain_session_memory_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    session_id_hash TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_llm_brain_artifacts_project_created
                    ON llm_brain_session_memory_artifacts(project, created_at);
                CREATE INDEX IF NOT EXISTS idx_llm_brain_artifacts_session
                    ON llm_brain_session_memory_artifacts(session_id_hash);
                CREATE TABLE IF NOT EXISTS llm_brain_source_refs (
                    source_ref_id TEXT PRIMARY KEY,
                    device_id_hash TEXT NOT NULL,
                    root_id TEXT NOT NULL,
                    relative_path_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    sync_policy TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    last_seen_at TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_llm_brain_source_refs_device_root
                    ON llm_brain_source_refs(device_id_hash, root_id);
                CREATE INDEX IF NOT EXISTS idx_llm_brain_source_refs_content_hash
                    ON llm_brain_source_refs(content_hash);
                CREATE TABLE IF NOT EXISTS profile_facts (
                    memory_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_packs (
                    pack_id TEXT PRIMARY KEY,
                    prompt_hash TEXT NOT NULL,
                    filters_json TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_pack_items (
                    pack_id TEXT NOT NULL,
                    item_index INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    reference_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    score REAL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(pack_id, item_index)
                );
                CREATE TABLE IF NOT EXISTS retrieval_audit (
                    audit_id TEXT PRIMARY KEY,
                    pack_id TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    filters_json TEXT NOT NULL,
                    result_count INTEGER NOT NULL,
                    private_allowed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auto_recall_audit (
                    audit_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    status TEXT NOT NULL,
                    policy_reasons_json TEXT NOT NULL,
                    private_policy_allowed INTEGER NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    preview_hash TEXT NOT NULL,
                    context_pack_id TEXT NOT NULL,
                    selected_items_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eval_queries (
                    query_id TEXT PRIMARY KEY,
                    query_hash TEXT NOT NULL,
                    query_terms_json TEXT NOT NULL,
                    project TEXT NOT NULL,
                    provider TEXT DEFAULT '',
                    expected_memory_ids_json TEXT NOT NULL,
                    k INTEGER NOT NULL,
                    min_recall REAL NOT NULL,
                    min_precision REAL NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS eval_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    project TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    k INTEGER NOT NULL,
                    query_count INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL,
                    failures_json TEXT NOT NULL,
                    network_used INTEGER NOT NULL,
                    mutation_performed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dirty_session_memory (
                    session_id_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_knowledge_id TEXT DEFAULT '',
                    dirty_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT DEFAULT '',
                    last_error_class TEXT DEFAULT '',
                    last_summary_knowledge_id TEXT DEFAULT '',
                    last_ingress_job_id TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS session_memory_terminal_skipped_audit (
                    session_id_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    original_status TEXT NOT NULL,
                    terminal_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    category TEXT NOT NULL,
                    source_knowledge_id TEXT DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    dirty_at TEXT NOT NULL,
                    skipped_at TEXT NOT NULL,
                    audited_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_memory_active_snapshots (
                    session_id_hash TEXT PRIMARY KEY,
                    active_knowledge_id TEXT NOT NULL,
                    active_content_hash TEXT NOT NULL,
                    previous_knowledge_id TEXT DEFAULT '',
                    activated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_memory_coverage_edges (
                    active_knowledge_id TEXT NOT NULL,
                    source_content_hash TEXT NOT NULL,
                    source_window_hash TEXT NOT NULL,
                    derived_content_hash TEXT NOT NULL,
                    redaction_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    turn_start_index INTEGER DEFAULT 0,
                    turn_end_index INTEGER DEFAULT 0,
                    PRIMARY KEY (
                        active_knowledge_id,
                        source_content_hash,
                        source_window_hash,
                        derived_content_hash
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_session_memory_coverage_active_knowledge
                    ON session_memory_coverage_edges(active_knowledge_id);
                CREATE INDEX IF NOT EXISTS idx_session_memory_terminal_skipped_audit_status
                    ON session_memory_terminal_skipped_audit(terminal_status, category);
                CREATE TABLE IF NOT EXISTS dirty_project_memory (
                    project_key_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_knowledge_id TEXT DEFAULT '',
                    dirty_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT DEFAULT '',
                    last_error_class TEXT DEFAULT '',
                    last_snapshot_knowledge_id TEXT DEFAULT '',
                    last_ingress_job_id TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS project_memory_active_snapshots (
                    project_key_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    active_knowledge_id TEXT NOT NULL,
                    active_content_hash TEXT NOT NULL,
                    previous_knowledge_id TEXT DEFAULT '',
                    activated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_evidence_summaries (
                    evidence_id_hash TEXT PRIMARY KEY,
                    session_id_hash TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    project TEXT NOT NULL,
                    category TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    command_summary TEXT NOT NULL DEFAULT '',
                    redacted_summary TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    observed_at TEXT DEFAULT '',
                    evidence_index INTEGER NOT NULL DEFAULT 0,
                    redaction_version TEXT NOT NULL DEFAULT 'redaction.v2',
                    source_status TEXT NOT NULL DEFAULT 'source_locator_private_spool_only',
                    knowledge_id TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'prepared',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_transcript_turns_session_turn_observed
                    ON transcript_turns(session_id_hash, turn_index, observed_at);
                CREATE INDEX IF NOT EXISTS idx_transcript_chunks_project_provider_session_turn
                    ON transcript_chunks(project, provider, session_id_hash, turn_start_index, turn_end_index, chunk_id);
                CREATE INDEX IF NOT EXISTS idx_tool_evidence_summaries_session
                    ON tool_evidence_summaries(project, provider, session_id_hash, category, evidence_index);
                CREATE INDEX IF NOT EXISTS idx_knowledge_items_type_status
                    ON knowledge_items(type, status);
                CREATE TABLE IF NOT EXISTS native_memory_mirror (
                    statement_id TEXT PRIMARY KEY,
                    brain_id TEXT NOT NULL,
                    session_tag TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    superseded_by TEXT DEFAULT '',
                    original_content_hash TEXT NOT NULL,
                    search_text TEXT NOT NULL DEFAULT '',
                    card_type TEXT NOT NULL DEFAULT '',
                    ragflow_memory_id TEXT DEFAULT '',
                    ragflow_disabled_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    superseded_at TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_native_memory_mirror_brain_status
                    ON native_memory_mirror(brain_id, status);
                CREATE TABLE IF NOT EXISTS memory_gc_audit (
                    audit_id TEXT PRIMARY KEY,
                    gc_kind TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    knowledge_id TEXT NOT NULL,
                    ragflow_document_id_hash TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    replacement_knowledge_id TEXT NOT NULL,
                    dirty_at TEXT NOT NULL DEFAULT '',
                    snapshot_updated_at TEXT NOT NULL DEFAULT '',
                    approval_operation TEXT NOT NULL DEFAULT '',
                    age_gate_seconds INTEGER NOT NULL DEFAULT 0,
                    mutated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_gc_audit_kind_created
                    ON memory_gc_audit(gc_kind, created_at);
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_ledger.v2', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_ledger.v3', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_scheduler_runs.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_memory_cards.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_context_packs.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_auto_recall_audit.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_backfill_sources.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_eval.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_ingress_queue.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_session_memory_state_machine.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_project_memory_state_machine.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_transcript_lookup_indexes.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_session_memory_sot.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_session_memory_terminology.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_tool_evidence_summary.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_native_memory_mirror.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                INSERT INTO schema_migrations(version, applied_at)
                VALUES ('agent_knowledge_memory_gc_audit.v1', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING;
                UPDATE knowledge_items SET type = 'session_memory' WHERE type = 'session_memory_sot';
                """
            )
            _ensure_column(connection, "knowledge_items", "session_id_hash", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "evidence_status", "TEXT DEFAULT 'historical'")
            _ensure_column(connection, "knowledge_items", "coverage_status", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "coverage_gap_count", "INTEGER DEFAULT 0")
            _ensure_column(connection, "knowledge_items", "coverage_duplicate_count", "INTEGER DEFAULT 0")
            _ensure_column(connection, "knowledge_items", "source_manifest_hash", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "source_chunk_count", "INTEGER DEFAULT 0")
            _ensure_column(connection, "knowledge_items", "indexed_at", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "ingress_target_profile", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "ingress_job_id", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "queued_at", "TEXT DEFAULT ''")
            _ensure_column(connection, "knowledge_items", "metadata_json", "TEXT DEFAULT '{}'")
            _ensure_column(connection, "transcript_chunks", "part_index", "INTEGER NOT NULL DEFAULT 1")
            _ensure_column(connection, "transcript_chunks", "part_count", "INTEGER NOT NULL DEFAULT 1")
            _ensure_column(connection, "transcript_chunks", "char_start", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(connection, "transcript_chunks", "char_end", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(connection, "transcript_validation_files", "validation_document_ids_json", "TEXT DEFAULT '[]'")
            _ensure_column(connection, "transcript_validation_files", "validation_knowledge_ids_json", "TEXT DEFAULT '[]'")
            _ensure_column(connection, "transcript_validation_files", "created_at", "TEXT DEFAULT ''")
            _ensure_column(connection, "transcript_validation_files", "updated_at", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "installed_version_evidence", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "native_parser_status", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "privacy_redaction_status", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "rollback_state", "TEXT DEFAULT ''")
            _ensure_column(connection, "provider_source_contracts", "redacted_evidence_ref", "TEXT DEFAULT ''")
            _ensure_column(connection, "session_memory_coverage_edges", "turn_start_index", "INTEGER DEFAULT 0")
            _ensure_column(connection, "session_memory_coverage_edges", "turn_end_index", "INTEGER DEFAULT 0")
            # native_memory_mirror.v1 흡수(미배포): reconcile용 search_text + governance tier용 card_type.
            _ensure_column(connection, "native_memory_mirror", "search_text", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "native_memory_mirror", "card_type", "TEXT NOT NULL DEFAULT ''")
            if _table_exists(connection, "session_memory_sot_coverage_edges"):
                _ensure_column(connection, "session_memory_sot_coverage_edges", "turn_start_index", "INTEGER DEFAULT 0")
                _ensure_column(connection, "session_memory_sot_coverage_edges", "turn_end_index", "INTEGER DEFAULT 0")
                connection.execute(
                    """
                    INSERT INTO session_memory_coverage_edges (
                        active_knowledge_id,
                        source_content_hash,
                        source_window_hash,
                        derived_content_hash,
                        redaction_version,
                        created_at,
                        turn_start_index,
                        turn_end_index
                    )
                    SELECT
                        active_sot_knowledge_id,
                        source_content_hash,
                        source_window_hash,
                        derived_content_hash,
                        redaction_version,
                        created_at,
                        turn_start_index,
                        turn_end_index
                    FROM session_memory_sot_coverage_edges
                    ON CONFLICT DO NOTHING
                    """
                )
            if _table_exists(connection, "session_memory_sot_active_snapshots"):
                connection.execute(
                    """
                    INSERT INTO session_memory_active_snapshots (
                        session_id_hash,
                        active_knowledge_id,
                        active_content_hash,
                        previous_knowledge_id,
                        activated_at,
                        updated_at
                    )
                    SELECT
                        session_id_hash,
                        active_sot_knowledge_id,
                        active_sot_content_hash,
                        previous_sot_knowledge_id,
                        activated_at,
                        updated_at
                    FROM session_memory_sot_active_snapshots
                    ON CONFLICT DO NOTHING
                    """
                )
















    def get_memory_card(self, memory_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT mc.*, ki.ragflow_dataset_id, ki.ragflow_document_id, ki.status AS ledger_status
                FROM memory_cards mc
                LEFT JOIN knowledge_items ki ON ki.knowledge_id = mc.memory_id
                WHERE mc.memory_id = ?
                """,
                (memory_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_memory_card_state(self, memory_id: str) -> str | None:
        """memory_cards.state 단건 조회(없으면 None). JOIN 없는 경량 read.

        native memory supersede-sync 가 mirror active row 마다 ledger 상태를 확인할 때
        N+1 로 호출하므로 get_memory_card(knowledge_items JOIN + 전컬럼) 대신 단일 컬럼만.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM memory_cards WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        return row["state"] if row else None
















    def list_memory_cards_for_eval(self, *, project: str | None = None, provider: str | None = None) -> list[dict]:
        filters = []
        values = []
        if project:
            filters.append("mc.project = ?")
            values.append(project)
        if provider:
            filters.append("mc.provider = ?")
            values.append(provider)
        where = "WHERE " + " AND ".join(filters) if filters else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    mc.memory_id,
                    mc.candidate_id,
                    mc.card_type,
                    mc.project,
                    mc.provider,
                    mc.title,
                    mc.summary,
                    mc.content_hash,
                    mc.state,
                    mc.approved_by,
                    mc.approved_at,
                    mc.supersedes,
                    mc.disabled_at AS card_disabled_at,
                    mc.disabled_by,
                    mc.disable_reason,
                    ki.status AS ledger_status,
                    ki.disabled_at AS ledger_disabled_at,
                    ki.authorization_status,
                    ki.valid_until,
                    ki.supersedes AS ledger_supersedes
                FROM memory_cards mc
                LEFT JOIN knowledge_items ki ON ki.knowledge_id = mc.memory_id
                {where}
                ORDER BY mc.approved_at, mc.memory_id
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]




    def record_context_pack(self, pack: dict, *, filters: dict | None = None, query_hash: str = "", private_allowed: bool = False) -> None:
        filters_json = json.dumps(filters or {}, sort_keys=True, separators=(",", ":"))
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO context_packs (pack_id, prompt_hash, filters_json, item_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (pack["pack_id"], pack["prompt_hash"], filters_json, len(pack.get("items", [])), created_at),
            )
            for index, item in enumerate(pack.get("items", [])):
                reference_id = item.get("memory_id") or item.get("knowledge_id") or ""
                connection.execute(
                    """
                    INSERT INTO context_pack_items (
                        pack_id, item_index, kind, reference_id, title, summary, score, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pack_id, item_index) DO UPDATE SET
                        kind=excluded.kind,
                        reference_id=excluded.reference_id,
                        title=excluded.title,
                        summary=excluded.summary,
                        score=excluded.score,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        pack["pack_id"],
                        index,
                        item["kind"],
                        reference_id,
                        item.get("title", ""),
                        _persisted_context_summary(item),
                        item.get("score"),
                        json.dumps(item.get("metadata", {}), sort_keys=True, separators=(",", ":")),
                    ),
                )
            audit_id = "audit_" + uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO retrieval_audit (
                    audit_id, pack_id, prompt_hash, query_hash, filters_json,
                    result_count, private_allowed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    pack["pack_id"],
                    pack["prompt_hash"],
                    query_hash,
                    filters_json,
                    len(pack.get("items", [])),
                    1 if private_allowed else 0,
                    created_at,
                ),
            )









    def mark_uploaded(self, knowledge_id: str, *, dataset_id: str, document_id: str, run: str) -> None:
        self._update_status(
            knowledge_id,
            "uploaded_unparsed",
            ragflow_dataset_id=dataset_id,
            ragflow_document_id=document_id,
            ingress_target_profile="",
            ingress_job_id="",
            queued_at="",
            ragflow_run=run,
            indexed_at="",
        )

    def mark_enqueued(self, knowledge_id: str, *, target_profile: str, job_id: str, run: str = "QUEUED") -> None:
        self._update_status(
            knowledge_id,
            "queued",
            ragflow_dataset_id="",
            ragflow_document_id="",
            ingress_target_profile=target_profile,
            ingress_job_id=job_id,
            queued_at=datetime.now(timezone.utc).isoformat(),
            ragflow_run=run,
            ragflow_progress=0,
            indexed_at="",
        )

    def mark_metadata_applied(self, knowledge_id: str) -> None:
        self._update_status(knowledge_id, "metadata_applied")

    def mark_parse_requested(self, knowledge_id: str) -> None:
        self._update_status(knowledge_id, "parse_requested")

    def mark_indexing(self, knowledge_id: str, *, run: str, progress: float) -> None:
        self._update_status(knowledge_id, "indexing", ragflow_run=run, ragflow_progress=progress, indexed_at="")

    def mark_indexed(self, knowledge_id: str, *, run: str) -> None:
        self._update_status(
            knowledge_id,
            "indexed",
            ragflow_run=run,
            ragflow_progress=1.0,
            indexed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._maybe_mark_session_memory_dirty_for_indexed_item(knowledge_id)
        self._maybe_mark_project_memory_dirty_for_indexed_item(knowledge_id)

    def mark_index_timeout(self, knowledge_id: str, *, run: str = "TIMEOUT", progress: float = 0) -> None:
        self._update_status(knowledge_id, "index_timeout", ragflow_run=run, ragflow_progress=progress, indexed_at="")




    def mark_parse_failed(self, knowledge_id: str, *, run: str = "FAIL") -> None:
        self._update_status(knowledge_id, "parse_failed", ragflow_run=run, indexed_at="")

    def mark_quarantined(
        self,
        knowledge_id: str,
        *,
        reason: str,
        disposition_action: str,
        run_bucket: str = "",
    ) -> bool:
        item = self.get_by_knowledge_id(knowledge_id) or {}
        return self.mark_quarantined_if_queued(
            knowledge_id,
            reason=reason,
            disposition_action=disposition_action,
            run_bucket=run_bucket,
            expected_target_profile=str(item.get("ingress_target_profile") or ""),
            expected_ingress_job_id=str(item.get("ingress_job_id") or ""),
            expected_updated_at=str(item.get("updated_at") or ""),
        )








    def mark_disabled(self, knowledge_id: str) -> None:
        self._update_status(
            knowledge_id,
            "disabled",
            disabled_at=datetime.now(timezone.utc).isoformat(),
            authorization_status="disabled",
        )

    def mark_enabled(self, knowledge_id: str) -> None:
        # G-7 (M-GC §3.2 B1/B4): mark_disabled의 역함수. status->'indexed',
        # authorization_status->'active', disabled_at->''. mark_disabled가 쓰는 컬럼만
        # 정확히 되돌린다.
        self._update_status(
            knowledge_id,
            "indexed",
            disabled_at="",
            authorization_status="active",
        )

    def upsert_session_summary(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        session_id_hash: str = "",
        title: str,
        summary: str,
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
        source_manifest_hash: str = "",
        source_chunk_count: int = 0,
    ) -> dict:
        item = self.upsert_prepared(
            knowledge_id=knowledge_id,
            content_hash=content_hash,
            provider=provider,
            project=project,
            domain="agent_memory",
            type="session_summary",
            session_id_hash=session_id_hash,
            title=title,
            summary=summary,
            privacy_level="private",
            evidence_status=evidence_status,
            coverage_status=coverage_status,
            coverage_gap_count=coverage_gap_count,
            coverage_duplicate_count=coverage_duplicate_count,
            source_manifest_hash=source_manifest_hash,
            source_chunk_count=source_chunk_count,
        )
        if item is None:
            item = self.get_by_content_hash(content_hash)
        if item is None:
            raise ValueError("failed to resolve canonical knowledge item for session summary")
        return item

    def upsert_session_memory_sot(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        session_id_hash: str = "",
        title: str,
        summary: str,
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
        source_manifest_hash: str = "",
        source_chunk_count: int = 0,
    ) -> dict:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        return self.upsert_session_memory(
            knowledge_id=knowledge_id,
            content_hash=content_hash,
            provider=provider,
            project=project,
            session_id_hash=session_id_hash,
            title=title,
            summary=summary,
            evidence_status=evidence_status,
            coverage_status=coverage_status,
            coverage_gap_count=coverage_gap_count,
            coverage_duplicate_count=coverage_duplicate_count,
            source_manifest_hash=source_manifest_hash,
            source_chunk_count=source_chunk_count,
        )

    def upsert_session_memory(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        session_id_hash: str = "",
        title: str,
        summary: str,
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
        source_manifest_hash: str = "",
        source_chunk_count: int = 0,
    ) -> dict:
        existing = self.get_by_knowledge_id(knowledge_id) or self.get_by_content_hash(content_hash)
        if existing is not None:
            if not source_manifest_hash:
                source_manifest_hash = str(existing.get("source_manifest_hash") or "")
            if int(source_chunk_count or 0) <= 0:
                source_chunk_count = int(existing.get("source_chunk_count") or 0)
        if not _is_sha256_hash(source_manifest_hash):
            raise ValueError("session memory source manifest hash is required")
        if int(source_chunk_count or 0) <= 0:
            raise ValueError("session memory source_manifest source_chunk_count is required")
        item = self.upsert_prepared(
            knowledge_id=knowledge_id,
            content_hash=content_hash,
            provider=provider,
            project=project,
            domain="agent_memory",
            type="session_memory",
            session_id_hash=session_id_hash,
            title=title,
            summary=summary,
            privacy_level="private",
            evidence_status=evidence_status,
            coverage_status=coverage_status,
            coverage_gap_count=coverage_gap_count,
            coverage_duplicate_count=coverage_duplicate_count,
            source_manifest_hash=source_manifest_hash,
            source_chunk_count=source_chunk_count,
        )
        if item is None:
            item = self.get_by_content_hash(content_hash)
        if item is None:
            raise ValueError("failed to resolve canonical knowledge item for session memory")
        return item

    def upsert_session_recap(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        session_id_hash: str = "",
        title: str,
        summary: str,
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
    ) -> dict:
        item = self.upsert_prepared(
            knowledge_id=knowledge_id,
            content_hash=content_hash,
            provider=provider,
            project=project,
            domain="agent_memory",
            type="session_recap",
            session_id_hash=session_id_hash,
            title=title,
            summary=summary,
            privacy_level="private",
            evidence_status=evidence_status,
            coverage_status=coverage_status,
            coverage_gap_count=coverage_gap_count,
            coverage_duplicate_count=coverage_duplicate_count,
        )
        if item is None:
            item = self.get_by_content_hash(content_hash)
        if item is None:
            raise ValueError("failed to resolve canonical knowledge item for session recap")
        return item








    def list_session_memory_indexed_candidates(self, *, limit: int = 50) -> list[dict]:
        return []

    def promote_session_memory_snapshot(self, knowledge_id: str) -> dict:
        raise ValueError("legacy session_summary pipeline has been removed; use promote_session_memory")

    def promote_session_memory(self, knowledge_id: str) -> dict:
        item = self.get_by_knowledge_id(knowledge_id)
        if item is None:
            raise ValueError("session memory not found")
        if item.get("type") != "session_memory":
            raise ValueError("session memory must be type=session_memory")
        if item.get("authorization_status") != "active":
            raise ValueError("session memory authorization must be active before promotion")
        if item.get("disabled_at"):
            raise ValueError("disabled session memory cannot be promoted")
        if item.get("status") != "indexed":
            raise ValueError("session memory must be indexed before promotion")
        if item.get("supersedes"):
            raise ValueError("superseded session memory cannot be promoted")
        if _is_expired(item.get("valid_until", "")):
            raise ValueError("expired session memory cannot be promoted")
        if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
            raise ValueError("session memory requires regenerated transcript provenance before promotion")
        if not _session_memory_coverage_is_complete(item):
            raise ValueError("session memory coverage must be complete before promotion")
        if not self._session_memory_coverage_edges_are_complete(item):
            raise ValueError("session memory coverage edges must match source manifest before promotion")
        if not item.get("ragflow_dataset_id"):
            raise ValueError("session memory requires ragflow_dataset_id before promotion")
        if not item.get("ragflow_document_id"):
            raise ValueError("session memory requires ragflow_document_id before promotion")
        if not self._dataset_is_enabled(item.get("ragflow_dataset_id", "")):
            raise ValueError("session memory dataset must be enabled before promotion")
        session_id_hash = item.get("session_id_hash") or ""
        if not session_id_hash:
            raise ValueError("session memory requires session_id_hash before promotion")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT active_knowledge_id FROM session_memory_active_snapshots WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
            previous_knowledge_id = str(previous["active_knowledge_id"]) if previous else ""
            if previous_knowledge_id and previous_knowledge_id != knowledge_id:
                connection.execute(
                    "UPDATE knowledge_items SET supersedes = ? WHERE knowledge_id = ?",
                    (knowledge_id, previous_knowledge_id),
                )
            connection.execute(
                """
                INSERT INTO session_memory_active_snapshots (
                    session_id_hash, active_knowledge_id, active_content_hash,
                    previous_knowledge_id, activated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id_hash) DO UPDATE SET
                    active_knowledge_id=excluded.active_knowledge_id,
                    active_content_hash=excluded.active_content_hash,
                    previous_knowledge_id=excluded.previous_knowledge_id,
                    activated_at=excluded.activated_at,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id_hash,
                    knowledge_id,
                    item.get("content_hash", ""),
                    previous_knowledge_id if previous_knowledge_id != knowledge_id else "",
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM session_memory_active_snapshots WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
        return dict(row)

    def promote_session_memory_sot(self, knowledge_id: str) -> dict:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        return self.promote_session_memory(knowledge_id)


    def get_session_memory_sot_active_snapshot(self, session_id_hash: str) -> dict | None:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        row = self.get_session_memory_active_snapshot(session_id_hash)
        if not row:
            return None
        return {
            "session_id_hash": row["session_id_hash"],
            "active_sot_knowledge_id": row["active_knowledge_id"],
            "active_sot_content_hash": row["active_content_hash"],
            "previous_sot_knowledge_id": row.get("previous_knowledge_id", ""),
            "activated_at": row["activated_at"],
            "updated_at": row["updated_at"],
        }


    def get_session_memory_sot_by_session_id_hash(self, session_id_hash: str) -> dict | None:
        """Deprecated compatibility getter; canonical type is session_memory."""
        return self.get_session_memory_by_session_id_hash(session_id_hash)

    def record_session_memory_sot_coverage(
        self,
        *,
        active_sot_knowledge_id: str,
        source_content_hash: str,
        source_window_hash: str,
        derived_content_hash: str,
        redaction_version: str,
        turn_start_index: int,
        turn_end_index: int,
    ) -> dict:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        row = self.record_session_memory_coverage(
            active_knowledge_id=active_sot_knowledge_id,
            source_content_hash=source_content_hash,
            source_window_hash=source_window_hash,
            derived_content_hash=derived_content_hash,
            redaction_version=redaction_version,
            turn_start_index=turn_start_index,
            turn_end_index=turn_end_index,
        )
        return {
            "active_sot_knowledge_id": row["active_knowledge_id"],
            "source_content_hash": row["source_content_hash"],
            "source_window_hash": row["source_window_hash"],
            "derived_content_hash": row["derived_content_hash"],
            "redaction_version": row["redaction_version"],
            "created_at": row["created_at"],
            "turn_start_index": row["turn_start_index"],
            "turn_end_index": row["turn_end_index"],
        }


    def list_session_memory_sot_coverage(self, active_sot_knowledge_id: str) -> list[dict]:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        return [
            {
                "active_sot_knowledge_id": row["active_knowledge_id"],
                "source_content_hash": row["source_content_hash"],
                "source_window_hash": row["source_window_hash"],
                "derived_content_hash": row["derived_content_hash"],
                "redaction_version": row["redaction_version"],
                "created_at": row["created_at"],
                "turn_start_index": row["turn_start_index"],
                "turn_end_index": row["turn_end_index"],
            }
            for row in self.list_session_memory_coverage(active_sot_knowledge_id)
        ]


    def _session_memory_coverage_edges_are_complete(self, item: dict) -> bool:
        try:
            expected_source_count = int(item.get("source_chunk_count") or 0)
        except (TypeError, ValueError):
            return False
        if expected_source_count <= 0:
            return False
        expected_manifest_hash = str(item.get("source_manifest_hash") or "")
        if not _is_sha256_hash(expected_manifest_hash):
            return False
        active_knowledge_id = str(item.get("knowledge_id") or "")
        content_hash = str(item.get("content_hash") or "")
        if not active_knowledge_id or not _is_sha256_hash(content_hash):
            return False
        edges = self.list_session_memory_coverage(active_knowledge_id)
        if len(edges) != expected_source_count:
            return False
        windows = []
        for edge in edges:
            if str(edge.get("derived_content_hash") or "") != content_hash:
                return False
            try:
                start = int(edge.get("turn_start_index") or 0)
                end = int(edge.get("turn_end_index") or 0)
            except (TypeError, ValueError):
                return False
            if start <= 0 or end < start:
                return False
            windows.append((start, end))
        previous_start = 0
        previous_end = 0
        for start, end in sorted(windows):
            if start == previous_start and end == previous_end:
                continue
            if start != previous_end + 1:
                return False
            previous_start = start
            previous_end = end
        pairs = [(str(edge["source_content_hash"]), str(edge["source_window_hash"])) for edge in edges]
        return _session_memory_coverage_edge_manifest_hash(pairs) == expected_manifest_hash

    def _session_memory_sot_coverage_edges_are_complete(self, item: dict) -> bool:
        """Deprecated compatibility wrapper for the retired session_memory_sot name."""
        return self._session_memory_coverage_edges_are_complete(item)








    def list_project_memory_indexed_candidates(self, *, limit: int = 50) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    ki.*
                FROM knowledge_items ki
                WHERE ki.type = 'project_context_snapshot'
                  AND ki.status = 'indexed'
                  AND ki.disabled_at = ''
                  AND ki.authorization_status = 'active'
                  AND ki.evidence_status = ?
                  AND ki.supersedes = ''
                  AND (ki.valid_until = '' OR ki.valid_until > ?)
                  AND ki.ragflow_dataset_id != ''
                  AND ki.ragflow_document_id != ''
                  AND NOT EXISTS (
                    SELECT 1 FROM ragflow_datasets rd
                    WHERE rd.dataset_id = ki.ragflow_dataset_id
                      AND (rd.enabled = 0 OR rd.disabled_at != '')
                  )
                ORDER BY ki.indexed_at ASC, ki.updated_at ASC
                LIMIT ?
                """,
                (SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS, now, max(int(limit), 1)),
            ).fetchall()
        candidates = []
        for row in rows:
            item = dict(row)
            dirty = self.get_dirty_project_memory(provider=item.get("provider", ""), project=item.get("project", ""))
            if dirty and dirty.get("status") == "promoted":
                continue
            candidates.append(item)
        return candidates[: max(int(limit), 1)]

    def promote_project_memory_snapshot(self, knowledge_id: str) -> dict:
        item = self.get_by_knowledge_id(knowledge_id)
        if item is None:
            raise ValueError("project memory snapshot not found")
        if item.get("type") != "project_context_snapshot":
            raise ValueError("project memory snapshot must be type=project_context_snapshot")
        if item.get("status") != "indexed":
            raise ValueError("project memory snapshot must be indexed before promotion")
        if item.get("authorization_status") != "active":
            raise ValueError("project memory snapshot authorization must be active before promotion")
        if item.get("disabled_at"):
            raise ValueError("disabled project memory snapshot cannot be promoted")
        if item.get("supersedes"):
            raise ValueError("superseded project memory snapshot cannot be promoted")
        if _is_expired(item.get("valid_until", "")):
            raise ValueError("expired project memory snapshot cannot be promoted")
        if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
            raise ValueError("project memory snapshot requires regenerated transcript provenance before promotion")
        if not item.get("ragflow_dataset_id"):
            raise ValueError("project memory snapshot requires ragflow_dataset_id before promotion")
        if not item.get("ragflow_document_id"):
            raise ValueError("project memory snapshot requires ragflow_document_id before promotion")
        if not self._dataset_is_enabled(item.get("ragflow_dataset_id", "")):
            raise ValueError("project memory snapshot dataset must be enabled before promotion")
        provider = item.get("provider") or ""
        project = item.get("project") or ""
        if not provider or not project:
            raise ValueError("project memory snapshot requires provider and project before promotion")
        project_key_hash = _project_key_hash(provider, project)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT active_knowledge_id FROM project_memory_active_snapshots WHERE project_key_hash = ?",
                (project_key_hash,),
            ).fetchone()
            previous_knowledge_id = str(previous["active_knowledge_id"]) if previous else ""
            if previous_knowledge_id and previous_knowledge_id != knowledge_id:
                connection.execute(
                    "UPDATE knowledge_items SET supersedes = ? WHERE knowledge_id = ?",
                    (knowledge_id, previous_knowledge_id),
                )
            connection.execute(
                """
                INSERT INTO project_memory_active_snapshots (
                    project_key_hash, provider, project, active_knowledge_id,
                    active_content_hash, previous_knowledge_id, activated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_key_hash) DO UPDATE SET
                    provider=excluded.provider,
                    project=excluded.project,
                    active_knowledge_id=excluded.active_knowledge_id,
                    active_content_hash=excluded.active_content_hash,
                    previous_knowledge_id=excluded.previous_knowledge_id,
                    activated_at=excluded.activated_at,
                    updated_at=excluded.updated_at
                """,
                (
                    project_key_hash,
                    provider,
                    project,
                    knowledge_id,
                    item.get("content_hash", ""),
                    previous_knowledge_id if previous_knowledge_id != knowledge_id else "",
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM project_memory_active_snapshots WHERE project_key_hash = ?",
                (project_key_hash,),
            ).fetchone()
        return dict(row)












    def upsert_tool_evidence_summary(self, *, record) -> dict:
        """Append-only upsert of one redacted tool-evidence record.

        Keyed by ``evidence_id_hash`` (content-addressed), so re-running the
        extractor over the same source is idempotent. Never touches
        conversation_chunk rows or their knowledge_items.
        """
        data = record.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_evidence_summaries (
                    evidence_id_hash, session_id_hash, provider, project,
                    category, outcome, tool_name, command_summary, redacted_summary,
                    content_hash, observed_at, evidence_index, redaction_version,
                    source_status, knowledge_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id_hash) DO UPDATE SET
                    session_id_hash=excluded.session_id_hash,
                    provider=excluded.provider,
                    project=excluded.project,
                    category=excluded.category,
                    outcome=excluded.outcome,
                    tool_name=excluded.tool_name,
                    command_summary=excluded.command_summary,
                    redacted_summary=excluded.redacted_summary,
                    content_hash=excluded.content_hash,
                    observed_at=excluded.observed_at,
                    evidence_index=excluded.evidence_index,
                    redaction_version=excluded.redaction_version,
                    source_status=excluded.source_status,
                    updated_at=excluded.updated_at
                """,
                (
                    data["evidence_id_hash"],
                    data["session_id_hash"],
                    data["provider"],
                    data["project"],
                    data["category"],
                    data["outcome"],
                    data["tool_name"],
                    data["command_summary"],
                    data["redacted_summary"],
                    data["content_hash"],
                    data.get("observed_at", ""),
                    data.get("evidence_index", 0),
                    data.get("redaction_version", "redaction.v2"),
                    data.get("source_status", "source_locator_private_spool_only"),
                    "",
                    "prepared",
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM tool_evidence_summaries WHERE evidence_id_hash = ?",
                (data["evidence_id_hash"],),
            ).fetchone()
        return dict(row) if row is not None else {}











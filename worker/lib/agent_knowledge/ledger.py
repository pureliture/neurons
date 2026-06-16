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


SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS = "regenerated-from-indexed-transcript"
SQLITE_BUSY_TIMEOUT_MS = 60000


class Ledger:
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

    def upsert_provider_source_contract(self, contract) -> dict:
        record = contract.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_source_contracts (
                    provider, contract_id, provider_version, installed_version_evidence,
                    hook_event, source_locator_field, parser_version,
                    native_parser_status, privacy_redaction_status, verification_status,
                    source_status, hook_install_status, rollback_state, evidence_hash,
                    redacted_evidence_ref, raw_prompt_policy, unsupported_reason, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    contract_id=excluded.contract_id,
                    provider_version=excluded.provider_version,
                    installed_version_evidence=excluded.installed_version_evidence,
                    hook_event=excluded.hook_event,
                    source_locator_field=excluded.source_locator_field,
                    parser_version=excluded.parser_version,
                    native_parser_status=excluded.native_parser_status,
                    privacy_redaction_status=excluded.privacy_redaction_status,
                    verification_status=excluded.verification_status,
                    source_status=excluded.source_status,
                    hook_install_status=excluded.hook_install_status,
                    rollback_state=excluded.rollback_state,
                    evidence_hash=excluded.evidence_hash,
                    redacted_evidence_ref=excluded.redacted_evidence_ref,
                    raw_prompt_policy=excluded.raw_prompt_policy,
                    unsupported_reason=excluded.unsupported_reason,
                    updated_at=excluded.updated_at
                """,
                (
                    record["provider"],
                    record["contract_id"],
                    record["provider_version"],
                    record.get("installed_version_evidence", ""),
                    record.get("hook_event", ""),
                    record.get("source_locator_field", ""),
                    record.get("parser_version", ""),
                    record.get("native_parser_status", ""),
                    record.get("privacy_redaction_status", ""),
                    record["verification_status"],
                    record["source_status"],
                    record["hook_install_status"],
                    record.get("rollback_state", ""),
                    record["evidence_hash"],
                    record.get("redacted_evidence_ref", ""),
                    record.get("raw_prompt_policy", ""),
                    record.get("unsupported_reason", ""),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM provider_source_contracts WHERE provider = ?",
                (record["provider"],),
            ).fetchone()
        return dict(row)

    def list_provider_source_contracts(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_source_contracts ORDER BY provider",
            ).fetchall()
        return [dict(row) for row in rows]

    def get_provider_source_contract(self, provider: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_source_contracts WHERE provider = ?",
                (provider,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_backfill_source(self, source: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO backfill_sources (
                    source_id, raw_source_path, source_path_hash, project, provider,
                    provider_contract_status, source_contract_status, parser_status,
                    inventory_status, quarantine_reason, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path_hash) DO UPDATE SET
                    raw_source_path=excluded.raw_source_path,
                    project=excluded.project,
                    provider=excluded.provider,
                    provider_contract_status=excluded.provider_contract_status,
                    source_contract_status=excluded.source_contract_status,
                    parser_status=excluded.parser_status,
                    inventory_status=excluded.inventory_status,
                    quarantine_reason=excluded.quarantine_reason,
                    updated_at=excluded.updated_at
                """,
                (
                    source["source_id"],
                    source["raw_source_path"],
                    source["source_path_hash"],
                    source["project"],
                    source["provider"],
                    source.get("provider_contract_status", ""),
                    source.get("source_contract_status", ""),
                    source.get("parser_status", ""),
                    source.get("inventory_status", "discovered"),
                    source.get("quarantine_reason", ""),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM backfill_sources WHERE source_path_hash = ?",
                (source["source_path_hash"],),
            ).fetchone()
        return dict(row)

    def update_backfill_source_status(
        self,
        source_path_hash: str,
        *,
        provider_contract_status: str = "",
        source_contract_status: str = "",
        parser_status: str = "",
        inventory_status: str,
        quarantine_reason: str = "",
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE backfill_sources
                SET provider_contract_status = ?,
                    source_contract_status = ?,
                    parser_status = ?,
                    inventory_status = ?,
                    quarantine_reason = ?,
                    updated_at = ?
                WHERE source_path_hash = ?
                """,
                (
                    provider_contract_status,
                    source_contract_status,
                    parser_status,
                    inventory_status,
                    quarantine_reason,
                    now,
                    source_path_hash,
                ),
            )
            row = connection.execute(
                "SELECT * FROM backfill_sources WHERE source_path_hash = ?",
                (source_path_hash,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown backfill source: {source_path_hash}")
        return dict(row)

    def list_backfill_sources(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM backfill_sources ORDER BY raw_source_path",
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_transcript_validation_file(self, record: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        document_ids_json = json.dumps(
            list(record.get("validation_document_ids", [])),
            sort_keys=True,
            separators=(",", ":"),
        )
        knowledge_ids_json = json.dumps(
            list(record.get("validation_knowledge_ids", [])),
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_validation_files (
                    legacy_document_id_hash, validation_dataset_id, source_dataset_id_hash,
                    source_locator_hash, provider, project, turn_start_index, turn_end_index,
                    status, validation_document_ids_json, validation_knowledge_ids_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(legacy_document_id_hash, validation_dataset_id) DO UPDATE SET
                    source_dataset_id_hash=excluded.source_dataset_id_hash,
                    source_locator_hash=excluded.source_locator_hash,
                    provider=excluded.provider,
                    project=excluded.project,
                    turn_start_index=excluded.turn_start_index,
                    turn_end_index=excluded.turn_end_index,
                    status=excluded.status,
                    validation_document_ids_json=excluded.validation_document_ids_json,
                    validation_knowledge_ids_json=excluded.validation_knowledge_ids_json,
                    updated_at=excluded.updated_at
                """,
                (
                    record["legacy_document_id_hash"],
                    record["validation_dataset_id"],
                    record.get("source_dataset_id_hash", ""),
                    record["source_locator_hash"],
                    record["provider"],
                    record["project"],
                    int(record["turn_start_index"]),
                    int(record["turn_end_index"]),
                    record["status"],
                    document_ids_json,
                    knowledge_ids_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM transcript_validation_files
                WHERE legacy_document_id_hash = ? AND validation_dataset_id = ?
                """,
                (record["legacy_document_id_hash"], record["validation_dataset_id"]),
            ).fetchone()
        return _transcript_validation_file_from_row(row)

    def list_transcript_validation_files(
        self,
        *,
        validation_dataset_id: str,
        status: str | None = None,
    ) -> list[dict]:
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    """
                    SELECT * FROM transcript_validation_files
                    WHERE validation_dataset_id = ? AND status = ?
                    ORDER BY updated_at, legacy_document_id_hash
                    """,
                    (validation_dataset_id, status),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM transcript_validation_files
                    WHERE validation_dataset_id = ?
                    ORDER BY updated_at, legacy_document_id_hash
                    """,
                    (validation_dataset_id,),
                ).fetchall()
        return [_transcript_validation_file_from_row(row) for row in rows]

    def insert_scheduler_run(self, run: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_runs (
                    run_id, scheduler_id, command_kind, status, started_at,
                    completed_at, error_class, argv_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    run["scheduler_id"],
                    run["command_kind"],
                    run["status"],
                    run["started_at"],
                    run.get("completed_at", ""),
                    run.get("error_class", ""),
                    json.dumps(list(run.get("argv", [])), separators=(",", ":")),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM scheduler_runs WHERE run_id = ?",
                (run["run_id"],),
            ).fetchone()
        return dict(row)

    def list_scheduler_runs(self, scheduler_id: str | None = None) -> list[dict]:
        with self._connect() as connection:
            if scheduler_id:
                rows = connection.execute(
                    "SELECT * FROM scheduler_runs WHERE scheduler_id = ? ORDER BY started_at",
                    (scheduler_id,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM scheduler_runs ORDER BY started_at").fetchall()
        return [dict(row) for row in rows]

    def upsert_memory_candidate(self, candidate: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_candidates (
                    candidate_id, candidate_type, project, provider, statement,
                    content_hash, sensitivity, requires_manual_approval,
                    approval_state, evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    candidate_type=excluded.candidate_type,
                    project=excluded.project,
                    provider=excluded.provider,
                    statement=excluded.statement,
                    content_hash=excluded.content_hash,
                    sensitivity=excluded.sensitivity,
                    requires_manual_approval=excluded.requires_manual_approval,
                    evidence_refs_json=excluded.evidence_refs_json
                """,
                (
                    candidate["candidate_id"],
                    candidate["candidate_type"],
                    candidate["project"],
                    candidate["provider"],
                    candidate["statement"],
                    candidate["content_hash"],
                    candidate["sensitivity"],
                    1 if candidate["requires_manual_approval"] else 0,
                    candidate.get("approval_state", "pending"),
                    json.dumps(candidate.get("evidence_refs", []), sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id = ?",
                (candidate["candidate_id"],),
            ).fetchone()
        return _memory_candidate_from_row(row)

    def get_memory_candidate(self, candidate_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return _memory_candidate_from_row(row) if row else None

    def list_memory_candidates(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_candidates ORDER BY created_at, candidate_id",
            ).fetchall()
        return [_memory_candidate_from_row(row) for row in rows]

    def update_memory_candidate_state(self, candidate_id: str, state: str, *, reviewed_by: str = "", reason: str = "") -> dict:
        reviewed_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE memory_candidates
                SET approval_state = ?, reviewed_at = ?, reviewed_by = ?, review_reason = ?
                WHERE candidate_id = ?
                """,
                (state, reviewed_at, reviewed_by, reason, candidate_id),
            )
        candidate = self.get_memory_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"unknown memory candidate: {candidate_id}")
        return candidate

    def upsert_memory_card(self, card: dict) -> dict:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_cards (
                    memory_id, candidate_id, card_type, project, provider, title,
                    summary, content_hash, state, approved_by, approved_at, supersedes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    candidate_id=excluded.candidate_id,
                    card_type=excluded.card_type,
                    project=excluded.project,
                    provider=excluded.provider,
                    title=excluded.title,
                    summary=excluded.summary,
                    content_hash=excluded.content_hash,
                    state=excluded.state,
                    approved_by=excluded.approved_by,
                    approved_at=excluded.approved_at,
                    supersedes=excluded.supersedes
                """,
                (
                    card["memory_id"],
                    card["candidate_id"],
                    card["card_type"],
                    card["project"],
                    card["provider"],
                    card["title"],
                    card["summary"],
                    card["content_hash"],
                    card.get("state", "active"),
                    card["approved_by"],
                    card["approved_at"],
                    card.get("supersedes", ""),
                ),
            )
        self.upsert_prepared(
            knowledge_id=card["memory_id"],
            content_hash=card["content_hash"],
            provider=card["provider"],
            project=card["project"],
            domain="agent_memory",
            type="memory_card",
            title=card["title"],
            summary=card["summary"],
            privacy_level="private",
        )
        self.mark_uploaded(
            card["memory_id"],
            dataset_id=card.get("ragflow_dataset_id") or "local-approved-memory-cards",
            document_id=card.get("ragflow_document_id") or f"memdoc_{card['memory_id']}",
            run="LOCAL",
        )
        self.mark_indexed(card["memory_id"], run="LOCAL")
        return self.get_memory_card(card["memory_id"])

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

    def add_memory_card_evidence(self, memory_id: str, evidence_refs: list[dict]) -> None:
        with self._connect() as connection:
            for ref in evidence_refs:
                connection.execute(
                    """
                    INSERT INTO memory_card_evidence (memory_id, knowledge_id, content_hash)
                    VALUES (?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (memory_id, ref["knowledge_id"], ref["content_hash"]),
                )

    def list_memory_card_evidence(self, memory_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, knowledge_id, content_hash
                FROM memory_card_evidence
                WHERE memory_id = ?
                ORDER BY knowledge_id
                """,
                (memory_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_llm_brain_memory_card(self, card: dict) -> dict:
        from .session_memory.memory_card import validate_memory_card_envelope

        validated = validate_memory_card_envelope(card)
        now = datetime.now(timezone.utc).isoformat()
        accepted_at = (
            str(validated.get("approved_at") or now)
            if validated["lifecycle_state"] in {"accepted", "human_accepted", "auto_accepted"}
            else ""
        )
        hash_source = dict(validated)
        hash_source.pop("content_hash", None)
        hash_source.pop("card_hash", None)
        content_hash = _sha256_text(
            json.dumps(hash_source, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
        validated["content_hash"] = content_hash
        validated["card_hash"] = content_hash
        envelope_json = json.dumps(validated, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_brain_memory_cards (
                    memory_id, brain_id, card_type, project, provider,
                    lifecycle_state, judgment_state, approval_state, currentness,
                    status, content_hash, envelope_json, accepted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    brain_id=excluded.brain_id,
                    card_type=excluded.card_type,
                    project=excluded.project,
                    provider=excluded.provider,
                    lifecycle_state=excluded.lifecycle_state,
                    judgment_state=excluded.judgment_state,
                    approval_state=excluded.approval_state,
                    currentness=excluded.currentness,
                    status=excluded.status,
                    content_hash=excluded.content_hash,
                    envelope_json=excluded.envelope_json,
                    accepted_at=excluded.accepted_at,
                    updated_at=excluded.updated_at
                """,
                (
                    validated["memory_id"],
                    validated["brain_id"],
                    validated["card_type"],
                    validated["project"],
                    validated["provider"],
                    validated["lifecycle_state"],
                    validated["judgment_state"],
                    validated["approval_state"],
                    validated["currentness"],
                    validated["status"],
                    content_hash,
                    envelope_json,
                    accepted_at,
                    now,
                ),
            )
        return self.get_llm_brain_memory_card(validated["memory_id"])

    def get_llm_brain_memory_card(self, memory_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT envelope_json FROM llm_brain_memory_cards WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["envelope_json"])

    def list_llm_brain_memory_cards(
        self,
        *,
        project: str | None = None,
        accepted_only: bool = False,
        current_only: bool = False,
        limit: int = 10,
    ) -> list[dict]:
        filters = []
        values: list[object] = []
        if project:
            filters.append("project = ?")
            values.append(project)
        if accepted_only:
            filters.append("lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')")
            filters.append("approval_state IN ('approved', 'auto_accepted')")
        if current_only:
            filters.append("currentness = 'current'")
        where = "WHERE " + " AND ".join(filters) if filters else ""
        values.append(max(int(limit), 1))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT envelope_json FROM llm_brain_memory_cards
                {where}
                ORDER BY COALESCE(NULLIF(accepted_at, ''), updated_at) DESC, memory_id
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [json.loads(row["envelope_json"]) for row in rows]

    def upsert_llm_brain_feedback_record(self, record: dict) -> dict:
        from .session_memory.memory_card import validate_feedback_record

        validated = validate_feedback_record(record)
        created_at = str(validated.get("timestamp") or datetime.now(timezone.utc).isoformat())
        record_json = json.dumps(validated, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_brain_feedback_records (
                    feedback_id, memory_id, decision_id, repo_id, final_status,
                    user_action, conflict_state, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feedback_id) DO UPDATE SET
                    memory_id=excluded.memory_id,
                    decision_id=excluded.decision_id,
                    repo_id=excluded.repo_id,
                    final_status=excluded.final_status,
                    user_action=excluded.user_action,
                    conflict_state=excluded.conflict_state,
                    record_json=excluded.record_json,
                    created_at=excluded.created_at
                """,
                (
                    validated["feedback_id"],
                    validated["memory_id"],
                    validated["decision_id"],
                    validated["repo_id"],
                    validated["final_status"],
                    validated["user_action"],
                    validated["conflict_state"],
                    record_json,
                    created_at,
                ),
            )
        return self.get_llm_brain_feedback_record(validated["feedback_id"])

    def get_llm_brain_feedback_record(self, feedback_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM llm_brain_feedback_records WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["record_json"])

    def list_llm_brain_feedback_records(self, *, memory_id: str | None = None, limit: int = 100) -> list[dict]:
        values: list[object] = []
        where = ""
        if memory_id:
            where = "WHERE memory_id = ?"
            values.append(memory_id)
        values.append(max(int(limit), 1))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT record_json FROM llm_brain_feedback_records
                {where}
                ORDER BY created_at, feedback_id
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def upsert_llm_brain_projection_job(self, job: dict) -> dict:
        job_json = json.dumps(job, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        memory_id = str((job.get("payload") or {}).get("memory_id") or job.get("memory_id") or "")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_brain_projection_jobs (
                    job_id, memory_id, idempotency_key, status, attempt_count, job_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    memory_id=excluded.memory_id,
                    idempotency_key=excluded.idempotency_key,
                    status=excluded.status,
                    attempt_count=excluded.attempt_count,
                    job_json=excluded.job_json,
                    updated_at=excluded.updated_at
                """,
                (
                    job["job_id"],
                    memory_id,
                    str(job.get("idempotency_key") or ""),
                    str(job.get("status") or "queued"),
                    int(job.get("attempt_count") or 0),
                    job_json,
                    now,
                ),
            )
        return self.get_llm_brain_projection_job(str(job["job_id"]))

    def get_llm_brain_projection_job(self, job_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_json FROM llm_brain_projection_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        return json.loads(row["job_json"])

    def list_llm_brain_projection_jobs(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        values: list[object] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            values.append(status)
        values.append(max(int(limit), 1))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT job_json FROM llm_brain_projection_jobs
                {where}
                ORDER BY updated_at, job_id
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [json.loads(row["job_json"]) for row in rows]

    def update_memory_card_state(
        self,
        memory_id: str,
        state: str,
        *,
        reviewed_by: str = "",
        reason: str = "",
    ) -> dict:
        disabled_at = datetime.now(timezone.utc).isoformat() if state in {"disabled", "superseded"} else ""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE memory_cards
                SET state = ?, disabled_at = ?, disabled_by = ?, disable_reason = ?
                WHERE memory_id = ?
                """,
                (state, disabled_at, reviewed_by, reason, memory_id),
            )
        if state in {"disabled", "superseded"}:
            self.mark_disabled(memory_id)
        card = self.get_memory_card(memory_id)
        if card is None:
            raise ValueError(f"unknown memory card: {memory_id}")
        return card

    def upsert_profile_fact(self, *, memory_id: str, project: str, fact_type: str, content_hash: str, state: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO profile_facts (memory_id, project, fact_type, content_hash, state)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    project=excluded.project,
                    fact_type=excluded.fact_type,
                    content_hash=excluded.content_hash,
                    state=excluded.state
                """,
                (memory_id, project, fact_type, content_hash, state),
            )

    def list_profile_facts(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT memory_id, project, fact_type, content_hash, state FROM profile_facts ORDER BY memory_id",
            ).fetchall()
        return [dict(row) for row in rows]

    def list_approved_memory_cards(self, *, project: str | None = None, limit: int = 10) -> list[dict]:
        with self._connect() as connection:
            if project:
                rows = connection.execute(
                    """
                    SELECT * FROM memory_cards
                    WHERE state = 'active' AND project = ?
                    ORDER BY approved_at DESC
                    LIMIT ?
                    """,
                    (project, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM memory_cards
                    WHERE state = 'active'
                    ORDER BY approved_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

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

    def upsert_eval_query(self, query: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO eval_queries (
                    query_id, query_hash, query_terms_json, project, provider,
                    expected_memory_ids_json, k, min_recall, min_precision,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_id) DO UPDATE SET
                    query_hash=excluded.query_hash,
                    query_terms_json=excluded.query_terms_json,
                    project=excluded.project,
                    provider=excluded.provider,
                    expected_memory_ids_json=excluded.expected_memory_ids_json,
                    k=excluded.k,
                    min_recall=excluded.min_recall,
                    min_precision=excluded.min_precision,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    query["query_id"],
                    query["query_hash"],
                    json.dumps(list(query.get("query_terms", [])), sort_keys=True, separators=(",", ":")),
                    query["project"],
                    query.get("provider", ""),
                    json.dumps(list(query["expected_memory_ids"]), sort_keys=True, separators=(",", ":")),
                    int(query["k"]),
                    float(query["min_recall"]),
                    float(query["min_precision"]),
                    1 if query.get("enabled", True) else 0,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM eval_queries WHERE query_id = ?",
                (query["query_id"],),
            ).fetchone()
        return _eval_query_from_row(row)

    def list_eval_queries(self, *, project: str | None = None, provider: str | None = None, enabled_only: bool = False) -> list[dict]:
        filters = []
        values = []
        if project:
            filters.append("project = ?")
            values.append(project)
        if provider:
            filters.append("provider = ?")
            values.append(provider)
        if enabled_only:
            filters.append("enabled = 1")
        where = "WHERE " + " AND ".join(filters) if filters else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM eval_queries
                {where}
                ORDER BY query_id
                """,
                values,
            ).fetchall()
        return [_eval_query_from_row(row) for row in rows]

    def insert_eval_run(self, run: dict) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO eval_runs (
                    run_id, status, project, provider, k, query_count,
                    metrics_json, failures_json, network_used,
                    mutation_performed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    run["status"],
                    run.get("project", ""),
                    run.get("provider", ""),
                    int(run["k"]),
                    int(run["query_count"]),
                    json.dumps(run["metrics"], sort_keys=True, separators=(",", ":")),
                    json.dumps(run["failures"], sort_keys=True, separators=(",", ":")),
                    1 if run.get("network_used", False) else 0,
                    1 if run.get("mutation_performed", False) else 0,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM eval_runs WHERE run_id = ?",
                (run["run_id"],),
            ).fetchone()
        return dict(row)

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

    def list_retrieval_audit(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT audit_id, pack_id, prompt_hash, query_hash, filters_json,
                       result_count, private_allowed, created_at
                FROM retrieval_audit
                ORDER BY created_at
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_context_pack_items(self, pack_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT pack_id, item_index, kind, reference_id, title, summary, score, metadata_json
                FROM context_pack_items
                WHERE pack_id = ?
                ORDER BY item_index
                """,
                (pack_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_auto_recall_audit(
        self,
        *,
        provider: str,
        project: str,
        status: str,
        policy_reasons: list[str],
        private_policy_allowed: bool,
        prompt_hash: str,
        preview_hash: str = "",
        context_pack_id: str = "",
        selected_items: list[dict] | None = None,
    ) -> dict:
        audit_id = "auto_recall_" + uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auto_recall_audit (
                    audit_id, provider, project, status, policy_reasons_json,
                    private_policy_allowed, prompt_hash, preview_hash,
                    context_pack_id, selected_items_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    provider,
                    project,
                    status,
                    json.dumps(list(policy_reasons), sort_keys=True, separators=(",", ":")),
                    1 if private_policy_allowed else 0,
                    prompt_hash,
                    preview_hash,
                    context_pack_id,
                    json.dumps(selected_items or [], sort_keys=True, separators=(",", ":")),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM auto_recall_audit WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
        return dict(row)

    def record_memory_gc_audit(
        self,
        *,
        gc_kind: str,
        operation: str,
        schema_version: str,
        mode: str,
        knowledge_id: str,
        ragflow_document_id: str,
        dataset_id: str,
        replacement_knowledge_id: str,
        dirty_at: str = "",
        snapshot_updated_at: str = "",
        approval_operation: str = "",
        age_gate_seconds: int = 0,
        mutated: bool = True,
    ) -> dict:
        """G-3 (M-GC contract §3.4 A1/A2/A3): durable append-only audit row for
        one successful GC mutation. The raw RAGFlow document id is NEVER stored;
        only its sha256 hex digest is persisted (A3). For the irreversible
        session_memory hard delete, ``replacement_knowledge_id`` records the
        active generation that justified the delete so it stays reconstructable
        after the doc disappears (A2). The bound epoch markers (``dirty_at`` and
        ``snapshot_updated_at``) make "which generation activation justified the
        GC" reconstructable (E3)."""
        audit_id = "memory_gc_" + uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        document_id_hash = hashlib.sha256(str(ragflow_document_id or "").encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_gc_audit (
                    audit_id, gc_kind, operation, schema_version, mode,
                    knowledge_id, ragflow_document_id_hash, dataset_id,
                    replacement_knowledge_id, dirty_at, snapshot_updated_at,
                    approval_operation, age_gate_seconds, mutated, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    gc_kind,
                    operation,
                    schema_version,
                    mode,
                    knowledge_id,
                    document_id_hash,
                    dataset_id,
                    replacement_knowledge_id,
                    dirty_at,
                    snapshot_updated_at,
                    approval_operation,
                    int(age_gate_seconds),
                    1 if mutated else 0,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_gc_audit WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
        return dict(row)

    def list_memory_gc_audit(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT audit_id, gc_kind, operation, schema_version, mode,
                       knowledge_id, ragflow_document_id_hash, dataset_id,
                       replacement_knowledge_id, dirty_at, snapshot_updated_at,
                       approval_operation, age_gate_seconds, mutated, created_at
                FROM memory_gc_audit
                ORDER BY created_at, audit_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_prepared(
        self,
        *,
        knowledge_id: str,
        content_hash: str,
        provider: str,
        project: str,
        domain: str,
        type: str,
        title: str,
        summary: str,
        privacy_level: str = "normal",
        supersedes: str = "",
        session_id_hash: str = "",
        evidence_status: str = "historical",
        coverage_status: str = "",
        coverage_gap_count: int = 0,
        coverage_duplicate_count: int = 0,
        source_manifest_hash: str = "",
        source_chunk_count: int = 0,
        metadata: dict | None = None,
    ) -> dict:
        metadata_json = _normalize_metadata_json(metadata)
        bounded_summary = summary[:500]
        coverage_gap_count = max(int(coverage_gap_count), 0)
        coverage_duplicate_count = max(int(coverage_duplicate_count), 0)
        source_chunk_count = max(int(source_chunk_count), 0)
        source_manifest_hash = str(source_manifest_hash or "")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
            if existing is not None:
                if not source_manifest_hash:
                    source_manifest_hash = str(existing["source_manifest_hash"] or "")
                if source_chunk_count <= 0:
                    source_chunk_count = max(int(existing["source_chunk_count"] or 0), 0)
            if existing is not None and existing["content_hash"] != content_hash:
                if (
                    existing["status"] != "prepared"
                    or existing["ragflow_dataset_id"]
                    or existing["ragflow_document_id"]
                    or existing["ingress_job_id"]
                    or existing["queued_at"]
                    or existing["indexed_at"]
                ):
                    raise ValueError("cannot change content hash for a delivered knowledge item")
                content_owner = connection.execute(
                    "SELECT knowledge_id FROM knowledge_items WHERE content_hash = ?",
                    (content_hash,),
                ).fetchone()
                if content_owner is not None and content_owner["knowledge_id"] != knowledge_id:
                    raise ValueError("content hash already belongs to another knowledge item")
                if existing is not None and metadata is None:
                    metadata_json = str(existing["metadata_json"] or "{}")
                connection.execute(
                    """
                    UPDATE knowledge_items
                    SET content_hash=?,
                        provider=?,
                        project=?,
                        domain=?,
                        type=?,
                        session_id_hash=CASE
                            WHEN ? != '' THEN ?
                            ELSE session_id_hash
                        END,
                        title=?,
                        summary=?,
                        privacy_level=?,
                        supersedes=?,
                        evidence_status=?,
                        coverage_status=?,
                        coverage_gap_count=?,
                        coverage_duplicate_count=?,
                        source_manifest_hash=?,
                        source_chunk_count=?,
                        metadata_json=?,
                        status='prepared',
                        ragflow_dataset_id='',
                        ragflow_document_id='',
                        ingress_target_profile='',
                        ingress_job_id='',
                        queued_at='',
                        ragflow_run='',
                        ragflow_progress=0,
                        indexed_at='',
                        disabled_at='',
                        authorization_status='active'
                    WHERE knowledge_id=?
                    """,
                    (
                        content_hash,
                        provider,
                        project,
                        domain,
                        type,
                        session_id_hash,
                        session_id_hash,
                        title,
                        bounded_summary,
                        privacy_level,
                        supersedes,
                        evidence_status,
                        coverage_status,
                        coverage_gap_count,
                        coverage_duplicate_count,
                        source_manifest_hash,
                        source_chunk_count,
                        metadata_json,
                        knowledge_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
                    (knowledge_id,),
                ).fetchone()
                return dict(row)
            connection.execute(
                """
                INSERT INTO knowledge_items (
                    knowledge_id, content_hash, provider, project, domain, type,
                    session_id_hash, title, summary, privacy_level, supersedes,
                    evidence_status, coverage_status, coverage_gap_count,
                    coverage_duplicate_count, source_manifest_hash, source_chunk_count,
                    metadata_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared')
                ON CONFLICT(content_hash) DO UPDATE SET
                    session_id_hash=CASE
                        WHEN excluded.session_id_hash != '' THEN excluded.session_id_hash
                        ELSE knowledge_items.session_id_hash
                    END,
                    title=excluded.title,
                    summary=excluded.summary,
                    evidence_status=excluded.evidence_status,
                    coverage_status=excluded.coverage_status,
                    coverage_gap_count=excluded.coverage_gap_count,
                    coverage_duplicate_count=excluded.coverage_duplicate_count,
                    source_manifest_hash=CASE
                        WHEN excluded.source_manifest_hash != '' THEN excluded.source_manifest_hash
                        ELSE knowledge_items.source_manifest_hash
                    END,
                    source_chunk_count=CASE
                        WHEN excluded.source_chunk_count > 0 THEN excluded.source_chunk_count
                        ELSE knowledge_items.source_chunk_count
                    END,
                    metadata_json=CASE
                        WHEN ? THEN knowledge_items.metadata_json
                        ELSE excluded.metadata_json
                    END,
                    status='prepared',
                    ragflow_dataset_id='',
                    ragflow_document_id='',
                    ingress_target_profile='',
                    ingress_job_id='',
                    queued_at='',
                    ragflow_run='',
                    ragflow_progress=0,
                    indexed_at='',
                    disabled_at='',
                    authorization_status='active'
                """,
                (
                    knowledge_id,
                    content_hash,
                    provider,
                    project,
                    domain,
                    type,
                    session_id_hash,
                    title,
                    bounded_summary,
                    privacy_level,
                    supersedes,
                    evidence_status,
                    coverage_status,
                    coverage_gap_count,
                    coverage_duplicate_count,
                    source_manifest_hash,
                    source_chunk_count,
                    metadata_json,
                    # CASE WHEN ? (boolean): metadata가 없으면 기존 metadata_json 보존.
                    # Python bool로 바인딩 — SQLite(truthy)·PostgreSQL(boolean) 양쪽 호환.
                    metadata is None,
                ),
            )
        return self.get_by_knowledge_id(knowledge_id)

    def get_by_knowledge_id(self, knowledge_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_items WHERE knowledge_id = ?", (knowledge_id,)).fetchone()
        return dict(row) if row else None

    def _update_status(self, knowledge_id: str, status: str, **fields) -> None:
        assignments = ["status = ?"]
        values = [status]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(knowledge_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE knowledge_items SET {', '.join(assignments)} WHERE knowledge_id = ?",
                values,
            )
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)

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

    def list_index_timeouts(self, *, dataset_id: str | None = None, limit: int = 50) -> list[dict]:
        query = """
            SELECT knowledge_id, ragflow_dataset_id, ragflow_document_id, ragflow_run, ragflow_progress
            FROM knowledge_items
            WHERE status = 'index_timeout'
              AND ragflow_dataset_id IS NOT NULL
              AND ragflow_dataset_id != ''
              AND ragflow_document_id IS NOT NULL
              AND ragflow_document_id != ''
        """
        params: list[object] = []
        if dataset_id:
            query += " AND ragflow_dataset_id = ?"
            params.append(dataset_id)
        query += " ORDER BY updated_at ASC LIMIT ?"
        params.append(max(limit, 1))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_queued_transcript_chunks(self, *, target_profile: str, limit: int = 50) -> list[dict]:
        query = """
            SELECT ki.knowledge_id AS knowledge_id,
                   tc.chunk_id AS chunk_id,
                   tc.provider AS provider,
                   tc.project AS project,
                   tc.session_id_hash AS session_id_hash,
                   ki.content_hash AS content_hash,
                   ki.ingress_job_id AS ingress_job_id
            FROM knowledge_items ki
            JOIN transcript_chunks tc ON tc.knowledge_id = ki.knowledge_id
            WHERE ki.type = 'conversation_chunk'
              AND ki.status = 'queued'
              AND ki.ingress_target_profile = ?
              AND ki.ingress_job_id IS NOT NULL
              AND ki.ingress_job_id != ''
            ORDER BY ki.queued_at ASC
            LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(query, [target_profile, max(limit, 1)]).fetchall()
        return [dict(row) for row in rows]

    def list_queued_documents(self, *, document_type: str, target_profile: str, limit: int = 50) -> list[dict]:
        query = """
            SELECT *
            FROM knowledge_items
            WHERE type = ?
              AND status = 'queued'
              AND ingress_target_profile = ?
              AND ingress_job_id IS NOT NULL
              AND ingress_job_id != ''
            ORDER BY queued_at ASC
            LIMIT ?
        """
        with self._connect() as connection:
            rows = [dict(row) for row in connection.execute(query, [document_type, target_profile, max(limit, 1)]).fetchall()]
            chunk_rows = {}
            if document_type == "conversation_chunk" and rows:
                chunk_rows = {
                    row["knowledge_id"]: dict(row)
                    for row in connection.execute(
                        "SELECT * FROM transcript_chunks WHERE knowledge_id IN (%s)" % ",".join("?" for _ in rows),
                        [row["knowledge_id"] for row in rows],
                    ).fetchall()
                }
        return [
            _queued_document_projection(
                row,
                chunk_rows.get(row["knowledge_id"]) if document_type == "conversation_chunk" else None,
            )
            for row in rows
        ]

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

    def mark_quarantined_if_queued(
        self,
        knowledge_id: str,
        *,
        reason: str,
        disposition_action: str,
        run_bucket: str = "",
        expected_target_profile: str,
        expected_ingress_job_id: str,
        expected_updated_at: str,
    ) -> bool:
        item = self.get_by_knowledge_id(knowledge_id) or {}
        metadata = _load_metadata_json(str(item.get("metadata_json") or "{}"))
        metadata["m5_disposition_status"] = "quarantined"
        metadata["m5_disposition_action"] = str(disposition_action)
        metadata["m5_disposition_reason"] = str(reason)
        if run_bucket:
            metadata["m5_backend_run_bucket"] = str(run_bucket)
        now = datetime.now(timezone.utc).isoformat()
        metadata["m5_disposition_at"] = now
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_items
                SET status = 'quarantined',
                    metadata_json = ?,
                    ragflow_run = ?,
                    indexed_at = '',
                    updated_at = ?
                WHERE knowledge_id = ?
                  AND status = 'queued'
                  AND ingress_target_profile = ?
                  AND ingress_job_id = ?
                  AND updated_at = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    run_bucket or str(item.get("ragflow_run") or ""),
                    now,
                    knowledge_id,
                    expected_target_profile,
                    expected_ingress_job_id,
                    expected_updated_at,
                ),
            )
            updated = cursor.rowcount == 1
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)
        return updated

    def mark_replay_requested_if_queued(
        self,
        knowledge_id: str,
        *,
        reason: str,
        expected_target_profile: str,
        expected_ingress_job_id: str,
        expected_updated_at: str,
    ) -> bool:
        """CAS record an idempotent local replay-request and re-arm a queued row.

        The row stays ``queued`` and keeps its ingress target/job; ragflow
        run/progress/document_id are reset and queued_at is refreshed, and an
        explicit ``m5_disposition_status=replay_requested`` marker plus attempt
        counter are stamped on the local legacy ledger only. This NEVER writes,
        disables, deletes, or directly replays a RAGFlow document.

        Important scope limit: this is a local re-arm + audit marker. It does not by
        itself re-enqueue a queue job or create a delivery record, so it does not on
        its own cause re-delivery. The actual queue-side re-enqueue (the path that
        re-delivers a replay-requested row through the rag-ingress-queue) is a
        separate, not-yet-implemented mechanism tracked as an M6 planning item.
        """
        item = self.get_by_knowledge_id(knowledge_id) or {}
        metadata = _load_metadata_json(str(item.get("metadata_json") or "{}"))
        now = datetime.now(timezone.utc).isoformat()
        metadata["m5_disposition_status"] = "replay_requested"
        metadata["m5_disposition_action"] = "replay_missing"
        metadata["m5_disposition_reason"] = str(reason)
        metadata["m5_disposition_at"] = now
        metadata["m5_replay_attempt"] = int(metadata.get("m5_replay_attempt") or 0) + 1
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_items
                SET metadata_json = ?,
                    ragflow_run = 'QUEUED',
                    ragflow_progress = 0,
                    ragflow_document_id = '',
                    indexed_at = '',
                    queued_at = ?,
                    updated_at = ?
                WHERE knowledge_id = ?
                  AND status = 'queued'
                  AND ingress_target_profile = ?
                  AND ingress_job_id = ?
                  AND updated_at = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                    knowledge_id,
                    expected_target_profile,
                    expected_ingress_job_id,
                    expected_updated_at,
                ),
            )
            updated = cursor.rowcount == 1
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)
        return updated

    def get_transcript_chunk_by_knowledge_id(self, knowledge_id: str) -> dict | None:
        """Read-only fetch of a transcript_chunks row by knowledge_id (for replay reconstruction)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transcript_chunks WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_replay_delivered_if_queued(
        self,
        knowledge_id: str,
        *,
        reason: str,
        new_job_id: str,
        expected_target_profile: str,
        expected_ingress_job_id: str,
        expected_updated_at: str,
    ) -> bool:
        """CAS record that a replay-requested row was genuinely re-enqueued.

        Unlike :meth:`mark_replay_requested_if_queued` (which only re-arms the row),
        this is called AFTER a successful re-POST to the rag-ingress-queue created a
        new queue job. It records the new ``ingress_job_id`` and marks
        ``m5_disposition_status=replay_delivered`` so the row drops out of the
        replay-requested selection on the next run (natural idempotency). The row
        stays ``queued`` because the existing delivery worker still has to drive the
        new job to RAGFlow. No RAGFlow document is written/disabled/deleted here.
        """
        item = self.get_by_knowledge_id(knowledge_id) or {}
        metadata = _load_metadata_json(str(item.get("metadata_json") or "{}"))
        now = datetime.now(timezone.utc).isoformat()
        metadata["m5_disposition_status"] = "replay_delivered"
        metadata["m5_disposition_action"] = "replay_missing"
        metadata["m5_disposition_reason"] = str(reason)
        metadata["m6_replay_delivered_at"] = now
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_items
                SET metadata_json = ?,
                    ingress_job_id = ?,
                    ragflow_run = 'QUEUED',
                    ragflow_progress = 0,
                    ragflow_document_id = '',
                    indexed_at = '',
                    queued_at = ?,
                    updated_at = ?
                WHERE knowledge_id = ?
                  AND status = 'queued'
                  AND ingress_target_profile = ?
                  AND ingress_job_id = ?
                  AND updated_at = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    str(new_job_id),
                    now,
                    now,
                    knowledge_id,
                    expected_target_profile,
                    expected_ingress_job_id,
                    expected_updated_at,
                ),
            )
            updated = cursor.rowcount == 1
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)
        return updated

    def mark_done_via_dedupe_if_queued(
        self,
        knowledge_id: str,
        *,
        reason: str,
        dataset_id: str,
        canonical_document_id: str,
        duplicate_doc_count: int,
        expected_target_profile: str,
        expected_ingress_job_id: str,
        expected_updated_at: str,
    ) -> bool:
        """CAS converge a duplicate-exact-DONE row to terminal ``indexed`` state.

        The row had more than one exact-match DONE backend document. The caller
        selects a deterministic canonical document id; this method reflects that
        single canonical into the ledger row. It does not delete, disable, or
        otherwise mutate any RAGFlow document; backend duplicate cleanup remains a
        separate operator-gated concern.
        """
        item = self.get_by_knowledge_id(knowledge_id) or {}
        metadata = _load_metadata_json(str(item.get("metadata_json") or "{}"))
        now = datetime.now(timezone.utc).isoformat()
        metadata["m5_disposition_status"] = "deduped"
        metadata["m5_disposition_action"] = "duplicate_done"
        metadata["m5_disposition_reason"] = str(reason)
        metadata["m5_disposition_at"] = now
        metadata["m5_dedupe_canonical_selected"] = True
        metadata["m5_dedupe_duplicate_doc_count"] = int(duplicate_doc_count)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_items
                SET status = 'indexed',
                    metadata_json = ?,
                    ragflow_dataset_id = ?,
                    ragflow_document_id = ?,
                    ragflow_run = 'DONE',
                    ragflow_progress = 1.0,
                    ingress_target_profile = '',
                    ingress_job_id = '',
                    queued_at = '',
                    indexed_at = ?,
                    updated_at = ?
                WHERE knowledge_id = ?
                  AND status = 'queued'
                  AND ingress_target_profile = ?
                  AND ingress_job_id = ?
                  AND updated_at = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    dataset_id,
                    canonical_document_id,
                    now,
                    now,
                    knowledge_id,
                    expected_target_profile,
                    expected_ingress_job_id,
                    expected_updated_at,
                ),
            )
            updated = cursor.rowcount == 1
        for side_file in self.path.parent.glob(self.path.name + "*"):
            if side_file.exists():
                os.chmod(side_file, 0o600)
        return updated

    def count_m5_dispositions(self, *, document_type: str) -> dict:
        """Count all M5 operator dispositions (quarantine/replay/dedupe) by status and action.

        Scans by document type only so it remains correct even after a disposition
        clears the row's ingress target profile (e.g. dedupe terminal state). These
        are operator dispositions recorded on the local ledger, not delivery success.
        """
        query = """
            SELECT metadata_json
            FROM knowledge_items
            WHERE type = ?
        """
        by_status: dict[str, int] = {}
        by_action: dict[str, int] = {}
        total = 0
        with self._connect() as connection:
            rows = connection.execute(query, (document_type,)).fetchall()
        for row in rows:
            metadata = _load_metadata_json(str(row["metadata_json"] or "{}"))
            status = str(metadata.get("m5_disposition_status") or "")
            if not status:
                continue
            total += 1
            action = str(metadata.get("m5_disposition_action") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            by_action[action] = by_action.get(action, 0) + 1
        return {
            "disposition_count": total,
            "by_status": dict(sorted(by_status.items())),
            "by_action": dict(sorted(by_action.items())),
            "raw_ids_printed": False,
        }

    def count_m5_quarantined_dispositions(self, *, document_type: str, target_profile: str) -> dict:
        query = """
            SELECT metadata_json
            FROM knowledge_items
            WHERE type = ?
              AND status = 'quarantined'
              AND ingress_target_profile = ?
        """
        by_action: dict[str, int] = {}
        by_action_run: dict[str, int] = {}
        with self._connect() as connection:
            rows = connection.execute(query, (document_type, target_profile)).fetchall()
        for row in rows:
            metadata = _load_metadata_json(str(row["metadata_json"] or "{}"))
            action = str(metadata.get("m5_disposition_action") or "unknown")
            run_bucket = str(metadata.get("m5_backend_run_bucket") or "")
            by_action[action] = by_action.get(action, 0) + 1
            key = f"{action}:{run_bucket or '-'}"
            by_action_run[key] = by_action_run.get(key, 0) + 1
        return {
            "quarantined_count": len(rows),
            "by_action": dict(sorted(by_action.items())),
            "by_action_run": dict(sorted(by_action_run.items())),
            "raw_ids_printed": False,
        }

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

    def mark_session_memory_dirty(
        self,
        *,
        session_id_hash: str,
        provider: str,
        project: str,
        reason: str,
        source_knowledge_id: str = "",
    ) -> dict:
        if not session_id_hash:
            raise ValueError("session_id_hash is required")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dirty_session_memory (
                    session_id_hash, provider, project, status, reason,
                    source_knowledge_id, dirty_at, updated_at, attempts,
                    next_attempt_at, last_error_class, last_summary_knowledge_id,
                    last_ingress_job_id
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 0, '', '', '', '')
                ON CONFLICT(session_id_hash) DO UPDATE SET
                    provider=excluded.provider,
                    project=excluded.project,
                    status='pending',
                    reason=excluded.reason,
                    source_knowledge_id=excluded.source_knowledge_id,
                    dirty_at=excluded.dirty_at,
                    updated_at=excluded.updated_at,
                    attempts=0,
                    next_attempt_at='',
                    last_error_class='',
                    last_summary_knowledge_id='',
                    last_ingress_job_id=''
                """,
                (session_id_hash, provider, project, reason, source_knowledge_id, now, now),
            )
        return self.get_dirty_session_memory(session_id_hash)

    def get_dirty_session_memory(self, session_id_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dirty_session_memory WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
        return dict(row) if row else None

    def list_dirty_session_memory(self, *, limit: int = 50, quiet_period_seconds: int = 60) -> list[dict]:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=max(int(quiet_period_seconds), 0))).isoformat()
        now_text = now.isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM dirty_session_memory
                WHERE status IN ('pending', 'failed')
                  AND dirty_at <= ?
                  AND (next_attempt_at = '' OR next_attempt_at <= ?)
                ORDER BY dirty_at ASC, updated_at ASC
                LIMIT ?
                """,
                (cutoff, now_text, max(int(limit), 1)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_dirty_session_memory_enqueued(
        self,
        *,
        session_id_hash: str,
        summary_knowledge_id: str,
        ingress_job_id: str,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_session_memory
                SET status='enqueued',
                    updated_at=?,
                    last_summary_knowledge_id=?,
                    last_ingress_job_id=?,
                    last_error_class=''
                WHERE session_id_hash=?
                """,
                (now, summary_knowledge_id, ingress_job_id, session_id_hash),
            )
        return self.get_dirty_session_memory(session_id_hash)

    def mark_dirty_session_memory_skipped(self, *, session_id_hash: str, reason: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_session_memory
                SET status='skipped',
                    reason=?,
                    updated_at=?,
                    last_error_class=''
                WHERE session_id_hash=?
                """,
                (reason, now, session_id_hash),
            )
        return self.get_dirty_session_memory(session_id_hash)

    def mark_dirty_session_memory_failed(self, *, session_id_hash: str, error_class: str) -> dict:
        now = datetime.now(timezone.utc)
        next_attempt = (now + timedelta(seconds=60)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_session_memory
                SET status='failed',
                    updated_at=?,
                    attempts=attempts + 1,
                    next_attempt_at=?,
                    last_error_class=?
                WHERE session_id_hash=?
                """,
                (now.isoformat(), next_attempt, error_class[:80], session_id_hash),
            )
        return self.get_dirty_session_memory(session_id_hash)

    def mark_dirty_session_memory_promoted(self, *, session_id_hash: str, summary_knowledge_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_session_memory
                SET status='promoted',
                    updated_at=?,
                    last_summary_knowledge_id=?,
                    last_error_class=''
                WHERE session_id_hash=?
                """,
                (now, summary_knowledge_id, session_id_hash),
            )
        return self.get_dirty_session_memory(session_id_hash)

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

    def get_session_memory_active_snapshot(self, session_id_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_memory_active_snapshots WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
        return dict(row) if row else None

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

    def get_session_memory_by_session_id_hash(self, session_id_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM knowledge_items
                WHERE type = 'session_memory' AND session_id_hash = ?
                ORDER BY ingested_at DESC, knowledge_id DESC LIMIT 1
                """,
                (session_id_hash,),
            ).fetchone()
        return dict(row) if row else None

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

    def record_session_memory_coverage(
        self,
        *,
        active_knowledge_id: str,
        source_content_hash: str,
        source_window_hash: str,
        derived_content_hash: str,
        redaction_version: str,
        turn_start_index: int,
        turn_end_index: int,
    ) -> dict:
        if not active_knowledge_id:
            raise ValueError("active_knowledge_id is required")
        if not _is_sha256_hash(source_content_hash):
            raise ValueError("source_content_hash must be a hash")
        if not _is_sha256_hash(source_window_hash):
            raise ValueError("source_window_hash must be a hash")
        if not _is_sha256_hash(derived_content_hash):
            raise ValueError("derived_content_hash must be a hash")
        if turn_start_index <= 0:
            raise ValueError("turn_start_index must be strictly positive")
        if turn_end_index < turn_start_index:
            raise ValueError("turn_end_index must be >= turn_start_index")
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash
                ) DO UPDATE SET
                    redaction_version=excluded.redaction_version,
                    created_at=excluded.created_at,
                    turn_start_index=excluded.turn_start_index,
                    turn_end_index=excluded.turn_end_index
                """,
                (
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash,
                    redaction_version,
                    created_at,
                    turn_start_index,
                    turn_end_index,
                ),
            )
            row = connection.execute(
                """
                SELECT
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash,
                    redaction_version,
                    created_at,
                    turn_start_index,
                    turn_end_index
                FROM session_memory_coverage_edges
                WHERE active_knowledge_id = ?
                  AND source_content_hash = ?
                  AND source_window_hash = ?
                  AND derived_content_hash = ?
                  AND redaction_version = ?
                """,
                (
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash,
                    redaction_version,
                ),
            ).fetchone()
        return dict(row)

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

    def list_session_memory_coverage(self, active_knowledge_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    active_knowledge_id,
                    source_content_hash,
                    source_window_hash,
                    derived_content_hash,
                    redaction_version,
                    created_at,
                    turn_start_index,
                    turn_end_index
                FROM session_memory_coverage_edges
                WHERE active_knowledge_id = ?
                ORDER BY turn_start_index, turn_end_index, created_at
                """,
                (active_knowledge_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def mark_project_memory_dirty(
        self,
        *,
        provider: str,
        project: str,
        reason: str,
        source_knowledge_id: str = "",
    ) -> dict:
        if not provider:
            raise ValueError("provider is required")
        if not project:
            raise ValueError("project is required")
        project_key_hash = _project_key_hash(provider, project)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dirty_project_memory (
                    project_key_hash, provider, project, status, reason,
                    source_knowledge_id, dirty_at, updated_at, attempts,
                    next_attempt_at, last_error_class, last_snapshot_knowledge_id,
                    last_ingress_job_id
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 0, '', '', '', '')
                ON CONFLICT(project_key_hash) DO UPDATE SET
                    provider=excluded.provider,
                    project=excluded.project,
                    status='pending',
                    reason=excluded.reason,
                    source_knowledge_id=excluded.source_knowledge_id,
                    dirty_at=excluded.dirty_at,
                    updated_at=excluded.updated_at,
                    attempts=0,
                    next_attempt_at='',
                    last_error_class='',
                    last_snapshot_knowledge_id='',
                    last_ingress_job_id=''
                """,
                (project_key_hash, provider, project, reason, source_knowledge_id, now, now),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)

    def get_dirty_project_memory(self, *, provider: str, project: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dirty_project_memory WHERE project_key_hash = ?",
                (_project_key_hash(provider, project),),
            ).fetchone()
        return dict(row) if row else None

    def list_dirty_project_memory(self, *, limit: int = 50, quiet_period_seconds: int = 60) -> list[dict]:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=max(int(quiet_period_seconds), 0))).isoformat()
        now_text = now.isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM dirty_project_memory
                WHERE status IN ('pending', 'failed')
                  AND dirty_at <= ?
                  AND (next_attempt_at = '' OR next_attempt_at <= ?)
                ORDER BY dirty_at ASC, updated_at ASC
                LIMIT ?
                """,
                (cutoff, now_text, max(int(limit), 1)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_dirty_project_memory_enqueued(
        self,
        *,
        provider: str,
        project: str,
        snapshot_knowledge_id: str,
        ingress_job_id: str,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_project_memory
                SET status='enqueued',
                    updated_at=?,
                    last_snapshot_knowledge_id=?,
                    last_ingress_job_id=?,
                    last_error_class=''
                WHERE project_key_hash=?
                """,
                (now, snapshot_knowledge_id, ingress_job_id, _project_key_hash(provider, project)),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)

    def mark_dirty_project_memory_skipped(self, *, provider: str, project: str, reason: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_project_memory
                SET status='skipped',
                    reason=?,
                    updated_at=?,
                    last_error_class=''
                WHERE project_key_hash=?
                """,
                (reason, now, _project_key_hash(provider, project)),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)

    def mark_dirty_project_memory_failed(self, *, provider: str, project: str, error_class: str) -> dict:
        now = datetime.now(timezone.utc)
        next_attempt = (now + timedelta(seconds=60)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_project_memory
                SET status='failed',
                    updated_at=?,
                    attempts=attempts + 1,
                    next_attempt_at=?,
                    last_error_class=?
                WHERE project_key_hash=?
                """,
                (now.isoformat(), next_attempt, error_class[:80], _project_key_hash(provider, project)),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)

    def mark_dirty_project_memory_promoted(self, *, provider: str, project: str, snapshot_knowledge_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE dirty_project_memory
                SET status='promoted',
                    updated_at=?,
                    last_snapshot_knowledge_id=?,
                    last_error_class=''
                WHERE project_key_hash=?
                """,
                (now, snapshot_knowledge_id, _project_key_hash(provider, project)),
            )
        return self.get_dirty_project_memory(provider=provider, project=project)

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

    def get_project_memory_active_snapshot(self, *, provider: str, project: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_memory_active_snapshots WHERE project_key_hash = ?",
                (_project_key_hash(provider, project),),
            ).fetchone()
        return dict(row) if row else None

    def update_authorization_metadata(
        self,
        knowledge_id: str,
        *,
        project: str | None = None,
        status: str | None = None,
        privacy_level: str | None = None,
        supersedes: str | None = None,
        valid_until: str | None = None,
        authorization_status: str | None = None,
    ) -> dict:
        fields = {
            "project": project,
            "status": status,
            "privacy_level": privacy_level,
            "supersedes": supersedes,
            "valid_until": valid_until,
            "authorization_status": authorization_status,
        }
        assignments = []
        values = []
        for key, value in fields.items():
            if value is None:
                continue
            assignments.append(f"{key} = ?")
            values.append(value)
        if not assignments:
            return self.get_by_knowledge_id(knowledge_id)
        values.append(knowledge_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE knowledge_items SET {', '.join(assignments)} WHERE knowledge_id = ?",
                values,
            )
        return self.get_by_knowledge_id(knowledge_id)

    def upsert_ragflow_dataset_plan(self, plan) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        dataset_id = plan.required_resource_ids["dataset_id"]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ragflow_datasets (
                    logical_name, dataset_id, metadata_policy_version,
                    contract_version, created_at, enabled, disabled_at
                ) VALUES (?, ?, ?, ?, ?, 1, '')
                ON CONFLICT(logical_name) DO UPDATE SET
                    dataset_id=excluded.dataset_id,
                    metadata_policy_version=excluded.metadata_policy_version,
                    contract_version=excluded.contract_version,
                    enabled=1,
                    disabled_at=''
                """,
                (
                    plan.logical_name,
                    dataset_id,
                    "redaction.v2",
                    plan.contract_version,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM ragflow_datasets WHERE logical_name = ?",
                (plan.logical_name,),
            ).fetchone()
        return dict(row)

    def get_ragflow_dataset(self, logical_name: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ragflow_datasets WHERE logical_name = ?",
                (logical_name,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_transcript_session(self, session) -> dict:
        record = session.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_sessions (
                    session_id_hash, provider, project, started_at, ended_at,
                    source_status, source_locator_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id_hash) DO UPDATE SET
                    provider=excluded.provider,
                    project=excluded.project,
                    started_at=excluded.started_at,
                    ended_at=excluded.ended_at,
                    source_status=excluded.source_status,
                    source_locator_hash=excluded.source_locator_hash,
                    updated_at=excluded.updated_at
                """,
                (
                    record["session_id_hash"],
                    record["provider"],
                    record["project"],
                    record.get("started_at", ""),
                    record.get("ended_at", ""),
                    record.get("source_status", "source_unproven"),
                    record.get("source_locator_hash", ""),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM transcript_sessions WHERE session_id_hash = ?",
                (record["session_id_hash"],),
            ).fetchone()
        return dict(row)

    def upsert_transcript_turn(self, turn) -> dict:
        record = turn.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_turns (
                    turn_id_hash, session_id_hash, turn_index, role, observed_at,
                    redacted_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id_hash) DO UPDATE SET
                    session_id_hash=excluded.session_id_hash,
                    turn_index=excluded.turn_index,
                    role=excluded.role,
                    observed_at=excluded.observed_at,
                    redacted_text=excluded.redacted_text,
                    updated_at=excluded.updated_at
                """,
                (
                    record["turn_id_hash"],
                    record["session_id_hash"],
                    record["turn_index"],
                    record["role"],
                    record.get("observed_at", ""),
                    record["redacted_text"],
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM transcript_turns WHERE turn_id_hash = ?",
                (record["turn_id_hash"],),
            ).fetchone()
        return dict(row)

    def list_transcript_sessions(self, *, project: str | None = None, provider: str | None = None, limit: int = 100) -> list[dict]:
        filters = []
        params: list[object] = []
        if project:
            filters.append("project = ?")
            params.append(project)
        if provider:
            filters.append("provider = ?")
            params.append(provider)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        params.append(max(int(limit), 1))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM transcript_sessions {where} ORDER BY session_id_hash LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_transcript_turns(self, session_id_hash: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transcript_turns WHERE session_id_hash = ? ORDER BY turn_index",
                (session_id_hash,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_transcript_session(self, session_id_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transcript_sessions WHERE session_id_hash = ?",
                (session_id_hash,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_transcript_tool_event(self, tool_event) -> dict:
        record = tool_event.to_record()
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_tool_events (
                    tool_event_id_hash, turn_id_hash, event_index, tool_name,
                    event_type, redacted_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_event_id_hash) DO UPDATE SET
                    turn_id_hash=excluded.turn_id_hash,
                    event_index=excluded.event_index,
                    tool_name=excluded.tool_name,
                    event_type=excluded.event_type,
                    redacted_summary=excluded.redacted_summary,
                    updated_at=excluded.updated_at
                """,
                (
                    record["tool_event_id_hash"],
                    record["turn_id_hash"],
                    record["event_index"],
                    record["tool_name"],
                    record["event_type"],
                    record["redacted_summary"],
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM transcript_tool_events WHERE tool_event_id_hash = ?",
                (record["tool_event_id_hash"],),
            ).fetchone()
        return dict(row)

    def upsert_transcript_chunk(self, *, knowledge_id: str, chunk) -> dict:
        record = chunk.to_record()
        item = self.get_by_content_hash(record["content_hash"])
        if item is None:
            item = self.upsert_prepared(
                knowledge_id=knowledge_id,
                content_hash=record["content_hash"],
                provider=record["provider"],
                project=record["project"],
                domain="agent_memory",
                type="conversation_chunk",
                title=chunk.title(),
                summary=chunk.summary(),
                privacy_level="private",
            )
        if item is None:
            item = self.get_by_content_hash(record["content_hash"])
        if item is None:
            raise ValueError("failed to resolve canonical knowledge item for transcript chunk")
        canonical_knowledge_id = item["knowledge_id"]
        if item["type"] != "conversation_chunk":
            self._update_transcript_knowledge_item(canonical_knowledge_id, chunk)
            item = self.get_by_knowledge_id(canonical_knowledge_id)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_chunks (
                    chunk_id, knowledge_id, session_id_hash, provider, project,
                    turn_start_index, turn_end_index, part_index, part_count,
                    char_start, char_end, content_hash, redacted_text,
                    source_status, redaction_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    knowledge_id=excluded.knowledge_id,
                    session_id_hash=excluded.session_id_hash,
                    provider=excluded.provider,
                    project=excluded.project,
                    turn_start_index=excluded.turn_start_index,
                    turn_end_index=excluded.turn_end_index,
                    part_index=excluded.part_index,
                    part_count=excluded.part_count,
                    char_start=excluded.char_start,
                    char_end=excluded.char_end,
                    content_hash=excluded.content_hash,
                    redacted_text=excluded.redacted_text,
                    source_status=excluded.source_status,
                    redaction_version=excluded.redaction_version,
                    updated_at=excluded.updated_at
                """,
                (
                    record["chunk_id"],
                    canonical_knowledge_id,
                    record["session_id_hash"],
                    record["provider"],
                    record["project"],
                    record["turn_start_index"],
                    record["turn_end_index"],
                    record.get("part_index", 1),
                    record.get("part_count", 1),
                    record.get("char_start", 0),
                    record.get("char_end", 0),
                    record["content_hash"],
                    record["redacted_text"],
                    record["source_status"],
                    record["redaction_version"],
                    now,
                    now,
                ),
            )
        return item

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

    def list_tool_evidence_summaries(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[dict]:
        filters: list[str] = []
        params: list[object] = []
        if project:
            filters.append("project = ?")
            params.append(project)
        if provider:
            filters.append("provider = ?")
            params.append(provider)
        if session_id_hash:
            filters.append("session_id_hash = ?")
            params.append(session_id_hash)
        where = (" WHERE " + " AND ".join(filters)) if filters else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM tool_evidence_summaries
                {where}
                ORDER BY session_id_hash, evidence_index, evidence_id_hash
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_indexed_transcript_chunks(
        self,
        *,
        project: str | None = None,
        provider: str | None = None,
        session_id_hash: str | None = None,
    ) -> list[dict]:
        filters = ["ki.type = 'conversation_chunk'", "ki.status = 'indexed'"]
        params: list[object] = []
        if project:
            filters.append("tc.project = ?")
            params.append(project)
        if provider:
            filters.append("tc.provider = ?")
            params.append(provider)
        if session_id_hash:
            filters.append("tc.session_id_hash = ?")
            params.append(session_id_hash)
        where = " AND ".join(filters)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    tc.knowledge_id,
                    tc.chunk_id,
                    tc.session_id_hash,
                    tc.provider,
                    tc.project,
                    tc.turn_start_index,
                    tc.turn_end_index,
                    tc.part_index,
                    tc.part_count,
                    tc.char_start,
                    tc.char_end,
                    COALESCE(
                        (
                            SELECT tt.observed_at
                            FROM transcript_turns tt
                            WHERE tt.session_id_hash = tc.session_id_hash
                              AND tt.turn_index >= tc.turn_start_index
                              AND tt.turn_index <= tc.turn_end_index
                              AND tt.observed_at != ''
                            ORDER BY tt.turn_index ASC
                            LIMIT 1
                        ),
                        ts.started_at,
                        tc.created_at
                    ) AS observed_at_start,
                    COALESCE(
                        (
                            SELECT tt.observed_at
                            FROM transcript_turns tt
                            WHERE tt.session_id_hash = tc.session_id_hash
                              AND tt.turn_index >= tc.turn_start_index
                              AND tt.turn_index <= tc.turn_end_index
                              AND tt.observed_at != ''
                            ORDER BY tt.turn_index DESC
                            LIMIT 1
                        ),
                        ts.ended_at,
                        tc.updated_at
                    ) AS observed_at_end,
                    tc.content_hash,
                    tc.redacted_text,
                    tc.source_status,
                    tc.redaction_version
                FROM transcript_chunks tc
                JOIN knowledge_items ki ON ki.knowledge_id = tc.knowledge_id
                LEFT JOIN transcript_sessions ts ON ts.session_id_hash = tc.session_id_hash
                WHERE {where}
                ORDER BY
                    tc.project,
                    tc.provider,
                    tc.session_id_hash,
                    tc.turn_start_index,
                    tc.turn_end_index,
                    tc.part_index,
                    tc.char_start,
                    tc.char_end,
                    tc.chunk_id
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def _maybe_mark_session_memory_dirty_for_indexed_item(self, knowledge_id: str) -> None:
        item = self.get_by_knowledge_id(knowledge_id)
        if item is None or item.get("type") != "conversation_chunk":
            return
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id_hash, provider, project
                FROM transcript_chunks
                WHERE knowledge_id = ?
                """,
                (knowledge_id,),
            ).fetchone()
        if row is None:
            return
        self.mark_session_memory_dirty(
            session_id_hash=str(row["session_id_hash"] or ""),
            provider=str(row["provider"] or item.get("provider") or ""),
            project=str(row["project"] or item.get("project") or ""),
            reason="new_chunk_indexed",
            source_knowledge_id=knowledge_id,
        )

    def _maybe_mark_project_memory_dirty_for_indexed_item(self, knowledge_id: str) -> None:
        item = self.get_by_knowledge_id(knowledge_id)
        if item is None or item.get("type") != "conversation_chunk":
            return
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT provider, project
                FROM transcript_chunks
                WHERE knowledge_id = ?
                """,
                (knowledge_id,),
            ).fetchone()
        if row is None:
            return
        self.mark_project_memory_dirty(
            provider=str(row["provider"] or item.get("provider") or ""),
            project=str(row["project"] or item.get("project") or ""),
            reason="new_chunk_indexed",
            source_knowledge_id=knowledge_id,
        )

    def get_by_content_hash(self, content_hash: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_items WHERE content_hash = ?", (content_hash,)).fetchone()
        return dict(row) if row else None

    def _update_transcript_knowledge_item(self, knowledge_id: str, chunk) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE knowledge_items
                SET provider = ?,
                    project = ?,
                    domain = 'agent_memory',
                    type = 'conversation_chunk',
                    title = ?,
                    summary = ?,
                    privacy_level = 'private',
                    redaction_version = ?,
                    status = 'prepared',
                    ragflow_dataset_id = '',
                    ragflow_document_id = '',
                    ingress_target_profile = '',
                    ingress_job_id = '',
                    queued_at = '',
                    ragflow_run = '',
                    ragflow_progress = 0,
                    indexed_at = '',
                    disabled_at = '',
                    authorization_status = 'active'
                WHERE knowledge_id = ?
                """,
                (
                    chunk.provider,
                    chunk.project,
                    chunk.title(),
                    chunk.summary(),
                    chunk.redaction_version,
                    knowledge_id,
                ),
            )

    def authorize_document(self, document_id: str, *, filters: dict | None = None, include_private: bool = False) -> dict | None:
        """Resolve a locally readable indexed document.

        The historical method name is kept for API compatibility. Local read
        access is full-trust for privacy, but authorization and
        lifecycle/data-quality states still apply. Missing, non-indexed,
        authorization-disabled, disabled, superseded, expired,
        disabled-dataset, or provenance-invalid records remain hidden from
        retrieval.
        """
        filters = filters or {}
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_items WHERE ragflow_document_id = ?",
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        if item["status"] != "indexed":
            return None
        if item.get("authorization_status") != "active":
            return None
        if item["disabled_at"]:
            return None
        if item["supersedes"]:
            return None
        if _is_expired(item.get("valid_until", "")):
            return None
        if not self._dataset_is_enabled(item.get("ragflow_dataset_id", "")):
            return None
        if item.get("type") == "session_summary":
            return None
        if item.get("type") == "session_memory":
            if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
                return None
            if not _session_memory_coverage_is_complete(item):
                return None
            if not self._session_memory_coverage_edges_are_complete(item):
                return None
            session_id_hash = item.get("session_id_hash", "")
            if not session_id_hash:
                return None
            active = self.get_session_memory_active_snapshot(session_id_hash)
            if not active:
                return None
            if active.get("active_knowledge_id") != item.get("knowledge_id"):
                return None
        if item.get("type") == "session_memory_sot":  # legacy compatibility
            if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
                return None
            if not _session_memory_coverage_is_complete(item):
                return None
            if not self._session_memory_coverage_edges_are_complete(item):
                return None
            session_id_hash = item.get("session_id_hash", "")
            if not session_id_hash:
                return None
            active = self.get_session_memory_sot_active_snapshot(session_id_hash)
            if not active:
                return None
            if active.get("active_sot_knowledge_id") != item.get("knowledge_id"):
                return None
        if item.get("type") == "project_context_snapshot":
            if item.get("evidence_status") != SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS:
                return None
            provider = item.get("provider", "")
            project = item.get("project", "")
            if not provider or not project:
                return None
            active = self.get_project_memory_active_snapshot(provider=provider, project=project)
            if not active:
                return None
            if active.get("active_knowledge_id") != item.get("knowledge_id"):
                return None
        for key in ("project", "provider", "domain", "type", "session_id_hash"):
            if filters.get(key) and item[key] != filters[key]:
                return None
        return item

    def _dataset_is_enabled(self, dataset_id: str) -> bool:
        if not dataset_id:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT enabled, disabled_at FROM ragflow_datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            return True
        return bool(row["enabled"]) and not row["disabled_at"]

    def get_conversation_chunk_by_document(self, document_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT tc.*
                FROM transcript_chunks tc
                JOIN knowledge_items ki ON ki.knowledge_id = tc.knowledge_id
                WHERE ki.ragflow_document_id = ?
                """,
                (document_id,),
            ).fetchone()
        return dict(row) if row else None

    def lifecycle_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM knowledge_items GROUP BY status ORDER BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def total_items(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM knowledge_items").fetchone()
        return int(row["count"])


def _is_expired(valid_until: str) -> bool:
    if not valid_until:
        return False
    try:
        expires_at = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def _is_sha256_hash(value: str | None) -> bool:
    if not value:
        return False
    value = str(value)
    if not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _session_memory_coverage_edge_manifest_hash(pairs: list[tuple[str, str]]) -> str:
    material = "\n".join("|".join(pair) for pair in sorted(pairs))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_memory_sot_coverage_edge_manifest_hash(pairs: list[tuple[str, str]]) -> str:
    """Deprecated compatibility wrapper for the retired session_memory_sot name."""
    return _session_memory_coverage_edge_manifest_hash(pairs)


def _session_memory_coverage_is_complete(item: dict) -> bool:
    coverage_status = str(item.get("coverage_status") or "")
    try:
        coverage_gap_count = int(item.get("coverage_gap_count") or 0)
        coverage_duplicate_count = int(item.get("coverage_duplicate_count") or 0)
    except (TypeError, ValueError):
        return False
    if coverage_gap_count or coverage_duplicate_count:
        return False
    return coverage_status in ("", "complete")


def _session_memory_sot_coverage_is_complete(item: dict) -> bool:
    """Deprecated compatibility wrapper for the retired session_memory_sot name."""
    return _session_memory_coverage_is_complete(item)


def _project_key_hash(provider: str, project: str) -> str:
    return "sha256:" + hashlib.sha256(f"{provider}|{project}".encode("utf-8")).hexdigest()


def _table_exists(connection, table: str) -> bool:
    if getattr(connection, "dialect", "sqlite") == "postgres":
        row = connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    return row is not None


def _normalize_metadata_json(metadata: dict | None) -> str:
    if metadata is None:
        return "{}"
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object")
    normalized = {}
    for key, value in metadata.items():
        if isinstance(value, (dict, list, tuple, set)):
            raise ValueError("metadata values must be scalar")
        normalized[str(key)] = "" if value is None else str(value)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_metadata_json(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(item) for key, item in parsed.items()}


def _queued_document_projection(row: dict, transcript_chunk: dict | None = None) -> dict:
    metadata = _load_metadata_json(row.get("metadata_json", "{}"))
    if transcript_chunk:
        metadata.setdefault("chunk_id", str(transcript_chunk.get("chunk_id") or ""))
        metadata.setdefault("session_id_hash", str(transcript_chunk.get("session_id_hash") or ""))
        metadata.setdefault("provider", str(transcript_chunk.get("provider") or row.get("provider") or ""))
        metadata.setdefault("project", str(transcript_chunk.get("project") or row.get("project") or ""))
    metadata.setdefault("knowledge_id", str(row.get("knowledge_id") or ""))
    metadata.setdefault("provider", str(row.get("provider") or ""))
    metadata.setdefault("project", str(row.get("project") or ""))
    metadata.setdefault("session_id_hash", str(row.get("session_id_hash") or ""))
    return {
        "knowledge_id": str(row["knowledge_id"]),
        "document_type": str(row["type"]),
        "provider": str(row["provider"]),
        "project": str(row["project"]),
        "content_hash": str(row["content_hash"]),
        "target_profile": str(row["ingress_target_profile"]),
        "ingress_job_id": str(row["ingress_job_id"]),
        "updated_at": str(row.get("updated_at") or ""),
        "session_id_hash": str(row.get("session_id_hash") or metadata.get("session_id_hash") or ""),
        "metadata": metadata,
    }


def _ensure_column(connection, table: str, column: str, declaration: str) -> None:
    if getattr(connection, "dialect", "sqlite") == "postgres":
        rows = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        ).fetchall()
        existing = {row["column_name"] for row in rows}
    else:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
    if column in existing:
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _memory_candidate_from_row(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["requires_manual_approval"] = bool(record["requires_manual_approval"])
    record["evidence_refs"] = json.loads(record.pop("evidence_refs_json") or "[]")
    return record


def _transcript_validation_file_from_row(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["validation_document_ids"] = json.loads(record.pop("validation_document_ids_json") or "[]")
    record["validation_knowledge_ids"] = json.loads(record.pop("validation_knowledge_ids_json") or "[]")
    return record


def _eval_query_from_row(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["query_terms"] = json.loads(record.pop("query_terms_json") or "[]")
    record["expected_memory_ids"] = json.loads(record.pop("expected_memory_ids_json") or "[]")
    record["enabled"] = bool(record["enabled"])
    record["k"] = int(record["k"])
    record["min_recall"] = float(record["min_recall"])
    record["min_precision"] = float(record["min_precision"])
    return record


def _persisted_context_summary(item: dict) -> str:
    if item.get("kind") == "conversation_chunk":
        return "[bounded-fallback-not-persisted]"
    if item.get("kind") == "memory_card":
        return "[approved-memory-summary-not-persisted]"
    return str(item.get("summary") or "")

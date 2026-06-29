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
from .ledger_base import *  # noqa: F401,F403


def upsert_llm_brain_memory_card_on(connection, card: dict) -> dict:
    """주입된 connection 으로 llm_brain MemoryCard 를 upsert 한다(commit/connect 하지 않음).

    public Ledger.upsert_llm_brain_memory_card 와 _LedgerTransaction 이 같은 로직을 공유해
    여러 write 를 단일 트랜잭션으로 묶을 수 있게 한다. read-back 도 같은 connection 에서 한다
    (트랜잭션 미커밋 상태의 값을 일관되게 반환)."""

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
    row = connection.execute(
        "SELECT envelope_json FROM llm_brain_memory_cards WHERE memory_id = ?",
        (validated["memory_id"],),
    ).fetchone()
    return json.loads(row["envelope_json"])


def upsert_llm_brain_feedback_record_on(connection, record: dict) -> dict:
    """주입된 connection 으로 llm_brain feedback record 를 upsert 한다(commit/connect 하지 않음)."""

    from .session_memory.memory_card import validate_feedback_record

    validated = validate_feedback_record(record)
    created_at = str(validated.get("timestamp") or datetime.now(timezone.utc).isoformat())
    record_json = json.dumps(validated, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
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
    row = connection.execute(
        "SELECT record_json FROM llm_brain_feedback_records WHERE feedback_id = ?",
        (validated["feedback_id"],),
    ).fetchone()
    return json.loads(row["record_json"])


class NativeMemoryMixin:
    """Native Memory & Memory Cards Synchronization — ledger.py god-class에서 분할(behavior-preserving).

    Ledger가 다중상속으로 합성하므로 self 는 Ledger 인스턴스. 호출부 변경 없음."""

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
        with self._connect() as connection:
            return upsert_llm_brain_memory_card_on(connection, card)
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
    def list_llm_brain_review_queue(
        self,
        *,
        project: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """검토 대기(pending) MemoryCard proposal 만 반환한다.

        accepted/rejected lane 은 제외하고, 사람이 봐야 할 candidate / suggested_accept /
        needs_review lifecycle 만 노출한다. lifecycle 집합은 모델 계층의 단일 정의
        REVIEW_LIFECYCLE_STATES 를 따른다. 읽기 전용이며 어떤 상태도 바꾸지 않는다.
        """

        from .session_memory.memory_card import REVIEW_LIFECYCLE_STATES

        review_states = sorted(REVIEW_LIFECYCLE_STATES)
        placeholders = ", ".join("?" for _ in review_states)
        filters = [f"lifecycle_state IN ({placeholders})"]
        values: list[object] = list(review_states)
        if project:
            filters.append("project = ?")
            values.append(project)
        where = "WHERE " + " AND ".join(filters)
        values.append(max(int(limit), 1))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT envelope_json FROM llm_brain_memory_cards
                {where}
                ORDER BY updated_at DESC, memory_id
                LIMIT ?
                """,
                values,
            ).fetchall()
        return [json.loads(row["envelope_json"]) for row in rows]
    def upsert_llm_brain_feedback_record(self, record: dict) -> dict:
        with self._connect() as connection:
            return upsert_llm_brain_feedback_record_on(connection, record)
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

    def upsert_qdrant_collection(
        self,
        *,
        logical_name: str,
        collection: str,
        embedding_model: str = "",
        vector_size: int = 0,
        distance: str = "Cosine",
        payload_index_version: str = "",
    ) -> dict:
        """Register/refresh a logical_name -> Qdrant collection mapping.

        Parallel to ``upsert_ragflow_dataset_plan`` for the searchable mirror. This
        records the INTENDED collection mapping + vector params; it performs no
        network call and touches no live Qdrant collection. A refresh updates only
        metadata and never re-enables a disabled row (use
        ``disable_qdrant_collection`` / a future enable verb for state changes).

        NOTE (Stage 1): the registry records intended enable state. Read/write
        ENFORCEMENT (consulting ``_qdrant_collection_is_enabled`` from the adapter)
        is wired at the M8 read-cutover; it is not load-bearing yet.
        """

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO qdrant_collections (
                    logical_name, collection, embedding_model, vector_size,
                    distance, payload_index_version, created_at, enabled, disabled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, '')
                ON CONFLICT(logical_name) DO UPDATE SET
                    collection=excluded.collection,
                    embedding_model=excluded.embedding_model,
                    vector_size=excluded.vector_size,
                    distance=excluded.distance,
                    payload_index_version=excluded.payload_index_version
                """,
                (
                    logical_name,
                    collection,
                    embedding_model,
                    int(vector_size),
                    distance,
                    payload_index_version,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM qdrant_collections WHERE logical_name = ?",
                (logical_name,),
            ).fetchone()
        return dict(row)

    def get_qdrant_collection(self, logical_name: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM qdrant_collections WHERE logical_name = ?",
                (logical_name,),
            ).fetchone()
        return dict(row) if row else None

    def list_qdrant_collections(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM qdrant_collections ORDER BY logical_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def disable_qdrant_collection(self, logical_name: str) -> dict | None:
        """Disable a registry mapping (enable transition is an explicit op).

        ``upsert_qdrant_collection`` only refreshes metadata and never re-enables a
        disabled row, so disable/enable are deliberate verbs rather than a side
        effect of an upsert. Returns the updated row (or None if absent).
        """

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                "UPDATE qdrant_collections SET enabled = 0, disabled_at = ? WHERE logical_name = ?",
                (now, logical_name),
            )
            row = connection.execute(
                "SELECT * FROM qdrant_collections WHERE logical_name = ?",
                (logical_name,),
            ).fetchone()
        return dict(row) if row else None

    def _qdrant_collection_is_enabled(self, collection: str) -> bool:
        # Fail-closed: a collection absent from the registry is NOT treated as
        # enabled (unlike ragflow_datasets which fails open). The mirror must be
        # explicitly registered before any live use.
        if not collection:
            return False
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT enabled, disabled_at FROM qdrant_collections WHERE collection = ?",
                (collection,),
            ).fetchall()
        if not rows:
            return False
        # ``collection`` is not UNIQUE (single physical collection can back several
        # logical_names). Fail-closed: enabled only if EVERY mapping is enabled.
        return all(bool(row["enabled"]) and not row["disabled_at"] for row in rows)
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

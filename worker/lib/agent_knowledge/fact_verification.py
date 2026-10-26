"""관측 고정본과 별개인 서버 현재 검증 영수증·기한 재검토.

만료는 값이 틀렸다는 증거가 아니다. 새 검증을 예약하고 검증 상태만 표시한다.
"""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg.errors import DeadlockDetected, LockNotAvailable, SerializationFailure

from .public_safe_util import hash_payload


class FactVerificationService:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def record_on(cur, *, scope, resolution_id, assertion_id, observation, commit):
        """실제 현재 검증이 통과한 새 terminal resolution 안에서만 호출한다."""
        max_age = getattr(commit, "max_age_seconds", 300)
        policy_id = getattr(commit, "policy_id", "internal-admission")
        if type(max_age) is not int or not 1 <= max_age <= 86400:
            raise ValueError("bounded current verification lifetime required")
        policy_version = observation.proof.get("policy_version")
        cur.execute("""INSERT INTO lbrain_fact_verifications
            (resolution_id,fact_key,assertion_id,policy_id,policy_version,max_age_seconds)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING checked_at,expires_at""",
            (resolution_id, scope.key, assertion_id, policy_id, policy_version, max_age))
        receipt = cur.fetchone()
        # 설정된 기존 fact만 자동 확인한다. 코어 callback 대역이나 미등록
        # source를 임의의 서버 정책으로 승격하지 않는다.
        cur.execute("""INSERT INTO lbrain_fact_check_schedules
            (fact_key,project,policy_id,verified_resolution_id,next_check_at)
            SELECT %s,%s,%s,%s,%s FROM lbrain_fact_admission_rules
            WHERE project=%s AND policy_id=%s AND fact_key=%s AND policy_version=%s
            ON CONFLICT(fact_key,policy_id) DO UPDATE SET
                verified_resolution_id=excluded.verified_resolution_id,next_check_at=excluded.next_check_at,
                state='scheduled',task_id=NULL,reason_code='',
                generation=lbrain_fact_check_schedules.generation+1""",
            (scope.key, scope.project, policy_id, resolution_id, receipt["expires_at"],
                scope.project, policy_id, scope.key, policy_version))
        return {"checked_at": receipt["checked_at"].isoformat(), "expires_at": receipt["expires_at"].isoformat()}

    def dispatch_once(self, *, project, batch_size=20, retry_seconds=60):
        if type(batch_size) is not int or not 1 <= batch_size <= 100 or not .1 <= retry_seconds <= 86400:
            raise ValueError("bounded verification dispatch required")
        try:
            return self._dispatch(project, batch_size, retry_seconds)
        except (DeadlockDetected, LockNotAvailable, SerializationFailure):
            return {"status": "waiting", "reason_code": "verification_busy"}

    def _dispatch(self, project, batch_size, retry_seconds):
        from .postgres_store.pgvector_store import PgVectorStore
        from .recheck_scheduler import PgRecheckQueue
        scheduled = blocked = 0
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self.store._lock_steward_authority_on(db)
            # upgrade 이전의 head에 새 확인 시각을 지어내지 않는다. 확인이
            # 필요한 기존 fact만 bounded 초기 작업으로 예약한다.
            cur.execute("""INSERT INTO lbrain_fact_check_schedules(fact_key,project,policy_id)
                SELECT f.fact_key,r.project,r.policy_id FROM lbrain_fact_admission_rules r
                JOIN lbrain_fact_scopes f ON f.fact_key=r.fact_key AND f.project=r.project
                WHERE r.project=%s AND NOT EXISTS (SELECT 1 FROM lbrain_fact_check_schedules s
                    WHERE s.fact_key=f.fact_key AND s.policy_id=r.policy_id)
                ORDER BY f.fact_key,r.policy_id LIMIT %s ON CONFLICT DO NOTHING""", (project, batch_size))
            # source가 바뀌는 순간에 끝난 history-only 작업도 다음 확인을 잃지 않는다.
            cur.execute("""WITH completed AS (SELECT s.fact_key,s.policy_id
                FROM lbrain_fact_check_schedules s JOIN lbrain_recheck_tasks t ON t.task_id=s.task_id
                WHERE s.project=%s AND s.state='queued' AND t.state='resolved'
                ORDER BY s.fact_key,s.policy_id LIMIT %s)
                UPDATE lbrain_fact_check_schedules s SET state='scheduled',task_id=NULL,
                    generation=generation+1,next_check_at=clock_timestamp()+(%s*interval '1 second'),
                    reason_code='current_check_not_renewed'
                FROM completed c WHERE s.fact_key=c.fact_key AND s.policy_id=c.policy_id""",
                (project, batch_size, retry_seconds))
            cur.execute("""SELECT * FROM lbrain_fact_check_schedules
                WHERE project=%s AND state IN ('scheduled','blocked') AND next_check_at<=clock_timestamp()
                ORDER BY next_check_at,fact_key,policy_id FOR UPDATE SKIP LOCKED LIMIT %s""", (project, batch_size))
            due = cur.fetchall()
            queue = PgRecheckQueue(PgVectorStore(connection=db))
            for item in due:
                cur.execute("""SELECT r.*,f.memory_id,f.value FROM lbrain_fact_admission_rules r
                    JOIN lbrain_fact_scopes f ON f.fact_key=r.fact_key AND f.project=r.project
                    WHERE r.project=%s AND r.policy_id=%s AND r.fact_key=%s""",
                    (project, item["policy_id"], item["fact_key"]))
                rule = cur.fetchone()
                if not rule or not rule["active"]:
                    cur.execute("""UPDATE lbrain_fact_check_schedules SET state='blocked',reason_code='policy_unconfigured',
                        next_check_at=clock_timestamp()+(%s*interval '1 second') WHERE fact_key=%s AND policy_id=%s""",
                        (retry_seconds, item["fact_key"], item["policy_id"]))
                    blocked += 1
                    continue
                request = {"schema_version": "fact_source_job.v1", "policy_id": rule["policy_id"],
                    "fact_key": rule["fact_key"], "policy_version": rule["policy_version"], "lane": "intake",
                    "initial_memory_id": rule["memory_id"], "change": {"proposed_value": rule["value"],
                        "evidence": {"mode": "server_current"}},
                    "trigger": {"kind": "verification_due", "verified_resolution_id": item["verified_resolution_id"]}}
                task = queue.register(project=project, memory_id=rule["memory_id"], request=request,
                    request_key=hash_payload(["verification_due", rule["fact_key"], rule["policy_id"],
                        item["verified_resolution_id"], item["generation"]]))
                cur.execute("""UPDATE lbrain_fact_check_schedules SET state='queued',task_id=%s,
                    reason_code='verification_queued' WHERE fact_key=%s AND policy_id=%s""",
                    (task["task_id"], item["fact_key"], item["policy_id"]))
                scheduled += 1
        return {"status": "processed", "scheduled": scheduled, "blocked": blocked}

    @staticmethod
    def health_on(db, *, project, overdue_grace):
        with db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE state IN ('scheduled','blocked')
                    AND next_check_at<=clock_timestamp()-(%s*interval '1 second')) AS overdue,
                count(*) FILTER (WHERE state='blocked') AS blocked
                FROM lbrain_fact_check_schedules WHERE project=%s""", (overdue_grace, project))
            row = cur.fetchone()
            cur.execute("""SELECT count(*) AS n FROM lbrain_fact_admission_rules r
                JOIN lbrain_fact_scopes f ON f.fact_key=r.fact_key AND f.project=r.project
                WHERE r.project=%s AND NOT EXISTS (SELECT 1 FROM lbrain_fact_check_schedules s
                    WHERE s.fact_key=r.fact_key AND s.policy_id=r.policy_id)""", (project,))
            unscheduled = cur.fetchone()["n"]
        return {"overdue": row["overdue"], "blocked": row["blocked"], "unscheduled": unscheduled,
            "attention_required": bool(row["overdue"] or row["blocked"] or unscheduled)}

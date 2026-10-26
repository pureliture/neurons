"""근거 철회→내구성 영향 계획→공통 재검토의 server-side 연결.

원문/관측/과거 판단을 덮어쓰거나 옛 값을 자동 복원하지 않는다.
모델 판단은 기존 coordinator가 authority transaction 밖에서 수행한다.
"""
from __future__ import annotations

import json
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.errors import DeadlockDetected, LockNotAvailable, SerializationFailure

from .fact_resolution import FactResolutionError
from .public_safe_util import ensure_public_safe, hash_payload, require_sha256
from .recheck_scheduler import PgRecheckQueue


class FactSourceLifecycle:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _bound(db):
        db.execute("""SELECT set_config('lock_timeout',
            (SELECT (CASE WHEN setting::bigint=0 THEN 1000 ELSE LEAST(setting::bigint,1000) END)::text||'ms'
                FROM pg_settings WHERE name='lock_timeout'),true),
            set_config('statement_timeout',
            (SELECT (CASE WHEN setting::bigint=0 THEN 5000 ELSE LEAST(setting::bigint,5000) END)::text||'ms'
                FROM pg_settings WHERE name='statement_timeout'),true)""")

    def revoke(self, *, project, source_ref_id, expected_content_hash, request_key):
        """provider/admin의 내부 명령. public MCP 교정 입력에는 노출하지 않는다."""
        require_sha256(expected_content_hash, "expected source")
        for value, limit in ((project, 64), (source_ref_id, 180), (request_key, 180)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError("bounded source revocation identity required")
        request = {"project": project, "source_ref_id": source_ref_id, "expected_content_hash": expected_content_hash}
        ensure_public_safe(request, "SourceRevocation")
        command_id, request_hash = hash_payload([project, request_key]), hash_payload(request)
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self._bound(db)
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (command_id,))
            cur.execute("SELECT * FROM lbrain_fact_source_commands WHERE command_id=%s", (command_id,))
            previous = cur.fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise FactResolutionError("source_command_key_conflict")
                return {**previous["result"], "duplicate": True}
            cur.execute("SELECT * FROM lbrain_fact_source_receipts WHERE source_ref_id=%s FOR UPDATE", (source_ref_id,))
            receipt = cur.fetchone()
            if not receipt or receipt["content_hash"] != expected_content_hash:
                raise FactResolutionError("source_revision_conflict")
            if receipt["owner_project"] is None:
                cur.execute("SELECT DISTINCT project FROM lbrain_fact_admission_rules WHERE policy_version=%s",
                    (receipt["proof"].get("policy_version"),))
                owners = [row["project"] for row in cur.fetchall()]
                if owners != [project]:
                    raise FactResolutionError("source_project_mismatch")
            elif receipt["owner_project"] != project:
                raise FactResolutionError("source_project_mismatch")
            cur.execute("UPDATE lbrain_fact_source_receipts SET owner_project=%s,state='revoked' WHERE source_ref_id=%s",
                (project, source_ref_id))
            cur.execute("SELECT event_id FROM lbrain_fact_source_events WHERE source_ref_id=%s", (source_ref_id,))
            event = cur.fetchone()
            result = {"event_id": str(event["event_id"]), "duplicate": False, "recheck_scheduled": True}
            cur.execute("INSERT INTO lbrain_fact_source_commands(command_id,request_hash,result) VALUES (%s,%s,%s)",
                (command_id, request_hash, Jsonb(result)))
            return result

    def dispatch_once(self, *, project, batch_size=20, retry_seconds=60):
        if type(batch_size) is not int or not 1 <= batch_size <= 100 or not .1 <= retry_seconds <= 86400:
            raise ValueError("bounded lifecycle dispatch required")
        try:
            return self._dispatch(project, batch_size, retry_seconds)
        except (DeadlockDetected, LockNotAvailable, SerializationFailure):
            return {"status": "waiting", "reason_code": "lifecycle_busy"}

    def _dispatch(self, project, batch_size, retry_seconds):
        from .postgres_store.pgvector_store import PgVectorStore
        expanded = scheduled = blocked = 0
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self._bound(db)
            self.store._lock_steward_authority_on(db)
            cur.execute("""SELECT * FROM lbrain_fact_source_events WHERE project=%s AND NOT expansion_done
                ORDER BY created_at,event_id FOR UPDATE SKIP LOCKED LIMIT 1""", (project,))
            event = cur.fetchone()
            if event:
                cur.execute("""SELECT DISTINCT a.fact_key,COALESCE(r.policy_id,'') AS policy_id
                    FROM lbrain_fact_assertions a
                    JOIN lbrain_fact_observation_sources s ON s.observation_version=a.observation_version
                    JOIN lbrain_fact_scopes f ON f.fact_key=a.fact_key
                    LEFT JOIN lbrain_fact_admission_rules r ON r.fact_key=f.fact_key
                        AND r.project=f.project AND r.field_path=f.field_path
                    WHERE s.source_ref_id=%s AND (a.fact_key,COALESCE(r.policy_id,''))>(%s,%s)
                    ORDER BY a.fact_key,policy_id LIMIT %s""",
                    (event["source_ref_id"], event["last_fact_key"], event["last_policy_id"], batch_size+1))
                rows = cur.fetchall()
                for row in rows[:batch_size]:
                    cur.execute("""INSERT INTO lbrain_fact_source_targets(event_id,fact_key,policy_id)
                        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""", (event["event_id"], row["fact_key"], row["policy_id"]))
                    expanded += 1
                last = rows[min(len(rows), batch_size)-1] if rows else None
                cur.execute("""UPDATE lbrain_fact_source_events SET expansion_done=%s,
                    last_fact_key=%s,last_policy_id=%s WHERE event_id=%s""",
                    (len(rows)<=batch_size, last["fact_key"] if last else event["last_fact_key"],
                        last["policy_id"] if last else event["last_policy_id"], event["event_id"]))
            cur.execute("""SELECT t.*,e.source_ref_id FROM lbrain_fact_source_targets t
                JOIN lbrain_fact_source_events e ON e.event_id=t.event_id
                WHERE e.project=%s AND t.state IN ('pending','blocked') AND t.next_check_at<=clock_timestamp()
                ORDER BY t.next_check_at,t.event_id,t.fact_key,t.policy_id
                FOR UPDATE OF t SKIP LOCKED LIMIT %s""", (project, batch_size))
            targets = cur.fetchall()
            queue = PgRecheckQueue(PgVectorStore(connection=db))
            for target in targets:
                cur.execute("""UPDATE lbrain_fact_assertions a SET state='revoked'
                    FROM lbrain_fact_observation_sources s WHERE s.observation_version=a.observation_version
                        AND s.source_ref_id=%s AND a.fact_key=%s AND a.state IN ('support','pending')""",
                    (target["source_ref_id"], target["fact_key"]))
                head = self._refresh_head(cur, target["fact_key"])
                if head.get("retained_support") and head["state"] != "disputed":
                    cur.execute("""UPDATE lbrain_fact_source_targets SET state='covered',
                        reason_code='current_support_retained',attempt_count=attempt_count+1
                        WHERE event_id=%s AND fact_key=%s AND policy_id=%s""",
                        (target["event_id"], target["fact_key"], target["policy_id"]))
                    continue
                cur.execute("""SELECT * FROM lbrain_fact_admission_rules WHERE project=%s AND fact_key=%s
                    AND field_path=%s AND (%s='' OR policy_id=%s) AND policy_id>%s ORDER BY policy_id LIMIT %s""",
                    (project, target["fact_key"], head["field_path"], target["policy_id"], target["policy_id"],
                        target["last_policy_id"] if target["policy_id"] == '' else '', batch_size+1))
                rules = cur.fetchall()
                if not rules:
                    cur.execute("""UPDATE lbrain_fact_source_targets SET state='blocked',reason_code='policy_unconfigured',
                        next_check_at=clock_timestamp()+(%s*interval '1 second'),attempt_count=attempt_count+1
                        WHERE event_id=%s AND fact_key=%s AND policy_id=%s""",
                        (retry_seconds, target["event_id"], target["fact_key"], target["policy_id"]))
                    blocked += 1
                    continue
                if target["policy_id"] == '':
                    # 정책 부재로 보류된 대상도 나중에 등록되면 자동으로 재개한다.
                    for rule in rules[:batch_size]:
                        cur.execute("""INSERT INTO lbrain_fact_source_targets(event_id,fact_key,policy_id)
                            VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (target["event_id"], target["fact_key"], rule["policy_id"]))
                    cur.execute("""UPDATE lbrain_fact_source_targets SET state=%s,reason_code='policy_discovered',last_policy_id=%s
                        WHERE event_id=%s AND fact_key=%s AND policy_id=''""",
                        ('expanded' if len(rules)<=batch_size else 'pending', rules[min(len(rules), batch_size)-1]["policy_id"],
                            target["event_id"], target["fact_key"]))
                    continue
                rule = rules[0]
                request = {"schema_version": "fact_source_job.v1", "policy_id": rule["policy_id"],
                    "fact_key": head["fact_key"], "policy_version": rule["policy_version"], "lane": "intake",
                    "initial_memory_id": head["memory_id"], "change": {"proposed_value": head["value"],
                        "evidence": {"mode": "server_current"}},
                    "trigger": {"kind": "source_revocation", "event_id": str(target["event_id"])}}
                task = queue.register(project=project, memory_id=head["memory_id"], request=request,
                    request_key=hash_payload([str(target["event_id"]), head["fact_key"], rule["policy_id"], rule["policy_version"]]))
                cur.execute("""UPDATE lbrain_fact_source_targets SET state='queued',task_id=%s,
                    reason_code='source_recheck_queued',attempt_count=attempt_count+1
                    WHERE event_id=%s AND fact_key=%s AND policy_id=%s""",
                    (task["task_id"], target["event_id"], target["fact_key"], target["policy_id"]))
                scheduled += 1
        return {"status": "processed", "expanded": expanded, "scheduled": scheduled, "blocked": blocked}

    @staticmethod
    def _refresh_head(cur, fact_key):
        cur.execute("SELECT * FROM lbrain_fact_scopes WHERE fact_key=%s FOR UPDATE", (fact_key,))
        head = cur.fetchone()
        if head["head_assertion_id"]:
            cur.execute("""SELECT a.assertion_id FROM lbrain_fact_assertions a
                JOIN lbrain_fact_observation_sources s ON s.observation_version=a.observation_version
                JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
                WHERE a.fact_key=%s AND a.value_hash=%s AND a.state IN ('support','pending') AND r.state='active'
                    AND (a.state='support' OR a.assertion_id=%s)
                ORDER BY a.assertion_id LIMIT 1""", (fact_key, head["value_hash"], head["head_assertion_id"]))
            remaining = cur.fetchone()
            head["retained_support"] = bool(remaining)
            if not remaining:
                cur.execute("UPDATE lbrain_fact_scopes SET state='invalidated' WHERE fact_key=%s AND state<>'invalidated' RETURNING project", (fact_key,))
                changed = cur.fetchone()
            elif head["state"] == "invalidated":
                cur.execute("UPDATE lbrain_fact_scopes SET state='current' WHERE fact_key=%s RETURNING project", (fact_key,))
                changed = cur.fetchone()
                head["state"] = "current"
            else:
                changed = None
            if changed:
                cur.execute("""INSERT INTO lbrain_fact_project_versions(project,generation) VALUES (%s,1)
                    ON CONFLICT(project) DO UPDATE SET generation=lbrain_fact_project_versions.generation+1""",
                    (changed["project"],))
        return head

    def health(self, *, project, heartbeat_grace=120, overdue_grace=120, pending_age_seconds=86400,
               native_source_key=None):
        from .recheck_scheduler import inspect_recheck_health
        from .fact_verification import FactVerificationService
        from .postgres_store.pgvector_store import PgVectorStore
        with self.store._scope() as db, db.cursor(row_factory=dict_row) as cur:
            if self.store.connection is None:
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            self._bound(db)
            cur.execute("SELECT count(*) AS n FROM lbrain_fact_source_events WHERE project=%s AND NOT expansion_done", (project,))
            events = cur.fetchone()["n"]
            cur.execute("""SELECT count(*) AS n FROM lbrain_fact_source_targets t
                JOIN lbrain_fact_source_events e ON e.event_id=t.event_id
                WHERE e.project=%s AND t.state IN ('pending','blocked')""", (project,))
            targets = cur.fetchone()["n"]
            reviews = inspect_recheck_health(PgRecheckQueue(PgVectorStore(connection=db)), project=project,
                heartbeat_grace=heartbeat_grace, overdue_grace=overdue_grace, pending_age_seconds=pending_age_seconds)
            verifications = FactVerificationService.health_on(db, project=project, overdue_grace=overdue_grace)
            cur.execute("""SELECT count(*) AS configured,
                count(*) FILTER (WHERE checked_at IS NULL OR reason_code<>'' OR
                    checked_at<clock_timestamp()-(%s * interval '1 second')) AS unhealthy
                FROM lbrain_native_source_cursors WHERE project=%s
                    AND (%s::text IS NULL OR source_key=%s)""",
                (heartbeat_grace,project,native_source_key,native_source_key))
            native_sources=cur.fetchone()
            native_sources['missing_required']=bool(native_source_key and not native_sources['configured'])
        return {"pending_events": events, "pending_targets": targets, "reviews": reviews,
            "verifications": verifications,"native_sources":native_sources,
            "attention_required": bool(events or targets or reviews["attention_required"] or verifications["attention_required"]
                or native_sources["unhealthy"] or native_sources['missing_required'])}


def main(argv=None):
    import argparse
    import os
    import time
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from .fact_correction_coordinator import FactCorrectionCoordinator, load_configuration
    from .fact_verification import FactVerificationService
    from .postgres_store.pgvector_store import PgVectorStore
    parser = argparse.ArgumentParser(description="근거 철회·영향 재검토 worker와 독립 감시")
    parser.add_argument("mode", choices=("revoke", "worker", "monitor"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--dsn-env", default="NEURON_LBRAIN_PGVECTOR_DSN")
    parser.add_argument("--source-ref")
    parser.add_argument("--expected-hash")
    parser.add_argument("--request-key")
    parser.add_argument("--config")
    parser.add_argument("--native-source-policy-file",
        help="지연 native 관찰 참조를 자동 수집할 때 사용하는 기존 Couch source 정책")
    parser.add_argument("--worker-id", default="fact-source-lifecycle")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--retry-seconds", type=float, default=60)
    parser.add_argument("--lease-seconds", type=float)
    parser.add_argument("--heartbeat-grace", type=float, default=120)
    parser.add_argument("--overdue-grace", type=float, default=120)
    parser.add_argument("--pending-age-seconds", type=float, default=86400,
        help="미해결 작업의 누적 경과 경고 기준(초). 재시도·heartbeat로 초기화하지 않는다")
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= 100000 or not .01 <= args.poll_seconds <= 60:
        parser.error("bounded worker run required")
    if not .1 <= args.pending_age_seconds <= 86400:
        parser.error("pending age must be between 0.1 and 86400 seconds")
    try:
        dsn = os.environ.get(args.dsn_env, "")
        if not dsn:
            raise ValueError("explicit lifecycle DSN required")
        connection = conninfo_to_dict(dsn)
        deadline = int(connection.get("connect_timeout", "5"))
        store = PgVectorStore(dsn=make_conninfo(dsn, connect_timeout=5 if deadline<=0 else min(deadline,5)))
        service = FactSourceLifecycle(store)
        coordinator = None
        native_bridge = None
        catalog = None
        native_source_key = None
        if args.mode in {'worker','monitor'} and args.native_source_policy_file:
            from .session_memory.candidate_intake_cli import _build_observation_catalog
            catalog=_build_observation_catalog(args.native_source_policy_file)
            native_source_key=hash_payload([catalog.store.base_url,catalog.store.db])
        if args.mode == "worker":
            reader, rules = load_configuration(args.config)
            coordinator = FactCorrectionCoordinator(store=store, git_reader=reader, policies=rules,native_catalog=catalog)
            if catalog is not None:
                from .source_observation import NativeSourceObservationBridge
                native_bridge=NativeSourceObservationBridge(coordinator,catalog)
            review_worker = coordinator.worker(project=args.project, worker_id=args.worker_id,
                retry_seconds=args.retry_seconds)
            if args.lease_seconds is not None:
                if not .1 <= args.lease_seconds <= 3600:
                    raise ValueError("bounded lifecycle lease required")
                review_worker.lease_seconds = args.lease_seconds
        for index in range(args.iterations):
            if args.mode == "revoke":
                result = service.revoke(project=args.project, source_ref_id=args.source_ref,
                    expected_content_hash=args.expected_hash, request_key=args.request_key)
            elif args.mode == "monitor":
                result = service.health(project=args.project, heartbeat_grace=args.heartbeat_grace,
                    overdue_grace=args.overdue_grace, pending_age_seconds=args.pending_age_seconds,
                    native_source_key=native_source_key)
            else:
                coordinator.queue.heartbeat(project=args.project,worker_id=args.worker_id)
                result = {"dispatch": service.dispatch_once(project=args.project, retry_seconds=args.retry_seconds),
                    "verification": FactVerificationService(store).dispatch_once(project=args.project, retry_seconds=args.retry_seconds),
                    "native_discovery":native_bridge.dispatch_once(project=args.project) if native_bridge else {"status":"unconfigured"},
                    "review": review_worker.run_once()}
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if index+1 < args.iterations:
                time.sleep(args.poll_seconds)
        return 0
    except Exception as exc:
        reason = exc.reason_code if isinstance(exc, FactResolutionError) else "source_lifecycle_unavailable"
        print(json.dumps({"status": "unavailable", "attention_required": True, "reason_code": reason}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""서버 정책의 명시적 CAS 공표와 원래 근거의 효력 분리.

정책 공표는 현재 값의 검증이 아니다. 원문·관측·과거 결과를 유지하고
같은 사실을 새 정책으로 읽을 내구성 계획만 등록한다.
"""
from __future__ import annotations

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.errors import DeadlockDetected, LockNotAvailable, SerializationFailure

from .fact_resolution import FactResolutionError
from .fact_source_lifecycle import FactSourceLifecycle
from .public_safe_util import ensure_public_safe, hash_payload, require_sha256


class FactPolicyLifecycle:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store

    def publish(self, *, project, policy_id, expected_policy_version, request_key, actor, active=True,
                previous_descriptor=None):
        """등록된 동일 사실의 새 정책만 공표한다. public MCP에는 노출하지 않는다."""
        require_sha256(expected_policy_version, "expected policy version")
        if previous_descriptor is not None:
            ensure_public_safe(previous_descriptor,"PreviousPolicyDescriptor")
            if hash_payload(previous_descriptor)!=expected_policy_version:
                raise FactResolutionError("policy_previous_definition_mismatch")
        for value, maximum in ((project,64),(policy_id,128),(request_key,180),(actor,128)):
            if not isinstance(value, str) or not value.strip() or len(value)>maximum:
                raise ValueError("bounded explicit policy publication identity required")
        if type(active) is not bool:
            raise ValueError("explicit policy activation state required")
        rule = self.coordinator.policies.get(policy_id)
        if rule is None or rule.scope.project != project:
            raise FactResolutionError("policy_publication_scope_mismatch")
        request = {"project":project,"policy_id":policy_id,"expected_policy_version":expected_policy_version,
            "descriptor":rule.public_identity(),"actor":actor,"active":active}
        ensure_public_safe(request, "PolicyPublication")
        command_id = hash_payload([project,request_key])
        request_hash = hash_payload(request)
        publication_id = "polpub_" + hash_payload([command_id,request_hash])[7:47]
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            FactSourceLifecycle._bound(db)
            self.store._lock_steward_authority_on(db)
            cur.execute("SELECT * FROM lbrain_fact_policy_publications WHERE command_id=%s",(command_id,))
            previous = cur.fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise FactResolutionError("policy_publication_key_conflict")
                return self._receipt(previous, duplicate=True)
            cur.execute("SELECT * FROM lbrain_fact_admission_rules WHERE project=%s AND policy_id=%s FOR UPDATE",
                (project,policy_id))
            old = cur.fetchone()
            if old is None or old["policy_version"] != expected_policy_version:
                raise FactResolutionError("policy_publication_revision_conflict")
            if (old["fact_key"] != rule.scope.key or old["target_title"] != rule.target_title
                    or old["field_path"] != rule.field_path):
                raise FactResolutionError("policy_publication_scope_mismatch")
            if rule.publication_revision != old["publication_revision"]+1 or rule.version == old["policy_version"]:
                raise FactResolutionError("policy_publication_sequence_required")
            old_descriptor = old.get("policy_descriptor")
            if previous_descriptor is not None:
                ensure_public_safe(previous_descriptor,"PreviousPolicyDescriptor")
                if hash_payload(previous_descriptor)!=old["policy_version"] or (
                        old_descriptor is not None and hash_payload(old_descriptor)!=hash_payload(previous_descriptor)):
                    raise FactResolutionError("policy_previous_definition_mismatch")
                old_descriptor = previous_descriptor
            if old_descriptor is not None and hash_payload(old_descriptor)!=old["policy_version"]:
                raise FactResolutionError("policy_previous_definition_mismatch")
            cur.execute("SELECT memory_id FROM lbrain_fact_scopes WHERE fact_key=%s AND project=%s",
                (rule.scope.key,project))
            head = cur.fetchone()
            target_id = head["memory_id"] if head else old["target_memory_id"]
            from .postgres_store.pgvector_store import PgVectorStore
            from .evidence_correction import CorrectionError, _render_fact
            card = PgVectorStore(connection=db).get_steward_card(target_id)
            if (not card or card["project"]!=project or card["card_type"]!=old["card_type"]
                    or card["authorization_status"]!="active" or card["currentness"]!="current"):
                raise FactResolutionError("policy_publication_target_unavailable")
            try:
                _render_fact(card,{"field_path":rule.field_path,"summary_template":rule.render_template},
                    card.get("typed_payload",{}).get(rule.field_path.split(".",1)[1]))
            except (CorrectionError,TypeError,ValueError):
                raise FactResolutionError("policy_publication_display_scope_changed") from None
            if active:
                from .fact_correction_coordinator import GitSemanticFactPolicy
                if isinstance(rule,GitSemanticFactPolicy):
                    from .correction_capabilities import CapabilityUnavailable, ObservedCapability
                    from .correction_semantic import load_validated_profile
                    if rule.required_capability!=ObservedCapability.REPOSITORY_CONTENT.value:
                        raise FactResolutionError("policy_source_capability_unavailable")
                    try:
                        load_validated_profile(rule.validation_profile_path,expected_hash=rule.validation_profile_hash)
                    except CapabilityUnavailable:
                        raise FactResolutionError("policy_admission_profile_unvalidated") from None
            cur.execute("""INSERT INTO lbrain_fact_policy_publications
                (publication_id,project,policy_id,publication_revision,previous_version,policy_version,
                 previous_active,active,fact_key,target_memory_id,field_path,previous_descriptor,descriptor,command_id,request_hash,actor)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (publication_id,project,policy_id,rule.publication_revision,old["policy_version"],rule.version,
                 old["active"],active,rule.scope.key,target_id,rule.field_path,
                 Jsonb(old_descriptor) if old_descriptor is not None else None,Jsonb(rule.public_identity()),
                 command_id,request_hash,actor))
            publication = cur.fetchone()
            cur.execute("""UPDATE lbrain_fact_admission_rules SET policy_version=%s,publication_revision=%s,active=%s,policy_descriptor=%s
                WHERE project=%s AND policy_id=%s AND policy_version=%s""",
                (rule.version,rule.publication_revision,active,Jsonb(rule.public_identity()),project,policy_id,expected_policy_version))
            if cur.rowcount!=1:
                raise FactResolutionError("policy_publication_revision_conflict")
            # 본문이 유효해도 옛 interpreter의 assertion은 새 정책의 현재 값을
            # 증명하지 않는다. 다른 정책의 독립적인 지지는 유지한다.
            cur.execute("""UPDATE lbrain_fact_assertions a SET state='revoked'
                FROM lbrain_fact_observations o WHERE o.version_id=a.observation_version
                  AND a.fact_key=%s AND o.project=%s AND o.observation#>>'{proof,policy_version}'=%s
                  AND a.state IN ('support','pending')""",(rule.scope.key,project,old["policy_version"]))
            if head:
                FactSourceLifecycle._refresh_head(cur,rule.scope.key)
            cur.execute("INSERT INTO lbrain_fact_policy_reviews(publication_id,state) VALUES (%s,%s)",
                (publication_id,"scheduled" if active else "disabled"))
            return self._receipt(publication,duplicate=False)

    def dispatch_once(self, *, project, batch_size=20, retry_seconds=60):
        if type(batch_size) is not int or not 1<=batch_size<=100 or not .1<=retry_seconds<=86400:
            raise ValueError("bounded policy review dispatch required")
        try:
            return self._dispatch(project,batch_size,retry_seconds)
        except (DeadlockDetected,LockNotAvailable,SerializationFailure):
            return {"status":"waiting","reason_code":"policy_lifecycle_busy"}

    def _dispatch(self, project, batch_size, retry_seconds):
        from .postgres_store.pgvector_store import PgVectorStore
        from .recheck_scheduler import PgRecheckQueue
        scheduled = blocked = verified = superseded = 0
        with self.store.transaction() as db,db.cursor(row_factory=dict_row) as cur:
            FactSourceLifecycle._bound(db)
            self.store._lock_steward_authority_on(db)
            cur.execute("""SELECT p.*,v.state AS review_state,v.task_id,v.attempt_count
                FROM lbrain_fact_policy_publications p JOIN lbrain_fact_policy_reviews v USING(publication_id)
                WHERE p.project=%s AND v.state IN ('scheduled','blocked','queued')
                  AND v.next_check_at<=clock_timestamp()
                ORDER BY p.publication_revision,p.publication_id FOR UPDATE OF v SKIP LOCKED LIMIT %s""",
                (project,batch_size))
            rows = cur.fetchall()
            queue = PgRecheckQueue(PgVectorStore(connection=db))
            for publication in rows:
                pub_id = publication["publication_id"]
                cur.execute("SELECT * FROM lbrain_fact_admission_rules WHERE project=%s AND policy_id=%s",
                    (project,publication["policy_id"]))
                current = cur.fetchone()
                if (not current or current["policy_version"]!=publication["policy_version"]
                        or current["publication_revision"]!=publication["publication_revision"] or not current["active"]):
                    cur.execute("""UPDATE lbrain_fact_policy_reviews SET state='superseded',
                        reason_code='newer_policy_publication' WHERE publication_id=%s""",(pub_id,))
                    superseded += 1
                    continue
                if publication["review_state"]=="queued":
                    cur.execute("SELECT state FROM lbrain_recheck_tasks WHERE task_id=%s",(publication["task_id"],))
                    task = cur.fetchone()
                    if task and task["state"]!="resolved":
                        continue
                    cur.execute("""SELECT 1 FROM lbrain_fact_verifications v
                        JOIN lbrain_fact_resolutions r ON r.resolution_id=v.resolution_id
                        JOIN lbrain_recheck_tasks child ON child.result_ref=r.resolution_id
                            AND child.request_hash=r.request_hash
                        WHERE child.task_id=%s AND r.fact_key=%s AND r.terminal AND r.resolution_id=%s
                          AND v.policy_version=%s AND v.policy_id=%s""",
                        (publication["task_id"],publication["fact_key"],
                            "factres_"+hash_payload([publication["fact_key"],publication["task_id"]])[7:47],
                            publication["policy_version"],publication["policy_id"]))
                    if cur.fetchone():
                        cur.execute("""UPDATE lbrain_fact_policy_reviews SET state='verified',
                            reason_code='policy_revalidated' WHERE publication_id=%s""",(pub_id,))
                        verified += 1
                    else:
                        # history-only 결과는 새 정책의 현재 검증 완료가 아니다.
                        # 새 원천을 다시 읽을 별도 시도를 만들며 이전 시도도 보존한다.
                        cur.execute("""UPDATE lbrain_fact_policy_reviews SET state='scheduled',task_id=NULL,
                            next_check_at=clock_timestamp()+(%s*interval '1 second'),
                            reason_code='policy_current_check_required' WHERE publication_id=%s""",(retry_seconds,pub_id))
                    continue
                cur.execute("SELECT memory_id,value FROM lbrain_fact_scopes WHERE fact_key=%s AND project=%s",
                    (publication["fact_key"],project))
                head = cur.fetchone()
                memory_id = head["memory_id"] if head else publication["target_memory_id"]
                card = PgVectorStore(connection=db).get_steward_card(memory_id)
                value = head["value"] if head else (card or {}).get("typed_payload",{}).get(
                    publication["field_path"].split(".",1)[1])
                if (not card or card["project"]!=project or card["authorization_status"]!="active"
                        or card["currentness"]!="current" or not isinstance(value,str) or not value.strip()):
                    cur.execute("""UPDATE lbrain_fact_policy_reviews SET state='blocked',
                        reason_code='policy_target_unavailable',next_check_at=clock_timestamp()+(%s*interval '1 second')
                        WHERE publication_id=%s""",(retry_seconds,pub_id))
                    blocked += 1
                    continue
                attempt = publication["attempt_count"]+1
                request = {"schema_version":"fact_source_job.v1","policy_id":publication["policy_id"],
                    "fact_key":publication["fact_key"],"policy_version":publication["policy_version"],"lane":"intake",
                    "initial_memory_id":memory_id,"change":{"proposed_value":value,"evidence":{"mode":"server_current"}},
                    "trigger":{"kind":"policy_publication","publication_id":pub_id,"attempt":attempt}}
                task = queue.register(project=project,memory_id=memory_id,request=request,
                    request_key=hash_payload(["policy-publication",pub_id,attempt]))
                cur.execute("""INSERT INTO lbrain_fact_policy_review_attempts(publication_id,attempt,task_id)
                    VALUES (%s,%s,%s)""",(pub_id,attempt,task["task_id"]))
                cur.execute("""UPDATE lbrain_fact_policy_reviews SET state='queued',task_id=%s,
                    attempt_count=%s,reason_code='policy_recheck_queued' WHERE publication_id=%s""",
                    (task["task_id"],attempt,pub_id))
                if head:
                    # 기한 검증과 같은 새 검토를 공유한다. 원래 queue 작업은 보존한다.
                    cur.execute("""UPDATE lbrain_fact_check_schedules SET state='queued',task_id=%s,
                        generation=generation+1,reason_code='policy_recheck_queued'
                        WHERE fact_key=%s AND policy_id=%s""",(task["task_id"],publication["fact_key"],publication["policy_id"]))
                scheduled += 1
        return {"status":"processed","scheduled":scheduled,"blocked":blocked,"verified":verified,"superseded":superseded}

    def review_snapshot(self, task, *, connection=None):
        """옛 요청의 의미를 바꾸지 않고 새 정책 검토와의 관계만 읽는다."""
        request = task["request"]
        # brain_source_observe는 queue 작업을 만들지 않고 같은 독립 reader를
        # 재사용한다. 실제 예약된 요청에만 정책 변경 결과를 연결한다.
        if not task.get("task_id") or not task.get("project") or not request.get("fact_key"):
            return None
        with self.store._scope(conn=connection) as db,db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT p.*,v.task_id FROM lbrain_fact_policy_publications p
                JOIN lbrain_fact_admission_rules a ON a.project=p.project AND a.policy_id=p.policy_id
                    AND a.policy_version=p.policy_version AND a.active=p.active
                JOIN lbrain_fact_policy_reviews v USING(publication_id)
                WHERE p.project=%s AND p.policy_id=%s AND p.fact_key=%s AND p.policy_version<>%s
                  AND EXISTS(SELECT 1 FROM lbrain_fact_policy_publications prior
                    WHERE prior.project=p.project AND prior.policy_id=p.policy_id AND prior.fact_key=p.fact_key
                      AND (prior.previous_version=%s OR prior.policy_version=%s))
                ORDER BY p.publication_revision DESC LIMIT 1""",
                (task["project"],request.get("policy_id"),request.get("fact_key"),request.get("policy_version"),
                    request.get("policy_version"),request.get("policy_version")))
            publication = cur.fetchone()
            if publication is None:
                return None
            result = {"publication_id":publication["publication_id"],"project":publication["project"],
                "policy_id":publication["policy_id"],"policy_version":publication["policy_version"],
                "publication_revision":publication["publication_revision"],
                "publication_request_hash":publication["request_hash"],"fact_key":publication["fact_key"],
                "status":"pending" if publication["active"] else "disabled",
                "review_task_id":publication["task_id"],"resolution_id":None,"resolution_hash":None}
            if publication["active"] and publication["task_id"]:
                cur.execute("""SELECT r.resolution_id,r.result FROM lbrain_fact_resolutions r
                    JOIN lbrain_fact_verifications v ON v.resolution_id=r.resolution_id
                    JOIN lbrain_recheck_tasks child ON child.result_ref=r.resolution_id AND child.request_hash=r.request_hash
                    WHERE child.task_id=%s AND child.result_ref=r.resolution_id AND r.terminal
                      AND r.fact_key=%s AND r.resolution_id=%s AND v.policy_id=%s AND v.policy_version=%s""",
                    (publication["task_id"],publication["fact_key"],
                        "factres_"+hash_payload([publication["fact_key"],publication["task_id"]])[7:47],
                        publication["policy_id"],publication["policy_version"]))
                resolved = cur.fetchone()
                if resolved:
                    result.update(status="verified",resolution_id=resolved["resolution_id"],
                        resolution_hash=hash_payload(resolved["result"]))
            if result["status"]=="disabled":
                result["review_task_id"] = None
            ensure_public_safe(result,"PolicyReviewSnapshot")
            return result

    def complete_original(self, task, snapshot, judgment):
        from .recheck_scheduler import RecheckBlocked, RecheckOutcome
        dependency = snapshot.data["policy_review"]
        if judgment!={"schema_version":"fact_policy_review_judgment.v1","policy_review":dependency}:
            raise RecheckBlocked("policy_review_binding_mismatch")
        if dependency["status"]=="pending":
            return RecheckOutcome("waiting","policy_revalidation_pending")
        with self.coordinator.queue.owned_transaction(task) as db,db.cursor(row_factory=dict_row) as cur:
            self.store._lock_steward_authority_on(db)
            if self.review_snapshot(task,connection=db)!=dependency:
                raise RecheckBlocked("policy_review_changed")
            reason = "policy_revalidated" if dependency["status"]=="verified" else "policy_disabled"
            result_id = "policyrev_"+hash_payload([task["task_id"],dependency["publication_id"],
                dependency["resolution_id"],task["request_hash"]])[7:47]
            receipt = {"status":"superseded","reason_code":reason,"original_task_id":task["task_id"],
                "original_request_hash":task["request_hash"],"original_policy_version":task["request"]["policy_version"],
                "publication_id":dependency["publication_id"],"policy_version":dependency["policy_version"],
                "review_task_id":dependency["review_task_id"],"review_resolution_id":dependency["resolution_id"],
                "original_change_applied":False,"canonical_write_performed":False}
            historical_resolution_id = None
            native = snapshot.data.get("native_source_read")
            if native:
                from .source_observation import SourceObservationService
                reference = task["request"]["change"]["evidence"]["observation_ref"]
                historical_resolution_id = SourceObservationService(self.coordinator).previous_resolution(reference,connection=db)
                receipt["native_source_read"] = {"observation_ref":reference,"join_only":True,
                    "authority_write_performed":False,"historical_resolution_id":historical_resolution_id}
            cur.execute("""INSERT INTO lbrain_fact_policy_review_results
                (result_id,task_id,publication_id,request_hash,input_hash,review_task_id,resolution_id,snapshot,result)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (result_id,task["task_id"],dependency["publication_id"],task["request_hash"],snapshot.fingerprint,
                    dependency["review_task_id"],dependency["resolution_id"],Jsonb(dependency),Jsonb(receipt)))
            cur.execute("SELECT * FROM lbrain_fact_policy_review_results WHERE task_id=%s",(task["task_id"],))
            saved = cur.fetchone()
            if not saved or any(saved[key]!=value for key,value in {
                "result_id":result_id,"publication_id":dependency["publication_id"],"request_hash":task["request_hash"],
                "input_hash":snapshot.fingerprint,"review_task_id":dependency["review_task_id"],
                "resolution_id":dependency["resolution_id"],"snapshot":dependency,"result":receipt}.items()):
                raise RecheckBlocked("policy_review_result_conflict")
            if native:
                from .source_observation import NativeSourceObservationBridge
                NativeSourceObservationBridge(self.coordinator,self.coordinator.native_catalog).attach_policy_history_on(
                    db,task,native,result_id,historical_resolution_id=historical_resolution_id)
            self.coordinator.queue.checkpoint_result(task,result_id,connection=db)
        return RecheckOutcome("resolved",reason,result_id)

    def receipt(self, *, project, request_key):
        for value,maximum in ((project,64),(request_key,180)):
            if not isinstance(value,str) or not value.strip() or len(value)>maximum:
                raise ValueError("bounded policy publication receipt identity required")
        ensure_public_safe({"project":project,"request_key":request_key},"PolicyPublicationReceipt")
        with self.store._scope() as db,db.cursor(row_factory=dict_row) as cur:
            if self.store.connection is None:
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            FactSourceLifecycle._bound(db)
            cur.execute("SELECT * FROM lbrain_fact_policy_publications WHERE project=%s AND command_id=%s",
                (project,hash_payload([project,request_key])))
            publication = cur.fetchone()
            if publication is None:
                raise FactResolutionError("policy_publication_unavailable")
            cur.execute("SELECT state,task_id,attempt_count,reason_code FROM lbrain_fact_policy_reviews WHERE publication_id=%s",
                (publication["publication_id"],))
            return {**self._receipt(publication,duplicate=True),"review":cur.fetchone()}

    def plan(self, *, project, policy_id):
        rule = self.coordinator.policies.get(policy_id)
        if not rule or rule.scope.project!=project:
            raise FactResolutionError("policy_publication_scope_mismatch")
        with self.store._scope() as db,db.cursor(row_factory=dict_row) as cur:
            if self.store.connection is None:
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            FactSourceLifecycle._bound(db)
            cur.execute("SELECT * FROM lbrain_fact_admission_rules WHERE project=%s AND policy_id=%s",(project,policy_id))
            current = cur.fetchone()
            if current is None:
                raise FactResolutionError("policy_publication_unavailable")
            cur.execute("""SELECT count(*) AS n FROM lbrain_fact_assertions a JOIN lbrain_fact_observations o
                ON o.version_id=a.observation_version WHERE a.fact_key=%s AND a.state IN ('support','pending')
                AND o.observation#>>'{proof,policy_version}'=%s""",(current["fact_key"],current["policy_version"]))
            affected = cur.fetchone()["n"]
        return {"project":project,"policy_id":policy_id,"current_version":current["policy_version"],
            "proposed_version":rule.version,"current_publication_revision":current["publication_revision"],
            "proposed_publication_revision":rule.publication_revision,"affected_assertions":affected,
            "previous_definition_available":current.get("policy_descriptor") is not None,
            "canonical_write_performed":False,"publication_performed":False,
            "requires_cas_and_current_target_validation":True}

    @staticmethod
    def health_on(db, *, project, overdue_grace, pending_age_seconds):
        with db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE v.state IN ('scheduled','blocked','queued')) AS unfinished,
                count(*) FILTER (WHERE v.state='blocked') AS blocked,
                count(*) FILTER (WHERE v.state IN ('scheduled','blocked')
                    AND v.next_check_at<clock_timestamp()-(%s*interval '1 second')) AS overdue,
                count(*) FILTER (WHERE v.state IN ('scheduled','blocked','queued')
                    AND p.created_at<clock_timestamp()-(%s*interval '1 second')) AS aged
                FROM lbrain_fact_policy_reviews v JOIN lbrain_fact_policy_publications p USING(publication_id)
                WHERE p.project=%s""",(overdue_grace,pending_age_seconds,project))
            row = cur.fetchone()
        return {**row,"attention_required":bool(row["blocked"] or row["overdue"] or row["aged"])}

    @staticmethod
    def _receipt(publication, *, duplicate):
        return {"publication_id":publication["publication_id"],"project":publication["project"],
            "policy_id":publication["policy_id"],"policy_version":publication["policy_version"],
            "publication_revision":publication["publication_revision"],"active":publication["active"],
            "duplicate":duplicate,"canonical_write_performed":False,
            "recheck_scheduled":publication["active"]}


def main(argv=None):
    import argparse
    import json
    import os
    from types import SimpleNamespace
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from .fact_correction_coordinator import FactCorrectionCoordinator, load_configuration
    from .postgres_store.pgvector_store import PgVectorStore
    parser = argparse.ArgumentParser(description="서버 정책의 CAS 공표·읽기 계획·저장 결과 조회")
    parser.add_argument("mode",choices=("plan","publish","receipt"))
    parser.add_argument("--project",required=True)
    parser.add_argument("--policy-id")
    parser.add_argument("--config")
    parser.add_argument("--previous-config",help="옛 v1 등록에 설명이 없을 때 기존 hash와 대조할 이전 서버 설정")
    parser.add_argument("--expected-policy-version")
    parser.add_argument("--request-key")
    parser.add_argument("--actor")
    parser.add_argument("--inactive",action="store_true")
    parser.add_argument("--dsn-env",default="NEURON_LBRAIN_PGVECTOR_DSN")
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get(args.dsn_env,"")
        if not dsn:
            raise ValueError("explicit policy lifecycle DSN required")
        connection = conninfo_to_dict(dsn)
        deadline = int(connection.get("connect_timeout","5"))
        store = PgVectorStore(dsn=make_conninfo(dsn,connect_timeout=5 if deadline<=0 else min(deadline,5)))
        if args.mode=="receipt":
            if not args.request_key:
                raise ValueError("publication request key required")
            service = FactPolicyLifecycle(SimpleNamespace(store=store,policies={}))
            result = service.receipt(project=args.project,request_key=args.request_key)
        else:
            if not args.config or not args.policy_id:
                raise ValueError("configured server policy required")
            reader,rules,target_search = load_configuration(args.config,with_target_search=True)
            service = FactPolicyLifecycle(FactCorrectionCoordinator(store=store,git_reader=reader,policies=rules,
                target_search=target_search))
            if args.mode=="plan":
                result = service.plan(project=args.project,policy_id=args.policy_id)
            else:
                previous_descriptor = None
                if args.previous_config:
                    _,previous_rules = load_configuration(args.previous_config)
                    previous = next((rule for rule in previous_rules if rule.policy_id==args.policy_id
                        and rule.scope.project==args.project),None)
                    if previous is None:
                        raise FactResolutionError("policy_previous_definition_mismatch")
                    previous_descriptor = previous.public_identity()
                result = service.publish(project=args.project,policy_id=args.policy_id,
                    expected_policy_version=args.expected_policy_version,request_key=args.request_key,
                    actor=args.actor,active=not args.inactive,previous_descriptor=previous_descriptor)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return 0
    except Exception as exc:
        code = exc.reason_code if isinstance(exc,FactResolutionError) else "policy_lifecycle_unavailable"
        print(json.dumps({"status":"unavailable","reason_code":code,"attention_required":True}),flush=True)
        return 2


if __name__=="__main__":
    raise SystemExit(main())

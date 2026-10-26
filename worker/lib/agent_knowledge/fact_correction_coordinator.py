"""교정 batch와 source 수집을 같은 내구성 queue·fact resolution에 연결한다.

첫 source admission은 서버에 등록한 Git JSON 필드의 직접 검증이다.
자연어 의미 판단을 이 adapter의 직접 검증으로 대체하거나 허용하지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Literal

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .correction_sources import GitSourceReader, SourceUnavailable, VerifiedGitSource
from .fact_resolution import FactResolutionError, FactResolutionService, FactScope, StewardFactCommit, VerifiedObservation
from .public_safe_util import ensure_public_safe, hash_payload, sha256_text
from .recheck_scheduler import PgRecheckQueue, RecheckBlocked, RecheckOutcome, RecheckSnapshot, RecheckWorker


class FactEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class FactChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    change_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_:-]+$")
    policy_id: str = Field(min_length=1, max_length=128)
    target_memory_id: str | None = Field(default=None, min_length=1, max_length=64)
    target_description: str | None = Field(default=None, min_length=1, max_length=512)
    expected_content_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    old_value: str = Field(min_length=1, max_length=2048)
    proposed_value: str = Field(min_length=1, max_length=2048)
    discrepancy: str = Field(min_length=1, max_length=2048)
    evidence: FactEvidence

    @model_validator(mode="after")
    def target(self):
        if bool(self.target_memory_id) == bool(self.target_description):
            raise ValueError("one exact target or description required")
        if self.target_memory_id and not self.expected_content_hash:
            raise ValueError("exact target requires expected revision")
        return self


class FactBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["brain_object_correction.v2"]
    project: str = Field(min_length=1, max_length=64)
    repository: str = Field(min_length=1, max_length=240)
    idempotency_key: str = Field(min_length=1, max_length=128)
    changes: list[FactChange] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_changes(self):
        if len({item.change_id for item in self.changes}) != len(self.changes):
            raise ValueError("duplicate change identity")
        ensure_public_safe(self.model_dump(), "FactBatch")
        return self


@dataclass(frozen=True)
class GitJsonFactPolicy:
    policy_id: str
    scope: FactScope
    target_title: str
    field_path: str
    render_template: str
    relative_path: str
    json_pointer: str
    max_age_seconds: int = 300

    def __post_init__(self):
        if not self.policy_id or not self.target_title or not self.json_pointer.startswith("/"):
            raise ValueError("explicit fact identity and source field mapping required")
        if not self.scope.branch.startswith("refs/heads/"):
            # HEAD는 checkout 변경에 따라 다른 branch를 가리킨다. 같은 fact key에
            # 서로 다른 branch의 값을 덮어쓰지 않도록 자동 교정 정책은 고정한다.
            raise ValueError("automatic correction requires an explicit fixed branch")
        # 기존 commit primitive의 필드·표시 범위 제약을 그대로 적용한다.
        StewardFactCommit(field_path=self.field_path, render_template=self.render_template,
            policy_id=self.policy_id, max_age_seconds=self.max_age_seconds)
        ensure_public_safe(self.public_identity(), "GitJsonFactPolicy")

    def public_identity(self):
        data = asdict(self)
        data["relative_path_hash"] = sha256_text(data.pop("relative_path"))
        return data

    @property
    def version(self):
        return hash_payload(self.public_identity())


class FactCorrectionCoordinator:
    def __init__(self, *, store, git_reader: GitSourceReader, policies: list[GitJsonFactPolicy]):
        self.store, self.git_reader = store, git_reader
        self.policies = {rule.policy_id: rule for rule in policies}
        if not policies or len(self.policies) != len(policies):
            raise ValueError("unique server fact policies required")
        for rule in policies:
            configured = git_reader.policies.get(rule.scope.repository)
            if not configured or configured.branch_ref != rule.scope.branch:
                raise ValueError("fact scope and admitted source branch must agree")
        self.queue = PgRecheckQueue(store)
        self.resolver = FactResolutionService(store)

    def submit(self, arguments, *, lane="correction"):
        if lane not in {"correction", "intake"}:
            raise ValueError("unknown fact admission lane")
        batch = FactBatch.model_validate(arguments)
        request = {**batch.model_dump(), "lane": lane}
        request_hash = hash_payload(request)
        batch_id = "factbatch_" + hash_payload([batch.project, batch.repository, batch.idempotency_key, lane])[7:47]
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self.store._lock_steward_authority_on(db)
            cur.execute("SELECT * FROM lbrain_fact_batches WHERE batch_id=%s", (batch_id,))
            previous = cur.fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise FactResolutionError("batch_key_conflict")
                return {**previous["result"], "duplicate": True}
            from .postgres_store.pgvector_store import PgVectorStore
            queue = PgRecheckQueue(PgVectorStore(connection=db))
            results = []
            for change in batch.changes:
                rule = self.policies.get(change.policy_id)
                if not rule or (rule.scope.project, rule.scope.repository) != (batch.project, batch.repository):
                    results.append({"change_id": change.change_id, "status": "rejected", "reason_code": "policy_scope_mismatch"})
                    continue
                try:
                    target = self._target(cur, rule, change)
                    job = {"schema_version": "fact_source_job.v1", "policy_id": rule.policy_id,
                        "fact_key": rule.scope.key,
                        "policy_version": rule.version, "lane": lane, "change": change.model_dump(),
                        "initial_memory_id": target["memory_id"], "initial_revision": target["content_hash"]}
                    job["stale_target"] = target.get("_stale_target", False)
                    task = queue.register(project=batch.project, memory_id=target.get("_queue_memory_id", target["memory_id"]), request=job,
                        request_key=hash_payload([batch_id, change.change_id]))
                    results.append({"change_id": change.change_id, "status": "queued", "task_id": task["task_id"]})
                except FactResolutionError as exc:
                    results.append({"change_id": change.change_id, "status": "rejected", "reason_code": exc.reason_code})
            result = {"schema_version": "brain_object_correction.v2", "batch_id": batch_id,
                "duplicate": False, "authority_write_performed": False, "changes": results}
            cur.execute("INSERT INTO lbrain_fact_batches(batch_id,project,request_hash,result) VALUES (%s,%s,%s,%s)",
                (batch_id, batch.project, request_hash, Jsonb(result)))
            return result

    def _target(self, cur, rule, change):
        if change.target_memory_id:
            cur.execute("SELECT steward_envelope FROM memory_cards WHERE project=%s AND memory_id=%s",
                (rule.scope.project, change.target_memory_id))
        else:
            # 설명 검색은 전체 cardinality를 먼저 확인한다. LIMIT로 후보를 잘라 단일 대상으로 오인하지 않는다.
            # 첫 adapter는 정확한 title/summary 문구만 다룬다. 의미 검색 확대는 별도 검증이 필요하다.
            cur.execute("""SELECT steward_envelope FROM memory_cards WHERE project=%s
                AND authorization_status='active' AND currentness='current'
                AND (title=%s OR summary=%s) LIMIT 2""",
                (rule.scope.project, change.target_description, change.target_description))
        rows = cur.fetchall()
        if len(rows) != 1 or not rows[0]["steward_envelope"]:
            raise FactResolutionError("target_ambiguous" if rows else "target_not_found")
        card = rows[0]["steward_envelope"]
        if card.get("authorization_status") != "active" or card.get("title") != rule.target_title:
            raise FactResolutionError("target_scope_mismatch")
        if card.get("currentness") == "superseded" and change.target_memory_id:
            if change.expected_content_hash != card["content_hash"]:
                raise FactResolutionError("target_revision_conflict")
            cur.execute("""SELECT f.memory_id FROM lbrain_fact_members m JOIN lbrain_fact_scopes f
                ON f.fact_key=m.fact_key WHERE m.memory_id=%s AND m.field_path=%s AND f.fact_key=%s""",
                (card["memory_id"], rule.field_path, rule.scope.key))
            head = cur.fetchone()
            if not head or card.get("typed_payload", {}).get(rule.field_path.split(".", 1)[1]) != change.old_value:
                raise FactResolutionError("target_revision_conflict")
            return {**card, "_queue_memory_id": head["memory_id"], "_stale_target": True}
        if card.get("currentness") != "current":
            raise FactResolutionError("target_scope_mismatch")
        card = self.resolver.project_cards([card], connection=cur.connection)[0]
        if change.expected_content_hash and change.expected_content_hash != card["content_hash"]:
            raise FactResolutionError("target_revision_conflict")
        if card.get("typed_payload", {}).get(rule.field_path.split(".", 1)[1]) != change.old_value:
            raise FactResolutionError("target_value_conflict")
        return card

    def worker(self, *, project, worker_id, retry_seconds=60):
        return RecheckWorker(queue=self.queue, project=project, worker_id=worker_id,
            load=self._load, judge=lambda task, snapshot: {"direct_source_mapping": True},
            apply=self._apply, retry_seconds=retry_seconds)

    def submit_candidate(self, candidate, *, refresh_watermark):
        """정규 miner 후보를 새 승인 대신 기존 사실의 동일 resolution으로 보낸다.

        이 adapter는 명시적 서버 정책에 정확히 대응하는 title·field만 다룬다.
        후보 문장의 의미 유사성을 검증한 것으로 취급하지 않는다.
        """
        rules = [rule for rule in self.policies.values() if rule.scope.project == candidate.get("project")
            and rule.target_title == candidate.get("title")
            and isinstance(candidate.get("typed_payload", {}).get(rule.field_path.split(".", 1)[1]), str)]
        if not rules:
            return None
        results = []
        for rule in rules:
            trigger = {"kind": "miner_candidate", "source_refs": candidate.get("source_refs", []),
                "span_refs": candidate.get("span_refs", []), "content_hash": candidate.get("content_hash", "")}
            ensure_public_safe(trigger, "FactIntakeTrigger")
            proposed_value = candidate["typed_payload"][rule.field_path.split(".", 1)[1]]
            route_key = hash_payload(["intake", rule.version, candidate["memory_id"]])
            input_hash = hash_payload([trigger, proposed_value])
            with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
                self.store._lock_steward_authority_on(db)
                # target이 successor로 바뀌어도 동일 후보의 기존 내구성 작업을
                # 찾아야 한다. 현재 target ID로 다시 queue ID를 만들면 재시도가
                # 매번 새로운 작업이 되어 버린다.
                cur.execute("""SELECT task_id,state,request FROM lbrain_recheck_tasks
                    WHERE project=%s AND request->>'candidate_route_key'=%s""", (rule.scope.project, route_key))
                previous = cur.fetchone()
                if previous:
                    if previous["request"].get("candidate_input_hash") != input_hash:
                        raise FactResolutionError("candidate_route_conflict")
                    results.append({"policy_id": rule.policy_id,
                        "status": "resolved" if previous["state"] == "resolved" else "queued",
                        "task_id": previous["task_id"], "duplicate": True})
                    continue
                cur.execute("""SELECT steward_envelope FROM memory_cards WHERE project=%s AND title=%s
                    AND authorization_status='active' AND currentness='current' LIMIT 2""",
                    (rule.scope.project, rule.target_title))
                rows = cur.fetchall()
                if len(rows) != 1 or not rows[0]["steward_envelope"]:
                    results.append({"policy_id": rule.policy_id, "status": "needs_review", "reason_code": "existing_target_not_unique"})
                    continue
                card = rows[0]["steward_envelope"]
                request = {"schema_version": "fact_source_job.v1", "policy_id": rule.policy_id,
                    "fact_key": rule.scope.key,
                    "policy_version": rule.version, "lane": "intake", "initial_memory_id": card["memory_id"],
                    "candidate_memory_id": candidate["memory_id"],
                    "candidate_route_key": route_key, "candidate_input_hash": input_hash,
                    "change": {"proposed_value": proposed_value,
                        "evidence": {"mode": "server_current"}}}
                # native 참조는 재검토를 촉발한 입력이다. 그 주장이 최신 사실이라는
                # 증거나 독립 Git 검증과 동일 관측이라는 주장으로 승격하지 않는다.
                request["trigger"] = trigger
                from .postgres_store.pgvector_store import PgVectorStore
                queue = PgRecheckQueue(PgVectorStore(connection=db))
                task = queue.register(project=rule.scope.project, memory_id=card["memory_id"], request=request,
                    request_key=route_key)
                results.append({"policy_id": rule.policy_id, "status": "queued", "task_id": task["task_id"]})
        return {"managed": True, "results": results}

    def _load(self, task):
        if task["result_ref"]:
            with self.store._scope() as db, db.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT terminal,result FROM lbrain_fact_resolutions WHERE resolution_id=%s", (task["result_ref"],))
                row = cur.fetchone()
                if row and row["terminal"]:
                    return RecheckSnapshot.from_data(task, {"recovered_result": task["result_ref"]})
        request = task["request"]
        rule = self.policies.get(request["policy_id"])
        if not rule or rule.version != request["policy_version"]:
            raise RecheckBlocked("fact_policy_changed")
        try:
            descriptor = request["change"]["evidence"]
            if request["lane"] == "intake" and descriptor == {"mode": "server_current"}:
                source = self.git_reader.read_current(repository=rule.scope.repository, relative_path=rule.relative_path)
            else:
                evidence = FactEvidence.model_validate(descriptor)
                source = self.git_reader.read(repository=rule.scope.repository, revision=evidence.revision,
                    relative_path=rule.relative_path, expected_content_hash=evidence.content_hash)
        except SourceUnavailable as exc:
            raise RecheckBlocked(exc.reason_code) from None
        # JSON 내용을 바꾼 redaction 결과를 원본의 명시적 값으로 취급하지 않는다.
        if source.content_hash != source.redacted_content_hash:
            raise RecheckBlocked("source_redaction_changed_content")
        try:
            document = json.loads(source.redacted_text, object_pairs_hook=_unique_object)
            value = _pointer(document, rule.json_pointer)
            if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 2048:
                raise ValueError("explicit string fact required")
        except (ValueError, TypeError, KeyError):
            raise RecheckBlocked("source_fact_unverifiable") from None
        ensure_public_safe(value, "SourceFact")
        proof = source.public_proof()
        proof.pop("observed_at")
        proof["policy_version"] = rule.version
        proof["source_ref_id"] = "src_fact_" + hash_payload(proof)[7:47]
        observation = VerifiedObservation(observation_id=hash_payload([rule.scope.context_hash, source.content_identity]),
            project=rule.scope.project, source_id=hash_payload([rule.scope.context_hash, source.relative_path_hash, rule.json_pointer]),
            source_revision=source.revision, content_hash=source.content_hash, context_hash=rule.scope.context_hash,
            interpretation_version="git_json.v1:" + rule.version[7:23], proof=proof,
            current_verified=source.current_verified, observed_at=source.observed_at)
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute("""INSERT INTO lbrain_fact_source_receipts(source_ref_id,content_hash,redacted_content,proof)
                VALUES (%s,%s,%s,%s) ON CONFLICT(source_ref_id) DO NOTHING""",
                (proof["source_ref_id"], source.content_hash, source.redacted_text, Jsonb(proof)))
            cur.execute("SELECT content_hash,redacted_content,proof,state FROM lbrain_fact_source_receipts WHERE source_ref_id=%s",
                (proof["source_ref_id"],))
            receipt = cur.fetchone()
            if (receipt["content_hash"] != source.content_hash or receipt["redacted_content"] != source.redacted_text
                    or receipt["proof"] != proof or receipt["state"] != "active"):
                raise RecheckBlocked("source_receipt_unavailable")
            cur.execute("""INSERT INTO lbrain_fact_observations
                (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(version_id) DO NOTHING""",
                (observation.version_id, observation.observation_id, observation.project, observation.source_id,
                    observation.source_revision, observation.interpretation_version, Jsonb(asdict(observation))))
            cur.execute("SELECT observation FROM lbrain_fact_observations WHERE version_id=%s", (observation.version_id,))
            observation = VerifiedObservation(**cur.fetchone()["observation"])
        # 원문을 snapshot/model cache에 복제하지 않고 receipt로 고정한다.
        return RecheckSnapshot.from_data(task, {"value": value, "observation": asdict(observation)})

    def _current(self, rule, observation, db):
        admitted_rule = next((item for item in self.policies.values()
            if item.version == observation.proof.get("policy_version") and item.scope.key == rule.scope.key), None)
        if admitted_rule is None:
            return False
        rule = admitted_rule
        with db.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT state FROM lbrain_fact_source_receipts WHERE source_ref_id=%s",
                (observation.proof["source_ref_id"],))
            receipt = cur.fetchone()
        if not receipt or receipt["state"] != "active":
            return False
        proof = observation.proof
        source = VerifiedGitSource(repository=rule.scope.repository, revision=observation.source_revision,
            blob_id=proof["blob_id"], relative_path_hash=proof["relative_path_hash"],
            content_hash=observation.content_hash, redacted_content_hash=proof["redacted_content_hash"],
            redacted_text="", observed_at=observation.observed_at, head_revision=observation.source_revision,
            branch_ref_hash=proof["branch_ref_hash"])
        try:
            self.git_reader.assert_still_current(source)
            return True
        except SourceUnavailable:
            return False

    def _apply(self, task, snapshot, judgment):
        if "recovered_result" in snapshot.data:
            return RecheckOutcome("resolved", "fact_result_recovered", snapshot.data["recovered_result"])
        from .postgres_store.pgvector_store import PgVectorStore
        rule = self.policies[task["request"]["policy_id"]]
        observation = VerifiedObservation(**snapshot.data["observation"])
        with self.queue.owned_transaction(task) as db, db.cursor(row_factory=dict_row) as cur:
            bound = PgVectorStore(connection=db)
            resolver = FactResolutionService(bound)
            bound._lock_steward_authority_on(db)
            cur.execute("SELECT memory_id FROM lbrain_fact_scopes WHERE fact_key=%s", (rule.scope.key,))
            head = cur.fetchone()
            target = bound.get_steward_card(head["memory_id"] if head else task["memory_id"])
            if not target or target.get("currentness") != "current":
                raise RecheckBlocked("fact_target_not_current")
            view = resolver.project_cards([target], connection=db)[0]
            if (task["request"].get("stale_target") and snapshot.data["value"] !=
                    view.get("typed_payload", {}).get(rule.field_path.split(".", 1)[1])):
                raise RecheckBlocked("target_revision_conflict")
            try:
                result = resolver.reconcile(scope=rule.scope, memory_id=view["memory_id"],
                    field_path=rule.field_path, render_template=rule.render_template,
                    expected_content_hash=view["content_hash"], value=snapshot.data["value"], observation=observation,
                    request_key=task["task_id"], request_hash=task["request_hash"],
                    requested_value=task["request"]["change"]["proposed_value"],
                    is_current=lambda obs: self._current(rule, obs, db),
                    compare=lambda old, incoming: self.git_reader.compare_revisions(repository=rule.scope.repository,
                        previous=old.source_revision, incoming=incoming.source_revision),
                    commit=StewardFactCommit(field_path=rule.field_path, render_template=rule.render_template,
                        policy_id=rule.policy_id, max_age_seconds=rule.max_age_seconds))
            except FactResolutionError as exc:
                raise RecheckBlocked(exc.reason_code) from None
            if result["action"] in {"disputed", "pending_evidence"}:
                return RecheckOutcome("waiting", result["action"])
            self.queue.checkpoint_result(task, result["resolution_id"], connection=db)
            return RecheckOutcome("resolved", result["action"], result["resolution_id"])

    def result(self, task_id):
        task = self.queue.get(task_id)
        if not task:
            raise FactResolutionError("fact_task_not_found")
        result = {"task_id": task_id, "state": task["state"], "reason_code": task["reason_code"],
            "attempt_count": task["attempt_count"], "automatic_recheck_scheduled": task["state"] != "resolved"}
        resolution_ref = task["result_ref"]
        if not resolution_ref and task["request"].get("fact_key"):
            resolution_ref = "factres_" + hash_payload([task["request"]["fact_key"], task_id])[7:47]
        if resolution_ref:
            with self.store._scope() as db, db.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT result FROM lbrain_fact_resolutions WHERE resolution_id=%s", (resolution_ref,))
                row = cur.fetchone()
                if row:
                    receipt = row["result"]
                    receipt["decision_time_current"] = receipt.pop("current", None)
                    receipt["current"] = self.resolver._head(cur, receipt["fact_key"])
                    result["resolution"] = receipt
                    result["consistency_token"] = {"project": task["project"],
                        "min_fact_generation": receipt["project_generation"]}
        return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate source key")
        result[key] = value
    return result


def _pointer(value, pointer):
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or token not in value:
            raise ValueError("missing explicit source field")
        value = value[token]
    return value


def load_configuration(path):
    """운영 경로를 출력하지 않고 서버가 소유한 adapter 설정만 읽는다."""
    from pathlib import Path
    from .correction_sources import GitSourcePolicy
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError("bounded fact configuration required")
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(data, dict) or set(data) != {"schema_version", "repositories", "rules"}
            or data["schema_version"] != "fact_correction_config.v1"
            or not isinstance(data["repositories"], list) or not isinstance(data["rules"], list)
            or not 1 <= len(data["repositories"]) <= 20 or not 1 <= len(data["rules"]) <= 200):
        raise ValueError("explicit fact configuration required")
    repositories = [GitSourcePolicy(**item) for item in data["repositories"]]
    if len({item.repository for item in repositories}) != len(repositories):
        raise ValueError("duplicate source repository")
    reader = GitSourceReader({item.repository: item for item in repositories})
    rules = [GitJsonFactPolicy(**{**item, "scope": FactScope(**item["scope"])}) for item in data["rules"]]
    return reader, rules


def main(argv=None):
    import argparse
    import os
    import time
    from .postgres_store.pgvector_store import PgVectorStore
    from .recheck_scheduler import inspect_recheck_health
    parser = argparse.ArgumentParser(description="공통 fact source 교정·수집 queue worker와 독립 감시")
    parser.add_argument("mode", choices=("worker", "monitor"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--worker-id", default="fact-source-worker")
    parser.add_argument("--dsn-env", default="NEURON_LBRAIN_PGVECTOR_DSN")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--retry-seconds", type=float, default=60)
    parser.add_argument("--heartbeat-grace", type=float, default=120)
    parser.add_argument("--overdue-grace", type=float, default=120)
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= 100000 or not .01 <= args.poll_seconds <= 60:
        parser.error("bounded worker iterations/poll interval required")
    try:
        dsn = os.environ.get(args.dsn_env, "")
        reader, policies = load_configuration(args.config)
        coordinator = FactCorrectionCoordinator(store=PgVectorStore(dsn=dsn), git_reader=reader, policies=policies)
        if args.project not in {rule.scope.project for rule in policies}:
            raise ValueError("project not configured")
        worker = coordinator.worker(project=args.project, worker_id=args.worker_id, retry_seconds=args.retry_seconds)
        for attempt in range(args.iterations):
            result = (worker.run_once() if args.mode == "worker" else
                inspect_recheck_health(coordinator.queue, project=args.project,
                    heartbeat_grace=args.heartbeat_grace, overdue_grace=args.overdue_grace))
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if attempt + 1 < args.iterations:
                time.sleep(args.poll_seconds)
        return 0
    except Exception as exc:
        # source 본문·private path·DSN·provider exception을 출력하지 않는다.
        reason = exc.reason_code if isinstance(exc, (FactResolutionError, RecheckBlocked)) else "fact_worker_unavailable"
        print(json.dumps({"status": "unavailable", "reason_code": reason}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

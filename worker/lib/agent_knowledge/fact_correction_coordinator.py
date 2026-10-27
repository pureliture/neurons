"""교정 batch와 source 수집을 같은 내구성 queue·fact resolution에 연결한다.

Git JSON 필드는 직접 검증한다. 의미 기반 Git source는 별도 독립 평가를
통과한 profile과 서버의 고정 주장/근거 범위를 재검증한 뒤에만 적용한다.
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


class ReferencedFactEvidence(FactEvidence):
    observation_ref: str = Field(pattern=r"^obsread_[0-9a-f]{48}$")


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
    evidence: FactEvidence | ReferencedFactEvidence

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


@dataclass(frozen=True)
class GitSemanticFactPolicy:
    """서버가 고정한 source 범위와 값별 주장만 모델에 판별시킨다."""
    policy_id: str
    scope: FactScope
    target_title: str
    field_path: str
    render_template: str
    relative_path: str
    scope_description: str
    claims: tuple[tuple[str, str], ...]
    required_capability: str
    validation_profile_path: str
    validation_profile_hash: str
    max_age_seconds: int = 300

    def __post_init__(self):
        from .correction_capabilities import ObservedCapability
        from .correction_semantic import validate_claims
        from .correction_sources import _relative_path
        from .public_safe_util import require_sha256
        if (not self.policy_id or not self.target_title or not self.scope.branch.startswith("refs/heads/")
                or not isinstance(self.scope_description, str) or not self.scope_description.strip()
                or len(self.scope_description) > 2048 or not self.validation_profile_path):
            raise ValueError("explicit fixed semantic source policy required")
        validate_claims(self.claims)
        object.__setattr__(self, "claims", tuple(tuple(pair) for pair in self.claims))
        ObservedCapability(self.required_capability)
        require_sha256(self.validation_profile_hash, "semantic validation profile")
        _relative_path(self.relative_path)
        StewardFactCommit(field_path=self.field_path, render_template=self.render_template,
            policy_id=self.policy_id, max_age_seconds=self.max_age_seconds)
        ensure_public_safe(self.public_identity(), "GitSemanticFactPolicy")

    def public_identity(self):
        data = asdict(self)
        data["adapter"] = "git_semantic.v1"
        data["relative_path_hash"] = sha256_text(data.pop("relative_path"))
        data["validation_profile_path_hash"] = sha256_text(data.pop("validation_profile_path"))
        return data

    @property
    def version(self):
        return hash_payload(self.public_identity())


class FactCorrectionCoordinator:
    def __init__(self, *, store, git_reader: GitSourceReader, policies: list[GitJsonFactPolicy | GitSemanticFactPolicy],
                 relation_client=None, native_catalog=None, target_search=None, target_client=None):
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
        self.relation_client = relation_client
        self.native_catalog = native_catalog
        from .correction_target import TargetSearchPolicy
        if target_search is not None and not isinstance(target_search, TargetSearchPolicy):
            raise ValueError("explicit target search configuration required")
        self.target_search = target_search
        self.target_client = target_client

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
                    if isinstance(change.evidence, ReferencedFactEvidence):
                        from .source_observation import SourceObservationService
                        try:
                            SourceObservationService(self).validate_reference(rule, change.evidence, connection=db)
                        except RecheckBlocked as exc:
                            raise FactResolutionError(exc.reason_code) from None
                    self._register_rule(cur, rule, target)
                    job = {"schema_version": "fact_source_job.v1", "policy_id": rule.policy_id,
                        "fact_key": rule.scope.key,
                        "policy_version": rule.version, "lane": lane, "change": change.model_dump(),
                        "initial_memory_id": target["memory_id"], "initial_revision": target["content_hash"]}
                    job["stale_target"] = target.get("_stale_target", False)
                    if target.get("_semantic_target"):
                        job["target_search"] = {"description": change.target_description,
                            "policy_version": self.target_search.version}
                    task = queue.register(project=batch.project, memory_id=target.get("_queue_memory_id", target["memory_id"]), request=job,
                        request_key=hash_payload([batch_id, change.change_id]))
                    if isinstance(change.evidence, ReferencedFactEvidence):
                        from .source_observation import SourceObservationService
                        SourceObservationService(self).bind_task(db, task["task_id"], change.evidence)
                    results.append({"change_id": change.change_id, "status": "queued", "task_id": task["task_id"]})
                except FactResolutionError as exc:
                    results.append({"change_id": change.change_id, "status": "rejected", "reason_code": exc.reason_code})
            result = {"schema_version": "brain_object_correction.v2", "batch_id": batch_id,
                "duplicate": False, "authority_write_performed": False, "changes": results}
            cur.execute("INSERT INTO lbrain_fact_batches(batch_id,project,request_hash,result) VALUES (%s,%s,%s,%s)",
                (batch_id, batch.project, request_hash, Jsonb(result)))
            return result

    def _target(self, cur, rule, change, *, allow_search=True, anchor_only=False):
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
        if not rows and change.target_description and self.target_search is not None and allow_search:
            # source 정책의 기존 대상에 검토를 예약한다. 의미 일치가 확인되기
            # 전에는 이 anchor를 실제로 선택한 것으로 취급하거나 값을 쓰지 않는다.
            anchored = self._target(cur, rule,
                change.model_copy(update={"target_description": rule.target_title}), allow_search=False, anchor_only=True)
            return {**anchored, "_semantic_target": True}
        if len(rows) != 1 or not rows[0]["steward_envelope"]:
            raise FactResolutionError("target_ambiguous" if rows else "target_not_found")
        card = rows[0]["steward_envelope"]
        if card.get("authorization_status") != "active" or card.get("title") != rule.target_title:
            raise FactResolutionError("target_scope_mismatch")
        self._check_binding(cur, rule, card)
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
        if not anchor_only and card.get("typed_payload", {}).get(rule.field_path.split(".", 1)[1]) != change.old_value:
            raise FactResolutionError("target_value_conflict")
        return card

    @staticmethod
    def _check_binding(cur, rule, card):
        cur.execute("SELECT fact_key FROM lbrain_fact_members WHERE memory_id=%s AND field_path=%s",
            (card["memory_id"], rule.field_path))
        bound = cur.fetchone()
        if bound and bound["fact_key"] != rule.scope.key:
            raise FactResolutionError("target_scope_mismatch")

    def _register_rule(self, cur, rule, card):
        self._check_binding(cur, rule, card)
        # 실행 중인 옛 worker가 새 설정을 덮어쓰지 않는다. 설정 변경/철회는
        # 별도 관리 작업에서 CAS한다. 읽기 전용 constructor는 DB를 바꾸지 않는다.
        cur.execute("""INSERT INTO lbrain_fact_admission_rules
            (project,policy_id,policy_version,fact_key,target_memory_id,target_title,card_type,field_path)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (rule.scope.project, rule.policy_id, rule.version, rule.scope.key,
                card["memory_id"], rule.target_title, card["card_type"], rule.field_path))
        if not self._registered_rule(cur, rule, card_type=card["card_type"]):
            raise FactResolutionError("fact_policy_changed")
        cur.execute("SELECT 1 FROM lbrain_fact_scopes WHERE fact_key=%s AND project=%s", (rule.scope.key, rule.scope.project))
        if cur.fetchone():
            # 서버가 같은 사실로 등록한 기존 alias도 즉시 공통 head에 연결한다.
            # 근거 미확인 상태에서 새 값을 승인하는 작업과는 별개다.
            self.resolver._bind(cur, card["memory_id"], rule.field_path, rule.scope.key, rule.render_template)
            if cur.rowcount:
                cur.execute("""INSERT INTO lbrain_fact_project_versions(project,generation) VALUES (%s,1)
                    ON CONFLICT(project) DO UPDATE SET generation=lbrain_fact_project_versions.generation+1""",
                    (rule.scope.project,))

    @staticmethod
    def _registered_rule(cur, rule, *, card_type=None):
        cur.execute("SELECT * FROM lbrain_fact_admission_rules WHERE project=%s AND policy_id=%s",
            (rule.scope.project, rule.policy_id))
        row = cur.fetchone()
        return bool(row and row["active"] and row["policy_version"] == rule.version
            and row["fact_key"] == rule.scope.key and row["target_title"] == rule.target_title
            and row["field_path"] == rule.field_path and (card_type is None or row["card_type"] == card_type))

    def worker(self, *, project, worker_id, retry_seconds=60):
        semantic = any(isinstance(rule, GitSemanticFactPolicy) and rule.scope.project == project
            for rule in self.policies.values())
        return RecheckWorker(queue=self.queue, project=project, worker_id=worker_id,
            load=self._load, judge=self._judge,
            apply=self._apply, retry_seconds=retry_seconds,
            lease_seconds=600 if self.target_search is not None else 180 if semantic else 60)

    def _semantic_reader(self, rule):
        from .correction_capabilities import EvidenceRequirement, GitRepositoryReader, ObservedCapability
        reader = GitRepositoryReader(self.git_reader, scope=rule.scope, relative_path=rule.relative_path)
        return reader, EvidenceRequirement(ObservedCapability(rule.required_capability),
            rule.scope.context_hash, reader.resource_hash)

    def _judge(self, task, snapshot):
        if "recovered_result" in snapshot.data:
            return {"recovered_result": snapshot.data["recovered_result"]}
        if "target_inventory" in snapshot.data:
            from .correction_target import load_profile, select_target
            from .correction_capabilities import CapabilityUnavailable
            from .correction_judgment import JudgmentUnavailable
            from .correction_target_identity import CorrectionTargetClient
            import os
            try:
                policy, _ = load_profile(self.target_search)
                client = self.target_client or CorrectionTargetClient(
                    api_key=os.environ.get("OPENROUTER_API_KEY", ""))
                selection = select_target(snapshot.data["target_inventory"], client=client,
                    policy=policy, policy_version=self.target_search.version)
                return {"schema_version": "fact_target_judgment.v1", "target_selection": selection,
                    "target_inventory": snapshot.data["target_inventory"],
                    "source_selection": self._judge_source(task, snapshot) if selection["memory_id"] else None}
            except (CapabilityUnavailable, JudgmentUnavailable) as exc:
                raise RecheckBlocked(exc.reason_code) from None
        return self._judge_source(task, snapshot)

    def _judge_source(self, task, snapshot):
        rule = self.policies[task["request"]["policy_id"]]
        if not isinstance(rule, GitSemanticFactPolicy):
            return {"direct_source_mapping": True}
        from .correction_capabilities import CapabilityUnavailable
        from .correction_judgment import JudgmentUnavailable
        from .correction_relation import CorrectionRelationClient
        from .correction_semantic import load_validated_profile, select_value
        import os
        try:
            profile = load_validated_profile(rule.validation_profile_path, expected_hash=rule.validation_profile_hash)
            reader, requirement = self._semantic_reader(rule)
            # 인증 객체 생성 전에도 source capability를 검사한다. credential 부재가
            # 다른 범위의 source를 허용하는 이유나 모델 fallback의 이유가 되지 않는다.
            if not reader.read().satisfies(requirement):
                raise CapabilityUnavailable("source_capability_or_scope_mismatch")
            client = self.relation_client or CorrectionRelationClient(api_key=os.environ.get("OPENROUTER_API_KEY", ""))
            return select_value(reader=reader, requirement=requirement, scope_description=rule.scope_description,
                claims=rule.claims, policy=profile.policy, policy_version=rule.version, client=client,
                expected_source_binding=snapshot.data["semantic_source_binding"])
        except (CapabilityUnavailable, JudgmentUnavailable) as exc:
            raise RecheckBlocked(exc.reason_code) from None

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
                self._register_rule(cur, rule, card)
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
        native = None
        if task["request"].get("native_source_read"):
            from .source_observation import NativeSourceObservationBridge
            native = NativeSourceObservationBridge(self, self.native_catalog).read_native(task)
        if task["result_ref"]:
            with self.store._scope() as db, db.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT terminal,result FROM lbrain_fact_resolutions WHERE resolution_id=%s", (task["result_ref"],))
                row = cur.fetchone()
                if row and row["terminal"]:
                    return RecheckSnapshot.from_data(task, {"recovered_result": task["result_ref"]})
                cur.execute("""SELECT 1 FROM lbrain_fact_target_decisions
                    WHERE decision_id=%s AND task_id=%s AND request_hash=%s""",
                    (task["result_ref"], task["task_id"], task["request_hash"]))
                if cur.fetchone():
                    return RecheckSnapshot.from_data(task, {"recovered_result": task["result_ref"]})
        if native:
            from .source_observation import SourceObservationService
            previous = SourceObservationService(self).previous_resolution(
                task["request"]["change"]["evidence"]["observation_ref"])
            if previous:
                return RecheckSnapshot.from_data(task, {"native_source_read": native, "recovered_result": previous})
        request = task["request"]
        rule = self.policies.get(request["policy_id"])
        if not rule or rule.version != request["policy_version"]:
            raise RecheckBlocked("fact_policy_changed")
        with self.store._scope() as db, db.cursor(row_factory=dict_row) as cur:
            if not self._registered_rule(cur, rule):
                raise RecheckBlocked("fact_policy_changed")
        try:
            descriptor = request["change"]["evidence"]
            if request["lane"] == "intake" and descriptor == {"mode": "server_current"}:
                source = self.git_reader.read_current(repository=rule.scope.repository, relative_path=rule.relative_path)
            else:
                kind = ReferencedFactEvidence if "observation_ref" in descriptor else FactEvidence
                evidence = kind.model_validate(descriptor)
                if isinstance(evidence, ReferencedFactEvidence):
                    from .source_observation import SourceObservationService
                    SourceObservationService(self).validate_reference(rule, evidence)
                source = self.git_reader.read(repository=rule.scope.repository, revision=evidence.revision,
                    relative_path=rule.relative_path, expected_content_hash=evidence.content_hash)
            if isinstance(rule, GitSemanticFactPolicy) and not source.current_verified:
                # 지연 입력은 trigger로 남기고 등록된 현재 source를 독립 재조회한다.
                # 오래된 본문을 현재 관측으로 재인증하지 않는다.
                self._save_receipt(rule, source)
                source = self.git_reader.read_current(repository=rule.scope.repository, relative_path=rule.relative_path)
        except SourceUnavailable as exc:
            raise RecheckBlocked(exc.reason_code) from None
        # JSON 내용을 바꾼 redaction 결과를 원본의 명시적 값으로 취급하지 않는다.
        if source.content_hash != source.redacted_content_hash:
            raise RecheckBlocked("source_redaction_changed_content")
        semantic_binding = None
        value = None
        if isinstance(rule, GitSemanticFactPolicy):
            from .correction_capabilities import CapabilityUnavailable
            from .correction_semantic import load_validated_profile
            try:
                load_validated_profile(rule.validation_profile_path, expected_hash=rule.validation_profile_hash)
                reader, requirement = self._semantic_reader(rule)
                observed = reader.read()
                if not observed.satisfies(requirement):
                    raise CapabilityUnavailable("source_capability_or_scope_mismatch")
                # _load의 receipt와 모델이 읽은 source가 다르면 다음 예약에서 재조회한다.
                expected_proof = source.public_proof()
                expected_proof.pop("observed_at")
                if observed.value != source.redacted_text or observed.revision_hash != hash_payload(expected_proof):
                    raise CapabilityUnavailable("source_read_changed")
                semantic_binding = hash_payload(asdict(observed))
            except CapabilityUnavailable as exc:
                raise RecheckBlocked(exc.reason_code) from None
        else:
            try:
                document = json.loads(source.redacted_text, object_pairs_hook=_unique_object)
                value = _pointer(document, rule.json_pointer)
                if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 2048:
                    raise ValueError("explicit string fact required")
            except (ValueError, TypeError, KeyError):
                raise RecheckBlocked("source_fact_unverifiable") from None
            ensure_public_safe(value, "SourceFact")
        proof = self._save_receipt(rule, source)
        source_field = "semantic_claims" if isinstance(rule, GitSemanticFactPolicy) else rule.json_pointer
        interpretation = "git_semantic.v1:" if isinstance(rule, GitSemanticFactPolicy) else "git_json.v1:"
        observation = VerifiedObservation(observation_id=hash_payload([rule.scope.context_hash, source.content_identity]),
            project=rule.scope.project, source_id=hash_payload([rule.scope.context_hash, source.relative_path_hash, source_field]),
            source_revision=source.revision, content_hash=source.content_hash, context_hash=rule.scope.context_hash,
            interpretation_version=interpretation + rule.version[7:23], proof=proof,
            current_verified=source.current_verified, observed_at=source.observed_at)
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute("""INSERT INTO lbrain_fact_observations
                (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(version_id) DO NOTHING""",
                (observation.version_id, observation.observation_id, observation.project, observation.source_id,
                    observation.source_revision, observation.interpretation_version, Jsonb(asdict(observation))))
            cur.execute("SELECT observation FROM lbrain_fact_observations WHERE version_id=%s", (observation.version_id,))
            observation = VerifiedObservation(**cur.fetchone()["observation"])
        # source 본문은 immutable receipt에만 두고 snapshot/cache에 복제하지 않는다.
        data = {"value": value, "observation": asdict(observation)}
        if semantic_binding:
            data["semantic_source_binding"] = semantic_binding
        if native:
            data["native_source_read"] = native
        if request.get("target_search"):
            from .correction_target import PgTargetInventory, load_profile
            from .correction_capabilities import CapabilityUnavailable
            try:
                if self.target_search is None or request["target_search"]["policy_version"] != self.target_search.version:
                    raise CapabilityUnavailable("target_search_policy_changed")
                load_profile(self.target_search)
                data["target_inventory"] = PgTargetInventory(self).read(rule, request["target_search"]["description"])
            except CapabilityUnavailable as exc:
                raise RecheckBlocked(exc.reason_code) from None
        return RecheckSnapshot.from_data(task, data)

    def _save_receipt(self, rule, source):
        if source.content_hash != source.redacted_content_hash:
            raise RecheckBlocked("source_redaction_changed_content")
        proof = source.public_proof()
        proof.pop("observed_at")
        proof["policy_version"] = rule.version
        proof["source_ref_id"] = "src_fact_" + hash_payload(proof)[7:47]
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
        return proof

    @staticmethod
    def _wake_competing_check(db, rule, policy_version=None):
        # 다른 worker의 queue row를 authority 잠금 안에서 기다리지 않는다.
        # 내구성 일정만 앞당기고 기존 dispatcher가 queue에 연결한다.
        db.execute("""INSERT INTO lbrain_fact_check_schedules(fact_key,project,policy_id,next_check_at,reason_code)
            SELECT fact_key,project,policy_id,clock_timestamp(),'competing_source_recheck_required'
            FROM lbrain_fact_admission_rules WHERE project=%s AND fact_key=%s
                AND ((%s::text IS NULL AND policy_id=%s) OR policy_version=%s)
            ON CONFLICT(fact_key,policy_id) DO UPDATE SET
                next_check_at=LEAST(lbrain_fact_check_schedules.next_check_at,clock_timestamp()),
                reason_code='competing_source_recheck_required'
            WHERE lbrain_fact_check_schedules.state IN ('scheduled','blocked')""",
            (rule.scope.project, rule.scope.key, policy_version, rule.policy_id, policy_version))

    def _source_coverage(self, rule, observation, db):
        """반대 값뿐 아니라 같은 값의 source가 달라졌는지도 확인한다.

        새 본문의 값 선택은 이 잠금 안에서 하지 않는다. 선택할 정보가
        부족하면 incoming assertion만 pending으로 남기고 해당 source를 예약한다.
        """
        with db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT * FROM lbrain_fact_admission_rules WHERE project=%s AND fact_key=%s
                AND policy_id<>%s AND active ORDER BY policy_id LIMIT 257""",
                (rule.scope.project, rule.scope.key, rule.policy_id))
            others = cur.fetchall()
            if len(others)>256:
                return False
            for registration in others:
                other = self.policies.get(registration["policy_id"])
                if not other or not self._registered_rule(cur, other):
                    self._wake_competing_check(db, rule, policy_version=registration["policy_version"])
                    return False
                try:
                    if isinstance(other, GitSemanticFactPolicy):
                        from .correction_semantic import load_validated_profile
                        load_validated_profile(other.validation_profile_path, expected_hash=other.validation_profile_hash)
                    current = self.git_reader.read_current(repository=other.scope.repository, relative_path=other.relative_path)
                except (SourceUnavailable, ValueError):
                    self._wake_competing_check(db, other)
                    return False
                if (current.revision != observation.source_revision
                        or current.branch_ref_hash != observation.proof.get("branch_ref_hash")
                        or current.content_hash != current.redacted_content_hash):
                    self._wake_competing_check(db, other)
                    return False
                field = "semantic_claims" if isinstance(other, GitSemanticFactPolicy) else other.json_pointer
                source_id = hash_payload([other.scope.context_hash, current.relative_path_hash, field])
                interpretation = ("git_semantic.v1:" if isinstance(other, GitSemanticFactPolicy) else "git_json.v1:") + other.version[7:23]
                cur.execute("""SELECT 1 FROM lbrain_fact_assertions a
                    JOIN lbrain_fact_observations o ON o.version_id=a.observation_version
                    JOIN lbrain_fact_observation_sources s ON s.observation_version=o.version_id
                    JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
                    WHERE a.fact_key=%s AND o.source_id=%s AND o.interpretation_version=%s
                        AND o.observation->>'content_hash'=%s AND o.observation#>>'{proof,blob_id}'=%s
                        AND o.observation#>>'{proof,branch_ref_hash}'=%s
                        AND o.observation#>>'{proof,policy_version}'=%s AND o.observation->>'current_verified'='true'
                        AND a.state IN ('support','pending') AND r.state='active' LIMIT 1""",
                    (rule.scope.key, source_id, interpretation, current.content_hash,
                        current.blob_id, current.branch_ref_hash, other.version))
                if not cur.fetchone():
                    self._wake_competing_check(db, other)
                    return False
        return True

    def _current(self, rule, observation, db, incoming=None):
        admitted_rule = next((item for item in self.policies.values()
            if item.version == observation.proof.get("policy_version") and item.scope.key == rule.scope.key), None)
        if admitted_rule is None:
            self._wake_competing_check(db, rule, policy_version=observation.proof.get("policy_version"))
            return None
        rule = admitted_rule
        if isinstance(rule, GitSemanticFactPolicy):
            from .correction_capabilities import CapabilityUnavailable
            from .correction_semantic import load_validated_profile
            try:
                load_validated_profile(rule.validation_profile_path, expected_hash=rule.validation_profile_hash)
            except CapabilityUnavailable:
                self._wake_competing_check(db, rule)
                return None
        with db.cursor(row_factory=dict_row) as cur:
            if not self._registered_rule(cur, rule):
                self._wake_competing_check(db, rule)
                return None
            cur.execute("SELECT state FROM lbrain_fact_source_receipts WHERE source_ref_id=%s",
                (observation.proof["source_ref_id"],))
            receipt = cur.fetchone()
        if not receipt:
            return None
        if receipt["state"] != "active":
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
            pass
        if incoming and observation.version_id == incoming.version_id:
            # incoming은 이번 판단이 읽은 고정 HEAD를 유지한다. 다른 파일의
            # commit이 판단 도중 생겨도 전체 source 확인을 새 snapshot에서 재시도한다.
            self._wake_competing_check(db, rule)
            return None
        try:
            current = self.git_reader.read_current(repository=rule.scope.repository, relative_path=rule.relative_path)
        except SourceUnavailable:
            self._wake_competing_check(db, rule)
            return None
        if (current.branch_ref_hash == source.branch_ref_hash and current.blob_id == source.blob_id
                and current.relative_path_hash == source.relative_path_hash
                and current.content_hash == source.content_hash
                and current.redacted_content_hash == source.redacted_content_hash):
            return True
        if (incoming and observation.source_id == incoming.source_id
                and observation.interpretation_version == incoming.interpretation_version
                and incoming.content_hash == current.content_hash
                and incoming.proof.get("blob_id") == current.blob_id
                and incoming.proof.get("branch_ref_hash") == current.branch_ref_hash):
            return False
        with db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT 1 FROM lbrain_fact_assertions a
                JOIN lbrain_fact_observations o ON o.version_id=a.observation_version
                JOIN lbrain_fact_observation_sources s ON s.observation_version=o.version_id
                JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
                WHERE a.fact_key=%s AND o.source_id=%s AND o.interpretation_version=%s
                    AND o.observation->>'content_hash'=%s AND o.observation#>>'{proof,blob_id}'=%s
                    AND o.observation#>>'{proof,branch_ref_hash}'=%s
                    AND o.observation#>>'{proof,policy_version}'=%s
                    AND a.state IN ('support','pending') AND r.state='active'
                    AND o.observation->>'current_verified'='true' LIMIT 1""",
                (rule.scope.key, observation.source_id, observation.interpretation_version,
                    current.content_hash, current.blob_id, current.branch_ref_hash, rule.version))
            if cur.fetchone():
                return False
        # 상대 source가 바뀌었다는 사실만으로 상대의 현재 값이 무엇인지
        # 알 수 없다. 새 해석을 authority transaction 밖에서 재검토한다.
        self._wake_competing_check(db, rule)
        return None

    def _apply(self, task, snapshot, judgment):
        if "recovered_result" in snapshot.data:
            if snapshot.data.get("native_source_read"):
                from .source_observation import NativeSourceObservationBridge
                with self.queue.owned_transaction(task) as db:
                    NativeSourceObservationBridge(self, self.native_catalog).attach_on(
                        db, task, snapshot.data["native_source_read"], snapshot.data["recovered_result"],
                        join_only=True, authority_write_performed=False)
                    self.queue.checkpoint_result(task, snapshot.data["recovered_result"], connection=db)
            return RecheckOutcome("resolved", "fact_result_recovered", snapshot.data["recovered_result"])
        from .postgres_store.pgvector_store import PgVectorStore
        rule = self.policies[task["request"]["policy_id"]]
        target_admission = None
        if "target_inventory" in snapshot.data:
            from .correction_target import load_profile, verify_selection
            from .correction_capabilities import CapabilityUnavailable
            try:
                if self.target_search is None or task["request"]["target_search"]["policy_version"] != self.target_search.version:
                    raise CapabilityUnavailable("target_search_policy_changed")
                policy, _ = load_profile(self.target_search)
                selected = verify_selection(snapshot.data["target_inventory"], judgment["target_selection"],
                    policy=policy, policy_version=self.target_search.version)
                if selected is None:
                    reason = judgment["target_selection"]["reason_code"]
                    if reason == "target_not_found":
                        return self._reject_target(task, snapshot, judgment["target_selection"])
                    return RecheckOutcome("waiting", reason)
                candidate = next(row for row in snapshot.data["target_inventory"]["candidates"] if row["memory_id"] == selected)
                if not ((candidate["fact_key"] == rule.scope.key) or
                        (candidate["fact_key"] is None and selected == task["request"]["initial_memory_id"]
                         and candidate["title"] == rule.target_title)):
                    raise CapabilityUnavailable("target_source_scope_mismatch")
                target_admission = {"profile_hash": self.target_search.validation_profile_hash,
                    "selection_hash": hash_payload(judgment["target_selection"]),
                    "inventory_hash": hash_payload(snapshot.data["target_inventory"]), "memory_id": selected}
                judgment = judgment["source_selection"]
            except CapabilityUnavailable as exc:
                raise RecheckBlocked(exc.reason_code) from None
        observation = VerifiedObservation(**snapshot.data["observation"])
        value, admission = snapshot.data["value"], None
        if isinstance(rule, GitSemanticFactPolicy):
            from .correction_capabilities import CapabilityUnavailable
            from .correction_semantic import load_validated_profile, verify_selection
            try:
                profile = load_validated_profile(rule.validation_profile_path, expected_hash=rule.validation_profile_hash)
                reader, requirement = self._semantic_reader(rule)
                observed = reader.read()
                if hash_payload(asdict(observed)) != snapshot.data["semantic_source_binding"]:
                    raise CapabilityUnavailable("source_read_changed")
                value = verify_selection(observed=observed, requirement=requirement,
                    scope_description=rule.scope_description, claims=rule.claims, policy=profile.policy,
                    policy_version=rule.version, selection=judgment)
                reader.assert_still_current(observed)
            except CapabilityUnavailable as exc:
                raise RecheckBlocked(exc.reason_code) from None
            if value is None:
                return RecheckOutcome("waiting", judgment["reason_code"])
            admission = {"kind": "semantic", "profile_hash": profile.artifact_hash,
                "judgment_hash": hash_payload(judgment), "task_id": task["task_id"], "input_hash": snapshot.fingerprint}
        if target_admission is not None:
            admission = {**(admission or {"kind": "direct_source"}), "target_search": target_admission}
        with self.queue.owned_transaction(task) as db, db.cursor(row_factory=dict_row) as cur:
            bound = PgVectorStore(connection=db)
            resolver = FactResolutionService(bound)
            bound._lock_steward_authority_on(db)
            if target_admission is not None:
                from .correction_target import PgTargetInventory
                from .correction_capabilities import CapabilityUnavailable
                try:
                    load_profile(self.target_search)
                    current_inventory = PgTargetInventory(self).read(rule, task["request"]["target_search"]["description"], connection=db)
                except CapabilityUnavailable as exc:
                    raise RecheckBlocked(exc.reason_code) from None
                if hash_payload(current_inventory) != target_admission["inventory_hash"]:
                    raise RecheckBlocked("target_inventory_changed")
            cur.execute("SELECT memory_id FROM lbrain_fact_scopes WHERE fact_key=%s", (rule.scope.key,))
            head = cur.fetchone()
            target = bound.get_steward_card(head["memory_id"] if head else task["memory_id"])
            if not target or target.get("currentness") != "current":
                raise RecheckBlocked("fact_target_not_current")
            view = resolver.project_cards([target], connection=db)[0]
            if (task["request"].get("stale_target") and value !=
                    view.get("typed_payload", {}).get(rule.field_path.split(".", 1)[1])):
                raise RecheckBlocked("target_revision_conflict")
            try:
                source_complete = self._source_coverage(rule, observation, db)
                result = resolver.reconcile(scope=rule.scope, memory_id=view["memory_id"],
                    field_path=rule.field_path, render_template=rule.render_template,
                    expected_content_hash=view["content_hash"], value=value, observation=observation,
                    request_key=task["task_id"], request_hash=task["request_hash"],
                    requested_value=task["request"]["change"]["proposed_value"],
                    is_current=lambda obs: self._current(rule, obs, db, observation),
                    compare=lambda old, incoming: self.git_reader.compare_revisions(repository=rule.scope.repository,
                        previous=old.source_revision, incoming=incoming.source_revision),
                    admission=admission,
                    source_coverage_complete=source_complete,
                    commit=StewardFactCommit(field_path=rule.field_path, render_template=rule.render_template,
                        policy_id=rule.policy_id, max_age_seconds=rule.max_age_seconds))
            except FactResolutionError as exc:
                raise RecheckBlocked(exc.reason_code) from None
            if result["action"] in {"disputed", "pending_evidence"}:
                return RecheckOutcome("waiting", result["action"])
            if target_admission is not None:
                self._save_target_on(db, task, snapshot, {"status": "verified", "reason_code": "target_verified",
                    "authority_write_performed": False,
                    "project": task["project"], "profile_hash": self.target_search.validation_profile_hash,
                    **target_admission}, resolution_id=result["resolution_id"])
            if snapshot.data.get("native_source_read"):
                from .source_observation import NativeSourceObservationBridge
                NativeSourceObservationBridge(self, self.native_catalog).attach_on(
                    db, task, snapshot.data["native_source_read"], result["resolution_id"],
                    join_only=False, authority_write_performed=result["authority_write_performed"])
            self.queue.checkpoint_result(task, result["resolution_id"], connection=db)
            return RecheckOutcome("resolved", result["action"], result["resolution_id"])

    def _reject_target(self, task, snapshot, selection):
        from .correction_target import PgTargetInventory, load_profile
        from .correction_capabilities import CapabilityUnavailable
        rule = self.policies[task["request"]["policy_id"]]
        with self.queue.owned_transaction(task) as db:
            self.store._lock_steward_authority_on(db)
            try:
                load_profile(self.target_search)
                current = PgTargetInventory(self).read(rule, task["request"]["target_search"]["description"], connection=db)
            except CapabilityUnavailable as exc:
                raise RecheckBlocked(exc.reason_code) from None
            if hash_payload(current) != hash_payload(snapshot.data["target_inventory"]):
                raise RecheckBlocked("target_inventory_changed")
            result = {"status": "rejected", "reason_code": "target_not_found", "search_complete": True,
                "authority_write_performed": False, "project": task["project"],
                "inventory_hash": hash_payload(current), "selection_hash": hash_payload(selection),
                "profile_hash": self.target_search.validation_profile_hash}
            decision_id = self._save_target_on(db, task, snapshot, result)
            self.queue.checkpoint_result(task, decision_id, connection=db)
        return RecheckOutcome("resolved", "target_not_found", decision_id)

    @staticmethod
    def _save_target_on(db, task, snapshot, result, *, resolution_id=None):
        decision_id = "targetdec_" + hash_payload([task["task_id"], snapshot.fingerprint])[7:47]
        db.execute("""INSERT INTO lbrain_fact_target_decisions
            (decision_id,task_id,request_hash,input_hash,resolution_id,snapshot,result)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (decision_id, task["task_id"], task["request_hash"], snapshot.fingerprint, resolution_id,
                Jsonb(snapshot.data["target_inventory"]), Jsonb(result)))
        saved = db.execute("""SELECT decision_id,request_hash,input_hash,resolution_id,snapshot,result
            FROM lbrain_fact_target_decisions WHERE task_id=%s""", (task["task_id"],)).fetchone()
        if not saved or tuple(saved.values()) != (decision_id, task["request_hash"], snapshot.fingerprint,
                resolution_id, snapshot.data["target_inventory"], result):
            raise RecheckBlocked("target_decision_conflict")
        return decision_id

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
                cur.execute("SELECT result FROM lbrain_fact_target_decisions WHERE decision_id=%s AND task_id=%s",
                    (resolution_ref, task_id))
                target_decision = cur.fetchone()
                if target_decision:
                    result["target_resolution"] = target_decision["result"]
                cur.execute("SELECT result FROM lbrain_fact_resolutions WHERE resolution_id=%s", (resolution_ref,))
                row = cur.fetchone()
                if row:
                    receipt = row["result"]
                    receipt["decision_time_current"] = receipt.pop("current", None)
                    receipt["current"] = self.resolver._head(cur, receipt["fact_key"])
                    if task["request"].get("native_source_read"):
                        cur.execute("""SELECT join_only,authority_write_performed,observation_ref
                            FROM lbrain_fact_native_read_links WHERE task_id=%s""", (task_id,))
                        joined = cur.fetchone()
                        if joined:
                            receipt["native_source_read"] = joined
                            receipt["authority_write_performed"] = joined["authority_write_performed"]
                    result["resolution"] = receipt
                    cur.execute("SELECT generation FROM lbrain_fact_project_versions WHERE project=%s", (task["project"],))
                    latest = cur.fetchone()
                    result["consistency_token"] = {"project": task["project"],
                        "min_fact_generation": max(receipt["project_generation"], latest["generation"] if latest else 0)}
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


def load_configuration(path, *, with_target_search=False):
    """운영 경로를 출력하지 않고 서버가 소유한 adapter 설정만 읽는다."""
    from pathlib import Path
    from .correction_sources import GitSourcePolicy
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError("bounded fact configuration required")
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(data, dict) or not {"schema_version", "repositories", "rules"} <= set(data)
            or set(data) - {"schema_version", "repositories", "rules", "target_search"}
            or data["schema_version"] != "fact_correction_config.v1"
            or not isinstance(data["repositories"], list) or not isinstance(data["rules"], list)
            or not 1 <= len(data["repositories"]) <= 20 or not 1 <= len(data["rules"]) <= 200):
        raise ValueError("explicit fact configuration required")
    repositories = [GitSourcePolicy(**item) for item in data["repositories"]]
    if len({item.repository for item in repositories}) != len(repositories):
        raise ValueError("duplicate source repository")
    reader = GitSourceReader({item.repository: item for item in repositories})
    rules = []
    for item in data["rules"]:
        fields = dict(item)
        adapter = fields.pop("adapter", "git_json.v1")
        kind = {"git_json.v1": GitJsonFactPolicy, "git_semantic.v1": GitSemanticFactPolicy}.get(adapter)
        if kind is None:
            raise ValueError("unknown fact source adapter")
        rules.append(kind(**{**fields, "scope": FactScope(**fields["scope"])}))
    from .correction_target import TargetSearchPolicy
    target_search = TargetSearchPolicy(**data["target_search"]) if "target_search" in data else None
    if with_target_search:
        return reader, rules, target_search
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
        reader, policies, target_search = load_configuration(args.config, with_target_search=True)
        coordinator = FactCorrectionCoordinator(store=PgVectorStore(dsn=dsn), git_reader=reader,
            policies=policies, target_search=target_search)
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

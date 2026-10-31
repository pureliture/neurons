"""교정·수집에 공통으로 사용하는 기존 사실의 resolution 코어.

admission은 server source adapter가 담당한다. 이 코어는 검증된 관측을
영속화하고, 실제 source 재검증과 PG authority 변경을 하나의 처리로 묶는다.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Any, Callable, Mapping

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.errors import DeadlockDetected, LockNotAvailable, SerializationFailure

from .public_safe_util import ensure_public_safe, hash_payload, require_sha256


# f는 호출 SQL의 lbrain_fact_scopes alias다. 마지막 승인 head가 pending인
# disputed 상태도 포함하므로, 우열 불명 충돌의 조회 정책을 바꾸지 않는다.
CURRENT_FACT_EVIDENCE_SQL = """EXISTS (
    SELECT 1 FROM lbrain_fact_assertions a
    JOIN lbrain_fact_observation_sources s ON s.observation_version=a.observation_version
    JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
    WHERE a.fact_key=f.fact_key AND a.value_hash=f.value_hash AND r.state='active'
        AND a.state IN ('support','pending')
        AND (a.state='support' OR a.assertion_id=f.head_assertion_id))"""


class FactResolutionError(ValueError):
    def __init__(self, reason_code: str, *, retryable: bool = False):
        self.reason_code = reason_code
        self.retryable = retryable
        super().__init__(reason_code)


@dataclass(frozen=True)
class FactScope:
    project: str
    repository: str
    branch: str
    environment: str
    subject: str
    predicate: str

    def __post_init__(self):
        for name, value in asdict(self).items():
            _text(value, 64 if name == "project" else 512)
        ensure_public_safe(asdict(self), "FactScope")

    @property
    def key(self):
        return "fact_" + hash_payload(asdict(self))[7:47]

    @property
    def context_hash(self):
        return hash_payload({key: value for key, value in asdict(self).items() if key not in {"subject", "predicate"}})


@dataclass(frozen=True)
class VerifiedObservation:
    """RPC 입력이 아니다. server admission 뒤 생성하는 source witness다.

    current_verified=False는 검증된 과거 snapshot이다. 아직 admission하지
    못한 입력을 이 타입으로 만들어 현재 사실 판단에 참여시키지 않는다.
    """

    observation_id: str
    project: str
    source_id: str
    source_revision: str
    content_hash: str
    context_hash: str
    interpretation_version: str
    proof: Mapping[str, Any]
    current_verified: bool
    observed_at: str = ""

    def __post_init__(self):
        for value in (self.observation_id, self.source_id, self.content_hash, self.context_hash):
            require_sha256(value, "observation digest")
        _text(self.project, 64); _text(self.source_revision, 180); _text(self.interpretation_version, 128)
        if type(self.current_verified) is not bool or not isinstance(self.proof, Mapping):
            raise ValueError("server observation witness required")
        if self.observed_at:
            stamp = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("timezone-aware observation required")
        object.__setattr__(self, "proof", copy.deepcopy(dict(self.proof)))
        ensure_public_safe(asdict(self), "VerifiedObservation")
        if len(str(self.proof).encode()) > 32768:
            raise ValueError("bounded source proof required")

    @property
    def version_id(self):
        return hash_payload({key: value for key, value in asdict(self).items()
            if key not in {"current_verified", "observed_at"}})


class FactResolutionService:
    def __init__(self, store):
        self.store = store

    def reconcile(self, **arguments):
        try:
            return self._reconcile(**arguments)
        except (DeadlockDetected, LockNotAvailable, SerializationFailure):
            # 짧은 authority lock 한도를 늘리지 않는다. 내구성 queue가 같은 요청을 재시도한다.
            raise FactResolutionError("authority_busy", retryable=True) from None

    def _reconcile(self, *, scope: FactScope, memory_id: str, field_path: str, render_template: str,
                  expected_content_hash: str, value: str, observation: VerifiedObservation,
                  request_key: str, request_hash: str, requested_value: str | None,
                  is_current: Callable[[VerifiedObservation], bool | None],
                  compare: Callable[[VerifiedObservation, VerifiedObservation], str],
                  commit: Callable[..., Mapping[str, Any]], admission: Mapping[str, Any] | None = None,
                  source_coverage_complete: bool = True) -> dict:
        """source 재검증 후 canonical commit과 resolution을 같은 PG tx에 보존한다.

        commit은 제공한 connection의 PgVectorStore로 steward_decision을 호출하고
        card/target/decision_id를 반환해야 한다. 외부 별도 connection 쓰기는 금지다.
        """
        _text(memory_id, 64); _text(request_key, 180); _text(value, 2048)
        _field(field_path); _template(render_template)
        require_sha256(expected_content_hash, "expected revision")
        require_sha256(request_hash, "request hash")
        if admission is not None:
            ensure_public_safe(admission, "FactAdmission")
            if len(str(admission).encode()) > 4096:
                raise ValueError("bounded admission receipt required")
        if observation.project != scope.project or observation.context_hash != scope.context_hash:
            raise FactResolutionError("observation_scope_mismatch")
        ensure_public_safe(value, "assertion value")
        if requested_value is not None:
            _text(requested_value, 2048); ensure_public_safe(requested_value, "requested value")
        resolution_id = "factres_" + hash_payload([scope.key, request_key])[7:47]
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self.store._lock_steward_authority_on(db)
            cur.execute("SELECT * FROM lbrain_fact_resolutions WHERE resolution_id=%s FOR UPDATE", (resolution_id,))
            previous = cur.fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise FactResolutionError("request_key_conflict")
                if previous["terminal"]:
                    result = copy.deepcopy(previous["result"])
                    result.update(duplicate=True, authority_write_performed=False, current=self._head(cur, scope.key))
                    return result
            card = self.store.get_steward_card(memory_id, conn=db)
            if not card or card["project"] != scope.project or card.get("authorization_status") != "active":
                raise FactResolutionError("target_not_current")
            original_memory_id = memory_id
            superseded_same_value = False
            if card.get("currentness") == "superseded":
                cur.execute("""SELECT f.* FROM lbrain_fact_members m JOIN lbrain_fact_scopes f
                    ON f.fact_key=m.fact_key WHERE m.memory_id=%s AND m.field_path=%s""", (memory_id, field_path))
                linked = cur.fetchone()
                if expected_content_hash != card["content_hash"]:
                    raise FactResolutionError("target_revision_conflict")
                if linked and linked["fact_key"] == scope.key and linked["value_hash"] == hash_payload(value):
                    superseded_same_value = True
                else:
                    # 다른 필드만 먼저 교정된 요청은 원래 지정 필드가 모든 후속 revision에서
                    # 그대로였는지 확인한 뒤 현재 card로 이어간다. 지정 필드가 바뀌었으면 CAS 실패다.
                    card = self._unchanged_successor(card, field_path, connection=db)
                    memory_id = card["memory_id"]
                    expected_content_hash = self.project_cards([card], connection=db)[0]["content_hash"]
            elif card.get("currentness") != "current":
                raise FactResolutionError("target_not_current")
            projected = self.project_cards([card], connection=db)[0]
            if not superseded_same_value and expected_content_hash != projected["content_hash"]:
                raise FactResolutionError("target_revision_conflict")
            prior_value = _value(card, field_path)
            if not isinstance(prior_value, str):
                raise FactResolutionError("target_field_unsupported")
            cur.execute("""INSERT INTO lbrain_fact_scopes
                (fact_key,project,scope,memory_id,field_path,value,value_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(fact_key) DO NOTHING""",
                (scope.key, scope.project, Jsonb(asdict(scope)), memory_id, field_path, Jsonb(prior_value), hash_payload(prior_value)))
            head = self._head(cur, scope.key, locked=True)
            if head["scope"] != asdict(scope):
                raise FactResolutionError("fact_scope_conflict")
            self._bind(cur, memory_id, field_path, scope.key, render_template)
            if memory_id != original_memory_id:
                self._bind(cur, original_memory_id, field_path, scope.key, render_template)
            self._observation(cur, observation)
            assertion_id = "assert_" + hash_payload([scope.key, observation.version_id, hash_payload(value)])[7:47]
            cur.execute("""INSERT INTO lbrain_fact_assertions
                (assertion_id,fact_key,observation_version,value,value_hash,state)
                VALUES (%s,%s,%s,%s,%s,'pending') ON CONFLICT(assertion_id) DO NOTHING""",
                (assertion_id, scope.key, observation.version_id, Jsonb(value), hash_payload(value)))
            cur.execute("SELECT state FROM lbrain_fact_assertions WHERE assertion_id=%s", (assertion_id,))
            assertion_state = cur.fetchone()["state"]
            source_active = self._source_active(cur, observation.version_id)
            old_observation = self._head_observation(cur, head)
            # history는 과거의 선택 위치다. 실제 current source가 그 고정본으로
            # 돌아왔음을 독립 재검증하면 새 resolution에서 다시 사용할 수 있다.
            # 철회는 별개이며 source tombstone/현재성 검사는 우회하지 않는다.
            checked = is_current(observation) if (assertion_state != "revoked" and source_active
                and observation.current_verified) else False
            current, unknown = checked is True, checked is None
            source_was_current = current
            if current:
                # 같은 source/해석의 새 현재 고정본이 확인됐으면 이전의 다른
                # 본문은 현재 근거가 아니다. 시각이나 도착 순서로 고르지 않는다.
                cur.execute("""UPDATE lbrain_fact_assertions a SET state='history'
                    FROM lbrain_fact_observations o WHERE o.version_id=a.observation_version
                        AND a.fact_key=%s AND o.source_id=%s AND o.interpretation_version=%s
                        AND o.observation->>'content_hash'<>%s AND a.state IN ('support','pending')""",
                    (scope.key, observation.source_id, observation.interpretation_version, observation.content_hash))
            competing, incomplete = self._competing_current(cur, scope.key, hash_payload(value), is_current) if current else (False, False)
            # source가 재검증 사이에 바뀌면 해당 관측으로 새 authority를 만들지 않는다.
            if current:
                checked = is_current(observation)
                current, unknown = checked is True, checked is None
            relation = "unknown"
            if old_observation and old_observation.source_id == observation.source_id:
                relation = compare(old_observation, observation)
                if relation not in {"same", "incoming_before", "incoming_after", "incomparable", "unknown"}:
                    raise FactResolutionError("invalid_source_order")
            if not observation.current_verified:
                # 과거 입력의 처리 완료는 새 current 승인이나 검증 기한 연장이
                # 아니다. 최신 source 재검토와 별개로 history 합류를 끝낼 수 있다.
                action = "retain_history"
            elif incomplete or unknown or not source_coverage_complete:
                action = "pending_evidence"
            elif current and competing:
                action = "disputed"
            elif head["value_hash"] == hash_payload(value):
                action = "attach_evidence" if current else "retain_history"
            elif not source_active:
                action = "retain_history"
            elif not current:
                action = "retain_history" if relation == "incoming_before" else "pending_evidence"
            else:
                # 실제 현재 source가 옛 hash로 돌아온 경우도 독립 재검증 후 갱신한다.
                action = "replace_current"
            result = {"schema_version": "fact_resolution.v1", "resolution_id": resolution_id,
                "fact_key": scope.key, "action": action, "requested_value": requested_value,
                "applied_value": head["value"], "authority_write_performed": False,
                "observation_id": observation.observation_id, "observation_version": observation.version_id,
                "assertion_id": assertion_id, "duplicate": False, "generation": head["generation"]}
            if admission is not None:
                result["admission"] = copy.deepcopy(dict(admission))
            if action == "replace_current":
                base = self.store.get_steward_card(head["memory_id"], conn=db)
                if not base or base.get("currentness") != "current":
                    raise FactResolutionError("fact_head_not_current")
                raw_revision = base["content_hash"]
                base = self.project_cards([base], connection=db)[0]
                base.pop("fact_views", None)
                base.pop("canonical_content_hash", None)
                base["content_hash"] = raw_revision
                db.execute("SELECT set_config('neurons.fact_resolution_id',%s,true)", (resolution_id,))
                changed = dict(commit(connection=db, current=base, value=value,
                    observation=observation, resolution_id=resolution_id))
                if not is_current(observation):
                    raise FactResolutionError("source_changed_during_commit")
                successor = changed.get("card")
                if (not isinstance(successor, Mapping) or successor.get("project") != scope.project
                        or successor.get("currentness") != "current" or _value(successor, head["field_path"]) != value):
                    raise FactResolutionError("canonical_commit_unverified")
                stored = self.store.get_steward_card(successor["memory_id"], conn=db)
                if not stored or stored["content_hash"] != successor["content_hash"] or stored.get("authorization_status") != "active":
                    raise FactResolutionError("canonical_commit_unverified")
                # 같은 card의 변경하지 않은 사실 binding도 successor에 연결한다.
                cur.execute("""SELECT field_path,fact_key,render_template FROM lbrain_fact_members
                    WHERE memory_id=%s""", (head["memory_id"],))
                members = cur.fetchall()
                for member in members:
                    self._bind(cur, successor["memory_id"], member["field_path"], member["fact_key"], member["render_template"])
                    if member["fact_key"] != scope.key:
                        cur.execute("""UPDATE lbrain_fact_scopes SET memory_id=%s,updated_at=clock_timestamp()
                            WHERE fact_key=%s AND memory_id=%s AND value_hash=%s""",
                            (successor["memory_id"], member["fact_key"], head["memory_id"], hash_payload(_value(successor, member["field_path"]))))
                cur.execute("""UPDATE lbrain_fact_scopes SET memory_id=%s,value=%s,value_hash=%s,
                    generation=generation+1,head_assertion_id=%s,state='current',updated_at=clock_timestamp()
                    WHERE fact_key=%s""", (successor["memory_id"], Jsonb(value), hash_payload(value), assertion_id, scope.key))
                cur.execute("""UPDATE lbrain_fact_assertions SET state='history'
                    WHERE fact_key=%s AND value_hash<>%s AND state IN ('support','pending')""", (scope.key, hash_payload(value)))
                result.update(applied_value=value, authority_write_performed=True,
                    decision_id=changed["decision_id"], successor_memory_id=successor["memory_id"])
            elif action == "attach_evidence" and current:
                cur.execute("""UPDATE lbrain_fact_scopes SET head_assertion_id=%s,state='current',
                    updated_at=clock_timestamp() WHERE fact_key=%s""", (assertion_id, scope.key))
            elif action in {"disputed", "pending_evidence"}:
                cur.execute("UPDATE lbrain_fact_scopes SET state=%s WHERE fact_key=%s",
                    ("disputed" if action == "disputed" or head["state"] == "disputed" else "pending", scope.key))
            assertion_state = {"attach_evidence": "support", "replace_current": "support", "retain_history": "history"}.get(action, "pending")
            cur.execute("UPDATE lbrain_fact_assertions SET state=%s WHERE assertion_id=%s", (assertion_state, assertion_id))
            if current and action in {"replace_current", "attach_evidence"}:
                cur.execute("""SELECT DISTINCT ON(o.source_id,o.interpretation_version)
                        a.assertion_id,o.observation,a.state FROM lbrain_fact_assertions a
                    JOIN lbrain_fact_observations o ON o.version_id=a.observation_version
                    JOIN lbrain_fact_observation_sources s ON s.observation_version=o.version_id
                    JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
                    WHERE a.fact_key=%s AND a.value_hash=%s AND a.state IN ('support','pending') AND r.state='active'
                        AND NOT (o.source_id=%s AND o.interpretation_version=%s)
                        AND o.observation->>'current_verified'='true'
                    ORDER BY o.source_id,o.interpretation_version,a.created_at DESC,a.assertion_id LIMIT 256""",
                    (scope.key, hash_payload(value), observation.source_id, observation.interpretation_version))
                corroborating = []
                for row in cur.fetchall():
                    if is_current(VerifiedObservation(**row["observation"])) is True:
                        if row["state"] == "pending":
                            cur.execute("UPDATE lbrain_fact_assertions SET state='support' WHERE assertion_id=%s", (row["assertion_id"],))
                        cur.execute("INSERT INTO lbrain_fact_corroborations(resolution_id,assertion_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                            (resolution_id, row["assertion_id"]))
                        corroborating.append(row["assertion_id"])
                if corroborating:
                    result["corroborating_assertion_ids"] = corroborating
                if is_current(observation) is not True:
                    raise FactResolutionError("source_changed_during_resolution")
                from .fact_verification import FactVerificationService
                result["verification"] = FactVerificationService.record_on(cur, scope=scope,
                    resolution_id=resolution_id, assertion_id=assertion_id, observation=observation, commit=commit)
            elif source_was_current and is_current(observation) is not True:
                raise FactResolutionError("source_changed_during_resolution")
            cur.execute("""INSERT INTO lbrain_fact_project_versions(project,generation) VALUES (%s,1)
                ON CONFLICT(project) DO UPDATE SET generation=lbrain_fact_project_versions.generation+1 RETURNING generation""", (scope.project,))
            project_generation = cur.fetchone()["generation"]
            current_head = self._head(cur, scope.key)
            result.update(current=current_head, generation=current_head["generation"], project_generation=project_generation)
            terminal = action in {"replace_current", "attach_evidence", "retain_history"}
            ensure_public_safe(result, "FactResolutionResult")
            cur.execute("""INSERT INTO lbrain_fact_resolutions(resolution_id,fact_key,request_hash,terminal,result)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT(resolution_id) DO UPDATE SET
                terminal=excluded.terminal,result=excluded.result,updated_at=clock_timestamp()""",
                (resolution_id, scope.key, request_hash, terminal, Jsonb(result)))
            event_id = "factevt_" + hash_payload([resolution_id, assertion_id, action, current_head["generation"]])[7:47]
            cur.execute("""INSERT INTO lbrain_fact_resolution_events(event_id,resolution_id,assertion_id,result)
                VALUES (%s,%s,%s,%s) ON CONFLICT(event_id) DO NOTHING""", (event_id, resolution_id, assertion_id, Jsonb(result)))
            return result

    def project_cards(self, cards: list[Mapping], *, connection=None) -> list[dict]:
        """여러 card의 동일 사실을 PG의 단일 현재 값으로 표시한다."""
        if not cards:
            return []
        from .evidence_correction import _render_fact
        with self.store._scope(conn=connection) as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT m.*,f.value,f.generation,f.state,f.memory_id AS head_memory_id,
                    c.steward_envelope->>'render_text' AS original_render_text,
                    f.head_assertion_id,
                    EXISTS (SELECT 1 FROM lbrain_fact_assertions a
                        JOIN lbrain_fact_observation_sources s ON s.observation_version=a.observation_version
                        JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
                        WHERE a.assertion_id=f.head_assertion_id AND r.state='active') AS head_source_active,
                    proof.current_evidence,verification.checked_at,verification.expires_at,
                    verification.observed_at AS verified_observed_at,verification.source_proof,
                    verification.source_content_hash,verification.expires_at<=clock_timestamp() AS verification_due
                FROM lbrain_fact_members m JOIN lbrain_fact_scopes f ON f.fact_key=m.fact_key
                JOIN memory_cards c ON c.memory_id=m.memory_id
                LEFT JOIN LATERAL (SELECT jsonb_agg(p.evidence ORDER BY p.assertion_id) AS current_evidence FROM (
                    SELECT a.assertion_id,jsonb_build_object('observation_version',o.version_id,
                        'source_ref_id',r.source_ref_id,'content_hash',r.content_hash,
                        'observed_at',o.observation->>'observed_at',
                        'proof',o.observation->'proof') AS evidence
                    FROM lbrain_fact_assertions a JOIN lbrain_fact_observations o ON o.version_id=a.observation_version
                    JOIN lbrain_fact_observation_sources s ON s.observation_version=o.version_id
                    JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
                    WHERE a.fact_key=f.fact_key AND a.value_hash=f.value_hash AND r.state='active'
                        AND a.state IN ('support','pending') AND (a.state='support' OR a.assertion_id=f.head_assertion_id)
                    ORDER BY a.assertion_id LIMIT 17) p) proof ON TRUE
                LEFT JOIN LATERAL (SELECT v.checked_at,v.expires_at,o.observation->>'observed_at' AS observed_at,
                        o.observation->'proof' AS source_proof,o.observation->>'content_hash' AS source_content_hash
                    FROM lbrain_fact_verifications v JOIN lbrain_fact_assertions a ON a.assertion_id=v.assertion_id
                    JOIN lbrain_fact_observations o ON o.version_id=a.observation_version
                    JOIN lbrain_fact_observation_sources s ON s.observation_version=o.version_id
                    JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
                    WHERE v.fact_key=f.fact_key AND a.value_hash=f.value_hash AND r.state='active'
                        AND a.state IN ('support','pending') AND (a.state='support' OR a.assertion_id=f.head_assertion_id)
                    ORDER BY v.checked_at DESC,v.resolution_id LIMIT 1) verification ON TRUE
                WHERE m.memory_id=ANY(%s) ORDER BY m.memory_id,m.field_path""", ([c["memory_id"] for c in cards],))
            by_memory = {}
            for row in cur.fetchall():
                by_memory.setdefault(row["memory_id"], []).append(row)
        result = []
        for original in cards:
            card = copy.deepcopy(dict(original))
            views = []
            for member in by_memory.get(card["memory_id"], []):
                if _value(card, member["field_path"]) != member["value"]:
                    if "render_text" not in card:
                        card["render_text"] = member["original_render_text"]
                    summary, rendered, _ = _render_fact(card, {"field_path": member["field_path"],
                        "summary_template": member["render_template"]}, member["value"])
                    card["summary"], card["render_text"] = summary, rendered
                    card["typed_payload"][member["field_path"].split(".", 1)[1]] = member["value"]
                    # 표시를 갱신해도 원래 card의 다른 사실까지 새로 검증된 것은 아니다.
                    card["freshness_state"] = "unchecked"
                    card.setdefault("field_verifications", {}).pop(member["field_path"], None)
                view = {"fact_key": member["fact_key"], "field_path": member["field_path"],
                    "generation": member["generation"], "state": member["state"], "head_memory_id": member["head_memory_id"]}
                if member["head_assertion_id"]:
                    evidence = member["current_evidence"] or []
                    proof_hashes = set()
                    for proof_ref in evidence:
                        proof = proof_ref.pop("proof")
                        proof.update(content_hash=proof_ref["content_hash"], observed_at=proof_ref["observed_at"])
                        proof_hashes.add(hash_payload([proof]))
                    view.update(current_evidence=evidence[:16], evidence_truncated=len(evidence)>16)
                    if not evidence:
                        view["state"] = "invalidated"
                    verification = card.get("field_verifications", {}).get(member["field_path"])
                    stale_verification = bool(verification and verification.get("source_hash") not in proof_hashes)
                    if not member["head_source_active"] or stale_verification:
                        card["freshness_state"] = "unchecked"
                        card.setdefault("field_verifications", {}).pop(member["field_path"], None)
                    if member["checked_at"]:
                        checked_at, expires_at = member["checked_at"].isoformat(), member["expires_at"].isoformat()
                        view["verification"] = {"checked_at": checked_at, "expires_at": expires_at}
                        view["verification_state"] = "recheck_due" if member["verification_due"] else "verified_current"
                        proof = member["source_proof"]
                        proof.update(content_hash=member["source_content_hash"], observed_at=member["verified_observed_at"])
                        card.setdefault("field_verifications", {})[member["field_path"]] = {
                            "state": view["verification_state"], "observed_at": member["verified_observed_at"],
                            "verified_at": checked_at, "next_check_after": expires_at,
                            "value_hash": hash_payload(member["value"]), "source_hash": hash_payload([proof])}
                    else:
                        view["verification_state"] = "unchecked"
                        card.setdefault("field_verifications", {}).pop(member["field_path"], None)
                views.append(view)
            if views:
                card["canonical_content_hash"] = card["content_hash"]
                card["fact_views"] = views
                # 기한의 경과 자체는 target revision 변경이 아니다. 새 검증
                # receipt/source/current 변화는 hash와 project generation에 반영한다.
                stable_views = [{k: v for k, v in view.items() if k != "verification_state"} for view in views]
                card["content_hash"] = hash_payload({"canonical_content_hash": card["canonical_content_hash"], "fact_views": stable_views})
            result.append(card)
        return result

    def read_view(self, *, project, cards, as_of=None):
        """현재 조회만 공통 fact 값으로 표시한다. 과거 시점 재구성은 범위 밖이다."""
        if as_of:
            return {"cards": cards, "metadata": {"fact_resolution": "historical_unmodified"}}
        with self.store._scope() as db:
            # 카드의 검색/graph hash는 원본으로 검증한 뒤, bounded 조회 대상만 표시한다.
            # 읽기는 writer를 전역 차단하지 않고 하나의 MVCC snapshot을 사용한다.
            # 주입된 connection의 transaction/isolation은 호출자가 소유한다.
            if self.store.connection is None:
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            ids = [card["memory_id"] for card in cards if not card["memory_id"].startswith("chunk:")]
            with db.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT memory_id,content_hash,currentness FROM memory_cards WHERE project=%s AND memory_id=ANY(%s)",
                    (project, ids))
                canonical = {row["memory_id"]: row for row in cur.fetchall()}
            for card in cards:
                if card["memory_id"] in ids:
                    row = canonical.get(card["memory_id"])
                    if (not row or row["content_hash"] != card["content_hash"]
                            or row["currentness"] != card.get("currentness")):
                        raise FactResolutionError("authority_changed_during_read")
            projected = self.project_cards(cards, connection=db)
            # 조회 전체를 실패시키지 않는다. 같은 snapshot에서 무효 필드만
            # 빼고 유효 필드의 등록된 표시로 재구성한다. 원본 authority는 불변이다.
            with db.cursor(row_factory=dict_row) as cur:
                cur.execute("""SELECT memory_id,field_path,render_template FROM lbrain_fact_members
                    WHERE memory_id=ANY(%s)""", (ids,))
                templates = {(r["memory_id"], r["field_path"]): r["render_template"] for r in cur.fetchall()}
                cur.execute(f"""SELECT count(DISTINCT f.fact_key) AS n
                    FROM lbrain_fact_scopes f WHERE f.project=%s AND f.head_assertion_id IS NOT NULL
                    AND NOT {CURRENT_FACT_EVIDENCE_SQL} AND EXISTS (
                        SELECT 1 FROM lbrain_fact_members m JOIN memory_cards c ON c.memory_id=m.memory_id
                        WHERE m.fact_key=f.fact_key AND c.currentness='current'
                            AND c.authorization_status='active'
                            AND c.lifecycle_state IN ('accepted','human_accepted','auto_accepted'))""", (project,))
                gap_fields = cur.fetchone()["n"]
            visible = []
            partial_cards = 0
            for card in projected:
                views = card.get("fact_views", [])
                invalid = [v for v in views if v["state"] == "invalidated"]
                if not invalid:
                    visible.append(card)
                    continue
                kept = [v for v in views if v["state"] != "invalidated"]
                if not kept:
                    continue
                for view in invalid:
                    card["typed_payload"].pop(view["field_path"].split(".", 1)[1], None)
                    card.setdefault("field_verifications", {}).pop(view["field_path"], None)
                rendered = "; ".join(templates[(card["memory_id"], v["field_path"])].replace(
                    "{value}", str(_value(card, v["field_path"]))) for v in kept)
                card.update(summary=rendered, render_text=rendered, freshness_state="unchecked")
                card["verification_scope"] = {"whole_card": False, "fields": [v["field_path"] for v in kept]}
                visible.append(card)
                partial_cards += 1
            with db.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT generation FROM lbrain_fact_project_versions WHERE project=%s", (project,))
                row = cur.fetchone()
            generation = row["generation"] if row else 0
            return {"cards": visible, "metadata": {"fact_resolution": "postgres_current",
                "fact_generation": generation, "source_evidence_gap_fields": gap_fields,
                "source_evidence_partial_cards": partial_cards}}

    def _observation(self, cur, observation):
        value = asdict(observation)
        cur.execute("SELECT observation FROM lbrain_fact_observations WHERE version_id=%s", (observation.version_id,))
        previous = cur.fetchone()
        if previous and previous["observation"] != value:
            raise FactResolutionError("observation_version_conflict")
        cur.execute("""INSERT INTO lbrain_fact_observations
            (version_id,observation_id,project,source_id,source_revision,interpretation_version,observation)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(version_id) DO NOTHING""",
            (observation.version_id, observation.observation_id, observation.project, observation.source_id,
             observation.source_revision, observation.interpretation_version, Jsonb(value)))

    @staticmethod
    def _source_active(cur, observation_version):
        cur.execute("""SELECT r.state FROM lbrain_fact_observation_sources s
            JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
            WHERE s.observation_version=%s FOR SHARE OF r""", (observation_version,))
        row = cur.fetchone()
        return bool(row and row["state"] == "active")

    @staticmethod
    def _bind(cur, memory_id, field_path, fact_key, template):
        cur.execute("SELECT fact_key,render_template FROM lbrain_fact_members WHERE memory_id=%s AND field_path=%s", (memory_id, field_path))
        previous = cur.fetchone()
        if previous and (previous["fact_key"] != fact_key or previous["render_template"] != template):
            raise FactResolutionError("fact_binding_conflict")
        cur.execute("""INSERT INTO lbrain_fact_members(memory_id,field_path,fact_key,render_template)
            VALUES (%s,%s,%s,%s) ON CONFLICT(memory_id,field_path) DO NOTHING""", (memory_id, field_path, fact_key, template))

    @staticmethod
    def _head(cur, fact_key, locked=False):
        cur.execute("SELECT * FROM lbrain_fact_scopes WHERE fact_key=%s" + (" FOR UPDATE" if locked else ""), (fact_key,))
        row = cur.fetchone()
        if not row:
            return None
        result = {key: value for key, value in row.items() if key != "updated_at"}
        if row["head_assertion_id"]:
            cur.execute("""SELECT 1 FROM lbrain_fact_assertions a
                JOIN lbrain_fact_observation_sources s ON s.observation_version=a.observation_version
                JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
                WHERE a.fact_key=%s AND a.value_hash=%s AND a.state IN ('support','pending')
                    AND (a.state='support' OR a.assertion_id=%s) AND r.state='active' LIMIT 1""",
                (fact_key, row["value_hash"], row["head_assertion_id"]))
            if cur.fetchone() is None:
                result["state"] = "invalidated"
            cur.execute("""SELECT v.checked_at,v.expires_at,v.expires_at<=clock_timestamp() AS due
                FROM lbrain_fact_verifications v JOIN lbrain_fact_assertions a ON a.assertion_id=v.assertion_id
                JOIN lbrain_fact_observation_sources s ON s.observation_version=a.observation_version
                JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id
                WHERE v.fact_key=%s AND a.value_hash=%s AND r.state='active'
                    AND a.state IN ('support','pending') AND (a.state='support' OR a.assertion_id=%s)
                ORDER BY v.checked_at DESC,v.resolution_id LIMIT 1""",
                (fact_key, row["value_hash"], row["head_assertion_id"]))
            verification = cur.fetchone()
            if verification:
                result["verification"] = {key: verification[key].isoformat() for key in ("checked_at", "expires_at")}
                result["verification_state"] = "recheck_due" if verification["due"] else "verified_current"
            else:
                result["verification_state"] = "unchecked"
        return result

    @staticmethod
    def _head_observation(cur, head):
        if not head["head_assertion_id"]:
            return None
        cur.execute("""SELECT o.observation FROM lbrain_fact_assertions a JOIN lbrain_fact_observations o
            ON o.version_id=a.observation_version WHERE a.assertion_id=%s""", (head["head_assertion_id"],))
        row = cur.fetchone()
        return VerifiedObservation(**row["observation"]) if row else None

    def _unchanged_successor(self, card, field_path, *, connection):
        prior = _value(card, field_path)
        visited = {card["memory_id"]}
        for _ in range(32):
            following = card.get("superseded_by") or []
            if len(following) != 1 or following[0] in visited:
                break
            successor = self.store.get_steward_card(following[0], conn=connection)
            if (not successor or successor["project"] != card["project"]
                    or successor.get("authorization_status") != "active"
                    or card["memory_id"] not in successor.get("supersedes", [])
                    or _value(successor, field_path) != prior):
                break
            visited.add(successor["memory_id"])
            if successor.get("currentness") == "current":
                return successor
            card = successor
        raise FactResolutionError("target_revision_conflict")

    @staticmethod
    def _competing_current(cur, fact_key, value_hash, is_current):
        cur.execute("""SELECT a.assertion_id,o.observation FROM lbrain_fact_assertions a JOIN lbrain_fact_observations o
            ON o.version_id=a.observation_version
            JOIN lbrain_fact_observation_sources s ON s.observation_version=o.version_id
            JOIN lbrain_fact_source_receipts r ON r.source_ref_id=s.source_ref_id AND r.state='active'
            WHERE a.fact_key=%s AND a.value_hash<>%s AND a.state IN ('support','pending')
              AND o.observation->>'current_verified'='true'
            ORDER BY o.source_id,o.source_revision,o.interpretation_version,a.value_hash,o.created_at DESC
            LIMIT 257""", (fact_key, value_hash))
        rows = cur.fetchall()
        if len(rows) > 256:
            return False, True
        for row in rows:
            checked = is_current(VerifiedObservation(**row["observation"]))
            if checked is None:
                return False, True
            if checked:
                return True, False
            # False는 독립 확인된 소멸/대체다. 확인 불가(None)와 구분한다.
            # 지난 값을 지지하던 assertion을 현재 근거로 계속 표시하지 않는다.
            cur.execute("UPDATE lbrain_fact_assertions SET state='history' WHERE assertion_id=%s",
                (row["assertion_id"],))
        return False, False


class StewardFactCommit:
    """공통 resolution이 기존 canonical authority를 같은 PG transaction에서 갱신한다."""

    def __init__(self, *, field_path: str, render_template: str, policy_id: str, max_age_seconds: int = 300):
        _field(field_path)
        _template(render_template)
        _text(policy_id, 180)
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86400:
            raise ValueError("bounded source lifetime required")
        self.field_path = field_path
        self.render_template = render_template
        self.policy_id = policy_id
        self.max_age_seconds = max_age_seconds

    def __call__(self, *, connection, current, value, observation, resolution_id):
        from .evidence_correction import _candidate
        from .postgres_store.pgvector_store import PgVectorStore
        from .session_memory.memory_card import validate_memory_card_envelope
        from .session_memory.memory_promotion import commit_supersession

        store = PgVectorStore(connection=connection)
        rule = {"policy_id": self.policy_id, "memory_id": current["memory_id"],
            "field_path": self.field_path, "summary_template": self.render_template,
            "max_age_seconds": self.max_age_seconds}
        proof = dict(observation.proof)
        proof.update(content_hash=observation.content_hash, observed_at=observation.observed_at)
        if not proof.get("source_ref_id"):
            raise FactResolutionError("source_receipt_required")
        request_hash = hash_payload([resolution_id, observation.version_id, value, current["content_hash"]])
        candidate_id = "mem_fact_" + request_hash[7:47]
        decision_id = "factdec_" + request_hash[7:47]
        checked = connection.execute("SELECT clock_timestamp() AS checked_at").fetchone()["checked_at"]
        candidate = _candidate(current, rule, value, [proof], candidate_id, request_hash, decision_id,
            verified_at=checked)
        # GUC만으로는 과거 resolution을 재사용할 수 있다. 이번 transaction과
        # 실제 target/successor를 묶고, commit 시 최종 resolution FK도 검증한다.
        connection.execute("""INSERT INTO lbrain_fact_mutation_intents
            (resolution_id,fact_key,transaction_id,target_memory_id,candidate_memory_id)
            SELECT %s,fact_key,pg_current_xact_id()::text,%s,%s
            FROM lbrain_fact_members WHERE memory_id=%s AND field_path=%s""",
            (resolution_id, current["memory_id"], candidate_id, current["memory_id"], self.field_path))
        store.put_steward_proposal(candidate)

        def transform(proposal, target):
            if (not target or target["content_hash"] != current["content_hash"]
                    or target.get("currentness") != "current" or target.get("authorization_status") != "active"):
                raise FactResolutionError("target_revision_conflict")
            accepted = copy.deepcopy(proposal)
            accepted.update(lifecycle_state="auto_accepted", approval_state="auto_accepted",
                judgment_state="auto_status", status="accepted", auto_policy_ready=True,
                authorization_status="active", currentness="current", freshness="current")
            validate_memory_card_envelope(accepted)
            return accepted, commit_supersession(target, superseded_by=accepted["memory_id"])

        result = store.steward_decision(decision_id=decision_id, memory_id=candidate_id,
            action="correct_fact", actor="fact_resolution.v1", content_hash=candidate["content_hash"],
            target_memory_id=current["memory_id"], transform=transform, fact_resolution_id=resolution_id)
        return {**result, "decision_id": decision_id}


def _value(card, field):
    return card.get("typed_payload", {}).get(field.split(".", 1)[1])


def _field(value):
    if not isinstance(value, str) or not re.fullmatch(r"typed_payload\.[A-Za-z][A-Za-z0-9_]{0,95}", value):
        raise ValueError("explicit factual payload field required")


def _text(value, limit):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > limit:
        raise ValueError("bounded canonical text required")


def _template(value):
    _text(value, 1024)
    if value.count("{value}") != 1 or any(c in value.replace("{value}", "") for c in "{}"):
        raise ValueError("single fact render template required")

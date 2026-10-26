"""서버의 검증된 source 관측과 뒤늦은 native 참조를 같은 사실에 연결한다.

Native 출력은 관측 참조일 뿐 독립적인 세계 상태 증명이 아니다. 실제 값은
등록 source reader와 기존 공통 resolution만 결정한다.
"""

from __future__ import annotations

import json
import secrets
import time

from pydantic import BaseModel, ConfigDict, Field
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .fact_resolution import FactResolutionError, VerifiedObservation
from .public_safe_util import ensure_public_safe, hash_payload, sha256_text
from .recheck_scheduler import RecheckBlocked

SCHEMA = "brain_source_observation.v1"


class SourceObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    project: str = Field(min_length=1, max_length=64)
    repository: str = Field(min_length=1, max_length=240)
    policy_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class SourceObservationService:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store

    def _read(self, reference, connection=None, lock=False):
        from .postgres_store.pgvector_store import PgVectorStore

        store = PgVectorStore(connection=connection) if connection else self.store
        with store._scope() as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT r.*,o.observation FROM lbrain_fact_source_reads r
                JOIN lbrain_fact_observations o ON o.version_id=r.observation_version
                WHERE r.observation_ref=%s"""
                + (" FOR KEY SHARE OF r" if lock else ""),
                (reference,),
            )
            row = cur.fetchone()
        if row is None:
            raise RecheckBlocked("source_observation_unavailable")
        if hash_payload(row["result"]) != row["result_hash"]:
            raise RecheckBlocked("source_observation_integrity_invalid")
        return row

    def validate_reference(self, rule, evidence, connection=None):
        row = self._read(
            evidence.observation_ref, connection, lock=connection is not None
        )
        original = VerifiedObservation(**row["observation"])
        if (
            (row["project"], row["policy_id"], row["policy_version"], row["fact_key"])
            != (rule.scope.project, rule.policy_id, rule.version, rule.scope.key)
            or (original.context_hash, original.source_revision, original.content_hash)
            != (rule.scope.context_hash, evidence.revision, evidence.content_hash)
            or original.proof.get("policy_version") != rule.version
            or row["result"].get("observation_ref") != evidence.observation_ref
            or row["result"].get("evidence")
            != {
                "revision": evidence.revision,
                "content_hash": evidence.content_hash,
                "observation_ref": evidence.observation_ref,
            }
        ):
            raise RecheckBlocked("source_observation_binding_mismatch")
        return row

    def bind_task(self, db, task_id, evidence):
        db.execute(
            """INSERT INTO lbrain_fact_source_read_tasks(task_id,observation_ref)
            VALUES (%s,%s) ON CONFLICT DO NOTHING""",
            (task_id, evidence.observation_ref),
        )

    def _target(self, db, rule):
        """등록된 사실은 current head, 첫 관측은 기존 유일한 카드만 허용한다."""
        with db.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT f.memory_id FROM lbrain_fact_admission_rules r JOIN lbrain_fact_scopes f
                ON f.fact_key=r.fact_key AND f.project=r.project WHERE r.project=%s AND r.policy_id=%s""",
                (rule.scope.project, rule.policy_id),
            )
            head = cur.fetchone()
            if head:
                card = self.store.get_steward_card(head["memory_id"], conn=db)
            else:
                cur.execute(
                    """SELECT steward_envelope FROM memory_cards WHERE project=%s AND title=%s
                    AND authorization_status='active' AND currentness='current' LIMIT 2""",
                    (rule.scope.project, rule.target_title),
                )
                rows = cur.fetchall()
                if len(rows) != 1:
                    raise FactResolutionError("existing_target_not_unique")
                card = rows[0]["steward_envelope"]
            if (
                not card
                or card.get("project") != rule.scope.project
                or card.get("currentness") != "current"
            ):
                raise FactResolutionError("target_not_current")
            self.coordinator._register_rule(cur, rule, card)
            return card

    def observe(self, **arguments):
        request = SourceObservationRequest.model_validate(arguments)
        ensure_public_safe(request.model_dump(), "SourceObservationRequest")
        rule = self.coordinator.policies.get(request.policy_id)
        if not rule or (rule.scope.project, rule.scope.repository) != (
            request.project,
            request.repository,
        ):
            raise FactResolutionError("policy_scope_mismatch")
        digest = hash_payload(request.model_dump())
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self.store._lock_steward_authority_on(db)
            target = self._target(db, rule)
            cur.execute(
                "SELECT * FROM lbrain_fact_source_reads WHERE project=%s AND request_key=%s",
                (request.project, request.idempotency_key),
            )
            previous = cur.fetchone()
            if previous:
                if previous["request_hash"] != digest:
                    raise FactResolutionError("source_read_request_key_conflict")
                if previous["policy_version"] != rule.version:
                    raise FactResolutionError("fact_policy_changed")
                return previous["result"]
        # 기존 _load는 등록 reader로 원문을 독립 확인하고 관측/receipt를 먼저
        # 보존한다. 가짜 승인/queue task를 만들거나 authority apply를 호출하지 않는다.
        job = {
            "result_ref": "",
            "request_hash": digest,
            "request": {
                "policy_id": rule.policy_id,
                "policy_version": rule.version,
                "lane": "intake",
                "change": {"evidence": {"mode": "server_current"}},
            },
        }
        snapshot = self.coordinator._load(job)
        observed = VerifiedObservation(**snapshot.data["observation"])
        reference = "obsread_" + secrets.token_hex(24)
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self.store._lock_steward_authority_on(db)
            if not self.coordinator._registered_rule(cur, rule):
                raise FactResolutionError("fact_policy_changed")
            if self.coordinator._current(rule, observed, db) is not True:
                raise RecheckBlocked("source_read_changed")
            cur.execute(
                "SELECT * FROM lbrain_fact_source_reads WHERE project=%s AND request_key=%s",
                (request.project, request.idempotency_key),
            )
            concurrent = cur.fetchone()
            if concurrent:
                if concurrent["request_hash"] != digest:
                    raise FactResolutionError("source_read_request_key_conflict")
                return concurrent["result"]
            cur.execute("SELECT clock_timestamp() AS now")
            timestamp = cur.fetchone()["now"].isoformat()
            result = {
                "schema_version": SCHEMA,
                "observation_ref": reference,
                "project": request.project,
                "repository": request.repository,
                "policy_id": rule.policy_id,
                "fact_key": rule.scope.key,
                "observation_id": observed.observation_id,
                "observation_version": observed.version_id,
                "observed_at": timestamp,
                "evidence": {
                    "revision": observed.source_revision,
                    "content_hash": observed.content_hash,
                    "observation_ref": reference,
                },
                "canonical_write_performed": False,
            }
            ensure_public_safe(result, "SourceObservationResult")
            cur.execute(
                """INSERT INTO lbrain_fact_source_reads
                (observation_ref,project,request_key,request_hash,policy_id,policy_version,fact_key,target_memory_id,
                    observation_version,result,result_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    reference,
                    request.project,
                    request.idempotency_key,
                    digest,
                    rule.policy_id,
                    rule.version,
                    rule.scope.key,
                    target["memory_id"],
                    observed.version_id,
                    Jsonb(result),
                    hash_payload(result),
                ),
            )
            return result

    def previous_resolution(self, reference):
        with self.store._scope() as db:
            row = db.execute(
                """SELECT x.resolution_id FROM lbrain_fact_source_reads r
                JOIN lbrain_fact_resolutions x ON x.fact_key=r.fact_key
                WHERE r.observation_ref=%s AND x.terminal
                    AND x.result->>'observation_version'=r.observation_version
                ORDER BY x.updated_at,x.resolution_id LIMIT 1""",
                (reference,),
            ).fetchone()
        return row["resolution_id"] if row else None


def _referenced_results(text):
    """완전한 JSON에 포함된 서버 관측 영수증만 찾는다. prose는 파싱하지 않는다."""
    from .session_memory.native_tool_receipts import _loads

    try:
        root = _loads(text)
    except (ValueError, TypeError):
        return []
    found = {}
    pending = [(root, 0)]
    visited = 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if depth > 8 or visited > 128:
            raise ValueError("native_source_reference_limit")
        if isinstance(node, dict):
            if node.get("schema_version") == SCHEMA:
                key = node.get("observation_ref")
                if isinstance(key, str):
                    if key in found and hash_payload(found[key]) != hash_payload(node):
                        raise ValueError("native_source_reference_collision")
                    found[key] = node
                continue
            for key, value in node.items():
                if key == "text" and isinstance(value, str):
                    try:
                        pending.append((_loads(value), depth + 1))
                    except ValueError:
                        pass
                elif isinstance(value, (dict, list)):
                    pending.append((value, depth + 1))
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    return list(found.values())


class NativeSourceObservationBridge:
    def __init__(self, coordinator, catalog):
        self.coordinator = coordinator
        self.store = coordinator.store
        self.catalog = catalog
        self.reads = SourceObservationService(coordinator)

    def _checked_reference(self, project, payload):
        reference = payload.get("observation_ref")
        if not isinstance(reference, str):
            raise RecheckBlocked("source_observation_unavailable")
        row = self.reads._read(reference)
        if row["project"] != project or hash_payload(payload) != row["result_hash"]:
            raise RecheckBlocked("native_source_reference_mismatch")
        return row

    def dispatch_once(self, *, project, batch_size=20, io_seconds=10):
        if self.catalog is None:
            raise RecheckBlocked("native_source_unconfigured")
        if (
            type(batch_size) is not int
            or not 1 <= batch_size <= 100
            or not 0.1 <= io_seconds <= 30
        ):
            raise ValueError("bounded native discovery required")
        source = self.catalog.store
        key = hash_payload([source.base_url, source.db])
        with self.store.transaction() as db:
            db.execute(
                """INSERT INTO lbrain_native_source_cursors(project,source_key,attempted_at,reason_code)
                VALUES (%s,%s,clock_timestamp(),'native_discovery_not_completed')
                ON CONFLICT(project,source_key) DO UPDATE SET attempted_at=clock_timestamp()""",
                (project, key),
            )
        try:
            return self._dispatch(
                project=project, batch_size=batch_size, io_seconds=io_seconds
            )
        except Exception as exc:
            # 원격 오류에 포함될 수 있는 URL과 credential은 기록하지 않는다.
            reason = (
                exc.reason_code
                if isinstance(exc, RecheckBlocked)
                else "native_discovery_unavailable"
            )
            with self.store.transaction() as db:
                db.execute(
                    """UPDATE lbrain_native_source_cursors SET reason_code=%s
                    WHERE project=%s AND source_key=%s""",
                    (reason, project, key),
                )
            return {
                "status": "unavailable",
                "reason_code": reason,
                "attention_required": True,
            }

    def _dispatch(self, *, project, batch_size, io_seconds):
        source = self.catalog.store
        key = hash_payload([source.base_url, source.db])
        with self.store._scope() as db:
            cursor = db.execute(
                "SELECT sequence,generation FROM lbrain_native_source_cursors WHERE project=%s AND source_key=%s",
                (project, key),
            ).fetchone()
        sequence = cursor["sequence"] if cursor else "0"
        generation = cursor["generation"] if cursor else 0
        changes = source.changes_since(since=sequence, limit=batch_size)
        plans = []
        rejected = 0
        end = sequence
        deadline = time.monotonic() + io_seconds
        seen_documents = set()
        for change in changes:
            if time.monotonic() > deadline:
                break
            if change["deleted"]:
                end = change["seq"]
                continue
            if change["id"] in seen_documents:
                end = change["seq"]
                continue
            seen_documents.add(change["id"])
            doc = source.get(change["id"])
            if (
                doc
                and doc.get("doc_type") == "tool_evidence_bundle"
                and doc.get("project") == project
            ):
                from .session_memory.observation_refs import validate
                from .session_memory.native_tool_receipts import from_bundle

                refs = validate(
                    doc.get("observation_refs", []),
                    body=doc.get("body", ""),
                    session_id_hash=doc["session_id_hash"],
                    provider=doc["provider"],
                    project=project,
                )
                receipts = from_bundle(doc, refs)
                for evidence_id, receipt in receipts.items():
                    if not (
                        receipt["arguments_complete"] and receipt["result_complete"]
                    ):
                        continue
                    for payload in _referenced_results(receipt["result_text"]):
                        try:
                            read = self._checked_reference(project, payload)
                        except RecheckBlocked:
                            rejected += 1
                            continue
                        plans.append(
                            (
                                read,
                                {
                                    "provider": doc["provider"],
                                    "session_id_hash": doc["session_id_hash"],
                                    "observation_id": receipt["observation_id_hash"],
                                    "receipt_hash": receipt["receipt_hash"],
                                    "evidence_id_hash": evidence_id,
                                },
                            )
                        )
            end = change["seq"]
        from .postgres_store.pgvector_store import PgVectorStore
        from .recheck_scheduler import PgRecheckQueue

        queued = 0
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self.store._lock_steward_authority_on(db)
            cur.execute(
                """INSERT INTO lbrain_native_source_cursors(project,source_key) VALUES (%s,%s)
                ON CONFLICT DO NOTHING""",
                (project, key),
            )
            cur.execute(
                "SELECT * FROM lbrain_native_source_cursors WHERE project=%s AND source_key=%s FOR UPDATE",
                (project, key),
            )
            current = cur.fetchone()
            if (current["sequence"], current["generation"]) != (sequence, generation):
                return {"status": "waiting", "reason_code": "native_discovery_busy"}
            queue = PgRecheckQueue(PgVectorStore(connection=db))
            for read, native in plans:
                route = hash_payload(
                    [
                        read["observation_ref"],
                        native["observation_id"],
                        native["receipt_hash"],
                    ]
                )
                cur.execute(
                    "SELECT 1 FROM lbrain_recheck_tasks WHERE project=%s AND request->>'native_route_key'=%s",
                    (project, route),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    "SELECT memory_id FROM lbrain_fact_scopes WHERE fact_key=%s AND project=%s",
                    (read["fact_key"], project),
                )
                head = cur.fetchone()
                memory_id = head["memory_id"] if head else read["target_memory_id"]
                request = {
                    "schema_version": "fact_source_job.v1",
                    "policy_id": read["policy_id"],
                    "policy_version": read["policy_version"],
                    "fact_key": read["fact_key"],
                    "lane": "intake",
                    "initial_memory_id": read["target_memory_id"],
                    "change": {
                        "proposed_value": "server_verified_observation",
                        "evidence": read["result"]["evidence"],
                    },
                    "native_source_read": native,
                    "native_route_key": route,
                }
                task = queue.register(
                    project=project,
                    memory_id=memory_id,
                    request=request,
                    request_key=route,
                )
                cur.execute(
                    "INSERT INTO lbrain_fact_source_read_tasks(task_id,observation_ref) VALUES (%s,%s)",
                    (task["task_id"], read["observation_ref"]),
                )
                queued += 1
            cur.execute(
                """UPDATE lbrain_native_source_cursors SET sequence=%s,generation=generation+1,checked_at=clock_timestamp()
                ,reason_code='' WHERE project=%s AND source_key=%s""",
                (end, project, key),
            )
        return {
            "status": "processed",
            "scanned": len(changes),
            "queued": queued,
            "rejected": rejected,
        }

    def read_native(self, task):
        with self.store._scope() as db:
            existing = db.execute(
                "SELECT 1 FROM lbrain_fact_native_read_links WHERE task_id=%s",
                (task["task_id"],),
            ).fetchone()
        if existing and task["result_ref"]:
            return {"recovered": True}
        if self.catalog is None:
            raise RecheckBlocked("native_source_unconfigured")
        from .couchdb_source.source_revision import resolve_active_source_revision

        expected = task["request"]["native_source_read"]
        try:
            source = resolve_active_source_revision(
                store=self.catalog.store, session_id_hash=expected["session_id_hash"]
            )
            admitted = self.catalog.read_tool_result(
                project=task["project"],
                provider=expected["provider"],
                session_id_hash=expected["session_id_hash"],
                source_hash=source.source_hash,
                evidence_id_hash=expected["evidence_id_hash"],
            )
        except Exception:
            raise RecheckBlocked("native_source_not_ready") from None
        receipt = admitted.native_receipt
        if (receipt["receipt_hash"], receipt["observation_id_hash"]) != (
            expected["receipt_hash"],
            expected["observation_id"],
        ):
            raise RecheckBlocked("native_source_reference_mismatch")
        reference = task["request"]["change"]["evidence"]["observation_ref"]
        matches = [
            p
            for p in _referenced_results(receipt["result_text"])
            if p.get("observation_ref") == reference
        ]
        if len(matches) != 1:
            raise RecheckBlocked("native_source_reference_mismatch")
        self._checked_reference(task["project"], matches[0])
        return {"receipt": receipt, "proof": admitted.public_proof()}

    def attach_on(
        self, db, task, native, resolution_id, *, join_only, authority_write_performed
    ):
        if native.get("recovered"):
            return
        receipt = native["receipt"]
        body = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        proof = {
            "adapter": "native_source_read_reference.v1",
            "project": task["project"],
            "receipt_hash": receipt["receipt_hash"],
        }
        source_ref = "src_native_" + hash_payload(proof)[7:47]
        db.execute(
            """INSERT INTO lbrain_fact_source_receipts(source_ref_id,content_hash,redacted_content,proof,owner_project)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (source_ref, sha256_text(body), body, Jsonb(proof), task["project"]),
        )
        saved = db.execute(
            """SELECT content_hash,redacted_content,proof,owner_project,state
            FROM lbrain_fact_source_receipts WHERE source_ref_id=%s FOR KEY SHARE""",
            (source_ref,),
        ).fetchone()
        if not saved or (
            saved["content_hash"],
            saved["redacted_content"],
            saved["proof"],
            saved["owner_project"],
            saved["state"],
        ) != (sha256_text(body), body, proof, task["project"], "active"):
            raise RecheckBlocked("native_receipt_integrity_invalid")
        db.execute(
            """INSERT INTO lbrain_fact_native_read_links(task_id,observation_ref,native_observation_id,native_receipt_hash,
            native_source_ref_id,native_proof,resolution_id,join_only,authority_write_performed)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                task["task_id"],
                task["request"]["change"]["evidence"]["observation_ref"],
                receipt["observation_id_hash"],
                receipt["receipt_hash"],
                source_ref,
                Jsonb(native["proof"]),
                resolution_id,
                join_only,
                authority_write_performed,
            ),
        )
        saved = db.execute(
            """SELECT observation_ref,native_observation_id,native_receipt_hash,native_source_ref_id,resolution_id,
            join_only,authority_write_performed FROM lbrain_fact_native_read_links WHERE task_id=%s""",
            (task["task_id"],),
        ).fetchone()
        expected = (
            task["request"]["change"]["evidence"]["observation_ref"],
            receipt["observation_id_hash"],
            receipt["receipt_hash"],
            source_ref,
            resolution_id,
            join_only,
            authority_write_performed,
        )
        if not saved or tuple(saved.values()) != expected:
            raise RecheckBlocked("native_read_link_integrity_invalid")

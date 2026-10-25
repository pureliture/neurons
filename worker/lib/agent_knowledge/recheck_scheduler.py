"""PostgreSQL에 예약과 실행 소유권을 보존하는 재검토 코어.

의미 판단과 authority 반영은 명시적으로 주입한다. 이 모듈의 설치만으로
기존 기억의 자동 교정이나 충돌 상태 변경을 활성화하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import copy
import json
import re
import time
from typing import Any, Callable, Mapping
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .public_safe_util import ensure_public_safe, hash_payload, require_sha256


_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class LeaseLost(RuntimeError):
    """이미 다른 실행에 넘어간 작업을 이전 실행이 완료하려 했다."""


class RecheckBlocked(RuntimeError):
    def __init__(self, reason_code: str):
        if not _CODE.fullmatch(reason_code):
            raise ValueError("invalid recheck blocking reason")
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class RecheckSnapshot:
    fingerprint: str
    data: Mapping[str, Any]

    @classmethod
    def from_data(cls, task: Mapping[str, Any], data: Mapping[str, Any]) -> "RecheckSnapshot":
        detached = copy.deepcopy(dict(data))
        return cls(hash_payload({"request_hash": task["request_hash"], "data": detached}), detached)


@dataclass(frozen=True)
class RecheckOutcome:
    state: str
    reason_code: str
    result_ref: str = ""


class PgRecheckQueue:
    def __init__(self, store: Any):
        self.store = store

    def register(self, *, project: str, memory_id: str, request: Mapping[str, Any],
                 request_key: str, delay_seconds: float = 0) -> dict:
        _text(project, 64)
        _text(memory_id, 64)
        _text(request_key, 180)
        _delay(delay_seconds)
        ensure_public_safe(request, "RecheckRequest")
        if len(json.dumps(request).encode()) > 32768:
            raise ValueError("recheck request is too large")
        task_id = "recheck_" + hash_payload([project, memory_id, request_key])[7:47]
        request_hash = hash_payload(request)
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (task_id,))
            cur.execute("SELECT * FROM lbrain_recheck_tasks WHERE task_id=%s FOR UPDATE", (task_id,))
            previous = cur.fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise ValueError("recheck request key conflict")
                return previous
            cur.execute("""SELECT project, authorization_status, currentness FROM memory_cards
                           WHERE memory_id=%s FOR KEY SHARE""", (memory_id,))
            card = cur.fetchone()
            if (not card or card["project"] != project or card["authorization_status"] != "active"
                    or card["currentness"] != "current"):
                raise ValueError("recheck requires a current authorized same-project card")
            # 충돌 항목과 다음 검토 예약은 하나의 행에서 함께 생성한다.
            cur.execute("""INSERT INTO lbrain_recheck_tasks
                (task_id,project,memory_id,request_hash,request,next_check_at)
                VALUES (%s,%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second')) RETURNING *""",
                (task_id, project, memory_id, request_hash, Jsonb(dict(request)), delay_seconds))
            return cur.fetchone()

    def claim(self, *, project: str, worker_id: str, lease_seconds: float = 60) -> dict | None:
        _text(project, 64)
        _text(worker_id, 128)
        _delay(lease_seconds, minimum=0.1)
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT * FROM lbrain_recheck_tasks
                WHERE project=%s AND ((state IN ('queued','waiting','blocked') AND next_check_at<=clock_timestamp())
                OR (state='leased' AND lease_until<=clock_timestamp()))
                ORDER BY next_check_at,task_id FOR UPDATE SKIP LOCKED LIMIT 1""", (project,))
            previous = cur.fetchone()
            if not previous:
                return None
            if previous["state"] == "leased":
                cur.execute("""UPDATE lbrain_recheck_attempts SET state='lease_expired',finished_at=clock_timestamp()
                    WHERE task_id=%s AND generation=%s AND state='running'""",
                    (previous["task_id"], previous["generation"]))
            token = uuid.uuid4().hex
            cur.execute("""UPDATE lbrain_recheck_tasks SET state='leased',worker_id=%s,lease_token=%s,
                lease_until=clock_timestamp()+(%s * interval '1 second'),generation=generation+1,
                attempt_count=attempt_count+1,updated_at=clock_timestamp()
                WHERE task_id=%s RETURNING *""", (worker_id, token, lease_seconds, previous["task_id"]))
            task = cur.fetchone()
            cur.execute("""INSERT INTO lbrain_recheck_attempts(task_id,generation,worker_id)
                           VALUES (%s,%s,%s)""", (task["task_id"], task["generation"], worker_id))
            return task

    def get(self, task_id: str) -> dict | None:
        with self.store._scope() as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM lbrain_recheck_tasks WHERE task_id=%s", (task_id,))
            return cur.fetchone()

    def cached_judgment(self, task_id: str, fingerprint: str) -> dict | None:
        require_sha256(fingerprint, "fingerprint")
        with self.store._scope() as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT judgment FROM lbrain_recheck_judgments
                           WHERE task_id=%s AND input_hash=%s""", (task_id, fingerprint))
            row = cur.fetchone()
            return row["judgment"] if row else None

    def save_judgment(self, task: Mapping[str, Any], fingerprint: str, judgment: Mapping[str, Any]) -> dict:
        require_sha256(fingerprint, "fingerprint")
        ensure_public_safe(judgment, "RecheckJudgment")
        if len(json.dumps(judgment).encode()) > 32768:
            raise ValueError("recheck judgment is too large")
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self._fence(cur, task)
            cur.execute("""INSERT INTO lbrain_recheck_judgments(task_id,input_hash,judgment)
                VALUES (%s,%s,%s) ON CONFLICT(task_id,input_hash) DO NOTHING""",
                (task["task_id"], fingerprint, Jsonb(dict(judgment))))
            cur.execute("""SELECT judgment FROM lbrain_recheck_judgments
                           WHERE task_id=%s AND input_hash=%s""", (task["task_id"], fingerprint))
            # 실제 사용한 최초 결과를 재사용한다. 모델 재호출의 다른 답으로 덮어쓰지 않는다.
            return cur.fetchone()["judgment"]

    def finish(self, task: Mapping[str, Any], outcome: RecheckOutcome, *, fingerprint: str | None,
               delay_seconds: float = 60) -> None:
        if outcome.state not in {"waiting", "resolved", "blocked"} or not _CODE.fullmatch(outcome.reason_code):
            raise ValueError("invalid recheck outcome")
        if fingerprint is not None:
            require_sha256(fingerprint, "fingerprint")
        if outcome.state == "resolved" and not outcome.result_ref:
            raise ValueError("resolved recheck requires durable result reference")
        ensure_public_safe(outcome.result_ref, "RecheckResultReference")
        _text(outcome.result_ref, 180, allow_empty=True)
        _delay(delay_seconds)
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self._fence(cur, task)
            cur.execute("SELECT result_ref FROM lbrain_recheck_tasks WHERE task_id=%s", (task["task_id"],))
            recorded = cur.fetchone()["result_ref"]
            if recorded and outcome.result_ref and recorded != outcome.result_ref:
                raise ValueError("recheck result reference conflict")
            result_ref = outcome.result_ref or recorded
            cur.execute("""UPDATE lbrain_recheck_tasks SET state=%s,reason_code=%s,result_ref=%s,
                last_input_hash=%s,last_check_at=clock_timestamp(),
                next_check_at=clock_timestamp()+(%s * interval '1 second'),
                worker_id=NULL,lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp()
                WHERE task_id=%s""", (outcome.state, outcome.reason_code, result_ref,
                                     fingerprint, delay_seconds, task["task_id"]))
            cur.execute("""UPDATE lbrain_recheck_attempts SET state=%s,reason_code=%s,input_hash=%s,
                result_ref=%s,finished_at=clock_timestamp() WHERE task_id=%s AND generation=%s""",
                (outcome.state, outcome.reason_code, fingerprint, result_ref,
                 task["task_id"], task["generation"]))

    @staticmethod
    def _fence(cur: Any, task: Mapping[str, Any]) -> None:
        cur.execute("""SELECT task_id FROM lbrain_recheck_tasks WHERE task_id=%s AND state='leased'
            AND worker_id=%s AND lease_token=%s AND generation=%s
            AND lease_until>clock_timestamp() FOR UPDATE""",
            (task["task_id"], task["worker_id"], task["lease_token"], task["generation"]))
        if cur.fetchone() is None:
            raise LeaseLost("recheck lease no longer owned")

    @contextmanager
    def owned_transaction(self, task: Mapping[str, Any]):
        """authority adapter가 같은 PG transaction에 참여할 때 사용할 소유권 fence."""
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            self._fence(cur, task)
            yield db

    def checkpoint_result(self, task: Mapping[str, Any], result_ref: str, *, connection: Any) -> None:
        """authority를 쓰는 동일 transaction에 복구용 결정 참조를 남긴다.

        호출자는 독립 connection이 아닌 owned_transaction의 connection을 전달한다.
        authority commit과 참조가 함께 성공해야 근거 변경 후에도 완료를 복구할 수 있다.
        """
        _text(result_ref, 180)
        with connection.cursor(row_factory=dict_row) as cur:
            self._fence(cur, task)
            cur.execute("""UPDATE lbrain_recheck_tasks SET result_ref=%s,updated_at=clock_timestamp()
                WHERE task_id=%s AND (result_ref='' OR result_ref=%s) RETURNING task_id""",
                (result_ref, task["task_id"], result_ref))
            if cur.fetchone() is None:
                raise ValueError("recheck result reference conflict")

    def heartbeat(self, *, project: str, worker_id: str) -> None:
        _text(project, 64)
        _text(worker_id, 128)
        with self.store.transaction() as db, db.cursor() as cur:
            cur.execute("""INSERT INTO lbrain_recheck_heartbeats(project,worker_id,last_seen_at)
                VALUES (%s,%s,clock_timestamp()) ON CONFLICT(project,worker_id)
                DO UPDATE SET last_seen_at=excluded.last_seen_at""", (project, worker_id))

    def health(self, *, project: str, heartbeat_grace: float = 120, overdue_grace: float = 120) -> dict:
        _text(project, 64)
        _delay(heartbeat_grace)
        _delay(overdue_grace)
        with self.store._scope() as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE state<>'resolved') AS unfinished,
                count(*) FILTER (WHERE state='blocked') AS blocked,
                count(*) FILTER (WHERE state IN ('queued','waiting','blocked') AND
                    next_check_at<clock_timestamp()-(%s * interval '1 second')) AS overdue,
                count(*) FILTER (WHERE state='leased' AND lease_until<clock_timestamp()) AS expired_leases
                FROM lbrain_recheck_tasks WHERE project=%s""", (overdue_grace, project))
            counts = dict(cur.fetchone())
            cur.execute("""SELECT coalesce(max(last_seen_at)>clock_timestamp()-(%s * interval '1 second'),false)
                AS heartbeat_recent FROM lbrain_recheck_heartbeats WHERE project=%s""", (heartbeat_grace, project))
            recent = bool(cur.fetchone()["heartbeat_recent"])
        return {**{key: int(value) for key, value in counts.items()}, "heartbeat_recent": recent,
                "stalled": bool(counts["unfinished"] and (not recent or counts["overdue"] or counts["expired_leases"])),
                "attention_required": bool(counts["blocked"] or (counts["unfinished"] and
                    (not recent or counts["overdue"] or counts["expired_leases"])))}


class RecheckWorker:
    """로드·판단·반영을 분리하고 완료되지 않은 작업은 반드시 다음 예약을 남긴다."""

    def __init__(self, *, queue: PgRecheckQueue, project: str, worker_id: str,
                 load: Callable[[dict], RecheckSnapshot],
                 judge: Callable[[dict, RecheckSnapshot], Mapping[str, Any]],
                 apply: Callable[[dict, RecheckSnapshot, Mapping[str, Any]], RecheckOutcome],
                 lease_seconds: float = 60, retry_seconds: float = 60,
                 max_retry_seconds: float = 3600):
        _delay(lease_seconds, minimum=0.1)
        _delay(retry_seconds, minimum=0.1)
        _delay(max_retry_seconds, minimum=retry_seconds)
        self.queue, self.project, self.worker_id = queue, project, worker_id
        self.load, self.judge, self.apply = load, judge, apply
        self.lease_seconds, self.retry_seconds, self.max_retry_seconds = lease_seconds, retry_seconds, max_retry_seconds

    def run_once(self) -> dict:
        self.queue.heartbeat(project=self.project, worker_id=self.worker_id)
        task = self.queue.claim(project=self.project, worker_id=self.worker_id, lease_seconds=self.lease_seconds)
        if task is None:
            return {"claimed": False}
        fingerprint = None
        reused = False
        try:
            snapshot = self.load(task)
            require_sha256(snapshot.fingerprint, "fingerprint")
            if snapshot.fingerprint != RecheckSnapshot.from_data(task, snapshot.data).fingerprint:
                raise ValueError("snapshot fingerprint does not bind request and evidence")
            fingerprint = snapshot.fingerprint
            judgment = self.queue.cached_judgment(task["task_id"], fingerprint)
            reused = judgment is not None
            if judgment is None:
                judgment = self.queue.save_judgment(task, fingerprint, self.judge(task, snapshot))
            # apply는 durable result id와 CAS를 사용해 재실행돼도 같은 결정을 적용해야 한다.
            # queue lease만으로 외부 authority write를 exactly-once로 만들지는 않는다.
            outcome = self.apply(task, snapshot, judgment)
            self.queue.finish(task, outcome, fingerprint=fingerprint, delay_seconds=self.retry_seconds)
        except LeaseLost:
            return {"claimed": True, "lease_lost": True, "task_id": task["task_id"]}
        except RecheckBlocked as exc:
            outcome = RecheckOutcome("blocked", exc.reason_code)
            try:
                self.queue.finish(task, outcome, fingerprint=fingerprint, delay_seconds=self.retry_seconds)
            except LeaseLost:
                return {"claimed": True, "lease_lost": True, "task_id": task["task_id"]}
        except Exception as exc:
            # provider 예외 메시지에는 source/credential이 들어갈 수 있으므로 저장하지 않는다.
            code = "handler_" + re.sub(r"[^a-z0-9_]", "", type(exc).__name__.lower())[:50]
            delay = min(self.max_retry_seconds, self.retry_seconds * 2 ** min(task["attempt_count"] - 1, 10))
            try:
                self.queue.finish(task, RecheckOutcome("waiting", code), fingerprint=fingerprint, delay_seconds=delay)
            except LeaseLost:
                return {"claimed": True, "lease_lost": True, "task_id": task["task_id"]}
            outcome = RecheckOutcome("waiting", code)
        return {"claimed": True, "task_id": task["task_id"], "state": outcome.state,
                "reason_code": outcome.reason_code, "judgment_reused": reused}

    def run_loop(self, *, poll_seconds: float = 1, stop: Callable[[], bool] = lambda: False) -> None:
        _delay(poll_seconds, minimum=0.01)
        while not stop():
            self.run_once()
            time.sleep(poll_seconds)


def inspect_recheck_health(queue: PgRecheckQueue, *, project: str,
                           heartbeat_grace: float = 120, overdue_grace: float = 120) -> dict:
    """독립 감시에서 저장소 장애를 정상 상태로 오인하지 않게 한다."""
    try:
        result = queue.health(project=project, heartbeat_grace=heartbeat_grace, overdue_grace=overdue_grace)
        return {"status": "attention" if result["attention_required"] else "healthy", **result}
    except Exception:
        return {"status": "unavailable", "attention_required": True, "reason_code": "recheck_storage_unavailable"}


def _text(value: Any, maximum: int, *, allow_empty: bool = False) -> None:
    ensure_public_safe(value, "RecheckIdentity")
    if (not isinstance(value, str) or (not allow_empty and not value) or len(value) > maximum
            or any(char.isspace() for char in value)):
        raise ValueError("invalid recheck identity")


def _delay(value: float, minimum: float = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= 86400:
        raise ValueError("recheck interval is outside allowed bounds")

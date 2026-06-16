from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..dataset_contract import resolve_retention_policy
from ..ledger import Ledger
from .native_memory_sync_approval import ApprovalError, validate_goal3_live_approval
from ..ragflow_client import RagflowHttpClient
from .gc_backup import write_gc_backup
from .gc_safety_auditor import LedgerGCSafetyAuditor


SESSION_MEMORY_GC_OPERATION = "memory_regeneration_gc_dead_session_memory"
SESSION_MEMORY_GC_SCHEMA_VERSION = "agent_knowledge_session_memory_gc.v1"

# G-5 (M-GC contract §3.5 T1 / §6): session-memory-gc는 retention transition의
# 유일한 authorized 경로이며, retention policy가 'supersede_or_disable'인 dataset에만
# 동작해야 한다. opaque dataset_id만으로는 offline에서 policy를 알 수 없으므로, 선언된
# role/policy가 있을 때만 강제하고 없으면 기존 동작을 유지한다.
SESSION_MEMORY_GC_ALLOWED_RETENTION_POLICIES: frozenset[str] = frozenset(
    {"supersede_or_disable"}
)

# G-1 (M-GC contract §6): conservative non-bypassable retention floor for the
# irreversible hard-delete GC. A freshly disabled session_memory (e.g. a verify
# rollback that may be retried) must not be hard-deletable within this window,
# regardless of the value passed by the caller. The effective age gate is
# max(min_disabled_age_seconds, MIN_DISABLED_AGE_FLOOR_SECONDS).
MIN_DISABLED_AGE_FLOOR_SECONDS = 86400


@dataclass(frozen=True)
class SessionMemoryGcConfig:
    ledger_path: Path
    dataset_id: str
    ragflow_url: str
    max_items: int = 25
    min_disabled_age_seconds: int = MIN_DISABLED_AGE_FLOOR_SECONDS
    execute: bool = False
    # G-5: 선언된 dataset role/name/alias 또는 literal retention_policy. opaque
    # dataset_id만으로는 offline에서 policy를 해석할 수 없으므로, caller가 명시할 때만
    # retention gate를 강제한다. 둘 다 비면 기존 dataset_id-only 동작을 유지한다.
    declared_dataset_role: str = ""
    declared_retention_policy: str = ""
    # G-8 (recoverable delete): set to a private dir to back up the doc body +
    # recovery meta BEFORE the irreversible hard delete. Backup failure aborts
    # the delete (no delete without a backup).
    backup_dir: str = ""

    def effective_min_disabled_age_seconds(self) -> int:
        return max(int(self.min_disabled_age_seconds), MIN_DISABLED_AGE_FLOOR_SECONDS)

    def declared_policy_input(self) -> str:
        return (self.declared_retention_policy or self.declared_dataset_role or "").strip()


class SessionMemoryGcRunner:
    def __init__(
        self,
        *,
        config: SessionMemoryGcConfig,
        token: str = "",
        ragflow_client=None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.token = token
        # S0a 주입 seam: 기본 None이면 기존 동작(런타임에 RagflowHttpClient 생성 +
        # 실시간 clock). 특성화/테스트는 recording transport를 단 real client와 frozen
        # clock을 주입해 wire shape·순서를 결정적으로 고정한다(behavior-preserving).
        self._ragflow_client = ragflow_client
        self._now_fn = now_fn

    def _now(self) -> datetime:
        return self._now_fn() if self._now_fn is not None else datetime.now(timezone.utc)

    def run(self) -> dict:
        declared = self.config.declared_policy_input()
        if declared:
            try:
                policy = resolve_retention_policy(declared)
            except ValueError:
                policy = ""
            if policy not in SESSION_MEMORY_GC_ALLOWED_RETENTION_POLICIES:
                # G-5: 선언된 policy가 허용 집합 밖(또는 unknown)이면 후보 조회·삭제
                # 이전에 거부한다. 어떤 RAGFlow/ledger mutation도 일어나지 않는다.
                return self._blocked_retention_policy_report()
        ledger = Ledger(self.config.ledger_path)
        candidates = self._list_candidates(ledger)
        selected = candidates[: max(int(self.config.max_items), 1)]
        deleted_count = 0
        failed_count = 0
        attempted_count = 0
        revalidation_skipped_count = 0
        backed_up_count = 0
        failed_error_class = ""
        if self.config.execute and selected:
            ragflow = self._ragflow_client if self._ragflow_client is not None else RagflowHttpClient(
                base_url=self.config.ragflow_url,
                bearer_token=self.token,
                request_timeout_seconds=45,
            )
            for row in selected:
                document_id = str(row.get("ragflow_document_id") or "")
                knowledge_id = str(row.get("knowledge_id") or "")
                session_id_hash = str(row.get("session_id_hash") or "")
                # G-4 (M-GC §3.3 E2a/§6): intra-run TOCTOU guard. 후보는 루프 시작
                # 시점에 한 번 읽혔고, 앞선 row의 delete_documents가 네트워크 대기를
                # 가질 수 있어 그 사이 동시 writer가 active snapshot을 회전시키면
                # 선택된 row의 전제가 깨질 수 있다. 삭제 직전에 해당 row가 여전히
                # 자격(disabled·active-snapshot 아님·authorized replacement·미-tombstone)
                # 을 갖는지 재검증하고, 깨졌으면 mutation 없이 SKIP한다.
                if not self._still_qualifies(
                    ledger,
                    knowledge_id=knowledge_id,
                    session_id_hash=session_id_hash,
                ):
                    revalidation_skipped_count += 1
                    continue
                attempted_count += 1
                try:
                    if self.config.backup_dir:
                        # G-8: 백업이 성공해야만 삭제로 진행(백업 실패 시 예외→delete 안 함).
                        self._backup_before_delete(ledger, ragflow, row, document_id=document_id, knowledge_id=knowledge_id, session_id_hash=session_id_hash)
                        backed_up_count += 1
                    ragflow.delete_documents(self.config.dataset_id, [document_id])
                    self._mark_gc_deleted(ledger, knowledge_id)
                    self._record_audit(
                        ledger,
                        knowledge_id=knowledge_id,
                        document_id=document_id,
                        session_id_hash=session_id_hash,
                    )
                    deleted_count += 1
                except Exception as exc:
                    failed_error_class = exc.__class__.__name__
                    failed_count += 1
                    break
        return {
            "schema_version": SESSION_MEMORY_GC_SCHEMA_VERSION,
            "status": "ok" if failed_count == 0 else "partial_failed",
            "mode": "execute" if self.config.execute else "dry_run",
            "retention_policy_enforced": bool(self.config.declared_policy_input()),
            "min_disabled_age_floor_seconds": MIN_DISABLED_AGE_FLOOR_SECONDS,
            "effective_min_disabled_age_seconds": self.config.effective_min_disabled_age_seconds(),
            "eligible_count": len(candidates),
            "selected_count": len(selected),
            "attempted_count": attempted_count,
            "deleted_count": deleted_count,
            "revalidation_skipped_count": revalidation_skipped_count,
            "backed_up_count": backed_up_count,
            "backup_enabled": bool(self.config.backup_dir),
            "failed_count": failed_count,
            "failed_error_class": failed_error_class,
            "mutation_performed": bool(self.config.execute and deleted_count),
            "network_used": bool(self.config.execute),
            "raw_ids_printed": False,
        }

    def _blocked_retention_policy_report(self) -> dict:
        """G-5: 허용되지 않은(혹은 unknown) retention policy가 선언됐을 때, 어떤
        mutation도 하기 전에 돌려주는 거부 리포트. 모든 count는 0이고 network/mutation은
        False다."""
        return {
            "schema_version": SESSION_MEMORY_GC_SCHEMA_VERSION,
            "status": "blocked_retention_policy",
            "mode": "execute" if self.config.execute else "dry_run",
            "retention_policy_enforced": True,
            "min_disabled_age_floor_seconds": MIN_DISABLED_AGE_FLOOR_SECONDS,
            "effective_min_disabled_age_seconds": self.config.effective_min_disabled_age_seconds(),
            "eligible_count": 0,
            "selected_count": 0,
            "attempted_count": 0,
            "deleted_count": 0,
            "revalidation_skipped_count": 0,
            "failed_count": 0,
            "failed_error_class": "",
            "mutation_performed": False,
            "network_used": False,
            "raw_ids_printed": False,
        }

    def _backup_before_delete(self, ledger: Ledger, ragflow, row: dict, *, document_id: str, knowledge_id: str, session_id_hash: str) -> None:
        """G-8 (recoverable delete): 비가역 hard delete *직전*에 문서 본문(redacted MD)과
        복구 메타를 private backup store에 기록한다. 본문은 RAGFlow chunks로 재구성한다.
        이 단계가 예외를 던지면 호출부 try가 잡아 delete를 건너뛰므로 "백업 없는 삭제"가
        구조적으로 불가능하다. 복구 = 백업 본문을 재업로드 + 재임베딩 + ledger 복원."""
        body = "\n".join(ragflow.list_document_chunks(self.config.dataset_id, document_id))
        if not body.strip():
            # 빈 본문 백업은 lossy(복구 불가). 백업이 의미 없으면 삭제를 중단한다.
            raise ValueError("empty document body; backup would be lossy, aborting delete")
        snapshot = ledger.get_session_memory_active_snapshot(session_id_hash) or {}
        coverage = [
            str(edge.get("source_content_hash") or "")
            for edge in (ledger.list_session_memory_coverage(knowledge_id) or [])
        ]
        write_gc_backup(
            self.config.backup_dir,
            kind="session_memory",
            knowledge_id=knowledge_id,
            content_hash=str(row.get("content_hash") or ""),
            session_id_hash=session_id_hash,
            provider=str(row.get("provider") or ""),
            project=str(row.get("project") or ""),
            dataset_id=self.config.dataset_id,
            ragflow_document_id=document_id,
            body=body,
            replacement_knowledge_id=str(snapshot.get("active_knowledge_id") or ""),
            coverage=coverage,
        )

    def _still_qualifies(
        self,
        ledger: Ledger,
        *,
        knowledge_id: str,
        session_id_hash: str,
    ) -> bool:
        """G-4 (M-GC §3.3 E2a/§6): irreversible hard delete 직전 per-row 재검증.

        후보 리스트는 루프 시작 시 한 번만 읽혔으므로, 같은 run 안에서 앞선 row의
        삭제(네트워크 대기 포함) 사이 동시 writer가 active snapshot을 회전시키면
        뒤따르는 row의 선택 전제가 깨질 수 있다. 삭제 직전에 fresh row를 다시 읽어
        다음을 모두 만족할 때만 True를 돌려준다:
          - 여전히 ``status='disabled'``(rollback으로 re-enable되지 않음)
          - 여전히 등록된 active snapshot 대상이 *아님*(recall SoT가 아님)
          - replacement가 여전히 ``authorize_document``-valid(``_replacement_is_authorized``)
          - 아직 GC tombstone이 없음
        하나라도 깨지면 False -> 호출부가 mutation 없이 SKIP한다.
        """
        if not knowledge_id:
            return False
        row = ledger.get_by_knowledge_id(knowledge_id)
        if not row:
            return False
        if str(row.get("status") or "") != "disabled":
            return False
        if _is_gc_deleted(row):
            return False
        snapshot = ledger.get_session_memory_active_snapshot(session_id_hash) or {}
        if str(snapshot.get("active_knowledge_id") or "") == knowledge_id:
            return False
        return _replacement_is_authorized(
            ledger,
            session_id_hash=session_id_hash,
            old_knowledge_id=knowledge_id,
        )

    def _list_candidates(self, ledger: Ledger) -> list[dict]:
        cutoff = (
            self._now()
            - timedelta(seconds=self.config.effective_min_disabled_age_seconds())
        ).isoformat()
        with ledger._connect() as connection:
            rows = connection.execute(
                """
                SELECT old.*
                FROM knowledge_items old
                JOIN dirty_session_memory d ON d.session_id_hash = old.session_id_hash
                WHERE old.type = 'session_memory'
                  AND old.status = 'disabled'
                  AND old.authorization_status = 'disabled'
                  AND old.disabled_at != ''
                  AND old.disabled_at <= ?
                  AND old.ragflow_dataset_id = ?
                  AND old.ragflow_document_id != ''
                  AND d.status = 'promoted'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM session_memory_active_snapshots active_old
                    WHERE active_old.active_knowledge_id = old.knowledge_id
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM session_memory_active_snapshots active_snapshot
                    JOIN knowledge_items active
                      ON active.knowledge_id = active_snapshot.active_knowledge_id
                    WHERE active_snapshot.session_id_hash = old.session_id_hash
                      AND active.knowledge_id != old.knowledge_id
                      AND active.type = 'session_memory'
                      AND active.status IN ('indexed', 'active')
                      AND active.authorization_status = 'active'
                      AND active.disabled_at = ''
                      AND active.ragflow_dataset_id = old.ragflow_dataset_id
                      AND active.ragflow_document_id != ''
                  )
                ORDER BY old.disabled_at ASC, old.updated_at ASC
                """,
                (cutoff, self.config.dataset_id),
            ).fetchall()
        candidates: list[dict] = []
        for item in rows:
            row = dict(item)
            if _is_gc_deleted(row):
                continue
            if not _replacement_is_authorized(
                ledger,
                session_id_hash=str(row.get("session_id_hash") or ""),
                old_knowledge_id=str(row.get("knowledge_id") or ""),
            ):
                continue
            candidates.append(row)
        return candidates

    def _record_audit(
        self,
        ledger: Ledger,
        *,
        knowledge_id: str,
        document_id: str,
        session_id_hash: str,
    ) -> None:
        """G-3 (M-GC §3.4 A1/A2/A3): durable append-only audit row for the
        irreversible hard delete. Resolves the bound epoch markers from the
        active snapshot (replacement active_knowledge_id + updated_at, E3/A2) and
        the session's dirty row (dirty_at, E3). Only the doc-id hash is stored."""
        snapshot = ledger.get_session_memory_active_snapshot(session_id_hash) or {}
        dirty = ledger.get_dirty_session_memory(session_id_hash) or {}
        ledger.record_memory_gc_audit(
            gc_kind="session_memory",
            operation=SESSION_MEMORY_GC_OPERATION,
            schema_version=SESSION_MEMORY_GC_SCHEMA_VERSION,
            mode="execute" if self.config.execute else "dry_run",
            knowledge_id=knowledge_id,
            ragflow_document_id=document_id,
            dataset_id=self.config.dataset_id,
            replacement_knowledge_id=str(snapshot.get("active_knowledge_id") or ""),
            dirty_at=str(dirty.get("dirty_at") or ""),
            snapshot_updated_at=str(snapshot.get("updated_at") or ""),
            approval_operation=SESSION_MEMORY_GC_OPERATION,
            age_gate_seconds=self.config.effective_min_disabled_age_seconds(),
            mutated=True,
        )

    def _mark_gc_deleted(self, ledger: Ledger, knowledge_id: str) -> None:
        # S2: tombstone 쓰기 소유권을 GC Safety Lane seam(LedgerGCSafetyAuditor)으로 이동.
        # 동작 보존 — now_iso는 runner의 결정적 clock에서 온다.
        LedgerGCSafetyAuditor(ledger).mark_session_memory_deleted(
            knowledge_id,
            now_iso=self._now().isoformat(),
            operation=SESSION_MEMORY_GC_OPERATION,
        )


def _metadata_dict(row: dict) -> dict:
    try:
        metadata = json.loads(str(row.get("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def _is_gc_deleted(row: dict) -> bool:
    metadata = _metadata_dict(row)
    gc = metadata.get("session_memory_gc")
    return isinstance(gc, dict) and gc.get("status") == "deleted"


def _replacement_is_authorized(ledger: Ledger, *, session_id_hash: str, old_knowledge_id: str) -> bool:
    """G-2 (M-GC contract §6): the active replacement that justifies the
    irreversible hard delete of the superseded ``old`` must itself be
    recall-authorized via ``authorize_document``, not merely flagged active by
    the candidate SQL columns. ``authorize_document`` additionally re-checks
    evidence_status, coverage + coverage-edge completeness, expiry, dataset
    enablement, not-superseded, and that the doc IS the registered active
    snapshot. A replacement that fails these is not actually retrievable, so
    deleting ``old`` against it would be a recall regression.
    """
    if not session_id_hash:
        return False
    snapshot = ledger.get_session_memory_active_snapshot(session_id_hash)
    if not snapshot:
        return False
    active_knowledge_id = str(snapshot.get("active_knowledge_id") or "")
    if not active_knowledge_id or active_knowledge_id == old_knowledge_id:
        return False
    active = ledger.get_by_knowledge_id(active_knowledge_id)
    if not active:
        return False
    document_id = str(active.get("ragflow_document_id") or "")
    if not document_id:
        return False
    authorized = ledger.authorize_document(document_id)
    return bool(authorized and authorized.get("type") == "session_memory")


def main(argv: list[str] | None = None) -> int:
    import argparse

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--ragflow-url", required=True)
    parser.add_argument("--token-env", default="RAGFLOW_API_KEY")
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--min-disabled-age-seconds", type=int, default=MIN_DISABLED_AGE_FLOOR_SECONDS)
    # G-5 (M-GC §6): 선언된 dataset role / retention policy. canonical 플래그는
    # config 필드명과 정렬된 --declared-dataset-role / --declared-retention-policy이고,
    # --dataset-role / --retention-policy는 기존 호출 호환을 위한 alias다.
    parser.add_argument("--declared-dataset-role", "--dataset-role", dest="declared_dataset_role", default="")
    parser.add_argument("--declared-retention-policy", "--retention-policy", dest="declared_retention_policy", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="")
    # G-8 (recoverable delete): private dir로 삭제 전 본문+메타 백업. 백업 실패 시 삭제 중단.
    parser.add_argument("--backup-dir", dest="backup_dir", default="")
    args = parser.parse_args(raw_argv)

    token = os.environ.get(args.token_env, "")
    if args.execute:
        if not token:
            print("token env is not set", file=sys.stderr)
            return 2
        try:
            validate_goal3_live_approval(
                args.approval,
                operation=SESSION_MEMORY_GC_OPERATION,
                dataset_id=args.dataset_id,
                ragflow_base_url=args.ragflow_url,
                command_argv=["session-memory-gc", *raw_argv],
                max_wait_seconds=900,
            )
        except ApprovalError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    config = SessionMemoryGcConfig(
        ledger_path=Path(args.ledger),
        dataset_id=args.dataset_id,
        ragflow_url=args.ragflow_url,
        max_items=args.max_items,
        min_disabled_age_seconds=args.min_disabled_age_seconds,
        declared_dataset_role=args.declared_dataset_role,
        declared_retention_policy=args.declared_retention_policy,
        backup_dir=args.backup_dir,
        execute=bool(args.execute),
    )
    report = SessionMemoryGcRunner(config=config, token=token).run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("status") == "ok" else 1

"""Session-level transcript volume GC (RetiredIndexBridge-direct, coverage-by-summary).

전제(실측 2026-06-13): transcript 세션의 대다수(~73%)는 이미 session_memory로 요약돼 있다.
그러나 (a) per-chunk ``session_memory_coverage_edges``는 sparse/유실됐고, (b) 그 요약본들은
RetiredIndexBridge session-memory 데이터셋에 있을 뿐 neuron ledger(33행)엔 없다. 그래서 ledger-edge 기반
``transcript_volume_gc``는 실제 볼륨을 못 줄인다.

이 GC는 **ledger를 거치지 않고 RetiredIndexBridge를 직접** 본다:
  1. session-memory 데이터셋에서 *유효(active=status enabled, run DONE)* 요약본의 session_id_hash를
     모아 "요약된 세션 집합"을 만든다.
  2. transcript 데이터셋을 훑어, 그 집합에 속한 세션의 chunk를 backup 후 hard-delete(볼륨 회수).
삭제 기준은 age가 아니라 **"세션이 session_memory로 승격됐는가"**다(요약=durable, raw=staging).

안전:
- 요약(summary)이 *유효*할 때만 그 세션의 transcript를 지운다(disabled/rollback 요약은 제외).
- transcript chunk가 floor(``min_transcript_age_seconds``)보다 오래됐을 때만(세션이 아직 재요약
  중일 수 있는 갓 들어온 raw 보호). 나이를 못 읽으면 skip(fail-closed).
- backup-before-delete(빈 본문/백업 실패 시 삭제 중단) → ``gc_backup.restore_gc_backup``로 복구.
- dry-run/--execute+approval-gated, bounded(``max_items``), 스캔 페이지 bounded.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .native_memory_sync_approval import ApprovalError, validate_goal3_live_approval
from ..index_client import RetiredIndexBridgeHttpClient
from .gc_backup import write_gc_backup
from .gc_safety_auditor import hard_delete_documents

TRANSCRIPT_SESSION_GC_OPERATION = "memory_regeneration_gc_transcript_memory_session_volume_delete"
TRANSCRIPT_SESSION_GC_SCHEMA_VERSION = "agent_knowledge_transcript_session_gc.v1"

MIN_TRANSCRIPT_AGE_FLOOR_SECONDS = 86400

# circuit breaker: 개별 delete 실패는 skip-and-continue하되, 누적 실패가 이만큼이면 중단.
TRANSCRIPT_SESSION_GC_MAX_FAILURES = 10


@dataclass(frozen=True)
class TranscriptSessionGcConfig:
    transcript_dataset_id: str
    session_memory_dataset_id: str
    index_url: str
    backup_dir: str = ""
    max_items: int = 25
    min_transcript_age_seconds: int = MIN_TRANSCRIPT_AGE_FLOOR_SECONDS
    page_size: int = 100
    max_session_scan_pages: int = 500
    max_transcript_scan_pages: int = 500
    execute: bool = False

    def effective_min_transcript_age_seconds(self) -> int:
        return max(int(self.min_transcript_age_seconds), MIN_TRANSCRIPT_AGE_FLOOR_SECONDS)


@dataclass(frozen=True)
class _SessCandidate:
    document_id: str
    content_hash: str
    session_id_hash: str
    provider: str
    project: str


def _now_epoch() -> float:
    return time.time()


def _doc_age_seconds(doc: dict, now_epoch: float | None = None) -> float | None:
    """문서 나이(초). create_time(epoch s 또는 ms)을 우선 사용. 못 읽으면 None → fail-closed skip.

    now_epoch가 주어지면(S0a 주입 seam) 그 시각 기준으로 나이를 계산해 결정적으로 만든다."""
    raw = doc.get("create_time")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e12:  # epoch milliseconds
        value = value / 1000.0
    base = now_epoch if now_epoch is not None else _now_epoch()
    return max(0.0, base - value)


def _doc_is_active_summary(doc: dict) -> bool:
    # RetiredIndexBridge doc status '1' = enabled(retrievable), '0' = disabled. run DONE = parsed.
    if str(doc.get("status")) not in ("1", "1.0"):
        return False
    return str(doc.get("run") or "") == "DONE"


class TranscriptSessionGcRunner:
    def __init__(
        self,
        *,
        config: TranscriptSessionGcConfig,
        token: str = "",
        index_client=None,
        now_epoch_fn: Callable[[], float] | None = None,
    ):
        self.config = config
        self.token = token
        # S0a 주입 seam(기본 None=기존 동작, behavior-preserving). client는 read-scan용으로
        # 무조건 생성되므로 주입점을 명시해 특성화/테스트가 fake를 단다.
        self._index_client = index_client
        self._now_epoch_fn = now_epoch_fn

    def _now_epoch(self) -> float:
        return self._now_epoch_fn() if self._now_epoch_fn is not None else _now_epoch()

    def run(self) -> dict:
        retired_index_bridge = self._index_client if self._index_client is not None else RetiredIndexBridgeHttpClient(
            base_url=self.config.index_url,
            bearer_token=self.token,
            request_timeout_seconds=45,
        )
        summarized = self._summarized_sessions(retired_index_bridge)
        candidates = self._scan_candidates(retired_index_bridge, summarized)
        selected = candidates[: max(int(self.config.max_items), 1)]
        deleted = backed_up = attempted = failed = 0
        failed_error_class = ""
        if self.config.execute and selected and not self.config.backup_dir:
            return self._report(summarized, candidates, selected, 0, 0, 0, 1, "backup_dir_required")
        if self.config.execute and selected:
            for cand in selected:
                attempted += 1
                try:
                    body = "\n".join(retired_index_bridge.list_document_chunks(self.config.transcript_dataset_id, cand.document_id))
                    if not body.strip():
                        raise ValueError("empty document body; backup would be lossy, aborting delete")
                    write_gc_backup(
                        self.config.backup_dir,
                        kind="transcript_memory",
                        knowledge_id=cand.content_hash or cand.document_id,
                        content_hash=cand.content_hash,
                        session_id_hash=cand.session_id_hash,
                        provider=cand.provider,
                        project=cand.project,
                        dataset_id=self.config.transcript_dataset_id,
                        index_document_id=cand.document_id,
                        body=body,
                        replacement_knowledge_id="",
                        extra={"coverage": "session_level", "summarized_session": True},
                    )
                    backed_up += 1
                    hard_delete_documents(retired_index_bridge, self.config.transcript_dataset_id, [cand.document_id])
                    deleted += 1
                except Exception as exc:  # noqa: BLE001
                    # 단발 transient(RetiredIndexBridge CPU-bound 부하 중 delete 실패 등)에 배치 전체가
                    # 멈추지 않도록 skip-and-continue. 단 실패가 누적되면(systemic) circuit
                    # breaker로 중단. backup이 됐는데 delete가 실패한 doc은 다음 run에 다시
                    # eligible → 재시도(self-heal), backup은 content_hash 키라 중복 안 쌓임.
                    failed_error_class = exc.__class__.__name__
                    failed += 1
                    if failed >= TRANSCRIPT_SESSION_GC_MAX_FAILURES:
                        break
                    continue
        return self._report(summarized, candidates, selected, deleted, backed_up, attempted, failed, failed_error_class)

    def _report(self, summarized, candidates, selected, deleted, backed_up, attempted, failed, failed_error_class) -> dict:
        return {
            "schema_version": TRANSCRIPT_SESSION_GC_SCHEMA_VERSION,
            "status": "ok" if failed == 0 else "partial_failed",
            "mode": "execute" if self.config.execute else "dry_run",
            "coverage": "session_level",
            "min_transcript_age_floor_seconds": MIN_TRANSCRIPT_AGE_FLOOR_SECONDS,
            "effective_min_transcript_age_seconds": self.config.effective_min_transcript_age_seconds(),
            "summarized_session_count": len(summarized),
            "eligible_count": len(candidates),
            "selected_count": len(selected),
            "attempted_count": attempted,
            "deleted_count": deleted,
            "backed_up_count": backed_up,
            "failed_count": failed,
            "failed_error_class": failed_error_class,
            "backup_enabled": bool(self.config.backup_dir),
            "mutation_performed": bool(self.config.execute and deleted),
            "network_used": True,
            "raw_ids_printed": False,
            "hard_delete_performed": bool(self.config.execute and deleted),
        }

    def _summarized_sessions(self, retired_index_bridge) -> set:
        """유효(active) session_memory의 session_id_hash 집합. RetiredIndexBridge-direct, bounded scan."""
        sessions: set = set()
        for page in range(1, self.config.max_session_scan_pages + 1):
            try:
                docs = retired_index_bridge.list_documents(self.config.session_memory_dataset_id, page=page, page_size=self.config.page_size)
            except Exception:  # noqa: BLE001 - fail-closed: 못 읽으면 그만큼 덜 지운다
                break
            if not docs:
                break
            for doc in docs:
                if not isinstance(doc, dict) or not _doc_is_active_summary(doc):
                    continue
                sid = str((doc.get("meta_fields") or {}).get("session_id_hash") or "")
                if sid:
                    sessions.add(sid)
        return sessions

    def _scan_candidates(self, retired_index_bridge, summarized: set) -> list[_SessCandidate]:
        """transcript를 훑어, 요약된 세션 + floor 통과 chunk를 후보로 모은다(max_items까지)."""
        floor = self.config.effective_min_transcript_age_seconds()
        want = max(int(self.config.max_items), 1)
        out: list[_SessCandidate] = []
        for page in range(1, self.config.max_transcript_scan_pages + 1):
            try:
                docs = retired_index_bridge.list_documents(self.config.transcript_dataset_id, page=page, page_size=self.config.page_size)
            except Exception:  # noqa: BLE001
                break
            if not docs:
                break
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                meta = doc.get("meta_fields") or {}
                sid = str(meta.get("session_id_hash") or "")
                if not sid or sid not in summarized:
                    continue
                age = _doc_age_seconds(doc, self._now_epoch())
                if age is None or age < floor:  # 나이 불명/최근 → fail-closed skip
                    continue
                doc_id = str(doc.get("id") or doc.get("document_id") or "")
                if not doc_id:
                    continue
                out.append(
                    _SessCandidate(
                        document_id=doc_id,
                        content_hash=str(meta.get("content_hash") or ""),
                        session_id_hash=sid,
                        provider=str(meta.get("provider") or ""),
                        project=str(meta.get("project") or ""),
                    )
                )
                if len(out) >= want:
                    return out
        return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="transcript-session-gc")
    parser.add_argument("--transcript-dataset-id", required=True)
    parser.add_argument("--session-memory-dataset-id", required=True)
    parser.add_argument("--retired-index-bridge-url", required=True)
    parser.add_argument("--retired-index-bridge-token-env", default="RETIRED_INDEX_BRIDGE_API_KEY")
    parser.add_argument("--backup-dir", dest="backup_dir", default="")
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--min-transcript-age-seconds", type=int, default=MIN_TRANSCRIPT_AGE_FLOOR_SECONDS)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="")
    args = parser.parse_args(raw_argv)

    token = os.environ.get(args.retired_index_bridge_token_env, "")
    if not token:
        print("token env is not set", file=sys.stderr)
        return 2
    if args.execute:
        if not args.backup_dir:
            print("--backup-dir is required for --execute (no delete without backup)", file=sys.stderr)
            return 2
        try:
            validate_goal3_live_approval(
                args.approval,
                operation=TRANSCRIPT_SESSION_GC_OPERATION,
                dataset_id=args.transcript_dataset_id,
                index_base_url=args.retired_index_bridge_url,
                command_argv=["transcript-session-gc", *raw_argv],
                max_wait_seconds=900,
            )
        except ApprovalError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    config = TranscriptSessionGcConfig(
        transcript_dataset_id=args.transcript_dataset_id,
        session_memory_dataset_id=args.session_memory_dataset_id,
        index_url=args.retired_index_bridge_url,
        backup_dir=args.backup_dir,
        max_items=args.max_items,
        min_transcript_age_seconds=args.min_transcript_age_seconds,
        execute=bool(args.execute),
    )
    report = TranscriptSessionGcRunner(config=config, token=token).run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("status") == "ok" else 1

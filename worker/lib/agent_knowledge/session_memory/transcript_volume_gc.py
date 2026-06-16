"""Transcript-memory volume reclaim GC (coverage-driven, RAGFlow-direct).

기존 ``transcript_memory_gc``(disable-first)는 recall 노이즈만 줄이고 RAGFlow 인덱스
스토리지는 비우지 못한다. transcript-memory가 무한정 쌓이는 큰 데이터셋이므로, 볼륨을
실제로 회수하려면 **hard delete**가 필요하다. 안전하게 하려면:

- active·authorized·complete-coverage session_memory가 그 transcript를 *요약으로 보존*
  하고 있을 때만(= covered) 삭제한다(R2/R3). 요약이 raw를 대체하므로 recall은 유지된다.
- 덮는 session_memory의 active snapshot이 안정화 floor를 지났을 때만 삭제한다(rev 직후
  롤백 가능성 회피).
- 삭제 *전에* raw 본문을 backup store에 기록한다(G-8 recoverable delete). 백업 실패 시
  삭제 중단. 복구 = ``gc_backup.restore_gc_backup``(재업로드+재임베딩).

이 경로는 conversation_chunk ledger row에 의존하지 않는다(M9 은퇴 후 그 row가 없음).
대신 active session_memory의 ``session_memory_coverage_edges.source_content_hash``로
대상을 고르고, RAGFlow transcript 문서의 ``meta_fields.content_hash`` 정확 매칭으로
실문서를 찾는다(fuzzy 금지 — 매칭 안 되면 skip, 절대 추측 삭제 안 함).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from .native_memory_sync_approval import ApprovalError, validate_goal3_live_approval
from ..ragflow_client import RagflowHttpClient
from .gc_backup import write_gc_backup

TRANSCRIPT_VOLUME_GC_OPERATION = "memory_regeneration_gc_transcript_memory_volume_delete"
TRANSCRIPT_VOLUME_GC_SCHEMA_VERSION = "agent_knowledge_transcript_volume_gc.v1"

# 안정화 floor: 덮는 session_memory snapshot이 이만큼 지나야 그 covered transcript를 삭제.
MIN_ACTIVE_AGE_FLOOR_SECONDS = 86400


@dataclass(frozen=True)
class TranscriptVolumeGcConfig:
    ledger_path: Path
    transcript_dataset_id: str
    ragflow_url: str
    backup_dir: str = ""
    max_items: int = 25
    min_active_age_seconds: int = MIN_ACTIVE_AGE_FLOOR_SECONDS
    execute: bool = False

    def effective_min_active_age_seconds(self) -> int:
        return max(int(self.min_active_age_seconds), MIN_ACTIVE_AGE_FLOOR_SECONDS)


@dataclass(frozen=True)
class _Candidate:
    source_content_hash: str
    active_knowledge_id: str
    session_id_hash: str
    provider: str
    project: str


class TranscriptVolumeGcRunner:
    def __init__(
        self,
        *,
        config: TranscriptVolumeGcConfig,
        token: str = "",
        ragflow_client=None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.token = token
        # S0a 주입 seam(기본 None=기존 동작, behavior-preserving).
        self._ragflow_client = ragflow_client
        self._now_fn = now_fn

    def _now(self) -> datetime:
        return self._now_fn() if self._now_fn is not None else datetime.now(timezone.utc)

    def run(self) -> dict:
        ledger = Ledger(self.config.ledger_path)
        candidates = self._list_candidates(ledger)
        selected = candidates[: max(int(self.config.max_items), 1)]
        deleted_count = 0
        backed_up_count = 0
        unresolved_count = 0
        attempted_count = 0
        failed_count = 0
        failed_error_class = ""
        if self.config.execute and selected and not self.config.backup_dir:
            # G-8: 볼륨 회수 hard delete는 백업 없이 실행하지 않는다.
            return self._report(candidates, selected, 0, 0, 0, 0, 1, "backup_dir_required")
        if self.config.execute and selected:
            ragflow = self._ragflow_client if self._ragflow_client is not None else RagflowHttpClient(
                base_url=self.config.ragflow_url,
                bearer_token=self.token,
                request_timeout_seconds=45,
            )
            for cand in selected:
                doc_id = self._resolve_transcript_doc_id(ragflow, cand.source_content_hash, cand.session_id_hash)
                if not doc_id:
                    unresolved_count += 1
                    continue
                attempted_count += 1
                try:
                    body = "\n".join(ragflow.list_document_chunks(self.config.transcript_dataset_id, doc_id))
                    if not body.strip():
                        # 빈 본문 백업은 lossy(복구 불가) → 삭제 중단.
                        raise ValueError("empty document body; backup would be lossy, aborting delete")
                    write_gc_backup(
                        self.config.backup_dir,
                        kind="transcript_memory",
                        knowledge_id=cand.source_content_hash,
                        content_hash=cand.source_content_hash,
                        session_id_hash=cand.session_id_hash,
                        provider=cand.provider,
                        project=cand.project,
                        dataset_id=self.config.transcript_dataset_id,
                        ragflow_document_id=doc_id,
                        body=body,
                        replacement_knowledge_id=cand.active_knowledge_id,
                    )
                    backed_up_count += 1
                    ragflow.delete_documents(self.config.transcript_dataset_id, [doc_id])
                    deleted_count += 1
                except Exception as exc:  # noqa: BLE001
                    failed_error_class = exc.__class__.__name__
                    failed_count += 1
                    break
        return self._report(candidates, selected, deleted_count, backed_up_count, unresolved_count, attempted_count, failed_count, failed_error_class)

    def _report(self, candidates, selected, deleted, backed_up, unresolved, attempted, failed, failed_error_class) -> dict:
        return {
            "schema_version": TRANSCRIPT_VOLUME_GC_SCHEMA_VERSION,
            "status": "ok" if failed == 0 else "partial_failed",
            "mode": "execute" if self.config.execute else "dry_run",
            "min_active_age_floor_seconds": MIN_ACTIVE_AGE_FLOOR_SECONDS,
            "effective_min_active_age_seconds": self.config.effective_min_active_age_seconds(),
            "eligible_count": len(candidates),
            "selected_count": len(selected),
            "attempted_count": attempted,
            "deleted_count": deleted,
            "backed_up_count": backed_up,
            "unresolved_count": unresolved,
            "failed_count": failed,
            "failed_error_class": failed_error_class,
            "backup_enabled": bool(self.config.backup_dir),
            "mutation_performed": bool(self.config.execute and deleted),
            "network_used": bool(self.config.execute),
            "raw_ids_printed": False,
            "hard_delete_performed": bool(self.config.execute and deleted),
        }

    def _list_candidates(self, ledger: Ledger) -> list[_Candidate]:
        cutoff = (
            self._now() - timedelta(seconds=self.config.effective_min_active_age_seconds())
        ).isoformat()
        with ledger._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT
                    edge.source_content_hash AS source_content_hash,
                    active.knowledge_id AS active_knowledge_id,
                    active.session_id_hash AS session_id_hash,
                    active.provider AS provider,
                    active.project AS project,
                    active.ragflow_document_id AS active_document_id
                FROM session_memory_coverage_edges edge
                JOIN knowledge_items active
                  ON active.knowledge_id = edge.active_knowledge_id
                JOIN session_memory_active_snapshots snap
                  ON snap.active_knowledge_id = active.knowledge_id
                WHERE active.type = 'session_memory'
                  AND active.status IN ('indexed', 'active')
                  AND active.authorization_status = 'active'
                  AND active.disabled_at = ''
                  AND active.evidence_status = ?
                  AND active.coverage_status = 'complete'
                  AND active.coverage_gap_count = 0
                  AND active.coverage_duplicate_count = 0
                  AND active.ragflow_document_id != ''
                  AND coalesce(nullif(snap.updated_at, ''), nullif(snap.activated_at, '')) != ''
                  AND coalesce(nullif(snap.updated_at, ''), nullif(snap.activated_at, '')) <= ?
                  AND edge.source_content_hash != ''
                ORDER BY edge.source_content_hash ASC
                """,
                (SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS, cutoff),
            ).fetchall()
        candidates: list[_Candidate] = []
        auth_cache: dict[str, bool] = {}
        seen: set[str] = set()
        for item in rows:
            row = dict(item)
            source_hash = str(row.get("source_content_hash") or "")
            active_kid = str(row.get("active_knowledge_id") or "")
            if not source_hash or source_hash in seen:
                continue
            if not self._active_is_authorized(ledger, active_kid, str(row.get("active_document_id") or ""), auth_cache):
                continue
            seen.add(source_hash)
            candidates.append(
                _Candidate(
                    source_content_hash=source_hash,
                    active_knowledge_id=active_kid,
                    session_id_hash=str(row.get("session_id_hash") or ""),
                    provider=str(row.get("provider") or ""),
                    project=str(row.get("project") or ""),
                )
            )
        return candidates

    def _active_is_authorized(self, ledger: Ledger, active_knowledge_id: str, active_document_id: str, cache: dict[str, bool]) -> bool:
        # G-2 강도: 덮는 session_memory가 authorize_document로 실제 retrievable한지 재확인.
        if not active_knowledge_id or not active_document_id:
            return False
        if active_knowledge_id not in cache:
            authorized = ledger.authorize_document(active_document_id)
            cache[active_knowledge_id] = bool(authorized and authorized.get("type") == "session_memory")
        return cache[active_knowledge_id]

    def _resolve_transcript_doc_id(self, ragflow, content_hash: str, session_id_hash: str) -> str:
        """RAGFlow transcript 문서를 찾는다.

        RAGFlow keyword 검색은 meta가 아니라 문서 이름/내용을 매치하므로 content_hash(sha256)로는
        문서를 못 찾는다(G3 read model이 확인: "metadata-filtered retrieve는 단일 세션을 못 고른다,
        keyword listing이 authoritative"). 대신 session_id_hash fragment로 그 세션 문서를 좁힌 뒤
        (이름/내용에 fragment 포함) Python에서 ``meta_fields.content_hash`` 정확 매칭으로 단일 문서를
        고른다. fuzzy 금지: 정확히 1건일 때만 반환, 0/다건이면 skip(추측 삭제 방지).
        """
        fragment = str(session_id_hash or "").split(":")[-1][:12]
        if not fragment:
            return ""
        try:
            docs = ragflow.list_documents(self.config.transcript_dataset_id, keywords=fragment, page_size=200)
        except Exception:  # noqa: BLE001
            return ""
        matches = []
        for doc in docs or []:
            meta = doc.get("meta_fields") if isinstance(doc, dict) else None
            if isinstance(meta, dict) and str(meta.get("content_hash") or "") == content_hash:
                doc_id = str(doc.get("id") or doc.get("document_id") or "")
                if doc_id:
                    matches.append(doc_id)
        unique = sorted(set(matches))
        return unique[0] if len(unique) == 1 else ""


def main(argv: list[str] | None = None) -> int:
    import argparse

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="transcript-volume-gc")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--transcript-dataset-id", required=True)
    parser.add_argument("--ragflow-url", required=True)
    parser.add_argument("--token-env", default="RAGFLOW_API_KEY")
    parser.add_argument("--backup-dir", dest="backup_dir", default="")
    parser.add_argument("--max-items", type=int, default=25)
    parser.add_argument("--min-active-age-seconds", type=int, default=MIN_ACTIVE_AGE_FLOOR_SECONDS)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="")
    args = parser.parse_args(raw_argv)

    token = os.environ.get(args.token_env, "")
    if args.execute:
        if not token:
            print("token env is not set", file=sys.stderr)
            return 2
        if not args.backup_dir:
            print("--backup-dir is required for --execute (no delete without backup)", file=sys.stderr)
            return 2
        try:
            validate_goal3_live_approval(
                args.approval,
                operation=TRANSCRIPT_VOLUME_GC_OPERATION,
                dataset_id=args.transcript_dataset_id,
                ragflow_base_url=args.ragflow_url,
                command_argv=["transcript-volume-gc", *raw_argv],
                max_wait_seconds=900,
            )
        except ApprovalError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    config = TranscriptVolumeGcConfig(
        ledger_path=Path(args.ledger),
        transcript_dataset_id=args.transcript_dataset_id,
        ragflow_url=args.ragflow_url,
        backup_dir=args.backup_dir,
        max_items=args.max_items,
        min_active_age_seconds=args.min_active_age_seconds,
        execute=bool(args.execute),
    )
    report = TranscriptVolumeGcRunner(config=config, token=token).run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("status") == "ok" else 1

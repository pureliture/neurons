"""승인 memory card 배치 drain: ledger active-set ↔ mirror active-set 양방향 동기화.

- forward(B2): ledger 승인 card 중 mirror 미등록/내용변경분을 NativeMemoryMirrorWriter 로 write.
- supersede-sync(B1): ledger 에서 비활성(superseded/disabled/삭제)된 card 의 mirror active row 를
  mark_superseded 로 전이 → reconcile(Option C) 의 입력(pending)을 생성. 이 단계가 없으면
  "현재만 recall" 이 깨지고 reconcile 이 영영 빈손이 된다(spec 4-렌즈 critical).

god-class 회피: ledger/store 의 기존 read 만 조합한다. native_memory 로직은 store/writer 가,
memory_cards read 는 ledger 가 소유. runner 는 orchestration 만.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from ..ledger import Ledger
from .native_memory_mirror import (
    NativeMemoryMirrorStore,
    brain_id_for_project,
    session_tag_for,
)
from .native_memory_reconcile import (
    NativeMemoryReconcileConfig,
    NativeMemoryReconcileRunner,
)
from .native_memory_writer import ApprovedStatement, NativeMemoryMirrorWriter


@dataclass(frozen=True)
class NativeMemoryWriteConfig:
    batch_limit: int = 200
    brain_id_prefix: str = "/project/"


# writer.write 의 reason -> runner report 집계 키.
_REASON_KEY = {
    "duplicate_active": "duplicate_active",
    "governance_requires_approval": "governance_blocked",
    "operator_approval_required": "governance_blocked",
    "provenance_required": "governance_blocked",
    "eval_required": "governance_blocked",
    "add_message_rejected": "add_message_rejected",
}


def adapt_card_to_statement(card: dict, *, brain_id_prefix: str = "/project/") -> ApprovedStatement:
    """승인 memory_card(dict) -> ApprovedStatement.

    statement_id = card.memory_id(안정 PK, `mem_<hash[:16]>`), brain_id = prefix+project(메타 전용,
    라우팅 없음), text = summary(writer 가 redact_and_bound 멱등 적용), hash = content_hash(dedup 키),
    card_type 보존. approved=True — list_approved_memory_cards 는 CurationService.approve 통과분
    (state='active')만 반환하므로 high-risk 타입도 이미 승인됨(governance gate 통과).
    """
    return ApprovedStatement(
        statement_id=card["memory_id"],
        brain_id=brain_id_for_project(card["project"], prefix=brain_id_prefix),
        text=card["summary"],
        original_content_hash=card["content_hash"],
        card_type=card["card_type"],
        approved=True,
        provenance_status="pass",
        eval_status="pass",
    )


class NativeMemoryMirrorWriteRunner:
    def __init__(
        self,
        *,
        ledger: Ledger,
        store: NativeMemoryMirrorStore,
        writer: NativeMemoryMirrorWriter,
        config: NativeMemoryWriteConfig | None = None,
    ):
        self.ledger = ledger
        self.store = store
        self.writer = writer
        self.config = config or NativeMemoryWriteConfig()

    def run(self, *, dry_run: bool = False) -> dict:
        """dry_run=True 면 mutation(mark_superseded / writer.write) 없이 선별 카운트만.

        superseded_synced/pending 은 "would-process" 수, written 등은 0 으로 남는다.
        """
        report = {
            "superseded_synced": self._supersede_sync(dry_run=dry_run),
            "scanned": 0,
            "pending": 0,
            "written": 0,
            "duplicate_active": 0,
            "governance_blocked": 0,
            "add_message_rejected": 0,
            "batch_limit_hit": False,
        }
        cards = self.ledger.list_approved_memory_cards(limit=self.config.batch_limit)
        report["scanned"] = len(cards)
        # no silent cap: 승인 card 수가 batch_limit 을 채우면 오래된 card 누락 가능(§11).
        report["batch_limit_hit"] = len(cards) == self.config.batch_limit
        if not cards:
            return report

        tags = [session_tag_for(c["memory_id"]) for c in cards]
        existing = self.store.get_by_session_tags(tags)
        for card in cards:
            row = existing.get(session_tag_for(card["memory_id"]))
            if not self._is_pending(row, card):
                continue
            report["pending"] += 1
            if dry_run:
                continue
            result = self.writer.write(
                adapt_card_to_statement(card, brain_id_prefix=self.config.brain_id_prefix)
            )
            if result.get("written"):
                report["written"] += 1
            else:
                key = _REASON_KEY.get(result.get("reason", ""))
                if key:
                    report[key] += 1
        return report

    @staticmethod
    def _is_pending(row: dict | None, card: dict) -> bool:
        """미러 대상 선별: 미등록이거나, active 인데 내용(hash)이 바뀐 경우만.

        superseded 행은 제외(재활성화 방지 — major #2). writer 내부 dedup 은 이중 안전.
        """
        if row is None:
            return True
        return row["status"] == "active" and row["original_content_hash"] != card["content_hash"]

    def _supersede_sync(self, *, dry_run: bool = False) -> int:
        """mirror active row 중 ledger 에서 더 이상 active 가 아닌 orphan 을 superseded 로 전이.

        batch_limit 과 무관(mirror active 전체 순회 + ledger 단건 state 조회)하므로 forward 의
        페이지네이션이 멀쩡한 card 를 orphan 으로 오판하지 않는다. dry_run=True 면 전이 없이 카운트만.
        """
        synced = 0
        for active in self.store.list_active_statements():
            state = self.ledger.get_memory_card_state(active["statement_id"])
            if state != "active":  # None(삭제) 또는 superseded/disabled
                if dry_run:
                    synced += 1
                elif self.store.mark_superseded(active["statement_id"], superseded_by="ledger"):
                    synced += 1
        return synced


def run_native_memory_sync(
    *,
    ledger: Ledger,
    retired_index_bridge,
    memory_id: str,
    agent_id: str = "native-memory-sync",
    user_id: str = "",
    batch_limit: int = 200,
    reconcile_top_n: int = 50,
    dry_run: bool = False,
) -> dict:
    """1-tick 다단계: write-drain(supersede-sync + forward) → reconcile(Option C disable).

    단일 운영 memory_id 를 세 경로가 공유. dry_run 이면 write 는 선별만, reconcile 은 스킵.
    CLI 핸들러는 이 함수를 얇게 감싼다(argparse + token-env + approval + JSON 출력).
    """
    store = NativeMemoryMirrorStore(ledger)
    writer = NativeMemoryMirrorWriter(
        retired_index_bridge=retired_index_bridge, store=store, memory_id=memory_id, agent_id=agent_id, user_id=user_id
    )
    write_report = NativeMemoryMirrorWriteRunner(
        ledger=ledger, store=store, writer=writer,
        config=NativeMemoryWriteConfig(batch_limit=batch_limit),
    ).run(dry_run=dry_run)

    if dry_run:
        reconcile_report = {"status": "skipped_dry_run"}
    else:
        reconcile_report = NativeMemoryReconcileRunner(
            retired_index_bridge=retired_index_bridge, store=store,
            config=NativeMemoryReconcileConfig(memory_id=memory_id, reconcile_top_n=reconcile_top_n),
        ).run()

    return {"status": "ok", "dry_run": dry_run, "write": write_report, "reconcile": reconcile_report}


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="native-memory-sync")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--native-memory-id", default="")
    parser.add_argument("--agent-id", default="native-memory-sync")
    parser.add_argument("--batch-limit", type=int, default=200)
    parser.add_argument("--reconcile-top-n", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="")
    args = parser.parse_args(raw_argv)

    if not args.native_memory_id:
        print(json.dumps({"status": "not_executed_no_memory_binding"}, sort_keys=True))
        return 0
    if args.execute or not args.dry_run:
        print(
            json.dumps(
                {
                    "status": "blocked_live_execution",
                    "dry_run": False,
                    "mutation_performed": False,
                    "network_used": False,
                    "raw_ids_printed": False,
                    "failed_error_class": "live_native_memory_sync_not_vendored",
                },
                sort_keys=True,
            )
        )
        return 1

    report = run_native_memory_sync(
        ledger=Ledger(Path(args.ledger)),
        retired_index_bridge=None,
        memory_id=args.native_memory_id,
        agent_id=args.agent_id,
        batch_limit=args.batch_limit,
        reconcile_top_n=args.reconcile_top_n,
        dry_run=True,
    )
    report["mutation_performed"] = False
    report["network_used"] = False
    report["raw_ids_printed"] = False
    print(json.dumps(report, sort_keys=True))
    return 0

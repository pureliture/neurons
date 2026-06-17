"""Backlog promotion seed: mark un-summarized transcript sessions dirty.

Historical transcript sessions can sit outside the recent worker shadow-log
window, so they never enter the session-memory build queue. This helper scans
RAGFlow read surfaces to find transcript sessions that do not yet have an
active session-memory summary, then seeds the neuron-local dirty-session ledger.

It does not write to RAGFlow or delete transcript documents. The only mutation
is local ledger dirty-state seeding for the existing session-memory builder.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ..ledger import Ledger
from ..ragflow_client import RagflowHttpClient
from .transcript_session_gc import _doc_is_active_summary

TRANSCRIPT_BACKFILL_SCHEMA_VERSION = "agent_knowledge_transcript_backfill.v1"


@dataclass(frozen=True)
class TranscriptBackfillConfig:
    ledger_path: Path
    transcript_dataset_id: str
    session_memory_dataset_id: str
    ragflow_url: str
    max_sessions: int = 100
    page_size: int = 100
    max_session_scan_pages: int = 500
    max_transcript_scan_pages: int = 500


def _active_summarized_sessions(ragflow, dataset_id: str, *, page_size: int, max_pages: int) -> set[str]:
    sessions: set[str] = set()
    for page in range(1, max_pages + 1):
        try:
            docs = ragflow.list_documents(dataset_id, page=page, page_size=page_size)
        except Exception:  # noqa: BLE001 - fail closed: partial read means seed fewer sessions.
            break
        if not docs:
            break
        for doc in docs:
            if isinstance(doc, dict) and _doc_is_active_summary(doc):
                session_id_hash = str((doc.get("meta_fields") or {}).get("session_id_hash") or "")
                if session_id_hash:
                    sessions.add(session_id_hash)
    return sessions


class TranscriptBackfillRunner:
    def __init__(self, *, config: TranscriptBackfillConfig, token: str = ""):
        self.config = config
        self.token = token

    def run(self) -> dict:
        ragflow = RagflowHttpClient(
            base_url=self.config.ragflow_url,
            bearer_token=self.token,
            request_timeout_seconds=45,
        )
        summarized = _active_summarized_sessions(
            ragflow,
            self.config.session_memory_dataset_id,
            page_size=self.config.page_size,
            max_pages=self.config.max_session_scan_pages,
        )
        ledger = Ledger(self.config.ledger_path)
        want = max(int(self.config.max_sessions), 1)
        seeded: set[str] = set()
        for page in range(1, self.config.max_transcript_scan_pages + 1):
            try:
                docs = ragflow.list_documents(
                    self.config.transcript_dataset_id,
                    page=page,
                    page_size=self.config.page_size,
                )
            except Exception:  # noqa: BLE001 - fail closed: partial read means seed fewer sessions.
                break
            if not docs:
                break
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                meta = doc.get("meta_fields") or {}
                session_id_hash = str(meta.get("session_id_hash") or "")
                if not session_id_hash or session_id_hash in summarized or session_id_hash in seeded:
                    continue
                ledger.mark_session_memory_dirty(
                    session_id_hash=session_id_hash,
                    provider=str(meta.get("provider") or ""),
                    project=str(meta.get("project") or ""),
                    reason="backlog_backfill",
                )
                seeded.add(session_id_hash)
                if len(seeded) >= want:
                    return self._report(summarized, seeded)
        return self._report(summarized, seeded)

    def _report(self, summarized: set[str], seeded: set[str]) -> dict:
        return {
            "schema_version": TRANSCRIPT_BACKFILL_SCHEMA_VERSION,
            "status": "ok",
            "summarized_session_count": len(summarized),
            "seeded_session_count": len(seeded),
            "mutation_performed": bool(seeded),
            "network_used": True,
            "ragflow_write_performed": False,
            "raw_ids_printed": False,
        }


def main(argv: list[str] | None = None) -> int:
    import argparse

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="transcript-backfill")
    parser.add_argument("--ledger", required=True, help="neuron-local state ledger")
    parser.add_argument("--transcript-dataset-id", required=True)
    parser.add_argument("--session-memory-dataset-id", required=True)
    parser.add_argument("--ragflow-url", required=True)
    parser.add_argument("--token-env", default="RAGFLOW_API_KEY")
    parser.add_argument("--max-sessions", type=int, default=100)
    args = parser.parse_args(raw_argv)

    token = os.environ.get(args.token_env, "")
    if not token:
        print("token env is not set", file=sys.stderr)
        return 2
    config = TranscriptBackfillConfig(
        ledger_path=Path(args.ledger),
        transcript_dataset_id=args.transcript_dataset_id,
        session_memory_dataset_id=args.session_memory_dataset_id,
        ragflow_url=args.ragflow_url,
        max_sessions=args.max_sessions,
    )
    report = TranscriptBackfillRunner(config=config, token=token).run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0

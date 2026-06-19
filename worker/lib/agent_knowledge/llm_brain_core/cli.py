from __future__ import annotations

import argparse
import json
import sys

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.brain_read_model import LegacyLedgerBrainReadModel

from .ledger_adapter import LedgerSessionMemoryArtifactStore, LedgerSourceRefCatalog
from .runtime import build_runtime_brain_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge brain-context-resolve")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--current-file", action="append", default=[])
    parser.add_argument("--current-request", required=True)
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args(argv)

    try:
        ledger = Ledger.open_read_only(args.ledger)
        artifact_store = LedgerSessionMemoryArtifactStore(ledger)
        source_catalog = LedgerSourceRefCatalog(ledger)
        read_model = LegacyLedgerBrainReadModel(ledger)
        service = build_runtime_brain_service(
            project=args.project,
            artifact_store=artifact_store,
            read_model=read_model,
            source_catalog=source_catalog,
        )
        pack = service.brain_context_resolve(
            repository=args.repository,
            branch=args.branch,
            current_files=list(args.current_file or []),
            current_request=args.current_request,
            project=args.project,
            limit=args.limit,
        ).to_dict()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "llm_brain_context_resolve.v1",
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "message": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "schema_version": "llm_brain_context_resolve.v1",
                "status": "ok",
                "context_pack": pack,
            },
            sort_keys=True,
        )
    )
    return 0

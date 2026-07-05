from __future__ import annotations

import argparse
import json
import sys

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory.brain_read_model import LegacyLedgerBrainReadModel

from .ledger_adapter import LedgerSessionMemoryArtifactStore, LedgerSourceRefCatalog
from .models import CONTEXT_PACK_SCHEMA_VERSION
from .runtime import build_runtime_brain_service
from .runtime_graph import build_graph_adapter_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge brain-context-resolve")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--current-file", action="append", default=[])
    parser.add_argument("--current-request", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--response-mode", choices=["full", "compact", "degraded"], default="full")
    parser.add_argument("--consumer", choices=["unspecified", "codex", "claude-code", "gemini", "hermes"], default="unspecified")
    parser.add_argument("--enable-graph", action="store_true")
    parser.add_argument("--graph-required", action="store_true")
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
            graph_adapter=build_graph_adapter_from_env(
                enable_flag=True if args.enable_graph else None,
                required_flag=bool(args.graph_required),
            ),
        )
        pack = service.brain_context_resolve(
            repository=args.repository,
            branch=args.branch,
            current_files=list(args.current_file or []),
            current_request=args.current_request,
            project=args.project,
            limit=args.limit,
            consumer=args.consumer,
        ).to_dict(mode=args.response_mode)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": CONTEXT_PACK_SCHEMA_VERSION,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    # Do not echo raw exception text: it can carry private paths,
                    # tokens, or backend ids. Mirror the other entrypoints and
                    # emit only the exception class plus a static public message.
                    "message": "context resolve failed",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "schema_version": CONTEXT_PACK_SCHEMA_VERSION,
                "status": "ok",
                "context_pack": pack,
            },
            sort_keys=True,
        )
    )
    return 0

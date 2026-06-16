"""Server-owned command router for the neurons agent-knowledge surface."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable

from .ledger import Ledger
from .mcp_server import KnowledgeSearchService, build_ragflow_client, run_stdio_server
from .rag_ingress import state_cli
from .session_memory import (
    autopilot_cli,
    cleanup_readiness,
    memory_regeneration_cli,
    native_memory_write_runner,
    neuron_session_memory,
    session_memory_gc,
    session_memory_private_sync_cli,
    terminal_skipped_quarantine,
    transcript_backfill,
    transcript_memory_gc,
    transcript_session_gc,
    transcript_volume_gc,
    zombie_snapshot_repair,
)

BOUNDARY = "server worker -> state DB -> brain/session-memory -> GC safety planners"

CommandHandler = Callable[[list[str] | None], int]

PENDING_SERVER_COMMANDS = {
    "backfill",
    "context-for-prompt",
    "derived-memory-resources",
    "eval",
    "session-entry-recall",
    "transcript-migration",
    "transcript-quality",
    "transcript-resources",
    "transcript-retrieval",
}


def _pending_server_command(command: str) -> CommandHandler:
    def _main(argv: list[str] | None = None) -> int:
        _ = argv
        print(
            json.dumps(
                {
                    "schema_version": "neuron_knowledge_pending_command.v1",
                    "status": "blocked_pending_server_extraction",
                    "command": command,
                    "boundary": BOUNDARY,
                    "destination": "neurons",
                    "mutation_performed": False,
                    "network_used": False,
                },
                sort_keys=True,
            )
        )
        return 1

    return _main


COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "rag-ingress-state": state_cli.main,
    "memory-regeneration": memory_regeneration_cli.main,
    "cleanup-readiness": cleanup_readiness.main,
    "session-memory-private-sync": session_memory_private_sync_cli.main,
    "neuron-session-memory-build": neuron_session_memory.main,
    "native-memory-sync": native_memory_write_runner.main,
    "session-memory-gc": session_memory_gc.main,
    "transcript-backfill": transcript_backfill.main,
    "transcript-memory-gc": transcript_memory_gc.main,
    "transcript-session-gc": transcript_session_gc.main,
    "transcript-volume-gc": transcript_volume_gc.main,
    "session-memory-quarantine-terminal-skipped": terminal_skipped_quarantine.main,
    "session-memory-repair-zombie-snapshots": zombie_snapshot_repair.main,
    "backfill": _pending_server_command("backfill"),
    "context-for-prompt": _pending_server_command("context-for-prompt"),
    "derived-memory-resources": _pending_server_command("derived-memory-resources"),
    "eval": _pending_server_command("eval"),
    "memory": autopilot_cli.main,
    "session-entry-recall": _pending_server_command("session-entry-recall"),
    "transcript-migration": _pending_server_command("transcript-migration"),
    "transcript-quality": _pending_server_command("transcript-quality"),
    "transcript-resources": _pending_server_command("transcript-resources"),
    "transcript-retrieval": _pending_server_command("transcript-retrieval"),
}


def _mcp_stdio_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="neuron-knowledge mcp-stdio")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--dataset-id", action="append", default=[])
    parser.add_argument("--ragflow-url", default="")
    parser.add_argument("--token-env", default="")
    parser.add_argument("--policy-proxy-url", default="")
    parser.add_argument("--allow-private-results", action="store_true")
    parser.add_argument("--native-memory-id", default="")
    parser.add_argument("--state-db-recall", default="")
    parser.add_argument("--ragflow-direct-recall", action="store_true")
    args = parser.parse_args(argv)
    _ = args.state_db_recall
    _ = args.ragflow_direct_recall
    try:
        ledger = Ledger.open_read_only(args.ledger)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    token = os.environ.get(args.token_env, "") if args.token_env else ""
    ragflow = build_ragflow_client(
        ragflow_url=args.ragflow_url,
        token=token,
        policy_proxy_url=args.policy_proxy_url,
    )
    run_stdio_server(
        KnowledgeSearchService(
            ledger=ledger,
            ragflow=ragflow,
            dataset_ids=list(args.dataset_id or []),
            allow_private_results=bool(args.allow_private_results),
            native_memory_id=args.native_memory_id or os.environ.get("RAGFLOW_NATIVE_MEMORY_ID", ""),
        )
    )
    return 0


COMMAND_HANDLERS["mcp-stdio"] = _mcp_stdio_main


def _print_help() -> None:
    commands = "\n".join(f"  {command}" for command in sorted(COMMAND_HANDLERS))
    print(
        "usage: neuron-knowledge [--show-boundary] <command> [args...]\n\n"
        "Server-owned command router for neurons agent-knowledge surfaces.\n\n"
        "commands:\n"
        f"{commands}"
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv or raw_argv[0] in {"-h", "--help"}:
        _print_help()
        return 0
    if raw_argv[0] == "--show-boundary":
        print(BOUNDARY)
        return 0

    command = raw_argv[0]
    handler = COMMAND_HANDLERS.get(command)
    if handler is None:
        print(f"unknown neurons command: {command}", file=sys.stderr)
        return 2

    try:
        return int(handler(raw_argv[1:]) or 0)
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        raise


if __name__ == "__main__":
    raise SystemExit(main())

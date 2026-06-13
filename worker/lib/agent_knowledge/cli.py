"""Server-owned command router for the neurons agent-knowledge surface."""

from __future__ import annotations

import sys
from collections.abc import Callable

from .rag_ingress import state_cli
from .session_memory import (
    memory_regeneration_cli,
    native_memory_write_runner,
    neuron_session_memory,
    session_memory_gc,
    session_memory_private_sync_cli,
    terminal_skipped_quarantine,
    transcript_memory_gc,
    transcript_volume_gc,
    zombie_snapshot_repair,
)

BOUNDARY = "server worker -> state DB -> brain/session-memory -> GC safety planners"

CommandHandler = Callable[[list[str] | None], int]

COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "rag-ingress-state": state_cli.main,
    "memory-regeneration": memory_regeneration_cli.main,
    "session-memory-private-sync": session_memory_private_sync_cli.main,
    "neuron-session-memory-build": neuron_session_memory.main,
    "native-memory-sync": native_memory_write_runner.main,
    "session-memory-gc": session_memory_gc.main,
    "transcript-memory-gc": transcript_memory_gc.main,
    "transcript-volume-gc": transcript_volume_gc.main,
    "session-memory-quarantine-terminal-skipped": terminal_skipped_quarantine.main,
    "session-memory-repair-zombie-snapshots": zombie_snapshot_repair.main,
}


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

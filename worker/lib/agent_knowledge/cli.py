"""Server-owned command router for the neurons agent-knowledge surface."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable

from .couchdb_source import build_cli as couchdb_build_cli
from .couchdb_source import migration_flow_cli as couchdb_migration_flow_cli
from .couchdb_source import migration_cli
from .ledger import Ledger
from .llm_brain_core import cli as llm_brain_core_cli
from .llm_brain_core import bulk_semantic_cli as llm_brain_bulk_semantic_cli
from .llm_brain_core import bulk_semantic_trigger_cli as llm_brain_bulk_semantic_trigger_cli
from .llm_brain_core import couchdb_projection_cli as llm_brain_couchdb_projection_cli
from .llm_brain_core import graph_projection_status_cli as llm_brain_graph_projection_status_cli
from .llm_brain_core import graph_trigger_cli as llm_brain_graph_trigger_cli
from .llm_brain_core import portable_cli as llm_brain_portable_cli
from .llm_brain_core import projection_cli as llm_brain_projection_cli
from .llm_brain_core import regression_gate_cli as llm_brain_regression_gate_cli
from .llm_brain_core.runtime_graph import build_graph_adapter_from_env
from .mcp_server import KnowledgeSearchService, build_index_client, run_stdio_server
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
    "brain-context-resolve": llm_brain_core_cli.main,
    "brain-regression-gate": llm_brain_regression_gate_cli.main,
    "brain-export": llm_brain_portable_cli.export_main,
    "brain-import": llm_brain_portable_cli.import_main,
    "brain-project": llm_brain_projection_cli.main,
    "backfill": _pending_server_command("backfill"),
    "context-for-prompt": _pending_server_command("context-for-prompt"),
    "derived-memory-resources": _pending_server_command("derived-memory-resources"),
    "eval": _pending_server_command("eval"),
    "memory": autopilot_cli.main,
    "session-entry-recall": _pending_server_command("session-entry-recall"),
    "couchdb-session-memory-build": couchdb_build_cli.main,
    "couchdb-migration-flow": couchdb_migration_flow_cli.main,
    "couchdb-graph-trigger": llm_brain_graph_trigger_cli.main,
    "couchdb-graph-project": llm_brain_couchdb_projection_cli.main,
    "couchdb-graph-bulk-semantic": llm_brain_bulk_semantic_cli.main,
    "couchdb-bulk-semantic-trigger": llm_brain_bulk_semantic_trigger_cli.main,
    "couchdb-graph-status": llm_brain_graph_projection_status_cli.main,
    "transcript-migration": migration_cli.main,
    "transcript-quality": _pending_server_command("transcript-quality"),
    "transcript-resources": _pending_server_command("transcript-resources"),
    "transcript-retrieval": _pending_server_command("transcript-retrieval"),
}


class _ServiceWiringError(Exception):
    """recall service 와이어링 실패 + 매핑할 종료 코드(stderr 메시지는 redaction 완료)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _build_recall_service(args) -> KnowledgeSearchService:
    """mcp-stdio / mcp-http 공통 recall service 와이어링(단일 권위).

    두 transport main이 동일 service를 조립하던 복제 seam을 제거한다. 실패 시
    _ServiceWiringError(code, redacted_message)를 던져 호출부가 종료 코드로 매핑한다.
    오류 메시지는 raw 예외를 에코하지 않고 type name만 노출한다(private path 비노출).
    """
    try:
        steward_write_enabled = bool(
            getattr(args, "allow_steward_proposals", False)
            or getattr(args, "allow_steward_review_commit", False)
        )
        if steward_write_enabled:
            # MCP steward write runtimes attach to an existing production ledger.
            # Keep default Ledger(...) schema bootstrap for migration/parity tools,
            # but avoid running bootstrap during HTTP startup where SQLite-only
            # compatibility migrations can break server-backed stores.
            ledger = Ledger(args.ledger, initialize_schema=False)
        else:
            ledger = Ledger.open_read_only(args.ledger)
    except ValueError as exc:
        raise _ServiceWiringError(2, f"ledger open failed: {type(exc).__name__}") from exc
    retired_index_bridge = build_index_client()
    try:
        graph_adapter = build_graph_adapter_from_env(
            enable_flag=True if args.enable_graph else None,
            required_flag=bool(args.graph_required),
        )
    except Exception as exc:
        raise _ServiceWiringError(1, f"graph adapter unavailable: {type(exc).__name__}") from exc
    # M8 read cutover: when QDRANT_URL (+ COUCHDB_URL authority store) is configured,
    # fill brain.query's archive/evidence lanes from the Qdrant searchable mirror.
    # Additive -- the RetiredIndexBridge archive search is off in the live MCP (empty dataset_ids).
    from .rag_ingress.qdrant_recall import build_qdrant_brain_query_search_from_env

    mirror_search = build_qdrant_brain_query_search_from_env(os.environ)
    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=retired_index_bridge,
        dataset_ids=list(args.dataset_id or []),
        allow_private_results=bool(args.allow_private_results),
        native_memory_id=args.native_memory_id,
        graph_adapter=graph_adapter,
        mirror_search=mirror_search,
        allow_restricted_steward=bool(getattr(args, "allow_steward_review_commit", False)),
        allow_steward_auto_accept=False,
    )


def _add_recall_service_arguments(parser) -> None:
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--dataset-id", action="append", default=[])
    parser.add_argument("--policy-proxy-url", default="")
    parser.add_argument("--allow-private-results", action="store_true")
    parser.add_argument("--native-memory-id", default="")
    parser.add_argument("--state-db-recall", default="")
    parser.add_argument("--enable-graph", action="store_true")
    parser.add_argument("--graph-required", action="store_true")
    parser.add_argument(
        "--allow-steward-proposals",
        action="store_true",
        help="enable proposal-only Brain Steward writes; restricted approve/reject/auto-accept remain disabled unless review commit is also enabled",
    )
    parser.add_argument(
        "--allow-steward-review-commit",
        action="store_true",
        help="enable human-gated Brain Steward review commits approve/reject/supersede/stale; auto-accept remains disabled",
    )


def _mcp_stdio_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="neuron-knowledge mcp-stdio")
    _add_recall_service_arguments(parser)
    args = parser.parse_args(argv)
    _ = args.state_db_recall
    try:
        service = _build_recall_service(args)
    except _ServiceWiringError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    run_stdio_server(service)
    return 0


COMMAND_HANDLERS["mcp-stdio"] = _mcp_stdio_main


def _mcp_http_main(argv: list[str] | None = None) -> int:
    import argparse

    # mcp(FastMCP)는 optional extra(mcp-http)다. base CLI가 extra 없이도 동작하도록
    # transport 모듈은 이 핸들러 안에서만 lazy import한다.
    from . import mcp_http_server

    parser = argparse.ArgumentParser(prog="neuron-knowledge mcp-http")
    # 공통 인자: _mcp_stdio_main과 1:1 동일(service 구성 동일).
    _add_recall_service_arguments(parser)
    # HTTP transport 전용.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=mcp_http_server.DEFAULT_PORT)
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument("--allow-kubernetes-pod-ip", action="store_true")
    parser.add_argument("--allowed-host", action="append", default=[])
    args = parser.parse_args(argv)
    _ = args.state_db_recall
    try:
        allowed_hosts = mcp_http_server.resolve_allowed_hosts(args.allowed_host)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        service = _build_recall_service(args)
    except _ServiceWiringError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    mcp_http_server.serve(
        service,
        host=args.host,
        port=args.port,
        allow_non_loopback=args.allow_non_loopback,
        allow_kubernetes_pod_ip=args.allow_kubernetes_pod_ip,
        allowed_hosts=allowed_hosts,
    )
    return 0


COMMAND_HANDLERS["mcp-http"] = _mcp_http_main


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

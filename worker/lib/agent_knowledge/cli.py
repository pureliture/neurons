"""Server-owned command router for the neurons agent-knowledge surface."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from importlib import import_module

from .couchdb_source import build_cli as couchdb_build_cli
from .couchdb_source import historical_temporal_repair
from .couchdb_source import migration_cli
from .couchdb_source import temporal_evidence_inventory
from .ledger import Ledger
from .permission_audit import (
    DEFAULT_PERMISSION_AUDIT_STORE_URL,
    DEFAULT_TOKEN_REVIEW_URL,
    IndependentProductMutationMarkerReader,
    KubernetesTokenReviewer,
    LoopbackPermissionAuditStoreClient,
)
from .permission_audit_marker_runtime_env import (
    build_production_permission_audit_marker_reader,
)
from .rag_ingress import state_cli
from .rag_ingress import temporal_metadata_backfill
from .session_memory import (
    autopilot_cli,
    cleanup_readiness,
    eval_cli,
    eval_notify_discord,
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
_OBJECT_CLI_MODULE = ".llm_brain_core.objects.object_cli"

PENDING_SERVER_COMMANDS = {
    "backfill",
    "context-for-prompt",
    "derived-memory-resources",
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


def _lazy_handler(module_name: str, attribute: str = "main") -> CommandHandler:
    """실행할 command의 module만 늦게 import해 가벼운 router import를 보장한다."""

    def _main(argv: list[str] | None = None) -> int:
        handler = getattr(import_module(module_name, package=__package__), attribute)
        return handler(argv)

    return _main


def _lazy_object_handler(attribute: str) -> CommandHandler:
    return _lazy_handler(_OBJECT_CLI_MODULE, attribute)


def build_graph_adapter_from_env(*args, **kwargs):
    """MCP command가 실제 실행될 때만 graph runtime을 import하는 test seam."""
    from .llm_brain_core.runtime_graph import build_graph_adapter_from_env as build_adapter

    return build_adapter(*args, **kwargs)


def build_index_client():
    """MCP command가 실제 실행될 때만 search service 의존성을 import한다."""
    from .mcp_server import build_index_client as build_client

    return build_client()


def run_stdio_server(service) -> None:
    """MCP stdio transport를 실행 시점에만 import한다."""
    from .mcp_server import run_stdio_server as run_server

    run_server(service)


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
    "brain-context-resolve": _lazy_handler(".llm_brain_core.cli"),
    "object-query": _lazy_object_handler("object_query_main"),
    "agent-context-startup": _lazy_object_handler("agent_context_startup_main"),
    "artifact-preference-evaluate": _lazy_object_handler("artifact_preference_evaluate_main"),
    "object-explain": _lazy_object_handler("object_explain_main"),
    "corpus-status": _lazy_object_handler("corpus_status_main"),
    "corpus-ingest-plan": _lazy_object_handler("corpus_ingest_plan_main"),
    "corpus-ingest": _lazy_object_handler("corpus_ingest_main"),
    "corpus-ingest-readiness": _lazy_object_handler("corpus_ingest_readiness_main"),
    "object-authority-schema-ensure": _lazy_object_handler("object_authority_schema_ensure_main"),
    "source-to-candidate-graph": _lazy_object_handler("source_to_candidate_graph_main"),
    "candidate-review-edit": _lazy_object_handler("candidate_review_edit_main"),
    "approval-board-decide": _lazy_object_handler("approval_board_decide_main"),
    "golden-query-eval": _lazy_object_handler("golden_query_eval_main"),
    "source-to-candidate-runtime-readiness": _lazy_object_handler("source_to_candidate_runtime_readiness_main"),
    "temporal-acceptance-derive": _lazy_object_handler("temporal_acceptance_derive_main"),
    "okf-export": _lazy_object_handler("okf_export_main"),
    "brain-regression-gate": _lazy_handler(".llm_brain_core.regression_gate_cli"),
    "brain-export": _lazy_handler(".llm_brain_core.portable_cli", "export_main"),
    "brain-import": _lazy_handler(".llm_brain_core.portable_cli", "import_main"),
    "brain-project": _lazy_handler(".llm_brain_core.projection_cli"),
    "backfill": _pending_server_command("backfill"),
    "context-for-prompt": _pending_server_command("context-for-prompt"),
    "derived-memory-resources": _pending_server_command("derived-memory-resources"),
    "eval": eval_cli.main,
    "eval-notify-discord": eval_notify_discord.main,
    "memory": autopilot_cli.main,
    "session-entry-recall": _pending_server_command("session-entry-recall"),
    "couchdb-session-memory-build": couchdb_build_cli.main,
    "couchdb-migration-flow": _lazy_handler(".couchdb_source.migration_flow_cli"),
    "couchdb-graph-trigger": _lazy_handler(".llm_brain_core.graph_trigger_cli"),
    "couchdb-graph-project": _lazy_handler(".llm_brain_core.couchdb_projection_cli"),
    "couchdb-graph-bulk-semantic": _lazy_handler(".llm_brain_core.bulk_semantic_cli"),
    "couchdb-bulk-semantic-trigger": _lazy_handler(".llm_brain_core.bulk_semantic_trigger_cli"),
    "couchdb-graph-status": _lazy_handler(".llm_brain_core.graph_projection_status_cli"),
    "couchdb-projection-invalidation-canary": _lazy_handler(".rag_ingress.projection_invalidation_canary"),
    "couchdb-temporal-metadata-backfill": temporal_metadata_backfill.main,
    "couchdb-historical-temporal-repair": historical_temporal_repair.main,
    "couchdb-temporal-revision-rebuild": _lazy_handler(".rag_ingress.temporal_revision_rebuild"),
    "couchdb-temporal-evidence-inventory": temporal_evidence_inventory.main,
    "transcript-migration": migration_cli.main,
    "transcript-quality": _pending_server_command("transcript-quality"),
    "transcript-resources": _pending_server_command("transcript-resources"),
    "transcript-retrieval": _pending_server_command("transcript-retrieval"),
}


COMMAND_METADATA: dict[str, dict[str, object]] = {
    "neuron-session-memory-build": {
        "runtime_category": "legacy_compatibility",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    },
    "couchdb-session-memory-build": {
        "runtime_category": "active_runtime",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    },
    "couchdb-migration-flow": {
        "runtime_category": "human_gated_migration",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    },
    "transcript-migration": {
        "runtime_category": "human_gated_migration",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    },
    "object-authority-schema-ensure": {
        "runtime_category": "human_gated_schema_repair",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    },
    "couchdb-temporal-metadata-backfill": {
        "runtime_category": "human_gated_metadata_repair",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    },
    "couchdb-historical-temporal-repair": {
        "runtime_category": "human_gated_metadata_repair",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    },
    "couchdb-temporal-revision-rebuild": {
        "runtime_category": "human_gated_additive_repair",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    },
    "couchdb-projection-invalidation-canary": {
        "runtime_category": "human_gated_additive_canary",
        "deletion_candidate": False,
        "live_mutation_requires_approval": True,
    },
    "couchdb-temporal-evidence-inventory": {
        "runtime_category": "read_only",
        "deletion_candidate": False,
        "live_mutation_requires_approval": False,
    },
    "temporal-acceptance-derive": {
        "runtime_category": "read_only",
        "deletion_candidate": False,
        "live_mutation_requires_approval": False,
    },
}


class _ServiceWiringError(Exception):
    """recall service 와이어링 실패 + 매핑할 종료 코드(stderr 메시지는 redaction 완료)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _build_recall_service(
    args,
    *,
    permission_audit_marker_reader: IndependentProductMutationMarkerReader
    | None = None,
) -> "KnowledgeSearchService":
    """mcp-stdio / mcp-http 공통 recall service 와이어링(단일 권위).

    두 transport main이 동일 service를 조립하던 복제 seam을 제거한다. 실패 시
    _ServiceWiringError(code, redacted_message)를 던져 호출부가 종료 코드로 매핑한다.
    오류 메시지는 raw 예외를 에코하지 않고 type name만 노출한다(private path 비노출).
    """
    try:
        steward_write_enabled = bool(
            getattr(args, "allow_steward_proposals", False)
            or getattr(args, "allow_steward_review_commit", False)
            or getattr(args, "allow_object_authority_production_writes", False)
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
    semantic_ranker = None
    if os.environ.get("LLM_BRAIN_EMBEDDING_BASE_URL") and os.environ.get(
        "LLM_BRAIN_EMBEDDING_MODEL"
    ):
        try:
            from .session_memory.semantic_ranker import build_embedding_semantic_ranker

            semantic_ranker = build_embedding_semantic_ranker(environ=os.environ)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "brain query semantic ranker configured but unavailable: %s",
                type(exc).__name__,
            )
    audit_probe_enabled = bool(
        getattr(args, "allow_permission_sensitive_audit_probe", False)
    )
    token_reviewer = None
    store_append = None
    product_sentinel_reader = None
    if audit_probe_enabled:
        try:
            if permission_audit_marker_reader is None:
                permission_audit_marker_reader = (
                    build_production_permission_audit_marker_reader(os.environ)
                )
            if not isinstance(
                permission_audit_marker_reader,
                IndependentProductMutationMarkerReader,
            ):
                raise ValueError("exact marker reader unavailable")
            product_sentinel_reader = permission_audit_marker_reader
            token_reviewer = KubernetesTokenReviewer(
                str(getattr(args, "permission_audit_token_review_url", ""))
            )
            store_client = LoopbackPermissionAuditStoreClient(
                str(getattr(args, "permission_audit_store_url", ""))
            )
        except Exception as exc:
            message = (
                "permission audit exact marker reader unavailable"
                if "exact marker reader unavailable" in str(exc)
                else "permission audit exact marker configuration invalid"
            )
            raise _ServiceWiringError(
                2,
                message,
            ) from exc
        store_append = store_client.append_denied_once
    from .mcp_server import KnowledgeSearchService

    return KnowledgeSearchService(
        ledger=ledger,
        retired_index_bridge=retired_index_bridge,
        dataset_ids=list(args.dataset_id or []),
        allow_private_results=bool(args.allow_private_results),
        native_memory_id=args.native_memory_id,
        graph_adapter=graph_adapter,
        mirror_search=mirror_search,
        semantic_ranker=semantic_ranker,
        allow_restricted_steward=bool(getattr(args, "allow_steward_review_commit", False)),
        allow_steward_auto_accept=False,
        allow_production_object_authority_writes=bool(
            getattr(args, "allow_object_authority_production_writes", False)
        ),
        allow_permission_sensitive_audit_probe=audit_probe_enabled,
        permission_audit_token_reviewer=token_reviewer,
        permission_audit_store_append=store_append,
        permission_audit_product_sentinel_reader=product_sentinel_reader,
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
    parser.add_argument(
        "--allow-object-authority-production-writes",
        action="store_true",
        help="enable explicitly gated object authority production writes; every write still requires a per-call production_gate",
    )
    parser.add_argument(
        "--allow-permission-sensitive-audit-probe",
        action="store_true",
        help="enable the single bounded denial audit probe; disabled by default",
    )
    parser.add_argument(
        "--permission-audit-store-url",
        default=DEFAULT_PERMISSION_AUDIT_STORE_URL,
        help="loopback-only dedicated permission audit store endpoint",
    )
    parser.add_argument(
        "--permission-audit-token-review-url",
        default=DEFAULT_TOKEN_REVIEW_URL,
        help="explicit Kubernetes TokenReview API endpoint",
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

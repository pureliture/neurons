"""Dry-run/fail-closed CLI surface for server-owned RAG ingress state tools.

Backend selection (live drain only)
------------------------------------
Set ``INGRESS_DELIVERY_BACKEND`` in the environment to choose the live delivery
backend.  Accepted values:

``ragflow`` (default)
    Uses :class:`RagflowDeliveryBackend` against the RAGFlow dataset specified
    by ``--ragflow-url`` / ``--dataset-id``.

``couchdb``
    Uses :class:`CouchDBDeliveryBackend`.  Requires the following env vars:
    ``COUCHDB_URL``, ``COUCHDB_USER``, ``COUCHDB_PASSWORD``, ``COUCHDB_DB``.

The switch is read only when ``--dry-run`` is NOT set (live drain path).
Dry-run always short-circuits before constructing a backend.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..ledger import Ledger
from .backfill_apply import apply_backfill_to_state_db, read_queue_payloads
from .delivery_drain import drain_pending_deliveries
from .ingress_journal import IngressJournal
from .replay_delivery import replay_deliver_dispositions
from .state_db import RAGIngressStateDB

DEFAULT_TRANSCRIPT_TARGET_PROFILE = "ragflow-transcript-memory"

# Env var that selects the live delivery backend.
_BACKEND_ENV_VAR = "INGRESS_DELIVERY_BACKEND"
_BACKEND_RAGFLOW = "ragflow"
_BACKEND_COUCHDB = "couchdb"


class _DryRunReplayIngressClient:
    def enqueue_document_payload(self, payload: dict) -> dict:
        raise RuntimeError("dry-run replay CLI must not enqueue")


def _strip_program(argv: list[str]) -> list[str]:
    if argv and argv[0] == "rag-ingress-state":
        return argv[1:]
    return argv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rag-ingress-state")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay-deliver")
    replay.add_argument("--ledger", required=True)
    replay.add_argument("--target-profile", default=DEFAULT_TRANSCRIPT_TARGET_PROFILE)
    replay.add_argument("--ingress-url", required=True)
    replay.add_argument("--reason", required=True)
    replay.add_argument("--limit", type=int, default=50)
    replay.add_argument("--probe", action="store_true")
    replay.add_argument("--redact-paths", action="store_true")
    replay.add_argument("--dry-run", action="store_true")
    replay.add_argument("--from-journal", dest="from_journal")
    replay.add_argument("--approval")
    replay.add_argument("--max-runtime-seconds", type=float, default=300.0)

    backfill = subparsers.add_parser("backfill-apply")
    backfill.add_argument("--state-db", required=True)
    backfill.add_argument("--queue-root", required=True)
    backfill.add_argument("--redact-paths", action="store_true")
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--approval")
    backfill.add_argument("--max-runtime-seconds", type=float, default=300.0)

    drain = subparsers.add_parser("drain-deliveries")
    drain.add_argument("--state-db", required=True)
    drain.add_argument("--limit", type=int, default=10)
    drain.add_argument("--lease-owner", default="m8_drain")
    drain.add_argument("--max-attempts", type=int, default=3)
    drain.add_argument("--redact-paths", action="store_true")
    drain.add_argument("--dry-run", action="store_true")
    drain.add_argument("--approval")
    # RAGFlow backend args (default backend)
    drain.add_argument("--ragflow-url")
    drain.add_argument("--dataset-id")
    # CouchDB backend args (INGRESS_DELIVERY_BACKEND=couchdb)
    drain.add_argument("--couchdb-url", default=os.environ.get("COUCHDB_URL", ""))
    drain.add_argument("--couchdb-user", default=os.environ.get("COUCHDB_USER", ""))
    drain.add_argument("--couchdb-password", default=os.environ.get("COUCHDB_PASSWORD", ""))
    drain.add_argument("--couchdb-db", default=os.environ.get("COUCHDB_DB", "neurons_transcript_source"))
    drain.add_argument("--max-runtime-seconds", type=float, default=300.0)

    reconcile = subparsers.add_parser("reconcile-deliveries")
    reconcile.add_argument("--state-db", required=True)
    reconcile.add_argument("--status", action="append", default=[])
    reconcile.add_argument("--limit", type=int, default=10)
    reconcile.add_argument("--max-attempts", type=int, default=3)
    reconcile.add_argument("--redact-paths", action="store_true")
    reconcile.add_argument("--dry-run", action="store_true")
    reconcile.add_argument("--approval")
    reconcile.add_argument("--ragflow-url")
    reconcile.add_argument("--dataset-id")
    reconcile.add_argument("--max-runtime-seconds", type=float, default=300.0)

    return parser


def _print_live_blocked(command: str) -> int:
    print(
        json.dumps(
            {
                "status": "blocked_live_execution",
                "command": command,
                "dry_run": False,
                "mutation_performed": False,
                "network_used": False,
                "raw_ids_printed": False,
                "raw_paths_printed": False,
                "failed_error_class": "live_rag_ingress_state_cli_not_vendored",
            },
            sort_keys=True,
        )
    )
    return 1


def _require_redaction(command: str, redact_paths: bool) -> bool:
    if redact_paths:
        return True
    print(f"{command} requires --redact-paths", file=sys.stderr)
    return False


def _run_replay_deliver(args: argparse.Namespace) -> int:
    if not _require_redaction("replay-deliver", bool(args.redact_paths)):
        return 2
    if not args.dry_run:
        return _print_live_blocked("replay-deliver")
    try:
        journal = IngressJournal(args.from_journal) if args.from_journal else None
        result = replay_deliver_dispositions(
            ledger=Ledger.open_read_only(args.ledger),
            ingress_client=_DryRunReplayIngressClient(),
            target_profile=args.target_profile,
            reason=args.reason,
            limit=args.limit,
            probe=bool(args.probe),
            dry_run=True,
            journal=journal,
        )
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def _run_backfill_apply(args: argparse.Namespace) -> int:
    if not _require_redaction("backfill-apply", bool(args.redact_paths)):
        return 2
    if not args.dry_run:
        return _print_live_blocked("backfill-apply")
    try:
        payloads = read_queue_payloads(args.queue_root)
        result = apply_backfill_to_state_db(state_db=None, payloads=payloads, dry_run=True)
    except (ValueError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def _build_live_backend(args: argparse.Namespace, state_db: RAGIngressStateDB):
    """Construct the live DeliveryBackend selected by INGRESS_DELIVERY_BACKEND.

    Returns None (and prints an error) if the selection is unknown or the
    required env vars are missing for the chosen backend.  Keeping RAGFlow
    imports here avoids touching the couchdb path's import surface.
    """
    backend_name = os.environ.get(_BACKEND_ENV_VAR, _BACKEND_RAGFLOW).strip().lower()

    if backend_name == _BACKEND_COUCHDB:
        couchdb_url = getattr(args, "couchdb_url", "") or os.environ.get("COUCHDB_URL", "")
        couchdb_user = getattr(args, "couchdb_user", "") or os.environ.get("COUCHDB_USER", "")
        couchdb_password = getattr(args, "couchdb_password", "") or os.environ.get("COUCHDB_PASSWORD", "")
        couchdb_db = getattr(args, "couchdb_db", "") or os.environ.get("COUCHDB_DB", "neurons_transcript_source")
        missing = [v for v, k in [(couchdb_url, "COUCHDB_URL"), (couchdb_user, "COUCHDB_USER"), (couchdb_password, "COUCHDB_PASSWORD")] if not v]
        if missing:
            print(
                f"drain-deliveries: INGRESS_DELIVERY_BACKEND=couchdb requires "
                f"COUCHDB_URL, COUCHDB_USER, COUCHDB_PASSWORD env vars (missing: {missing})",
                file=sys.stderr,
            )
            return None
        from .couchdb_delivery_backend import build_couchdb_delivery_backend
        return build_couchdb_delivery_backend(
            state_db=state_db,
            couchdb_url=couchdb_url,
            couchdb_user=couchdb_user,
            couchdb_password=couchdb_password,
            couchdb_db=couchdb_db,
        )

    if backend_name == _BACKEND_RAGFLOW:
        # RAGFlow backend -- existing live path (remains blocked in CLI until vendored).
        return None  # caller will fall through to _print_live_blocked

    print(
        f"drain-deliveries: unknown INGRESS_DELIVERY_BACKEND={backend_name!r}; "
        f"accepted values: ragflow, couchdb",
        file=sys.stderr,
    )
    return None


def _run_drain_deliveries(args: argparse.Namespace) -> int:
    if not _require_redaction("drain-deliveries", bool(args.redact_paths)):
        return 2
    if not args.dry_run:
        # Live path: select backend via INGRESS_DELIVERY_BACKEND env var.
        backend_name = os.environ.get(_BACKEND_ENV_VAR, _BACKEND_RAGFLOW).strip().lower()
        if backend_name != _BACKEND_COUCHDB:
            # RAGFlow and unknown names keep the existing live-blocked gate.
            return _print_live_blocked("drain-deliveries")
        try:
            state_db = RAGIngressStateDB(args.state_db)
            backend = _build_live_backend(args, state_db)
            if backend is None:
                return 2
            result = drain_pending_deliveries(
                state_db=state_db,
                backend=backend,
                lease_owner=args.lease_owner,
                limit=args.limit,
                dry_run=False,
                max_attempts=args.max_attempts,
                max_runtime_seconds=args.max_runtime_seconds,
            )
        except (ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0
    try:
        state_db = RAGIngressStateDB(args.state_db)
        result = drain_pending_deliveries(
            state_db=state_db,
            lease_owner=args.lease_owner,
            limit=args.limit,
            dry_run=True,
            max_attempts=args.max_attempts,
            max_runtime_seconds=args.max_runtime_seconds,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def _run_reconcile_deliveries(args: argparse.Namespace) -> int:
    if not _require_redaction("reconcile-deliveries", bool(args.redact_paths)):
        return 2
    if not args.dry_run:
        return _print_live_blocked("reconcile-deliveries")
    try:
        state_db = RAGIngressStateDB(args.state_db)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    statuses = tuple(args.status or ["executing", "failed_retryable", "replayable"])
    selected: list[dict] = []
    for status in statuses:
        remaining = max(args.limit - len(selected), 0)
        if remaining <= 0:
            break
        selected.extend(state_db.list_delivery_jobs(status=status, limit=remaining))
    result = {
        "schema_version": "agent_knowledge_rag_ingress_delivery_reconcile.v1",
        "dry_run": True,
        "selected_count": len(selected),
        "selected_status_counts": {
            status: sum(1 for row in selected if str(row.get("status") or "") == status)
            for status in sorted(set(statuses))
        },
        "succeeded_count": 0,
        "retryable_count": 0,
        "replayable_count": 0,
        "quarantined_count": 0,
        "unknown_count": 0,
        "runtime_exceeded": False,
        "blockers": [],
        "execution_status": "dry_run",
        "mutation_performed": False,
        "network_used": False,
        "raw_ids_printed": False,
        "raw_paths_printed": False,
    }
    print(json.dumps(result, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = _strip_program(list(sys.argv[1:] if argv is None else argv))
    parser = _build_parser()
    args = parser.parse_args(raw_argv)
    if args.command == "replay-deliver":
        return _run_replay_deliver(args)
    if args.command == "backfill-apply":
        return _run_backfill_apply(args)
    if args.command == "drain-deliveries":
        return _run_drain_deliveries(args)
    if args.command == "reconcile-deliveries":
        return _run_reconcile_deliveries(args)
    parser.error("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Callable

from agent_knowledge.couchdb_source import build_cli as couchdb_build_cli
from agent_knowledge.session_memory.native_memory_sync_approval import (
    ApprovalError,
    validate_memory_enqueue_approval,
)

from agent_knowledge.llm_brain_core import couchdb_projection_cli

COUCHDB_MIGRATION_FLOW_SCHEMA_VERSION = "couchdb_migration_flow.v1"
COUCHDB_MIGRATION_FLOW_OPERATION = "couchdb_migration_flow"

ChildMain = Callable[[list[str] | None], int]


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="neuron-knowledge couchdb-migration-flow")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--project", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval", default="")
    parser.add_argument("--session-memory-approval", default="")
    parser.add_argument("--skip-session-memory", action="store_true")
    parser.add_argument("--skip-graph", action="store_true")
    parser.add_argument("--dataset-name", default="session-memory")
    parser.add_argument("--ragflow-url", default="")
    parser.add_argument("--token-env", default="RAGFLOW_API_KEY")
    parser.add_argument("--enable-graph", action="store_true", default=True)
    parser.add_argument("--graph-required", action="store_true")
    parser.add_argument("--reextract-entities", action="store_true")
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--progress-jsonl", default="")
    parser.add_argument("--dead-letter-jsonl", default="")
    parser.add_argument("--report-every", type=int, default=25)
    parser.add_argument("--couchdb-url", default="")
    parser.add_argument("--couchdb-db", default="")
    parser.add_argument("--couchdb-user", default="")
    parser.add_argument("--couchdb-password-env", default="COUCHDB_PASSWORD")
    args = parser.parse_args(raw_argv)

    try:
        report = run_migration_flow(
            ledger_path=Path(args.ledger),
            limit=int(args.limit),
            project=str(args.project or ""),
            provider=str(args.provider or ""),
            execute=bool(args.execute),
            approval=Path(args.approval) if args.approval else None,
            session_memory_approval=Path(args.session_memory_approval)
            if args.session_memory_approval
            else None,
            skip_session_memory=bool(args.skip_session_memory),
            skip_graph=bool(args.skip_graph),
            dataset_name=str(args.dataset_name or "session-memory"),
            ragflow_url=str(args.ragflow_url or ""),
            token_env=str(args.token_env or "RAGFLOW_API_KEY"),
            enable_graph=bool(args.enable_graph),
            graph_required=bool(args.graph_required),
            reextract_entities=bool(args.reextract_entities),
            runtime_dir=Path(args.runtime_dir) if args.runtime_dir else None,
            progress_jsonl=Path(args.progress_jsonl) if args.progress_jsonl else None,
            dead_letter_jsonl=Path(args.dead_letter_jsonl) if args.dead_letter_jsonl else None,
            report_every=int(args.report_every),
            couchdb_url=str(args.couchdb_url or ""),
            couchdb_db=str(args.couchdb_db or ""),
            couchdb_user=str(args.couchdb_user or ""),
            couchdb_password_env=str(args.couchdb_password_env or "COUCHDB_PASSWORD"),
            command_argv=raw_argv,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": COUCHDB_MIGRATION_FLOW_SCHEMA_VERSION,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "message": "couchdb migration flow failed",
                    "raw_paths_printed": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"dry_run", "ok"} else 1


def run_migration_flow(
    *,
    ledger_path: Path,
    limit: int,
    project: str = "",
    provider: str = "",
    execute: bool = False,
    approval: Path | None = None,
    session_memory_approval: Path | None = None,
    skip_session_memory: bool = False,
    skip_graph: bool = False,
    dataset_name: str = "session-memory",
    ragflow_url: str = "",
    token_env: str = "RAGFLOW_API_KEY",
    enable_graph: bool = True,
    graph_required: bool = False,
    reextract_entities: bool = False,
    runtime_dir: Path | None = None,
    progress_jsonl: Path | None = None,
    dead_letter_jsonl: Path | None = None,
    report_every: int = 25,
    couchdb_url: str = "",
    couchdb_db: str = "",
    couchdb_user: str = "",
    couchdb_password_env: str = "COUCHDB_PASSWORD",
    command_argv: list[str] | None = None,
    session_memory_main: ChildMain = couchdb_build_cli.main,
    graph_main: ChildMain = couchdb_projection_cli.main,
) -> dict[str, Any]:
    session_argv = _session_memory_argv(
        limit=limit,
        execute=execute,
        approval=session_memory_approval,
        dataset_name=dataset_name,
        ragflow_url=ragflow_url,
        token_env=token_env,
    )
    graph_argv = _graph_argv(
        ledger_path=ledger_path,
        limit=limit,
        project=project,
        provider=provider,
        enable_graph=enable_graph,
        graph_required=graph_required,
        reextract_entities=reextract_entities,
        runtime_dir=runtime_dir,
        progress_jsonl=progress_jsonl,
        dead_letter_jsonl=dead_letter_jsonl,
        report_every=report_every,
        couchdb_url=couchdb_url,
        couchdb_db=couchdb_db,
        couchdb_user=couchdb_user,
        couchdb_password_env=couchdb_password_env,
    )
    plan = _plan(
        execute=execute,
        skip_session_memory=skip_session_memory,
        skip_graph=skip_graph,
        session_argv=session_argv,
        graph_argv=graph_argv,
    )
    if not execute:
        return {
            "schema_version": COUCHDB_MIGRATION_FLOW_SCHEMA_VERSION,
            "status": "dry_run",
            "execute": False,
            "plan": plan,
            "mutation_performed": False,
            "network_used": False,
            "raw_paths_printed": False,
        }

    try:
        validate_memory_enqueue_approval(
            approval,
            operation=COUCHDB_MIGRATION_FLOW_OPERATION,
            command_argv=list(command_argv or []),
        )
    except ApprovalError as exc:
        return _blocked_report("approval_rejected", str(exc), plan=plan)
    if not skip_session_memory and session_memory_approval is None:
        return _blocked_report("session_memory_approval_required", "child approval is required", plan=plan)

    steps: list[dict[str, Any]] = []
    if not skip_session_memory:
        session_step = _call_json_child("session_memory_build", session_memory_main, session_argv)
        steps.append(session_step)
        if session_step["exit_code"] != 0:
            return _executed_report("partial", plan=plan, steps=steps)
    if not skip_graph:
        steps.append(_call_json_child("graph_project", graph_main, graph_argv))

    status = "ok" if all(step["exit_code"] == 0 for step in steps) else "partial"
    return _executed_report(status, plan=plan, steps=steps)


def _session_memory_argv(
    *,
    limit: int,
    execute: bool,
    approval: Path | None,
    dataset_name: str,
    ragflow_url: str,
    token_env: str,
) -> list[str]:
    argv: list[str] = ["--limit", str(int(limit))]
    if not execute:
        argv.append("--dry-run")
    else:
        argv.extend(["--approval", str(approval or "")])
    if dataset_name:
        argv.extend(["--dataset-name", dataset_name])
    if ragflow_url:
        argv.extend(["--ragflow-url", ragflow_url])
    if token_env:
        argv.extend(["--token-env", token_env])
    return argv


def _graph_argv(
    *,
    ledger_path: Path,
    limit: int,
    project: str,
    provider: str,
    enable_graph: bool,
    graph_required: bool,
    reextract_entities: bool,
    runtime_dir: Path | None,
    progress_jsonl: Path | None,
    dead_letter_jsonl: Path | None,
    report_every: int,
    couchdb_url: str,
    couchdb_db: str,
    couchdb_user: str,
    couchdb_password_env: str,
) -> list[str]:
    argv = ["--ledger", str(ledger_path), "--limit", str(int(limit)), "--extract-entities"]
    if project:
        argv.extend(["--project", project])
    if provider:
        argv.extend(["--provider", provider])
    if enable_graph:
        argv.append("--enable-graph")
    if graph_required:
        argv.append("--graph-required")
    if reextract_entities:
        argv.append("--reextract-entities")
    if runtime_dir is not None:
        argv.extend(["--runtime-dir", str(runtime_dir)])
    if progress_jsonl is not None:
        argv.extend(["--progress-jsonl", str(progress_jsonl)])
    if dead_letter_jsonl is not None:
        argv.extend(["--dead-letter-jsonl", str(dead_letter_jsonl)])
    argv.extend(["--report-every", str(int(report_every))])
    if couchdb_url:
        argv.extend(["--couchdb-url", couchdb_url])
    if couchdb_db:
        argv.extend(["--couchdb-db", couchdb_db])
    if couchdb_user:
        argv.extend(["--couchdb-user", couchdb_user])
    if couchdb_password_env:
        argv.extend(["--couchdb-password-env", couchdb_password_env])
    return argv


def _plan(
    *,
    execute: bool,
    skip_session_memory: bool,
    skip_graph: bool,
    session_argv: list[str],
    graph_argv: list[str],
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    if not skip_session_memory:
        steps.append(_step_plan("session_memory_build", session_argv))
    if not skip_graph:
        steps.append(_step_plan("graph_project", graph_argv))
    return {
        "mode": "execute" if execute else "dry_run",
        "step_count": len(steps),
        "steps": steps,
    }


def _step_plan(name: str, argv: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "argv_count": len(argv),
        "approval_required": name == "session_memory_build" and "--dry-run" not in argv,
    }


def _blocked_report(reason_code: str, reason: str, *, plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": COUCHDB_MIGRATION_FLOW_SCHEMA_VERSION,
        "status": "blocked",
        "reason_code": reason_code,
        "reason": reason,
        "execute": True,
        "plan": plan,
        "mutation_performed": False,
        "network_used": False,
        "raw_paths_printed": False,
    }


def _executed_report(status: str, *, plan: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": COUCHDB_MIGRATION_FLOW_SCHEMA_VERSION,
        "status": status,
        "execute": True,
        "plan": plan,
        "steps": steps,
        "mutation_performed": bool(steps),
        "network_used": bool(steps),
        "raw_paths_printed": False,
    }


def _call_json_child(name: str, child_main: ChildMain, argv: list[str]) -> dict[str, Any]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            exit_code = int(child_main(list(argv)) or 0)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
    return {
        "name": name,
        "exit_code": exit_code,
        "report": _parse_json(stdout.getvalue()),
        "stderr_present": bool(stderr.getvalue().strip()),
    }


def _parse_json(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text.splitlines()[-1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Any, Callable

from . import bulk_semantic_cli

BULK_SEMANTIC_TRIGGER_SCHEMA_VERSION = "llm_brain_bulk_semantic_trigger.v1"

ChildMain = Callable[[list[str] | None], int]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge couchdb-bulk-semantic-trigger")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--project", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--max-projects", type=int, default=5)
    # 0 => omit so the child resolves the cap from LLM_BRAIN_BULK_SEMANTIC_* env.
    parser.add_argument("--max-sessions-per-call", type=int, default=0)
    parser.add_argument("--max-session-chars", type=int, default=0)
    parser.add_argument("--allow-empty-sessions", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--report-every", type=int, default=25)
    parser.add_argument("--couchdb-url", default="")
    parser.add_argument("--couchdb-db", default="")
    parser.add_argument("--couchdb-user", default="")
    parser.add_argument("--couchdb-password-env", default="COUCHDB_PASSWORD")
    args = parser.parse_args(argv)

    report = run_bulk_semantic_trigger(
        ledger_path=Path(args.ledger),
        runtime_dir=Path(args.runtime_dir),
        limit=int(args.limit),
        project=str(args.project or ""),
        provider=str(args.provider or ""),
        max_projects=int(args.max_projects),
        max_sessions_per_call=int(args.max_sessions_per_call),
        max_session_chars=int(args.max_session_chars),
        allow_empty_sessions=bool(args.allow_empty_sessions),
        execute=bool(args.execute),
        report_every=int(args.report_every),
        couchdb_url=str(args.couchdb_url or ""),
        couchdb_db=str(args.couchdb_db or ""),
        couchdb_user=str(args.couchdb_user or ""),
        couchdb_password_env=str(args.couchdb_password_env or "COUCHDB_PASSWORD"),
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"dry_run", "ok", "already_running"} else 1


def run_bulk_semantic_trigger(
    *,
    ledger_path: Path,
    runtime_dir: Path,
    limit: int = 100,
    project: str = "",
    provider: str = "",
    max_projects: int = 5,
    max_sessions_per_call: int = 0,
    max_session_chars: int = 0,
    allow_empty_sessions: bool = False,
    execute: bool = False,
    report_every: int = 25,
    couchdb_url: str = "",
    couchdb_db: str = "",
    couchdb_user: str = "",
    couchdb_password_env: str = "COUCHDB_PASSWORD",
    bulk_main: ChildMain = bulk_semantic_cli.main,
) -> dict[str, Any]:
    progress_jsonl = runtime_dir / "bulk-semantic-progress.jsonl"
    dead_letter_jsonl = runtime_dir / "bulk-semantic-dead-letter.jsonl"
    child_argv = _child_argv(
        ledger_path=ledger_path,
        runtime_dir=runtime_dir,
        limit=limit,
        project=project,
        provider=provider,
        max_projects=max_projects,
        max_sessions_per_call=max_sessions_per_call,
        max_session_chars=max_session_chars,
        allow_empty_sessions=allow_empty_sessions,
        report_every=report_every,
        progress_jsonl=progress_jsonl,
        dead_letter_jsonl=dead_letter_jsonl,
        couchdb_url=couchdb_url,
        couchdb_db=couchdb_db,
        couchdb_user=couchdb_user,
        couchdb_password_env=couchdb_password_env,
    )
    plan = {
        "mode": "execute" if execute else "dry_run",
        "bounded": limit > 0,
        "limit": int(limit),
        "child_command": "couchdb-graph-bulk-semantic",
        "child_argv_count": len(child_argv),
        "runtime_lock": "graph-project.lock",
        "raw_paths_printed": False,
    }
    if not execute:
        return {
            "schema_version": BULK_SEMANTIC_TRIGGER_SCHEMA_VERSION,
            "status": "dry_run",
            "execute": False,
            "plan": plan,
            "mutation_performed": False,
            "network_used": False,
            "raw_paths_printed": False,
        }

    step = _call_child(bulk_main, child_argv)
    child_report = step.get("report") if isinstance(step.get("report"), dict) else {}
    child_status = str(child_report.get("status") or "")
    status = "already_running" if child_status == "already_running" else ("ok" if step["exit_code"] == 0 else "failed")
    mutation_performed, network_used = _derive_side_effects(child_report)
    return {
        "schema_version": BULK_SEMANTIC_TRIGGER_SCHEMA_VERSION,
        "status": status,
        "execute": True,
        "plan": plan,
        "step": step,
        "mutation_performed": mutation_performed,
        "network_used": network_used,
        "raw_paths_printed": False,
    }


def _child_argv(
    *,
    ledger_path: Path,
    runtime_dir: Path,
    limit: int,
    project: str,
    provider: str,
    max_projects: int,
    max_sessions_per_call: int,
    max_session_chars: int,
    allow_empty_sessions: bool,
    report_every: int,
    progress_jsonl: Path,
    dead_letter_jsonl: Path,
    couchdb_url: str,
    couchdb_db: str,
    couchdb_user: str,
    couchdb_password_env: str,
) -> list[str]:
    argv = [
        "--ledger",
        str(ledger_path),
        "--limit",
        str(int(limit)),
        "--runtime-dir",
        str(runtime_dir),
        "--progress-jsonl",
        str(progress_jsonl),
        "--dead-letter-jsonl",
        str(dead_letter_jsonl),
        "--report-every",
        str(int(report_every)),
        "--max-projects",
        str(int(max_projects)),
    ]
    # per-call sizing stays child<-env authoritative; only forward when explicitly set.
    if max_sessions_per_call > 0:
        argv.extend(["--max-sessions-per-call", str(int(max_sessions_per_call))])
    if max_session_chars > 0:
        argv.extend(["--max-session-chars", str(int(max_session_chars))])
    if project:
        argv.extend(["--project", project])
    if provider:
        argv.extend(["--provider", provider])
    if allow_empty_sessions:
        argv.append("--allow-empty-sessions")
    if couchdb_url:
        argv.extend(["--couchdb-url", couchdb_url])
    if couchdb_db:
        argv.extend(["--couchdb-db", couchdb_db])
    if couchdb_user:
        argv.extend(["--couchdb-user", couchdb_user])
    if couchdb_password_env:
        argv.extend(["--couchdb-password-env", couchdb_password_env])
    return argv


def _call_child(child_main: ChildMain, argv: list[str]) -> dict[str, Any]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            exit_code = int(child_main(list(argv)) or 0)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
    return {
        "name": "bulk_semantic",
        "exit_code": exit_code,
        "report": _parse_json(stdout.getvalue()),
        "stderr_present": bool(stderr.getvalue().strip()),
    }


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _derive_side_effects(report: dict[str, Any]) -> tuple[bool, bool]:
    """child report의 실제 카운터에서 mutation/network 여부를 유도한다.

    status 문자열은 신뢰할 수 없다: 부분 write 후 실패(status=failed)면 side-effect를
    거짓 음성으로, 0건 write 성공(status=ok)이면 거짓 양성으로 보고하기 때문이다.
    그래프 materialize/projection과 semantic 기록(entities/relations), LLM 배치 호출
    신호로 판정한다.
    """
    projection = report.get("projection")
    projection = projection if isinstance(projection, dict) else {}
    semantic = report.get("semantic")
    semantic = semantic if isinstance(semantic, dict) else {}
    materialized = _coerce_int(projection.get("materialized"))
    projected = _coerce_int(projection.get("projected"))
    failed = _coerce_int(projection.get("failed"))
    entities_written = _coerce_int(semantic.get("entities_written"))
    relations_written = _coerce_int(semantic.get("relations_written"))
    llm_batches = _coerce_int(semantic.get("llm_batches"))
    # 그래프 또는 semantic 계층에 실제로 기록된 게 있으면 mutation.
    mutation_performed = (
        materialized > 0
        or projected > 0
        or entities_written > 0
        or relations_written > 0
    )
    # LLM 배치 호출이나 실패 시도가 있었으면 백엔드/LLM 네트워크 접촉.
    network_used = mutation_performed or llm_batches > 0 or failed > 0
    return mutation_performed, network_used


def _parse_json(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text.splitlines()[-1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}

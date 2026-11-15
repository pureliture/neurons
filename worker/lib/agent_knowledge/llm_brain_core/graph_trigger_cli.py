from __future__ import annotations

import argparse
from collections.abc import Mapping
import contextlib
from enum import StrEnum
import io
import json
from pathlib import Path
import sys
from typing import Any, Callable

from ._util import (
    SafeErrorClass,
    SafeErrorCode,
    SafeErrorType,
    SafeReasonCode,
    classify_safe_error_class,
    classify_safe_error_code,
    classify_safe_error_param,
    classify_safe_error_step,
    classify_safe_error_type,
    extract_safe_error_diagnostics,
    sanitize_http_status,
    sanitize_structured_reason_code,
)
from . import couchdb_projection_cli
from .models import PROJECTION_SCHEMA_VERSION

GRAPH_TRIGGER_SCHEMA_VERSION = "llm_brain_graph_trigger.v1"
_ALLOWED_CHILD_ERROR_SCHEMAS = frozenset(
    {
        couchdb_projection_cli.COUCHDB_GRAPH_PROJECTION_SCHEMA_VERSION,
        PROJECTION_SCHEMA_VERSION,
    }
)


class SafeChildReportStatus(StrEnum):
    """Closed allowlist of child CLI report status values accepted from stderr."""

    FAILED = "failed"
    PARTIAL = "partial"
    ERROR = "error"
    UNAVAILABLE = "unavailable"
    ALREADY_RUNNING = "already_running"


_KNOWN_CHILD_STATUSES: frozenset[str] = frozenset(
    member.value for member in SafeChildReportStatus
)


def parse_sanitized_child_error_report(stderr_text: str) -> dict[str, Any]:
    """Recover only closed-allowlist error codes from child CLI JSON stderr."""
    text = str(stderr_text or "").strip()
    if not text:
        return {}
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, Mapping):
            continue
        schema_version = str(parsed.get("schema_version") or "")
        if schema_version not in _ALLOWED_CHILD_ERROR_SCHEMAS:
            continue

        raw_status = parsed.get("status")
        status = (
            raw_status.strip()
            if isinstance(raw_status, str) and raw_status.strip() in _KNOWN_CHILD_STATUSES
            else SafeChildReportStatus.FAILED.value
        )
        error_class = classify_safe_error_class(parsed.get("error_class"))
        http_status = sanitize_http_status(parsed.get("http_status"))
        error_code = classify_safe_error_code(parsed.get("error_code"))
        error_type = classify_safe_error_type(parsed.get("error_type"))
        error_param = classify_safe_error_param(parsed.get("error_param"))
        error_step = classify_safe_error_step(parsed.get("error_step"))
        structured_reason = sanitize_structured_reason_code(parsed.get("reason_code"))

        if structured_reason and structured_reason != SafeReasonCode.UNKNOWN_REASON_CODE.value:
            reason_code = structured_reason
        elif error_class or http_status is not None or error_code or error_type:
            if error_code and error_code != SafeErrorCode.UNKNOWN_ERROR_CODE.value:
                detail = error_code
            elif error_type and error_type != SafeErrorType.UNKNOWN_ERROR_TYPE.value:
                detail = error_type
            else:
                detail = error_code or error_type
            base_class = error_class or SafeErrorClass.UNCLASSIFIED_ERROR.value
            parts = [base_class]
            if http_status is not None:
                parts.append(f"http_{http_status}")
            if detail and detail != base_class:
                parts.append(detail)
            reason_code = ":".join(parts)
        else:
            reason_code = structured_reason or SafeChildReportStatus.FAILED.value

        safe_report: dict[str, Any] = {
            "schema_version": schema_version,
            "status": status,
            "reason_code": reason_code,
            "raw_paths_printed": False,
        }
        if error_class:
            safe_report["error_class"] = error_class
        if http_status is not None:
            safe_report["http_status"] = http_status
        if error_code:
            safe_report["error_code"] = error_code
        if error_type:
            safe_report["error_type"] = error_type
        if error_param:
            safe_report["error_param"] = error_param
        if error_step:
            safe_report["error_step"] = error_step
        return safe_report
    return {}

ChildMain = Callable[[list[str] | None], int]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge couchdb-graph-trigger")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--project", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--graph-required", action="store_true")
    parser.add_argument("--extract-entities", action="store_true")
    parser.add_argument("--reextract-entities", action="store_true")
    parser.add_argument("--report-every", type=int, default=25)
    parser.add_argument("--couchdb-url", default="")
    parser.add_argument("--couchdb-db", default="")
    parser.add_argument("--couchdb-user", default="")
    parser.add_argument("--couchdb-password-env", default="COUCHDB_PASSWORD")
    args = parser.parse_args(argv)

    report = run_graph_trigger(
        ledger_path=Path(args.ledger),
        runtime_dir=Path(args.runtime_dir),
        limit=int(args.limit),
        project=str(args.project or ""),
        provider=str(args.provider or ""),
        execute=bool(args.execute),
        graph_required=bool(args.graph_required),
        extract_entities=bool(args.extract_entities),
        reextract_entities=bool(args.reextract_entities),
        report_every=int(args.report_every),
        couchdb_url=str(args.couchdb_url or ""),
        couchdb_db=str(args.couchdb_db or ""),
        couchdb_user=str(args.couchdb_user or ""),
        couchdb_password_env=str(args.couchdb_password_env or "COUCHDB_PASSWORD"),
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"dry_run", "ok", "already_running"} else 1


def run_graph_trigger(
    *,
    ledger_path: Path,
    runtime_dir: Path,
    limit: int = 25,
    project: str = "",
    provider: str = "",
    execute: bool = False,
    graph_required: bool = False,
    extract_entities: bool = False,
    reextract_entities: bool = False,
    report_every: int = 25,
    couchdb_url: str = "",
    couchdb_db: str = "",
    couchdb_user: str = "",
    couchdb_password_env: str = "COUCHDB_PASSWORD",
    graph_main: ChildMain = couchdb_projection_cli.main,
) -> dict[str, Any]:
    progress_jsonl = runtime_dir / "graph-trigger-progress.jsonl"
    dead_letter_jsonl = runtime_dir / "graph-trigger-dead-letter.jsonl"
    child_argv = _child_argv(
        ledger_path=ledger_path,
        runtime_dir=runtime_dir,
        limit=limit,
        project=project,
        provider=provider,
        graph_required=graph_required,
        extract_entities=extract_entities,
        reextract_entities=reextract_entities,
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
        "child_command": "couchdb-graph-project",
        "child_argv_count": len(child_argv),
        "runtime_lock": "graph-project.lock",
        "raw_paths_printed": False,
    }
    if not execute:
        return {
            "schema_version": GRAPH_TRIGGER_SCHEMA_VERSION,
            "status": "dry_run",
            "execute": False,
            "plan": plan,
            "mutation_performed": False,
            "network_used": False,
            "raw_paths_printed": False,
        }

    step = _call_child(graph_main, child_argv)
    child_report = step.get("report") if isinstance(step.get("report"), dict) else {}
    child_status = str(child_report.get("status") or "")
    status = "already_running" if child_status == "already_running" else ("ok" if step["exit_code"] == 0 else "failed")
    mutation_performed, network_used = _derive_side_effects(child_report)
    return {
        "schema_version": GRAPH_TRIGGER_SCHEMA_VERSION,
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
    graph_required: bool,
    extract_entities: bool,
    reextract_entities: bool,
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
        "--enable-graph",
    ]
    if project:
        argv.extend(["--project", project])
    if provider:
        argv.extend(["--provider", provider])
    if graph_required:
        argv.append("--graph-required")
    # episode-only by default; per-session entity extraction is debug/manual opt-in.
    if extract_entities:
        argv.append("--extract-entities")
    if reextract_entities:
        argv.append("--reextract-entities")
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
    child_diag: dict[str, Any] = {}
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            exit_code = int(child_main(list(argv)) or 0)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001 - fail-closed without leaking raw text
            exit_code = 1
            child_diag = extract_safe_error_diagnostics(exc)
    stderr_text = stderr.getvalue()
    report = _parse_json(stdout.getvalue())
    if not report and stderr_text.strip():
        report = parse_sanitized_child_error_report(stderr_text)
    step: dict[str, Any] = {
        "name": "graph_project",
        "exit_code": exit_code,
        "report": report,
        "stderr_present": bool(stderr_text.strip()) or bool(child_diag),
    }
    source_diag = child_diag or (report if exit_code != 0 and isinstance(report, dict) else {})
    for key in (
        "error_class",
        "reason_code",
        "http_status",
        "error_code",
        "error_type",
        "error_param",
        "error_step",
    ):
        if key in source_diag and source_diag[key] not in ("", None):
            step[key] = source_diag[key]
    return step


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _derive_side_effects(report: dict[str, Any]) -> tuple[bool, bool]:
    """child report의 실제 카운터에서 mutation/network 여부를 유도한다.

    status 문자열은 신뢰할 수 없다: 부분 write 후 실패(status=failed)면 side-effect를
    거짓 음성으로, 0건 write 성공(status=ok)이면 거짓 양성으로 보고하기 때문이다.
    그래프에 실제로 기록(projected)했거나 백엔드에 접촉(projected/duplicates/failed)한
    신호로 판정한다.
    """
    projection = report.get("projection")
    projection = projection if isinstance(projection, dict) else {}
    projected = _coerce_int(projection.get("projected"))
    duplicates = _coerce_int(projection.get("duplicates"))
    failed = _coerce_int(projection.get("failed"))
    # 그래프에 새 에피소드가 기록됐으면 mutation.
    mutation_performed = projected > 0
    # projected/duplicate/failed 어느 쪽이든 그래프 백엔드에 접촉했으면 network.
    network_used = mutation_performed or duplicates > 0 or failed > 0
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

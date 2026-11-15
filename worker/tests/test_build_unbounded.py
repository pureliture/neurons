"""Synthetic subprocess tests for the unbounded build guard policy.

No DB/network: a fake `python` and `timeout` are placed first in PATH.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


WORKER = Path(__file__).resolve().parents[1]
SCRIPT = WORKER / "deploy/session-memory/build-once.sh"


def _resolve_real_py(
    *,
    executable: str | None = None,
    which_python3=shutil.which,
    env_python: str | None = None,
) -> str:
    """Resolve a portable Python 3 interpreter (`sys.executable` -> `which('python3')` -> hard fail)."""
    candidates = [
        env_python if env_python is not None else os.environ.get("PYTHON", ""),
        sys.executable if executable is None else executable,
        which_python3("python3") or "",
    ]
    for cand in candidates:
        token = str(cand or "").strip()
        if token and Path(token).is_file() and os.access(token, os.X_OK):
            return token
    pytest.fail("No executable python3 interpreter available for REAL_PY (sys.executable and python3 missing)")


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    python = bin_dir / "python"
    python.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$PY_LOG"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "agent_knowledge.cli" ]; then
  echo "first-line"
  sleep "${FAKE_SLEEP:-0}"
  case "${FAKE_REPORT_MODE:-json}" in
    none) echo "second-line"; exit "${FAKE_RC:-0}" ;;
    invalid) echo "{bad"; exit "${FAKE_RC:-0}" ;;
    oversize)
      "${REAL_PY}" -c 'import json; print(json.dumps({"x": "a"*10485760}))'
      exit "${FAKE_RC:-0}" ;;
    secret)
      echo "SECRET-LINE"; echo "second-line"
      printf '{"selected":%s,"projected":%s,"failed":%s}\n' "${FAKE_SELECTED:-0}" "${FAKE_PROJECTED:-0}" "${FAKE_FAILED:-0}"
      exit "${FAKE_RC:-0}" ;;
    progress)
      echo '{"schema_version":"embedding_progress.v1","chunks_done":1,"chunks_total":10,"secret":"NOPE"}'
      sleep 0.5
      printf '{"selected":%s,"projected":%s,"failed":%s}\n' "${FAKE_SELECTED:-0}" "${FAKE_PROJECTED:-0}" "${FAKE_FAILED:-0}"
      exit "${FAKE_RC:-0}" ;;
    outcomes)
      echo '{"selected":3,"projected":1,"failed":1,"sessions":[{"session_id_hash":"sha256:1111111111111111111111111111111111111111111111111111111111111111","status":"projected","error_class":""},{"session_id_hash":"sha256:2222222222222222222222222222222222222222222222222222222222222222","status":"failed","error_class":"SyntheticError","secret":"NOPE"},{"session_id_hash":"sha256:3333333333333333333333333333333333333333333333333333333333333333","status":"pending","error_class":""},{"session_id_hash":"sha256:4444444444444444444444444444444444444444444444444444444444444444","status":"skipped","error_class":""}]}'
      exit "${FAKE_RC:-0}" ;;
    *) echo "second-line"
       printf '{"selected":%s,"projected":%s,"failed":%s}\n' "${FAKE_SELECTED:-0}" "${FAKE_PROJECTED:-0}" "${FAKE_FAILED:-0}"
       exit "${FAKE_RC:-0}" ;;
  esac
fi
if [ -z "${REAL_PY:-}" ] || [ ! -x "${REAL_PY}" ]; then
  echo "REAL_PY missing or not executable: ${REAL_PY:-<unset>}" >&2
  exit 127
fi
exec "${REAL_PY}" "$@"
""",
        encoding="utf-8",
    )
    python.chmod(0o755)
    timeout = bin_dir / "timeout"
    timeout.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$TIMEOUT_LOG"
shift
exec "$@"
""",
        encoding="utf-8",
    )
    timeout.chmod(0o755)
    return bin_dir


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    real_py = _resolve_real_py()
    env = {
        "PATH": f"{_fake_bin(tmp_path)}:{os.environ.get('PATH','')}",
        "SESSION_MEMORY_APP_DIR": str(app),
        "PY_LOG": str(tmp_path / "py.log"),
        "TIMEOUT_LOG": str(tmp_path / "timeout.log"),
        "TMPDIR": str(tmp_path),
        "REAL_PY": real_py,
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30
    )


def test_unset_timeout_is_unlimited_and_does_not_invoke_timeout(tmp_path):
    result = _run(_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert '"status":"ok"' in result.stdout
    assert "first-line" not in result.stdout
    assert not (tmp_path / "timeout.log").exists()


def test_zero_timeout_is_unlimited_and_does_not_invoke_timeout(tmp_path):
    result = _run(_env(tmp_path, SESSION_MEMORY_BUILD_TIMEOUT_SECONDS="0"))
    assert result.returncode == 0, result.stderr
    assert '"status":"ok"' in result.stdout
    assert not (tmp_path / "timeout.log").exists()


def test_positive_timeout_preserves_legacy_limit(tmp_path):
    result = _run(_env(tmp_path, SESSION_MEMORY_BUILD_TIMEOUT_SECONDS="120"))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "timeout.log").read_text().startswith("120 ")


def test_invalid_timeout_fails_closed_before_cli(tmp_path):
    result = _run(_env(tmp_path, SESSION_MEMORY_BUILD_TIMEOUT_SECONDS="abc"))
    assert result.returncode == 2
    assert "invalid SESSION_MEMORY_BUILD_TIMEOUT_SECONDS" in result.stderr
    calls = (tmp_path / "py.log").read_text().splitlines()
    assert not any("-m agent_knowledge.cli" in line for line in calls)


def test_pipeline_exit_code_is_cli_code_not_tee(tmp_path):
    result = _run(_env(tmp_path, FAKE_RC="7"))
    assert result.returncode == 7
    assert '"error":"child_exit"' in result.stdout
    assert '"exit_code":7' in result.stdout


def test_legacy_timeout_exit_code_and_message(tmp_path):
    env = _env(tmp_path, SESSION_MEMORY_BUILD_TIMEOUT_SECONDS="1")
    # Make the fake timeout return 124 without running the child.
    timeout = tmp_path / "bin" / "timeout"
    timeout.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$TIMEOUT_LOG\"\nexit 124\n",
        encoding="utf-8",
    )
    timeout.chmod(0o755)
    result = _run(env)
    assert result.returncode == 124
    assert '"error":"child_exit"' in result.stdout
    assert '"exit_code":124' in result.stdout


def test_no_progress_guard_still_exits_nonzero(tmp_path):
    result = _run(_env(tmp_path, FAKE_SELECTED="1", FAKE_PROJECTED="0"))
    assert result.returncode == 1
    assert '"status":"no_progress"' in result.stdout


def test_secret_like_output_is_not_emitted(tmp_path):
    result = _run(_env(tmp_path, FAKE_REPORT_MODE="secret"))
    assert result.returncode == 0
    assert "SECRET" not in result.stdout
    assert "first-line" not in result.stdout
    assert '"status":"ok"' in result.stdout


def test_watch_build_has_no_overall_subprocess_timeout(monkeypatch, tmp_path):
    from agent_knowledge.couchdb_source import watch_cli

    script = tmp_path / "build.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    settings = watch_cli.WatchSettings(
        {"SESSION_MEMORY_BUILD_SCRIPT": str(script)}
    )
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(watch_cli.subprocess, "run", fake_run)
    watch_cli.run_build_script((), settings=settings, environ={})

    assert "timeout" not in captured


def test_missing_report_is_fail_closed(tmp_path):
    result = _run(_env(tmp_path, FAKE_REPORT_MODE="none"))
    assert result.returncode == 3
    assert "missing_report" in result.stdout
    assert not list(tmp_path.glob("session-memory-*"))


def test_invalid_report_is_fail_closed(tmp_path):
    result = _run(_env(tmp_path, FAKE_REPORT_MODE="invalid"))
    assert result.returncode == 3
    assert "invalid_report" in result.stdout
    assert not list(tmp_path.glob("session-memory-*"))


def test_oversize_report_is_bounded_and_fail_closed(tmp_path):
    result = _run(_env(tmp_path, FAKE_REPORT_MODE="oversize"))
    assert result.returncode == 3
    assert "report_too_large" in result.stdout
    assert not list(tmp_path.glob("session-memory-*"))


def test_non_json_log_is_not_retained_on_disk(tmp_path):
    result = _run(_env(tmp_path, FAKE_REPORT_MODE="json"))
    assert result.returncode == 0
    assert "first-line" not in result.stdout
    assert '"status":"ok"' in result.stdout
    assert not list(tmp_path.glob("session-memory-*"))


def _load_stream_report():
    import importlib.util

    path = WORKER / "deploy/session-memory/stream_report.py"
    spec = importlib.util.spec_from_file_location("stream_report", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_stream_filter_bounds_10mb_single_line_memory():
    import io
    import tracemalloc

    module = _load_stream_report()
    big = b'{"x":"' + b"a" * 10_485_760 + b'"}'
    stream = io.BytesIO(big)
    tracemalloc.start()
    tracemalloc.reset_peak()
    raw, oversize = module.scan_stream(stream)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert raw is None and oversize is True
    assert peak < 4 * 1024 * 1024


def test_stream_filter_discards_secret_and_extra_fields():
    import io
    import json

    module = _load_stream_report()
    data = (
        b"SECRET-LINE\n"
        b'{"selected":0,"projected":0,"failed":0,"secret":"SYNTHETIC"}\n'
    )
    raw, oversize = module.scan_stream(io.BytesIO(data))
    summary = module.summarize(raw)

    assert oversize is False
    assert summary["status"] == "ok"
    text = json.dumps(summary)
    assert "SECRET" not in text and "SYNTHETIC" not in text


def test_progress_event_is_forwarded_without_secret(tmp_path):
    env = _env(tmp_path, FAKE_REPORT_MODE="progress")
    proc = subprocess.Popen(
        ["bash", str(SCRIPT)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
    first = proc.stdout.readline() if ready else ""
    proc.wait(timeout=10)
    assert "embedding_progress.v1" in first
    assert '"chunks_done":1' in first
    assert "NOPE" not in first


def test_internal_outcomes_file_is_allowlisted_and_private(tmp_path):
    import json
    import stat

    path = tmp_path / "outcomes.json"
    env = _env(
        tmp_path,
        FAKE_REPORT_MODE="outcomes",
        SESSION_MEMORY_BUILD_OUTCOMES_FILE=str(path),
    )
    result = _run(env)
    assert result.returncode == 1  # failed=1 in the synthetic report
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == "build_outcomes.v1"
    assert payload["sessions"] == [
        {
            "session_id_hash": "sha256:" + "1" * 64,
            "status": "projected",
            "error_class": "",
        },
        {
            "session_id_hash": "sha256:" + "2" * 64,
            "status": "failed",
            "error_class": "SyntheticError",
        },
        {
            "session_id_hash": "sha256:" + "3" * 64,
            "status": "pending",
            "error_class": "",
        },
        {
            "session_id_hash": "sha256:" + "4" * 64,
            "status": "skipped",
            "error_class": "",
        },
    ]
    assert "NOPE" not in path.read_text()
    assert "sha256:" not in result.stdout


def test_watch_run_build_script_loads_internal_outcomes(tmp_path):
    from agent_knowledge.couchdb_source import watch_cli

    script = tmp_path / "build.sh"
    script.write_text(
        """#!/bin/sh
cat > "$SESSION_MEMORY_BUILD_OUTCOMES_FILE" <<'JSON'
{"schema_version":"build_outcomes.v1","sessions":[{"session_id_hash":"sha256:1111111111111111111111111111111111111111111111111111111111111111","status":"projected","error_class":""},{"session_id_hash":"sha256:2222222222222222222222222222222222222222222222222222222222222222","status":"failed","error_class":"SyntheticError"}]}
JSON
echo '{"selected":2,"projected":1,"failed":1}'
exit 0
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    settings = watch_cli.WatchSettings({"SESSION_MEMORY_BUILD_SCRIPT": str(script)})
    rc, report = watch_cli.run_build_script((), settings=settings, environ={})
    assert rc == 0
    outcomes = watch_cli.session_outcomes(report)
    assert outcomes["sha256:" + "1" * 64]["status"] == "projected"
    assert outcomes["sha256:" + "2" * 64]["status"] == "failed"

    from agent_knowledge.session_memory import change_events as ce

    def decision(sid: str) -> str:
        return watch_cli.classify_message(
            ce.SessionChangedEvent(session_id_hash=sid), rc=0, outcomes=outcomes
        )[0]

    assert decision("sha256:" + "1" * 64) == "ack"   # projected
    assert decision("sha256:" + "2" * 64) == "nak"   # failed
    assert decision("sha256:" + "5" * 64) == "nak"   # missing report row


def test_pipeline_progress_v1_is_forwarded():
    import io
    import json

    module = _load_stream_report()
    events: list[dict] = []
    data = (
        b'{"done":12,"schema_version":"pipeline_progress.v1","stage":"embedding_batch","total":40,"secret":"NOPE"}\n'
        b'{"selected":0,"projected":0,"failed":0}\n'
    )
    raw, over = module.scan_stream(
        io.BytesIO(data),
        on_json_line=lambda line: events.append(json.loads(line)),
    )
    assert over is False
    assert any(e.get("schema_version") == "pipeline_progress.v1" for e in events)


def test_pipeline_progress_forwarder_is_fixed_allowlist():
    import contextlib
    import io
    import json

    module = _load_stream_report()
    event = {
        "schema_version": "pipeline_progress.v1",
        "stage": "embedding_batch",
        "done": 12,
        "total": 40,
        "secret": "NOPE",
    }
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        module.forward_progress(json.dumps(event).encode())
    payload = json.loads(out.getvalue())
    assert payload == {
        "schema_version": "pipeline_progress.v1",
        "stage": "embedding_batch",
        "done": 12,
        "total": 40,
    }
    assert "NOPE" not in out.getvalue()


def test_entrypoint_no_updates_emits_no_progress_and_exits_zero(tmp_path):
    env = _env(
        tmp_path,
        FAKE_SELECTED="0",
        FAKE_PROJECTED="0",
        FAKE_FAILED="0",
    )
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert "pipeline_progress" not in result.stdout
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["status"] == "ok"
    assert summary["selected"] == 0 and summary["projected"] == 0
    assert not list(tmp_path.glob("session-memory-*"))


def test_real_py_portability_without_system_usr_bin_python3(tmp_path):
    """Ensure REAL_PY works in uv-only agent containers lacking /usr/bin/python3 and hard-fails if none exist."""
    symlink_py = tmp_path / "uv-only-python"
    symlink_py.symlink_to(Path(sys.executable))
    # Minimal PATH without /usr/bin/python3 still succeeds via sys.executable / symlink
    resolved = _resolve_real_py(executable=str(symlink_py), which_python3=lambda _name: None, env_python="")
    assert resolved == str(symlink_py)

    fallback = _resolve_real_py(executable="", which_python3=lambda _name: str(symlink_py), env_python="")
    assert fallback == str(symlink_py)

    with pytest.raises(pytest.fail.Exception):
        _resolve_real_py(executable="/nonexistent/python", which_python3=lambda _name: None, env_python="")

    env = _env(tmp_path, REAL_PY=str(symlink_py))
    result = _run(env)
    assert result.returncode == 0, result.stderr

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_knowledge.session_memory import neuron_session_memory

WORKER = Path(__file__).resolve().parents[1]
SCRIPT = WORKER / "deploy/session-memory/build-once.sh"


@pytest.mark.parametrize("configured", [None, "0"])
def test_build_once_passes_bounded_limit_without_explicit_limit(
    tmp_path: Path, configured: str | None
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "argv.jsonl"
    fake_python = bin_dir / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{capture}'\n"
        'if [ "${1:-}" = "-m" ]; then\n'
        "  printf '%s\\n' '{\"schema_version\":\"build_guard_summary.v1\",\"status\":\"ok\","
        "\"selected\":0,\"projected\":0,\"failed\":0,\"skipped\":0}'\n"
        "  exit 0\n"
        "fi\n"
        'exec "${REAL_PY}" "$@"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    env = {
        "REAL_PY": sys.executable,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "SESSION_MEMORY_APP_DIR": str(tmp_path),
        "PG_EMBEDDING_CYCLE_CAP_ENABLED": "1",
        "PG_EMBEDDING_CHUNKS_PER_CYCLE": "1000",
        "PG_EMBEDDING_CHUNKS_PER_SESSION": "16",
        "SESSION_MEMORY_PROJECTION_BACKEND": "postgres_pgvector",
    }
    if configured is not None:
        env["SESSION_MEMORY_BUILD_LIMIT"] = configured

    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=WORKER, env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    lines = capture.read_text(encoding="utf-8").splitlines()
    child = next(line for line in lines if "couchdb-session-memory-build" in line)
    assert "--limit 62" in child


def test_legacy_live_entrypoint_uses_same_cycle_limit(monkeypatch) -> None:
    monkeypatch.setenv("PG_EMBEDDING_CYCLE_CAP_ENABLED", "1")
    monkeypatch.setenv("PG_EMBEDDING_CHUNKS_PER_CYCLE", "1000")
    monkeypatch.setenv("PG_EMBEDDING_CHUNKS_PER_SESSION", "16")

    assert neuron_session_memory.live_cycle_session_limit() == 62

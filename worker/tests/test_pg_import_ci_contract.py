"""Importer SQL opt-in and regression suites stay in the dedicated CI lane."""
from pathlib import Path
import os
import subprocess

import pytest
import yaml


WORKFLOW = Path(__file__).parents[2] / ".github/workflows/test.yml"


def test_importer_ci_uses_dedicated_postgres_lane_only():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = workflow["jobs"]
    job = jobs["worker-lbrain-pgvector"]
    assert job["env"]["ATLAS_QRET_SYNTHETIC_SQL"] == "1"
    assert job["env"]["LBRAIN_TEST_PG_DSN"]
    assert job["services"]["postgres"]["image"] == "pgvector/pgvector:pg17"
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    for suite in (
        "test_pg_representation.py", "test_pg_projection_privacy.py",
        "test_pg_qdrant_import.py", "test_pg_qdrant_import_cli.py",
        "test_pg_import_underflow.py",
    ):
        assert "tests/" + suite in commands.split()
    assert "ATLAS_QRET_SYNTHETIC_SQL" not in workflow.get("env", {})
    for name, other in jobs.items():
        if name != "worker-lbrain-pgvector":
            assert "ATLAS_QRET_SYNTHETIC_SQL" not in other.get("env", {})
            assert all("ATLAS_QRET_SYNTHETIC_SQL" not in step.get("env", {})
                       for step in other["steps"])


@pytest.mark.parametrize("dsn", [None, "", "synthetic-configured-dsn"])
def test_dedicated_ci_dsn_guard_executes_fail_closed(dsn):
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["worker-lbrain-pgvector"]
    guard = next(step["run"] for step in job["steps"]
                 if step.get("name") == "LBRAIN_TEST_PG_DSN 필수 검증")
    env = {key: value for key, value in os.environ.items() if key != "LBRAIN_TEST_PG_DSN"}
    if dsn is not None:
        env["LBRAIN_TEST_PG_DSN"] = dsn
    result = subprocess.run(["bash", "-e", "-c", guard], env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == (0 if dsn else 1)
    assert not result.stdout
    if dsn:
        assert dsn not in result.stderr
    else:
        assert "LBRAIN_TEST_PG_DSN is required" in result.stderr

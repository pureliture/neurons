"""Importer SQL opt-in and regression suites stay in the dedicated CI lane."""
from pathlib import Path
import os
import subprocess

import pytest
import yaml


WORKFLOW = Path(__file__).parents[2] / ".github/workflows/test.yml"




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

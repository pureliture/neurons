"""Regression guards for the reviewed test harness (no product substitutes)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def test_scoped_id_executes_without_pg_dsn(tmp_path):
    env = dict(os.environ)
    env.pop("LBRAIN_TEST_PG_DSN", None)
    report = tmp_path / "scoped-id.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/test_pg_session_memory_integration.py::test_scoped_id_has_no_delimiter_alias",
         f"--junitxml={report}"],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cases = ET.parse(report).findall(".//testcase")
    assert len(cases) == 1
    assert cases[0].find("skipped") is None, "pure scoped-id test must execute without a DSN"

#!/usr/bin/env python3
from __future__ import annotations

import importlib
import importlib.util
import inspect
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PMD_PACKAGE_DIR = ROOT / ".github" / "pmd"
sys.path.insert(0, str(PMD_PACKAGE_DIR.parent))


def load_pmd_report():
    path = PMD_PACKAGE_DIR / "generate_report.py"
    spec = importlib.util.spec_from_file_location("pmd_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_files() -> list[dict]:
    return [
        {
            "path": "com/local/ragingressqueue/ingest/api/IngressController.java",
            "raw": "/repo/src/main/java/com/local/ragingressqueue/ingest/api/IngressController.java",
            "violations": [
                {
                    "line": 42,
                    "rule": "NPathComplexity",
                    "priority": 3,
                    "method": "enqueue",
                    "klass": "IngressController",
                    "package": "com.local.ragingressqueue.ingest.api",
                    "msg": "The method has an NPath complexity of 240",
                },
                {
                    "line": 57,
                    "rule": "CognitiveComplexity",
                    "priority": 3,
                    "method": "enqueue",
                    "klass": "IngressController",
                    "package": "com.local.ragingressqueue.ingest.api",
                    "msg": "Avoid deeply nested logic | with table separator",
                },
            ],
        },
        {
            "path": "com/local/ragingressqueue/status/service/StatusService.java",
            "raw": "/repo/src/main/java/com/local/ragingressqueue/status/service/StatusService.java",
            "violations": [
                {
                    "line": 13,
                    "rule": "CyclomaticComplexity",
                    "priority": 3,
                    "method": "status",
                    "klass": "StatusService",
                    "package": "com.local.ragingressqueue.status.service",
                    "msg": "The method has too many branches",
                },
            ],
        },
    ]


def write_cpd_xml(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            <?xml version="1.0" encoding="UTF-8"?>
            <pmd-cpd xmlns="https://pmd-code.org/schema/cpd-report">
              <duplication lines="12" tokens="140">
                <file path="/repo/src/main/java/com/local/ragingressqueue/ingest/api/IngressController.java" line="20"/>
                <file path="/repo/src/main/java/com/local/ragingressqueue/status/service/StatusService.java" line="88"/>
              </duplication>
              <duplication lines="5" tokens="60">
                <file path="/repo/src/main/java/com/local/ragingressqueue/adapter/ext/ragflow/RagFlowGateway.java" line="11"/>
                <file path="/repo/src/main/java/com/local/ragingressqueue/status/service/StatusService.java" line="120"/>
              </duplication>
            </pmd-cpd>
            """
        ),
        encoding="utf-8",
    )


class PmdReportContractTest(unittest.TestCase):
    def test_report_tooling_lives_under_github_pmd_package(self):
        self.assertTrue(PMD_PACKAGE_DIR.is_dir())
        self.assertTrue((PMD_PACKAGE_DIR / "__init__.py").is_file())
        self.assertTrue((PMD_PACKAGE_DIR / "ruleset.xml").is_file())
        self.assertFalse((ROOT / "config" / "pmd" / "generate_report.py").exists())

    def test_report_generator_modules_are_split_by_responsibility(self):
        for module_name in (
            "pmd.pmd_report_parsers",
            "pmd.pmd_report_rules",
            "pmd.pmd_report_html",
            "pmd.pmd_report_markdown",
        ):
            with self.subTest(module_name=module_name):
                self.assertIsNotNone(importlib.import_module(module_name))

    def test_generate_report_keeps_compatibility_wrappers_thin(self):
        module = load_pmd_report()
        for function_name in ("build_html", "build_markdown", "parse_xml", "parse_cpd"):
            source = inspect.getsource(getattr(module, function_name))
            with self.subTest(function_name=function_name):
                self.assertLessEqual(len(source.splitlines()), 12)

    def test_pr_comment_renders_human_readable_github_markdown_report(self):
        module = load_pmd_report()
        with tempfile.TemporaryDirectory() as temp_dir:
            cpd_path = Path(temp_dir) / "cpd.xml"
            write_cpd_xml(cpd_path)
            with patch.object(module, "CPD_XML_PATH", cpd_path):
                comment = module.build_markdown(
                    sample_files(),
                    ["src/main/java/com/local/ragingressqueue/ingest/api/IngressController.java"],
                    "https://github.com/pureliture/neurons/actions/runs/1",
                )

        self.assertIn("<!-- neurons:pmd-pr-comment:v1 -->", comment)
        self.assertIn("| measured area | this PR |", comment)
        self.assertIn("<details open>", comment)
        self.assertIn("NPathComplexity", comment)
        self.assertIn("CognitiveComplexity", comment)
        self.assertIn("Avoid deeply nested logic \\| with table separator", comment)
        self.assertIn("copied-code blocks involving changed files", comment)
        self.assertIn("IngressController.java:20", comment)
        self.assertIn("StatusService.java:88", comment)
        self.assertIn("[full HTML report](https://github.com/pureliture/neurons/actions/runs/1)", comment)
        self.assertNotIn("```json", comment)
        self.assertNotIn("/repo/", comment)

    def test_main_writes_html_and_pr_comment_from_pmd_xml(self):
        module = load_pmd_report()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pmd_xml = root / "build" / "reports" / "pmd" / "main.xml"
            cpd_xml = root / "build" / "reports" / "cpd" / "cpd.xml"
            html_out = root / "build" / "reports" / "pmd" / "main-custom.html"
            md_out = root / "build" / "reports" / "pmd" / "pr-comment.md"
            pmd_xml.parent.mkdir(parents=True)
            cpd_xml.parent.mkdir(parents=True)
            pmd_xml.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
                <pmd xmlns="http://pmd.sourceforge.net/report/2.0.0">
                  <file name="/repo/src/main/java/com/local/ragingressqueue/ingest/api/IngressController.java">
                    <violation beginline="42" rule="CognitiveComplexity" priority="3"
                      class="IngressController" method="enqueue"
                      package="com.local.ragingressqueue.ingest.api">complex</violation>
                  </file>
                </pmd>
                """,
                encoding="utf-8",
            )
            cpd_xml.write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
                <pmd-cpd xmlns="https://pmd-code.org/schema/cpd-report">
                  <duplication lines="12" tokens="120">
                    <file path="/repo/src/main/java/com/local/ragingressqueue/ingest/api/IngressController.java" line="42"/>
                    <file path="/repo/src/main/java/com/local/ragingressqueue/status/service/StatusService.java" line="10"/>
                  </duplication>
                </pmd-cpd>
                """,
                encoding="utf-8",
            )

            rc = module.main(
                [
                    "--repo-root",
                    str(root),
                    "--changed-files",
                    "src/main/java/com/local/ragingressqueue/ingest/api/IngressController.java",
                    "--artifact-url",
                    "https://github.com/pureliture/neurons/actions/runs/1",
                ]
            )
            html_body = html_out.read_text(encoding="utf-8")
            md_body = md_out.read_text(encoding="utf-8")

        self.assertEqual(rc, 0)
        self.assertIn("<title>PMD Report - neurons</title>", html_body)
        self.assertIn("<!-- neurons:pmd-pr-comment:v1 -->", md_body)

    def test_generate_report_script_help_runs_from_repo_root(self):
        result = subprocess.run(
            [sys.executable, ".github/pmd/generate_report.py", "--help"],
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)


class PmdWorkflowContractTest(unittest.TestCase):
    def test_workflow_is_path_limited_report_only_and_uses_github_pmd_package(self):
        workflow = ROOT / ".github" / "workflows" / "pmd.yml"
        text = workflow.read_text(encoding="utf-8")

        self.assertIn("pull_request:", text)
        self.assertIn("paths:", text)
        self.assertIn("src/main/java/**", text)
        self.assertIn(".github/pmd/**", text)
        self.assertIn("build.gradle", text)
        self.assertIn(".github/workflows/pmd.yml", text)
        self.assertIn("pull-requests: write", text)
        self.assertNotIn("startsWith(github.head_ref", text)
        self.assertNotIn("리팩토링", text)
        self.assertIn('java-version: "25"', text)
        self.assertIn("gradle-version:", text)
        self.assertIn("gradle pmdMain cpdCheck --no-daemon", text)
        self.assertIn("python3 .github/pmd/generate_report.py", text)
        self.assertIn("python3 scripts/test_pmd_report.py", text)
        self.assertIn(":(glob)src/main/java/**/*.java", text)
        self.assertIn("continue-on-error: true", text)
        self.assertNotIn("python3 config/pmd/generate_report.py", text)
        self.assertIn("body-includes: neurons:pmd-pr-comment:v1", text)
        self.assertIn("body-path: build/reports/pmd/pr-comment.md", text)
        self.assertIn("build/reports/pmd/main-custom.html", text)
        self.assertIn("build/reports/pmd/main.xml", text)
        self.assertIn("build/reports/cpd/cpd.xml", text)
        self.assertNotIn("pmd-metrics", text)
        self.assertNotIn("confluence", text.lower())


if __name__ == "__main__":
    unittest.main()

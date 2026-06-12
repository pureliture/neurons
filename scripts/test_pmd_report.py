#!/usr/bin/env python3
import importlib.util
import json
import re
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_pmd_report():
    path = ROOT / "config" / "pmd" / "generate_report.py"
    spec = importlib.util.spec_from_file_location("pmd_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PmdReportContractTest(unittest.TestCase):
    def test_pr_comment_contains_llm_readable_json_summary_and_findings(self):
        module = load_pmd_report()
        files = [
            {
                "path": "com/local/ragingressqueue/ingest/api/IngressController.java",
                "violations": [
                    {
                        "line": 42,
                        "rule": "CognitiveComplexity",
                        "priority": 3,
                        "method": "enqueue",
                        "klass": "IngressController",
                        "package": "com.local.ragingressqueue.ingest.api",
                        "msg": "The method 'enqueue()' has a cognitive complexity of 19.",
                    }
                ],
            }
        ]
        cpd = {
            "blocks": [
                {
                    "lines": 12,
                    "tokens": 120,
                    "occurrences": [
                        {"path": "com/local/ragingressqueue/ingest/api/IngressController.java", "line": 41},
                        {"path": "com/local/ragingressqueue/status/service/StatusService.java", "line": 10},
                    ],
                }
            ],
            "total_blocks": 1,
            "total_lines": 12,
        }

        comment = module.build_markdown(
            files,
            ["src/main/java/com/local/ragingressqueue/ingest/api/IngressController.java"],
            "https://github.com/pureliture/rag-ingress-queue/actions/runs/1",
            cpd=cpd,
        )

        self.assertIn("<!-- rag-ingress-queue:pmd-pr-comment:v1 -->", comment)
        match = re.search(r"```json\n(?P<body>.*?)\n```", comment, re.S)
        self.assertIsNotNone(match)
        summary = json.loads(match.group("body"))
        self.assertEqual(summary["schema"], "rag-ingress-queue.pmd-pr-comment.v1")
        self.assertEqual(summary["audience"], "llm")
        self.assertEqual(summary["scope"], "changed_files")
        self.assertEqual(summary["changed_file_count"], 1)
        self.assertEqual(summary["pmd_violation_count"], 1)
        self.assertEqual(summary["cpd_duplicate_block_count"], 1)
        self.assertEqual(summary["actionability"], "review_only")
        self.assertIn("## machine_findings", comment)
        self.assertIn("CognitiveComplexity", comment)

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
                    "https://github.com/pureliture/rag-ingress-queue/actions/runs/1",
                ]
            )
            html_body = html_out.read_text(encoding="utf-8")
            md_body = md_out.read_text(encoding="utf-8")

        self.assertEqual(rc, 0)
        self.assertIn("<title>PMD Report - rag-ingress-queue</title>", html_body)
        self.assertIn("rag-ingress-queue.pmd-pr-comment.v1", md_body)


class PmdWorkflowContractTest(unittest.TestCase):
    def test_workflow_runs_on_all_pull_requests_and_uploads_report_artifacts(self):
        workflow = ROOT / ".github" / "workflows" / "pmd.yml"
        text = workflow.read_text(encoding="utf-8")

        self.assertIn("pull_request:", text)
        self.assertIn("issues: write", text)
        self.assertNotIn("startsWith(github.head_ref", text)
        self.assertNotIn("리팩토링", text)
        self.assertIn("java-version: \"25\"", text)
        self.assertIn("gradle-version:", text)
        self.assertIn("gradle pmdMain cpdCheck --no-daemon", text)
        self.assertIn("python3 config/pmd/generate_report.py", text)
        self.assertIn("body-includes: rag-ingress-queue:pmd-pr-comment:v1", text)
        self.assertIn("body-path: build/reports/pmd/pr-comment.md", text)
        self.assertIn("build/reports/pmd/main-custom.html", text)
        self.assertIn("build/reports/pmd/main.xml", text)
        self.assertIn("build/reports/cpd/cpd.xml", text)


if __name__ == "__main__":
    unittest.main()

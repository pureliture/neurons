#!/usr/bin/env python3
"""Generate PMD/CPD HTML artifacts and an LLM-readable PR comment."""
from __future__ import annotations

import argparse
import html
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


PROJECT = "rag-ingress-queue"
COMMENT_SCHEMA = "rag-ingress-queue.pmd-pr-comment.v1"
COMMENT_MARKER = "<!-- rag-ingress-queue:pmd-pr-comment:v1 -->"
PMD_NS = {"pmd": "http://pmd.sourceforge.net/report/2.0.0"}
CPD_NS = {"cpd": "https://pmd-code.org/schema/cpd-report"}

RULE_META: dict[str, dict[str, str]] = {
    "CyclomaticComplexity": {"severity": "high", "kind": "branching_complexity"},
    "CognitiveComplexity": {"severity": "high", "kind": "reader_complexity"},
    "NPathComplexity": {"severity": "critical", "kind": "execution_path_explosion"},
    "GodClass": {"severity": "critical", "kind": "responsibility_overload"},
    "CouplingBetweenObjects": {"severity": "medium", "kind": "coupling"},
    "NcssCount": {"severity": "medium", "kind": "size"},
    "ExcessivePublicCount": {"severity": "low", "kind": "encapsulation"},
    "ExcessiveParameterList": {"severity": "low", "kind": "api_shape"},
}
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def normalize_path(path: str) -> str:
    value = path.strip().replace("\\", "/").lstrip("./")
    for marker in ("src/main/java/", "main/java/"):
        if marker in value:
            return value.split(marker, 1)[1]
    return value


def parse_xml(path: Path) -> list[dict]:
    tree = ET.parse(path)
    root = tree.getroot()
    files: list[dict] = []
    for file_elem in root.findall("pmd:file", PMD_NS):
        raw = file_elem.get("name", "")
        violations = []
        for violation in file_elem.findall("pmd:violation", PMD_NS):
            rule = violation.get("rule", "")
            meta = RULE_META.get(rule, {})
            violations.append(
                {
                    "line": int(violation.get("beginline", "0")),
                    "rule": rule,
                    "priority": int(violation.get("priority", "5")),
                    "severity": meta.get("severity", "medium"),
                    "kind": meta.get("kind", "unknown"),
                    "method": violation.get("method", ""),
                    "klass": violation.get("class", ""),
                    "package": violation.get("package", ""),
                    "message": (violation.text or "").strip(),
                }
            )
        if violations:
            files.append({"path": normalize_path(raw), "raw": raw, "violations": violations})
    return files


def parse_cpd(path: Path) -> dict:
    if not path.exists():
        return {"blocks": [], "total_blocks": 0, "total_lines": 0}

    tree = ET.parse(path)
    root = tree.getroot()
    blocks = []
    total_lines = 0
    for duplication in root.findall("cpd:duplication", CPD_NS):
        lines = int(duplication.get("lines", "0"))
        tokens = int(duplication.get("tokens", "0"))
        total_lines += lines
        occurrences = [
            {
                "path": normalize_path(file_elem.get("path", "")),
                "line": int(file_elem.get("line", "0")),
            }
            for file_elem in duplication.findall("cpd:file", CPD_NS)
        ]
        blocks.append({"lines": lines, "tokens": tokens, "occurrences": occurrences})
    return {"blocks": blocks, "total_blocks": len(blocks), "total_lines": total_lines}


def filter_cpd_for_changed(cpd: dict, changed_norm: set[str]) -> list[dict]:
    relevant: list[dict] = []
    for block in cpd.get("blocks", []):
        changed = [item for item in block["occurrences"] if item["path"] in changed_norm]
        if not changed:
            continue
        relevant.append(
            {
                "lines": block["lines"],
                "tokens": block["tokens"],
                "changed": changed,
                "others": [item for item in block["occurrences"] if item["path"] not in changed_norm],
            }
        )
    relevant.sort(key=lambda item: (-item["lines"], -item["tokens"]))
    return relevant


def _severity_counts(files: Iterable[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for file_item in files:
        for violation in file_item["violations"]:
            severity = violation.get("severity", RULE_META.get(violation.get("rule", ""), {}).get("severity", "medium"))
            counts[severity] += 1
    return dict(counts)


def _pmd_findings(files: list[dict]) -> list[dict]:
    findings: list[dict] = []
    for file_item in sorted(files, key=lambda item: (-len(item["violations"]), item["path"])):
        for violation in sorted(
            file_item["violations"],
            key=lambda item: (
                SEVERITY_RANK.get(item.get("severity", RULE_META.get(item.get("rule", ""), {}).get("severity", "medium")), 9),
                item["line"],
                item["rule"],
            ),
        ):
            rule = violation["rule"]
            meta = RULE_META.get(rule, {})
            findings.append(
                {
                    "type": "pmd_violation",
                    "path": file_item["path"],
                    "line": violation["line"],
                    "rule": rule,
                    "severity": violation.get("severity", meta.get("severity", "medium")),
                    "kind": violation.get("kind", meta.get("kind", "unknown")),
                    "class": violation.get("klass", ""),
                    "method": violation.get("method", ""),
                    "message": violation.get("message", violation.get("msg", "")),
                }
            )
    return findings


def _cpd_findings(relevant_cpd: list[dict]) -> list[dict]:
    findings: list[dict] = []
    for block in relevant_cpd:
        findings.append(
            {
                "type": "cpd_duplication",
                "lines": block["lines"],
                "tokens": block["tokens"],
                "changed_occurrences": block["changed"],
                "other_occurrences": block["others"][:5],
                "other_occurrence_count": len(block["others"]),
            }
        )
    return findings


def build_html(files: list[dict]) -> str:
    total = sum(len(item["violations"]) for item in files)
    timestamp = datetime.now(timezone.utc).isoformat()
    rows = []
    for finding in _pmd_findings(files):
        rows.append(
            "<tr>"
            f"<td>{html.escape(finding['path'])}</td>"
            f"<td>{finding['line']}</td>"
            f"<td>{html.escape(finding['severity'])}</td>"
            f"<td>{html.escape(finding['rule'])}</td>"
            f"<td>{html.escape(finding['message'])}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PMD Report - {PROJECT}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #111827; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; vertical-align: top; }}
th {{ background: #f3f4f6; text-align: left; }}
code {{ background: #f3f4f6; padding: 2px 4px; border-radius: 4px; }}
</style>
</head>
<body>
<h1>PMD Report - {PROJECT}</h1>
<p><code>{timestamp}</code> · total violations: <strong>{total}</strong></p>
<table>
<thead><tr><th>Path</th><th>Line</th><th>Severity</th><th>Rule</th><th>Message</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</body>
</html>
"""


def build_markdown(files: list[dict], changed_files: list[str], artifact_url: str | None, *, cpd: dict | None = None) -> str:
    changed_norm = {normalize_path(path) for path in changed_files}
    affected = [item for item in files if item["path"] in changed_norm] if changed_norm else []
    total_all = sum(len(item["violations"]) for item in files)
    pmd_findings = _pmd_findings(affected)
    cpd_data = cpd if cpd is not None else {"blocks": [], "total_blocks": 0, "total_lines": 0}
    relevant_cpd = filter_cpd_for_changed(cpd_data, changed_norm) if changed_norm else []
    cpd_findings = _cpd_findings(relevant_cpd)
    summary = {
        "schema": COMMENT_SCHEMA,
        "audience": "llm",
        "project": PROJECT,
        "scope": "changed_files",
        "actionability": "review_only",
        "build_blocking": False,
        "changed_file_count": len(changed_norm),
        "affected_file_count": len(affected),
        "pmd_violation_count": len(pmd_findings),
        "pmd_total_repo_violation_count": total_all,
        "severity_counts": _severity_counts(affected),
        "cpd_duplicate_block_count": len(relevant_cpd),
        "cpd_duplicate_line_count": sum(item["lines"] for item in relevant_cpd),
        "artifact_url": artifact_url,
    }
    findings = [*pmd_findings[:50], *cpd_findings[:20]]
    return "\n".join(
        [
            COMMENT_MARKER,
            "# PMD/CPD code-quality signal for LLM review",
            "",
            "```json",
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## machine_findings",
            "",
            "```json",
            json.dumps(findings, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## interpretation_contract",
            "",
            "- Treat this comment as machine-readable review context.",
            "- Do not treat findings as build-blocking failures.",
            "- Prioritize changed files only.",
            "- Use `artifact_url` for full HTML/XML evidence when present.",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--changed-files", nargs="*", default=[])
    parser.add_argument("--markdown-out", type=Path, default=None)
    parser.add_argument("--artifact-url", default=None)
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    pmd_dir = repo_root / "build" / "reports" / "pmd"
    pmd_xml = pmd_dir / "main.xml"
    cpd_xml = repo_root / "build" / "reports" / "cpd" / "cpd.xml"
    html_out = pmd_dir / "main-custom.html"
    markdown_out = args.markdown_out or pmd_dir / "pr-comment.md"

    if not pmd_xml.exists():
        print(f"[ERR] PMD XML not found: {pmd_xml}", file=sys.stderr)
        return 1

    files = parse_xml(pmd_xml)
    cpd = parse_cpd(cpd_xml)
    html_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(build_html(files), encoding="utf-8")
    markdown_out.write_text(build_markdown(files, args.changed_files, args.artifact_url, cpd=cpd), encoding="utf-8")
    print(f"[OK] HTML: {html_out.relative_to(repo_root)}")
    print(f"[OK] Markdown: {markdown_out.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""HTML renderer for PMD reports."""
from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timezone

from pmd.pmd_report_rules import RULE_META, SEVERITY_RANK


def build_html(files: list[dict]) -> str:
    total = sum(len(file_report["violations"]) for file_report in files)
    affected = len(files)
    rule_counts = Counter(violation["rule"] for file_report in files for violation in file_report["violations"])
    severity_counts = Counter(_severity(violation) for file_report in files for violation in file_report["violations"])
    return _page(
        total=total,
        affected=affected,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        summary=_summary_rows(rule_counts, severity_counts),
        files=_file_sections(files),
    )


def _severity(violation: dict) -> str:
    return RULE_META.get(violation["rule"], {}).get("severity", "medium")


def _rule_label(rule: str) -> str:
    return RULE_META.get(rule, {}).get("label", rule)


def _summary_rows(rule_counts: Counter, severity_counts: Counter) -> str:
    rows = []
    for rule, count in sorted(
        rule_counts.items(),
        key=lambda item: (SEVERITY_RANK.get(RULE_META.get(item[0], {}).get("severity", "medium"), 9), -item[1]),
    ):
        severity = RULE_META.get(rule, {}).get("severity", "medium")
        rows.append(
            "<tr>"
            f"<td>{html.escape(severity)}</td>"
            f"<td>{html.escape(_rule_label(rule))}</td>"
            f"<td><code>{html.escape(rule)}</code></td>"
            f"<td>{count}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4">No PMD findings.</td></tr>')
    sev_text = " · ".join(f"{severity}: {severity_counts.get(severity, 0)}" for severity in ("critical", "high", "medium", "low"))
    return f"<p class=\"muted\">{html.escape(sev_text)}</p><table><thead><tr><th>Severity</th><th>Signal</th><th>Rule</th><th>Count</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _file_sections(files: list[dict]) -> str:
    sections = []
    for file_report in sorted(files, key=lambda item: (-len(item["violations"]), item["path"])):
        rows = []
        for violation in sorted(file_report["violations"], key=lambda item: item["line"]):
            rows.append(
                "<tr>"
                f"<td>{violation['line']}</td>"
                f"<td>{html.escape(_severity(violation))}</td>"
                f"<td><code>{html.escape(violation['rule'])}</code><br>{html.escape(_rule_label(violation['rule']))}</td>"
                f"<td>{html.escape(violation['msg'])}</td>"
                "</tr>"
            )
        sections.append(
            f"<details open><summary><code>{html.escape(file_report['path'])}</code> - {len(file_report['violations'])} findings</summary>"
            f"<table><thead><tr><th>Line</th><th>Severity</th><th>Rule</th><th>Message</th></tr></thead><tbody>{''.join(rows)}</tbody></table></details>"
        )
    return "".join(sections) or "<p>No files with PMD findings.</p>"


def _page(*, total: int, affected: int, generated_at: str, summary: str, files: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PMD Report - neurons</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #17202a; background: #f6f8fa; }}
main {{ max-width: 1120px; margin: 0 auto; }}
section, details {{ background: #fff; border: 1px solid #d0d7de; border-radius: 8px; margin: 16px 0; padding: 16px; }}
h1 {{ margin-bottom: 4px; }}
.muted {{ color: #57606a; }}
.kpis {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
.kpi {{ background: #0969da; color: #fff; border-radius: 8px; padding: 16px; }}
.kpi strong {{ display: block; font-size: 28px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
th, td {{ border: 1px solid #d0d7de; padding: 8px 10px; vertical-align: top; }}
th {{ background: #f6f8fa; text-align: left; }}
code {{ background: #f6f8fa; padding: 2px 4px; border-radius: 4px; }}
summary {{ cursor: pointer; font-weight: 700; }}
</style>
</head>
<body>
<main>
<h1>PMD Report - neurons</h1>
<p class="muted">Observation mode. Findings do not block the build. Generated {html.escape(generated_at)}.</p>
<div class="kpis">
  <div class="kpi"><span>Total findings</span><strong>{total}</strong></div>
  <div class="kpi"><span>Affected files</span><strong>{affected}</strong></div>
  <div class="kpi"><span>Mode</span><strong>Report</strong></div>
</div>
<section><h2>Summary</h2>{summary}</section>
<section><h2>File details</h2>{files}</section>
</main>
</body>
</html>
"""

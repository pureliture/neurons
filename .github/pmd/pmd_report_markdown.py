"""Markdown renderer for PR PMD/CPD comments."""
from __future__ import annotations

from pathlib import Path

from pmd.pmd_report_parsers import filter_cpd_for_changed, parse_cpd
from pmd.pmd_report_rules import RULE_META, SEVERITY_RANK

COMMENT_MARKER = "<!-- neurons:pmd-pr-comment:v1 -->"


def _changed_paths(changed_files: list[str]) -> set[str]:
    return {path.removeprefix("src/main/java/").lstrip("./") for path in changed_files}


def _affected_files(files: list[dict], changed_norm: set[str]) -> list[dict]:
    return [file_report for file_report in files if file_report["path"] in changed_norm]


def _violation_count(files: list[dict]) -> int:
    return sum(len(file_report["violations"]) for file_report in files)


def _severity(violation: dict) -> str:
    return RULE_META.get(violation["rule"], {}).get("severity", "medium")


def _rule_label(rule: str) -> str:
    return RULE_META.get(rule, {}).get("label", rule)


def _top_violation(affected: list[dict]) -> tuple[str, str] | None:
    best: tuple[int, str, str] | None = None
    for file_report in affected:
        for violation in file_report["violations"]:
            rank = SEVERITY_RANK.get(_severity(violation), 9)
            candidate = (rank, file_report["path"].rsplit("/", 1)[-1], violation["rule"])
            if best is None or candidate < best:
                best = candidate
    return (best[1], best[2]) if best else None


def _headline(changed_files: list[str], affected: list[dict], affected_count: int) -> str:
    if not changed_files:
        return "> No Java files changed. The report stays in observation mode."
    if affected_count == 0:
        return f"> No PMD/CPD signals were found in the {len(changed_files)} changed Java file(s)."
    top = _top_violation(affected)
    suffix = f" Top signal: `{top[0]}` / `{top[1]}`." if top else ""
    return f"> {affected_count} PMD signal(s) in {len(affected)} of {len(changed_files)} changed Java file(s).{suffix} Findings do not block the build."


def _critical_count(affected: list[dict]) -> int:
    return sum(1 for file_report in affected for violation in file_report["violations"] if _severity(violation) == "critical")


def _summary_table(changed_files: list[str], affected: list[dict], affected_count: int, relevant_cpd: list[dict]) -> list[str]:
    cpd_blocks = len(relevant_cpd)
    cpd_lines = sum(block["lines"] for block in relevant_cpd)
    cpd_summary = f"**{cpd_blocks} block(s) / {cpd_lines} line(s)**" if cpd_blocks else "none"
    return [
        "| measured area | this PR |",
        "|---|---:|",
        f"| changed Java files | **{len(changed_files)}** |",
        f"| files with PMD signals | **{len(affected)}** |",
        f"| PMD signals in changed files | **{affected_count}** |",
        f"| Critical signals | **{_critical_count(affected)}** |",
        f"| copied-code blocks involving changed files | {cpd_summary} |",
        "",
    ]


def _file_details(affected: list[dict]) -> list[str]:
    lines = ["### File details", ""]
    for index, file_report in enumerate(sorted(affected, key=lambda item: (-len(item["violations"]), item["path"]))[:25]):
        lines.extend(_file_detail(file_report, open_first=index == 0))
    if len(affected) > 25:
        lines.extend([f"<sub>Additional affected files are available in the full HTML report: {len(affected) - 25}</sub>", ""])
    return lines


def _file_detail(file_report: dict, *, open_first: bool) -> list[str]:
    open_attr = " open" if open_first else ""
    lines = [
        f"<details{open_attr}>",
        f"<summary><b><code>{file_report['path']}</code> - {len(file_report['violations'])} finding(s)</b></summary>",
        "",
        "| line | severity | signal | message |",
        "|---:|---|---|---|",
    ]
    lines.extend(_violation_row(violation) for violation in sorted(file_report["violations"], key=lambda item: item["line"])[:50])
    lines.extend(["", "</details>", ""])
    return lines


def _violation_row(violation: dict) -> str:
    message = violation["msg"].replace("|", "\\|")
    return f"| L{violation['line']} | {_severity(violation)} | {_rule_label(violation['rule'])}<br><sub>`{violation['rule']}`</sub> | {message} |"


def _cpd_section(relevant_cpd: list[dict]) -> list[str]:
    if not relevant_cpd:
        return []
    lines = [
        "### CPD copied-code blocks involving changed files",
        "",
        "| lines | tokens | changed occurrence | other occurrence |",
        "|---:|---:|---|---|",
    ]
    lines.extend(_cpd_row(block) for block in relevant_cpd[:10])
    if len(relevant_cpd) > 10:
        lines.extend(["", f"<sub>Additional CPD blocks are available in the full HTML report: {len(relevant_cpd) - 10}</sub>"])
    lines.append("")
    return lines


def _occurrence_text(occurrences: list[dict], limit: int) -> str:
    visible = ", ".join(f"`{occurrence['path']}:{occurrence['line']}`" for occurrence in occurrences[:limit])
    if len(occurrences) > limit:
        visible += f" and {len(occurrences) - limit} more"
    return visible or "none"


def _cpd_row(block: dict) -> str:
    return f"| {block['lines']} | {block['tokens']} | {_occurrence_text(block['changed'], 3)} | {_occurrence_text(block['others'], 3)} |"


def _footer(total_all: int, cpd: dict, artifact_url: str | None) -> list[str]:
    artifact = f"[full HTML report]({artifact_url})" if artifact_url else "full HTML report in the Actions artifact"
    return [
        "---",
        "",
        f"<sub>Repository totals: PMD **{total_all}**"
        f" · CPD **{cpd['total_blocks']} block(s) / {cpd['total_lines']} line(s)**"
        f" · {artifact}</sub>",
        "",
        "<sub>Generated by `.github/pmd/generate_report.py`. PMD/CPD findings are review signals, not build blockers.</sub>",
    ]


def build_markdown(files: list[dict], changed_files: list[str], artifact_url: str | None, *, cpd_xml_path: Path) -> str:
    total_all = _violation_count(files)
    changed_norm = _changed_paths(changed_files)
    affected = _affected_files(files, changed_norm)
    affected_count = _violation_count(affected)
    cpd = parse_cpd(cpd_xml_path)
    relevant_cpd = filter_cpd_for_changed(cpd, changed_norm) if changed_files else []

    lines = [COMMENT_MARKER, "## PMD/CPD report - changed Java files", "", _headline(changed_files, affected, affected_count), ""]
    lines.extend(_summary_table(changed_files, affected, affected_count, relevant_cpd))
    if changed_files and affected_count > 0:
        lines.extend(_file_details(affected))
    lines.extend(_cpd_section(relevant_cpd))
    lines.extend(_footer(total_all, cpd, artifact_url))
    return "\n".join(lines)

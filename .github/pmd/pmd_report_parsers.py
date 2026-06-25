"""PMD and CPD report parsers."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"pmd": "http://pmd.sourceforge.net/report/2.0.0"}
CPD_NS = {"cpd": "https://pmd-code.org/schema/cpd-report"}


def short_path(full: str) -> str:
    value = full.strip().replace("\\", "/").lstrip("./")
    for marker in ("src/main/java/", "main/java/"):
        if marker in value:
            return value.split(marker, 1)[1]
    return value


def parse_xml(path: Path):
    tree = ET.parse(path)
    root = tree.getroot()
    files: list[dict] = []
    for file_elem in root.findall("pmd:file", NS):
        raw = file_elem.get("name", "")
        violations = []
        for violation in file_elem.findall("pmd:violation", NS):
            violations.append(
                {
                    "line": int(violation.get("beginline", "0")),
                    "rule": violation.get("rule", ""),
                    "priority": int(violation.get("priority", "5")),
                    "method": violation.get("method", ""),
                    "klass": violation.get("class", ""),
                    "package": violation.get("package", ""),
                    "msg": (violation.text or "").strip(),
                }
            )
        if violations:
            files.append({"path": short_path(raw), "raw": raw, "violations": violations})
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
            {"path": short_path(file_elem.get("path", "")), "line": int(file_elem.get("line", "0"))}
            for file_elem in duplication.findall("cpd:file", CPD_NS)
        ]
        blocks.append({"lines": lines, "tokens": tokens, "occurrences": occurrences})
    return {"blocks": blocks, "total_blocks": len(blocks), "total_lines": total_lines}


def filter_cpd_for_changed(cpd: dict, changed_norm: set[str]) -> list[dict]:
    output: list[dict] = []
    for block in cpd["blocks"]:
        changed = [occurrence for occurrence in block["occurrences"] if occurrence["path"] in changed_norm]
        if changed:
            others = [occurrence for occurrence in block["occurrences"] if occurrence["path"] not in changed_norm]
            output.append({"lines": block["lines"], "tokens": block["tokens"], "changed": changed, "others": others})
    output.sort(key=lambda item: (-item["lines"], -item["tokens"]))
    return output

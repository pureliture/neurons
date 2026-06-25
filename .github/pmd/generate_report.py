"""PMD XML results to HTML report and PR comment Markdown."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmd.pmd_report_html import build_html as _build_html
from pmd.pmd_report_markdown import build_markdown as _build_markdown
from pmd.pmd_report_parsers import (
    filter_cpd_for_changed as _filter_cpd_for_changed,
    parse_cpd as _parse_cpd,
    parse_xml as _parse_xml,
    short_path as _short_path,
)
from pmd.pmd_report_rules import RULE_META, SEVERITY_RANK

__all__ = [
    "RULE_META",
    "SEVERITY_RANK",
    "build_html",
    "build_markdown",
    "filter_cpd_for_changed",
    "parse_cpd",
    "parse_xml",
    "short_path",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
PMD_DIR = REPO_ROOT / "build" / "reports" / "pmd"
XML_PATH = PMD_DIR / "main.xml"
OUT_PATH = PMD_DIR / "main-custom.html"
MARKDOWN_OUT = PMD_DIR / "pr-comment.md"
CPD_XML_PATH = REPO_ROOT / "build" / "reports" / "cpd" / "cpd.xml"


def short_path(full: str) -> str:
    return _short_path(full)


def parse_xml(path: Path):
    return _parse_xml(path)


def build_html(files: list[dict]) -> str:
    return _build_html(files)


def parse_cpd(path: Path) -> dict:
    return _parse_cpd(path)


def filter_cpd_for_changed(cpd: dict, changed_norm: set[str]) -> list[dict]:
    return _filter_cpd_for_changed(cpd, changed_norm)


def build_markdown(files: list[dict], changed_files: list[str], artifact_url: str | None) -> str:
    return _build_markdown(files, changed_files, artifact_url, cpd_xml_path=CPD_XML_PATH)


def _output_path(path: Path | None, default: Path, repo_root: Path) -> Path:
    value = path or default
    if value.is_absolute():
        return value.resolve()
    return (repo_root / value).resolve()


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--changed-files", nargs="*", default=[])
    parser.add_argument("--markdown-out", type=Path, default=None)
    parser.add_argument("--artifact-url", default=None)
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    pmd_dir = repo_root / "build" / "reports" / "pmd"
    pmd_xml = pmd_dir / "main.xml"
    html_out = pmd_dir / "main-custom.html"
    markdown_out = _output_path(args.markdown_out, pmd_dir / "pr-comment.md", repo_root)

    if not pmd_xml.exists():
        print(f"[ERR] PMD XML not found: {pmd_xml}", file=sys.stderr)
        print("      Run the PMD task before generating the report.", file=sys.stderr)
        return 1

    files = parse_xml(pmd_xml)
    html_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(build_html(files), encoding="utf-8")
    cpd_xml = repo_root / "build" / "reports" / "cpd" / "cpd.xml"
    markdown = _build_markdown(files, args.changed_files, args.artifact_url, cpd_xml_path=cpd_xml)
    markdown_out.write_text(markdown, encoding="utf-8")
    total = sum(len(file_report["violations"]) for file_report in files)
    print(f"[OK] HTML: {total} findings -> {_display_path(html_out, repo_root)}")
    print(f"[OK] Markdown: {_display_path(markdown_out, repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

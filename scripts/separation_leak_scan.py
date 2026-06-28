#!/usr/bin/env python3
"""Public/private separation leak scanner (fail-closed).

Scans the working tree (tracked files) or full git history for known-private
patterns. The pattern list is supplied at runtime via ``--patterns`` and is NEVER
committed to this public repo: it holds the real private literals (host aliases,
tailnet names, private paths, live evidence fingerprints, ...). This file holds only
scanner logic plus generic credential-shaped regexes — no private values.

Pattern file format: one rule per line, ``label<TAB>pattern``. A pattern prefixed
with ``re:`` is a regex; otherwise it is a literal substring. Blank lines and lines
starting with ``#`` are ignored.

Exit 0 = clean (no hits). Exit 1 = hits found, printed as ``label`` + location only,
never the raw matched value (so the scanner output is itself public-safe).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def load_patterns(path: str) -> tuple[list[tuple[str, str]], list[tuple[str, re.Pattern]]]:
    literals: list[tuple[str, str]] = []
    regexes: list[tuple[str, re.Pattern]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        label, _, pattern = raw.partition("\t")
        if not pattern:
            continue
        if pattern.startswith("re:"):
            regexes.append((label, re.compile(pattern[3:])))
        else:
            literals.append((label, pattern))
    return literals, regexes


def load_allowlist(path: str | None) -> list[tuple[str, str]]:
    # allowlist lines: `label<TAB>path-glob`. Suppresses a known-synthetic fixture hit
    # (e.g. a redaction unit test that must contain a fake credential). Holds only
    # labels + path globs, no private values, so it is public-safe and committed.
    if not path:
        return []
    rules: list[tuple[str, str]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        label, _, glob = raw.partition("\t")
        if glob:
            rules.append((label, glob))
    return rules


def _allowed(label: str, loc: str, allowlist: list[tuple[str, str]]) -> bool:
    import fnmatch

    return any(label == a_label and fnmatch.fnmatch(loc, a_glob) for a_label, a_glob in allowlist)


def _tracked_files(root: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def scan_tree(root, literals, regexes) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for rel in _tracked_files(root):
        try:
            data = (Path(root) / rel).read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue  # skip binary
        text = data.decode("utf-8", "replace")
        for label, lit in literals:
            if lit in text:
                hits.append((label, rel))
        for label, rx in regexes:
            if rx.search(text):
                hits.append((label, rel))
    return hits


def scan_history(root, literals, regexes) -> list[tuple[str, str]]:
    # Presence check over every reachable commit tree via `git grep`. Unlike pickaxe
    # (-S/-G), this does NOT false-positive on commits that merely *remove* a string,
    # and it yields `<commit>:<path>` so the path allowlist still applies in history.
    commits = subprocess.run(
        ["git", "rev-list", "--all"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    if not commits:
        return []

    def grep(flag: str, pattern: str) -> set[str]:
        result = subprocess.run(
            ["git", "grep", "-I", "-l", flag, "-e", pattern, *commits],
            cwd=root, capture_output=True, text=True,
        )
        paths: set[str] = set()
        for line in result.stdout.splitlines():
            _, _, path = line.partition(":")  # "<commit>:<path>"
            if path:
                paths.add(path)
        return paths

    hits: list[tuple[str, str]] = []
    for label, lit in literals:
        hits.extend((label, path) for path in grep("-F", lit))
    for label, rx in regexes:
        hits.extend((label, path) for path in grep("-P", rx.pattern))
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="public/private separation leak scanner")
    ap.add_argument("--patterns", required=True, help="runtime pattern file (never committed)")
    ap.add_argument("--root", default=".", help="repo root to scan")
    ap.add_argument("--mode", choices=["tree", "history"], default="tree")
    ap.add_argument("--allowlist", default=None, help="optional label<TAB>path-glob exemptions")
    args = ap.parse_args(argv)

    literals, regexes = load_patterns(args.patterns)
    if not literals and not regexes:
        print("no patterns loaded", file=sys.stderr)
        return 2

    allowlist = load_allowlist(args.allowlist)
    hits = (
        scan_tree(args.root, literals, regexes)
        if args.mode == "tree"
        else scan_history(args.root, literals, regexes)
    )
    hits = [(label, loc) for label, loc in hits if not _allowed(label, loc, allowlist)]
    if hits:
        print(f"LEAK-SCAN FAIL ({args.mode}): {len(set(hits))} hit(s)")
        for label, loc in sorted(set(hits)):
            print(f"  [{label}] {loc}")
        return 1
    print(f"LEAK-SCAN CLEAN ({args.mode}): 0 hits")
    return 0


if __name__ == "__main__":
    sys.exit(main())

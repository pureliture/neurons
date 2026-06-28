"""Fail-closed contract test for the public/private separation manifest.

The manifest (`deploy/separation/separation-manifest.json`) is the executable
projection of `docs/public-private-separation.md`: it assigns every tracked path a
disposition + mechanic. This test fails closed when a tracked file is not covered by
any rule, so a newly added file cannot silently escape classification.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy" / "separation" / "separation-manifest.json"

DISPOSITIONS = {"public", "private-neurons-ops", "sanitize-then-public"}
MECHANICS = {"keep", "replace-text", "invert-path", "env-stub", "gitignore"}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _rule_specificity(match: str) -> tuple[int, int]:
    # exact paths beat globs; longer patterns beat shorter ones.
    is_glob = any(ch in match for ch in "*?[")
    return (0 if is_glob else 1, len(match))


def _matches(path: str, match: str) -> bool:
    if match.endswith("/**"):
        prefix = match[:-3]
        return path == prefix or path.startswith(prefix + "/")
    if any(ch in match for ch in "*?["):
        return fnmatch.fnmatch(path, match)
    return path == match


def _best_rule(path: str, rules: list[dict]) -> dict | None:
    best: dict | None = None
    best_key: tuple[int, int] = (-1, -1)
    for rule in rules:
        if _matches(path, rule["match"]):
            key = _rule_specificity(rule["match"])
            if key > best_key:
                best, best_key = rule, key
    return best


def test_manifest_is_well_formed() -> None:
    manifest = _load_manifest()
    assert manifest.get("version") == 1
    rules = manifest["rules"]
    assert rules, "manifest has no rules"
    for rule in rules:
        assert rule["disposition"] in DISPOSITIONS, rule
        assert rule["mechanic"] in MECHANICS, rule
        # invert-path (whole-file history removal) is only for private ops material.
        if rule["mechanic"] == "invert-path":
            assert rule["disposition"] == "private-neurons-ops", rule


def test_every_tracked_file_is_classified() -> None:
    rules = _load_manifest()["rules"]
    unclassified = [path for path in _tracked_files() if _best_rule(path, rules) is None]
    assert not unclassified, (
        "tracked files not covered by separation-manifest (fail-closed): "
        + ", ".join(sorted(unclassified)[:50])
    )


def test_exact_rules_point_at_real_files() -> None:
    # exact (non-glob) rules must reference a currently-tracked path, so the manifest
    # cannot rot into stale per-file rules.
    tracked = set(_tracked_files())
    stale = [
        rule["match"]
        for rule in _load_manifest()["rules"]
        if not any(ch in rule["match"] for ch in "*?[") and rule["match"] not in tracked
    ]
    assert not stale, "manifest exact rules reference missing files: " + ", ".join(sorted(stale))

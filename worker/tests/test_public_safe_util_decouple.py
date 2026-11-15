"""Guard the public-safe helper API and its compatibility re-exports.

The ingress and delivery modules consume these helpers without depending on the
llm_brain_core implementation.
"""

from __future__ import annotations

import ast
from pathlib import Path


def test_public_safe_util_has_helpers_and_util_reexports():
    from agent_knowledge import public_safe_util as ps
    from agent_knowledge.llm_brain_core import _util

    for name in (
        "ensure_public_safe",
        "hash_payload",
        "public_safe_text",
        "require_sha256",
        "SafeErrorClass",
        "SafeErrorCode",
        "SafeErrorParam",
        "SafeErrorStep",
        "SafeErrorType",
        "SafeReasonCode",
        "classify_safe_error_class",
        "classify_safe_error_code",
        "classify_safe_error_param",
        "classify_safe_error_step",
        "classify_safe_error_type",
        "classify_safe_reason_code",
        "extract_safe_error_diagnostics",
        "format_safe_reason_code",
        "sanitize_http_status",
        "sanitize_structured_reason_code",
    ):
        assert hasattr(ps, name), f"public_safe_util missing {name}"
        # llm_brain_core._util keeps re-exporting the SAME objects (back-compat)
        assert getattr(_util, name) is getattr(ps, name)


def test_public_safe_util_stays_leaf_without_llm_brain_or_cli_dependencies():
    from agent_knowledge import public_safe_util as ps

    source_path = Path(ps.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden_roots = {
        "llm_brain_core",
        "couchdb_source",
        "graphiti_core",
        "neo4j",
        "openai",
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = set(alias.name.split("."))
                if parts & forbidden_roots:
                    violations.append(f"L{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            parts = set(module_name.split("."))
            if parts & forbidden_roots:
                violations.append(f"L{node.lineno}: from {module_name} import ...")

    assert violations == []
    assert not (source_path.parent / "llm_brain_core" / "error_codes.py").exists()

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_LIB = REPO_ROOT / "worker" / "lib"
JAVA_SRC = REPO_ROOT / "src" / "main" / "java"

FORBIDDEN_PYTHON_IMPORT_ROOTS = {
    "dendrite",
}

FORBIDDEN_JAVA_IMPORT_FRAGMENTS = {
    "import dendrite.",
    "import com.local.dendrite.",
}


def test_worker_python_source_does_not_import_dendrite_client() -> None:
    violations: list[str] = []
    for path in sorted(WORKER_LIB.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root in FORBIDDEN_PYTHON_IMPORT_ROOTS:
                        violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".", 1)[0]
                if root in FORBIDDEN_PYTHON_IMPORT_ROOTS:
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: from {node.module} import ...")

    assert violations == []


def test_java_service_source_does_not_import_dendrite_client() -> None:
    violations: list[str] = []
    for path in sorted(JAVA_SRC.rglob("*.java")):
        text = path.read_text(encoding="utf-8")
        for fragment in FORBIDDEN_JAVA_IMPORT_FRAGMENTS:
            if fragment in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains {fragment}")

    assert violations == []

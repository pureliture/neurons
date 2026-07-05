from __future__ import annotations

from pathlib import Path

import llm_brain_core_package_depth as lint


def _write_package_root(
    tmp_path: Path,
    modules: set[str],
    *,
    init_source: str = "",
    object_modules: set[str] | None = None,
) -> Path:
    package_root = tmp_path / "llm_brain_core"
    package_root.mkdir()
    (package_root / "__init__.py").write_text(init_source, encoding="utf-8")
    for module in modules:
        (package_root / f"{module}.py").write_text("", encoding="utf-8")
    if object_modules is not None:
        objects_root = package_root / "objects"
        objects_root.mkdir()
        (objects_root / "__init__.py").write_text("", encoding="utf-8")
        for module in object_modules:
            (objects_root / f"{module}.py").write_text("", encoding="utf-8")
    return package_root


def test_llm_brain_core_package_depth_passes_on_current_code():
    assert lint.check_package_depth() == []


def test_llm_brain_core_package_depth_classifies_all_root_modules(
    tmp_path: Path,
    monkeypatch,
):
    package_root = _write_package_root(tmp_path, {"runtime", "runtime_graph"})
    monkeypatch.setattr(
        lint,
        "ROOT_MODULE_AREAS",
        {"runtime_services": frozenset({"runtime"})},
    )

    violations = lint.check_package_depth(package_root)

    assert any(
        "runtime_graph" in violation and "unclassified" in violation
        for violation in violations
    )


def test_llm_brain_core_package_depth_detects_stale_manifest_entries(
    tmp_path: Path,
    monkeypatch,
):
    package_root = _write_package_root(tmp_path, {"runtime"})
    monkeypatch.setattr(
        lint,
        "ROOT_MODULE_AREAS",
        {"runtime_services": frozenset({"runtime", "stale_runtime"})},
    )

    violations = lint.check_package_depth(package_root)

    assert any(
        "stale_runtime" in violation and "stale" in violation
        for violation in violations
    )


def test_llm_brain_core_package_depth_blocks_cli_reexports(
    tmp_path: Path,
    monkeypatch,
):
    package_root = _write_package_root(
        tmp_path,
        {"cli", "runtime"},
        init_source="from .cli import main\n",
    )
    monkeypatch.setattr(
        lint,
        "ROOT_MODULE_AREAS",
        {
            "cli": frozenset({"cli"}),
            "runtime_services": frozenset({"runtime"}),
        },
    )

    violations = lint.check_package_depth(package_root)

    assert any(
        "cli" in violation and "__init__" in violation
        for violation in violations
    )


def test_llm_brain_core_package_depth_requires_object_subpackage_modules(
    tmp_path: Path,
    monkeypatch,
):
    package_root = _write_package_root(tmp_path, {"knowledge_objects"})
    monkeypatch.setattr(
        lint,
        "ROOT_MODULE_AREAS",
        {"domain_contracts": frozenset({"knowledge_objects"})},
    )
    monkeypatch.setattr(lint, "OBJECT_SUBPACKAGE_MODULES", frozenset({"knowledge_objects"}))

    violations = lint.check_package_depth(package_root)

    assert any(
        "knowledge_objects" in violation and "objects subpackage" in violation
        for violation in violations
    )


def test_llm_brain_core_package_depth_requires_root_object_shims(
    tmp_path: Path,
    monkeypatch,
):
    package_root = _write_package_root(
        tmp_path,
        {"knowledge_objects"},
        object_modules={"knowledge_objects"},
    )
    (package_root / "knowledge_objects.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        lint,
        "ROOT_MODULE_AREAS",
        {"domain_contracts": frozenset({"knowledge_objects"})},
    )
    monkeypatch.setattr(lint, "OBJECT_SUBPACKAGE_MODULES", frozenset({"knowledge_objects"}))

    violations = lint.check_package_depth(package_root)

    assert any(
        "knowledge_objects" in violation and "compatibility shim" in violation
        for violation in violations
    )

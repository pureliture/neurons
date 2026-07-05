"""llm_brain_core root package depth guard.

The package still has many root modules. This eval-only lint does not move files;
it prevents the flat root from growing without classification and keeps CLI
entrypoints out of the public package re-export surface.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


ROOT_MODULE_AREA_MANIFEST: dict[str, frozenset[str]] = {
    "domain_contracts": frozenset({
        "_util",
        "authority_bundle",
        "authority_projection",
        "central_federation",
        "document_authority",
        "event_replay",
        "golden_query_eval",
        "infra_baseline",
        "knowledge_objects",
        "local_brain",
        "local_evidence",
        "models",
        "object_packs",
        "okf_export",
        "ontology",
        "preference_authority",
        "reference_corpus",
        "repo_style_profile",
        "source_ref",
        "workflow_authority",
    }),
    "runtime_services": frozenset({
        "bulk_semantic",
        "context",
        "context_builder",
        "portable",
        "runtime",
        "sync_shadow",
    }),
    "graph_projection": frozenset({
        "graph",
        "hybrid_graph",
        "projection",
        "runtime_graph",
    }),
    "adapters": frozenset({
        "artifact_store",
        "document_bridge",
        "graphiti_adapter",
        "graphiti_backend",
        "ledger_adapter",
    }),
    "cli": frozenset({
        "bulk_semantic_cli",
        "bulk_semantic_trigger_cli",
        "cli",
        "couchdb_projection_cli",
        "graph_projection_status_cli",
        "graph_trigger_cli",
        "object_cli",
        "portable_cli",
        "projection_cli",
        "regression_gate_cli",
    }),
}


ROOT_MODULE_AREAS = ROOT_MODULE_AREA_MANIFEST

OBJECT_SUBPACKAGE_MODULES = frozenset({
    "golden_query_eval",
    "knowledge_objects",
    "object_cli",
    "object_packs",
    "okf_export",
    "reference_corpus",
})


def _package_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        candidate = parent / "lib" / "agent_knowledge" / "llm_brain_core"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("llm_brain_core package root not found")


def _root_modules(package_root: Path) -> frozenset[str]:
    return frozenset(
        path.stem
        for path in package_root.glob("*.py")
        if path.name != "__init__.py"
    )


def _classified_modules() -> frozenset[str]:
    return frozenset().union(*ROOT_MODULE_AREAS.values())


def _module_to_area() -> dict[str, str]:
    index: dict[str, str] = {}
    for area, modules in ROOT_MODULE_AREAS.items():
        for module in modules:
            if module in index:
                raise ValueError(f"module {module!r} is classified twice")
            index[module] = area
    return index


def _init_imported_modules(init_path: Path) -> set[str]:
    tree = ast.parse(init_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def _object_subpackage_modules(package_root: Path) -> frozenset[str]:
    objects_root = package_root / "objects"
    if not objects_root.is_dir():
        return frozenset()
    return frozenset(
        path.stem
        for path in objects_root.glob("*.py")
        if path.name != "__init__.py"
    )


def _imports_object_subpackage(root_module_path: Path, module: str) -> bool:
    tree = ast.parse(root_module_path.read_text(encoding="utf-8"))
    expected = f"objects.{module}"
    return any(
        isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == expected
        for node in ast.walk(tree)
    )


def check_package_depth(package_root: Path | None = None) -> list[str]:
    root = package_root or _package_root()
    violations: list[str] = []

    try:
        module_to_area = _module_to_area()
    except ValueError as exc:
        violations.append(f"root module manifest overlap: {exc}")
        module_to_area = {}

    actual_modules = _root_modules(root)
    classified_modules = _classified_modules()
    for module in sorted(actual_modules - classified_modules):
        violations.append(f"llm_brain_core root module {module!r} is unclassified")
    for module in sorted(classified_modules - actual_modules):
        violations.append(f"llm_brain_core root module manifest entry {module!r} is stale")

    cli_modules = {
        module for module, area in module_to_area.items()
        if area == "cli"
    }
    imported_by_init = _init_imported_modules(root / "__init__.py")
    for module in sorted(cli_modules & imported_by_init):
        violations.append(
            f"llm_brain_core __init__ must not re-export CLI module {module!r}"
        )

    object_modules = _object_subpackage_modules(root)
    for module in sorted(OBJECT_SUBPACKAGE_MODULES - object_modules):
        violations.append(f"llm_brain_core objects subpackage module {module!r} is missing")
    for module in sorted(object_modules - OBJECT_SUBPACKAGE_MODULES):
        violations.append(f"llm_brain_core objects subpackage module {module!r} is unclassified")
    for module in sorted(OBJECT_SUBPACKAGE_MODULES & actual_modules):
        if not _imports_object_subpackage(root / f"{module}.py", module):
            violations.append(
                f"llm_brain_core root object module {module!r} must be a compatibility shim"
            )

    return violations


def _report(package_root: Path | None = None) -> None:
    root = package_root or _package_root()
    actual_modules = _root_modules(root)
    print("=== llm_brain_core root modules by area ===")
    for area, modules in ROOT_MODULE_AREAS.items():
        present = sorted(set(modules) & set(actual_modules))
        print(f"  {area}: {len(present)}")
        for module in present:
            print(f"    - {module}")
    object_modules = _object_subpackage_modules(root)
    print("=== llm_brain_core objects subpackage modules ===")
    for module in sorted(object_modules):
        print(f"  - {module}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-brain-core-package-depth")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)
    if args.report:
        _report()
        return 0
    violations = check_package_depth()
    if violations:
        print("LLM_BRAIN_CORE PACKAGE DEPTH VIOLATIONS:")
        for violation in violations:
            print("  -", violation)
        return 1
    print("llm_brain_core package depth OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

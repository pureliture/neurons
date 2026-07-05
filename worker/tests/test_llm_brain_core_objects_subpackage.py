from __future__ import annotations

import importlib
from collections.abc import Mapping

import pytest

OBJECT_MODULES = (
    "knowledge_objects",
    "object_packs",
    "reference_corpus",
    "extraction_pipeline",
    "golden_query_eval",
    "okf_export",
    "object_cli",
)

OBJECT_PARITY_SYMBOLS: dict[str, tuple[str, ...]] = {
    "knowledge_objects": (
        "KnowledgeObjectEnvelope",
        "KnowledgeEdge",
        "EvidenceRef",
        "memory_card_to_knowledge_object",
    ),
    "object_packs": (
        "build_documentation_cleanup_pack",
        "build_agent_context_object_packs",
    ),
    "reference_corpus": (
        "build_corpus_ingest_plan",
        "reference_corpus_objects_from_manifest",
    ),
    "extraction_pipeline": (
        "build_extractor_registry_report",
        "run_documentation_cleanup_strategy_comparison",
        "run_reference_corpus_extraction_preview",
        "run_runtime_truth_extraction_preview",
    ),
    "golden_query_eval": (
        "build_baseline_golden_query_report",
        "evaluate_object_pack_response",
    ),
    "okf_export": (
        "build_okf_bundle",
    ),
    "object_cli": (
        "object_query_main",
        "corpus_ingest_main",
    ),
}

ROOT_OBJECT_CLI_NAMES = (
    "object_query_main",
    "object_explain_main",
    "corpus_status_main",
    "corpus_ingest_plan_main",
    "corpus_ingest_main",
    "golden_query_eval_main",
    "okf_export_main",
)


def _import_objects_modules() -> Mapping[str, tuple[object, object]]:
    modules: dict[str, tuple[object, object]] = {}
    for module_name in OBJECT_MODULES:
        old_module = importlib.import_module(f"agent_knowledge.llm_brain_core.{module_name}")
        new_module = importlib.import_module(
            f"agent_knowledge.llm_brain_core.objects.{module_name}"
        )
        modules[module_name] = (old_module, new_module)
    return modules


def test_objects_subpackage_modules_exist():
    failures: list[str] = []
    for module_name in OBJECT_MODULES:
        try:
            importlib.import_module(
                f"agent_knowledge.llm_brain_core.objects.{module_name}"
            )
        except Exception as exc:
            failures.append(f"{module_name}: {exc}")

    assert not failures, "llm_brain_core objects subpackage modules must exist: " + ", ".join(failures)


def test_object_subpackage_exports_match_root_contracts():
    try:
        modules = _import_objects_modules()
    except Exception as exc:
        pytest.skip(f"objects subpackage not available yet: {exc}")

    for module_name, symbols in OBJECT_PARITY_SYMBOLS.items():
        old_module, new_module = modules[module_name]
        for symbol in symbols:
            old_obj = getattr(old_module, symbol)
            new_obj = getattr(new_module, symbol)
            assert old_obj is new_obj


def test_root_llm_brain_core_does_not_reexport_object_cli():
    from agent_knowledge import llm_brain_core

    exported = set(getattr(llm_brain_core, "__all__", ()))
    leaked = sorted(name for name in ROOT_OBJECT_CLI_NAMES if name in exported)
    assert not leaked

    leaked_attr = [name for name in ROOT_OBJECT_CLI_NAMES if hasattr(llm_brain_core, name)]
    assert not leaked_attr


def test_objects_modules_are_not_root_only_implementations():
    try:
        modules = _import_objects_modules()
    except Exception as exc:
        pytest.skip(f"objects subpackage not available yet: {exc}")

    for module_name, (old_module, new_module) in modules.items():
        assert old_module.__name__ == f"agent_knowledge.llm_brain_core.{module_name}"
        assert new_module.__name__ == f"agent_knowledge.llm_brain_core.objects.{module_name}"
        assert old_module is not new_module
        assert new_module.__name__ != old_module.__name__

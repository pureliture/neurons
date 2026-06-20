from __future__ import annotations

import ast
from pathlib import Path

from agent_knowledge.llm_brain_core import artifact_store as artifact_store_module
from agent_knowledge.llm_brain_core import ontology, projection, runtime, sync_shadow

_CORE_DIR = Path(runtime.__file__).resolve().parent


def _module_imports(module_path: Path) -> set[str]:
    """Return the set of intra-package module names imported by `module_path`."""

    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            # `from .runtime import X` -> level 1, module "runtime".
            names.add(node.module.split(".")[0])
    return names


def test_episode_from_memory_card_lives_in_ontology_and_is_reexported_by_runtime():
    # The pure card->episode mapper is owned by the ontology (mapper) layer.
    assert ontology.episode_from_memory_card.__module__ == (
        "agent_knowledge.llm_brain_core.ontology"
    )
    # runtime re-exports the same object so existing call-site imports keep working.
    assert runtime.episode_from_memory_card is ontology.episode_from_memory_card
    # Downstream callers resolve to the single ontology-owned function.
    assert projection.episode_from_memory_card is ontology.episode_from_memory_card
    assert sync_shadow.episode_from_memory_card is ontology.episode_from_memory_card


def test_ontology_does_not_import_back_into_runtime_layer():
    # Layering invariant: the mapper module must not depend on the adapter/runtime
    # module. A regression here would reintroduce the layering inversion.
    imports = _module_imports(_CORE_DIR / "ontology.py")
    assert "runtime" not in imports
    # It still only leans on leaf modules.
    assert imports <= {"_util", "models"}


def test_inmemory_artifact_store_has_no_dead_to_episode_method():
    # The divergent (brain_id-less) dead `to_episode` mapper was removed. Session
    # episode mapping is owned solely by `ontology.episode_from_session_artifact`.
    store = artifact_store_module.InMemorySessionMemoryArtifactStore()
    assert not hasattr(store, "to_episode")
    assert hasattr(ontology, "episode_from_session_artifact")

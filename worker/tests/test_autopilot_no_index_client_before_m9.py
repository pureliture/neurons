from pathlib import Path

import agent_knowledge.llm_brain_core as core


def test_m1_to_m5_core_package_has_no_document_corpus_client_binding():
    package_root = Path(core.__file__).parent
    forbidden_needles = (
        "retired_index_bridge",
        "rag_flow",
        "index_api_key",
        "build_index_client",
        "dataset_ids",
        "document_ids",
    )
    m9_bridge_files = {"document_bridge.py"}

    scanned = []
    for path in package_root.rglob("*.py"):
        if path.name in m9_bridge_files:
            continue
        text = path.read_text(encoding="utf-8")
        scanned.append(path.name)
        lowered = text.lower()
        for needle in forbidden_needles:
            assert needle not in lowered, f"{needle} leaked into {path.relative_to(package_root)}"

    assert "__init__.py" in scanned
    assert "context.py" in scanned

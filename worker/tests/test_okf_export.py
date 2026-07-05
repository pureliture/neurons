from agent_knowledge.llm_brain_core.okf_export import build_okf_bundle
from agent_knowledge.llm_brain_core.object_packs import build_documentation_cleanup_pack


def test_okf_export_builds_deterministic_review_bundle_without_raw_body():
    pack = build_documentation_cleanup_pack(
        documents=[
            {
                "path": "README.md",
                "status": "source_of_truth",
                "reason": "approved_markdown_source",
                "confidence": 0.9,
                "evidence_refs": ["mem_readme"],
            }
        ]
    )

    files = build_okf_bundle({"documentation_cleanup": pack}, root="okf")

    assert sorted(files) == [
        "okf/edges.yml",
        "okf/evidence.yml",
        "okf/manifest.yml",
        "okf/objects.yml",
        "okf/packs/documentation_cleanup.md",
    ]
    assert "schema_version: okf_review_bundle.v1" in files["okf/manifest.yml"]
    assert "object_type: RepoDocument" in files["okf/objects.yml"]
    assert "accepted_current" in files["okf/packs/documentation_cleanup.md"]
    assert "raw_body" not in "\n".join(files.values())

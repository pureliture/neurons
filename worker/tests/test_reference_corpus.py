from agent_knowledge.llm_brain_core.reference_corpus import (
    build_corpus_ingest_plan,
    reference_corpus_objects_from_manifest,
)


def _manifest():
    return {
        "corpus_name": "palantir-ontology-mini",
        "sources": [
            {
                "source_id": "palantir-ontology-001",
                "title": "Ontology overview",
                "source_type": "WEB_PAGE",
                "source_url": "https://example.test/ontology",
                "normalized_path": "sources-normalized/palantir-ontology-001.md",
                "content_hash": "sha256:" + "1" * 64,
                "metadata_hash": "sha256:" + "2" * 64,
                "summary": "Objects, links, actions, functions.",
            },
            {
                "source_id": "palantir-ontology-002",
                "title": "Manual excerpt",
                "source_type": "TEXT",
                "normalized_path": "sources-normalized/palantir-ontology-002.md",
                "content_hash": "sha256:" + "3" * 64,
                "metadata_hash": "sha256:" + "4" * 64,
                "summary": "Manual source with missing URL.",
            },
        ],
    }


def test_corpus_ingest_plan_reports_storage_policy_and_missing_url_gap():
    plan = build_corpus_ingest_plan(
        _manifest(),
        project="neurons",
        storage_mode="external_object_store",
    )

    assert plan["schema_version"] == "reference_corpus_ingest_plan.v1"
    assert plan["corpus"]["source_count"] == 2
    assert plan["storage_mode"] == "external_object_store"
    assert plan["writes_planned"] is False
    assert plan["authority_lane"] == "reference_only"
    assert plan["missing_url_count"] == 1
    assert plan["source_url_gaps"] == [
        {
            "source_id": "palantir-ontology-002",
            "source_url_status": "missing_manual_text",
            "gap": "freshness_gap",
        }
    ]


def test_managed_snapshot_requires_raw_body_policy_fields():
    plan = build_corpus_ingest_plan(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )

    policy = plan["raw_body_policy"]
    assert policy == {
        "raw_body_policy": "no_raw_return_by_default",
        "return_capability": "denied_without_explicit_approval",
        "retention_class": "user_managed_reference",
        "redaction_profile": "public_safe_summary",
        "deletion_policy": "delete_snapshot_keep_metadata",
        "license_source_rights": "operator_attested",
    }


def test_corpus_ingest_plan_skips_non_mapping_sources_and_none_gaps():
    manifest = {
        "corpus_name": "palantir-ontology-mini",
        "sources": [
            None,
            "not-a-source",
            {
                "source_id": "palantir-ontology-001",
                "title": "Ontology overview",
                "source_type": "WEB_PAGE",
                "source_url": "https://example.test/ontology",
                "normalized_path": "sources-normalized/palantir-ontology-001.md",
                "content_hash": "sha256:" + "1" * 64,
                "metadata_hash": "sha256:" + "2" * 64,
                "summary": "Objects, links, actions, functions.",
            },
        ],
        "gaps": [None, "", "freshness_gap"],
    }

    plan = build_corpus_ingest_plan(
        manifest,
        project="neurons",
        storage_mode="metadata_only",
    )

    assert plan["corpus"]["source_count"] == 1
    assert plan["gaps"] == ["freshness_gap"]


def test_reference_corpus_manifest_maps_to_reference_only_objects():
    result = reference_corpus_objects_from_manifest(
        _manifest(),
        project="neurons",
        storage_mode="metadata_only",
    )

    assert result["corpus"]["authority_lane"] == "reference_only"
    assert result["corpus"]["storage_mode"] == "metadata_only"
    assert [source["source_url_status"] for source in result["sources"]] == [
        "present",
        "missing_manual_text",
    ]
    assert result["extraction_run"]["evaluation"]["missing_url_count"] == 1
    assert all(obj["authority_lane"] == "reference_only" for obj in result["objects"])
    assert all(obj["verification_state"] == "source_hash_verified" for obj in result["objects"])

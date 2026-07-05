from agent_knowledge.llm_brain_core.reference_corpus import (
    build_corpus_ingest_plan,
    reference_corpus_objects_from_manifest,
)
from agent_knowledge.ledger import Ledger


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


def _palantir_full_count_manifest():
    sources = []
    for idx in range(1, 66):
        if idx <= 6:
            source_type = "PDF"
        elif idx <= 39:
            source_type = "WEB_PAGE"
        else:
            source_type = "TEXT"
        source = {
            "source_id": f"palantir-ontology-{idx:03d}",
            "title": f"Palantir ontology reference {idx:03d}",
            "source_type": source_type,
            "normalized_path": f"sources-normalized/palantir-ontology-{idx:03d}.md",
            "content_hash": "sha256:" + f"{idx:064x}"[-64:],
            "metadata_hash": "sha256:" + f"{idx + 100:064x}"[-64:],
            "summary": "Public-safe metadata-only reference fixture.",
        }
        if idx <= 39:
            source["source_url"] = f"https://example.test/palantir/ontology/{idx:03d}"
        sources.append(source)
    return {"corpus_name": "palantir-ontology", "sources": sources}


def test_corpus_ingest_plan_reports_storage_policy_and_missing_url_gap():
    plan = build_corpus_ingest_plan(
        _manifest(),
        project="neurons",
        storage_mode="external_object_store",
    )

    assert plan["schema_version"] == "reference_corpus_ingest_plan.v1"
    assert plan["corpus"]["source_count"] == 2
    assert plan["source_url_count"] == 1
    assert plan["manual_text_without_url_count"] == 1
    assert plan["source_type_counts"] == {"TEXT": 1, "WEB_PAGE": 1}
    assert plan["storage_mode"] == "external_object_store"
    assert plan["manifest_hash"].startswith("sha256:")
    assert plan["hash_verification_status"] == "source_hash_verified"
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


def test_palantir_full_count_manifest_gate_is_metadata_only():
    plan = build_corpus_ingest_plan(
        _palantir_full_count_manifest(),
        project="neurons",
        storage_mode="external_object_store",
    )

    assert plan["corpus"]["name"] == "palantir-ontology"
    assert plan["corpus"]["source_count"] == 65
    assert plan["source_url_count"] == 39
    assert plan["manual_text_without_url_count"] == 26
    assert plan["missing_url_count"] == 26
    assert plan["source_type_counts"] == {"PDF": 6, "TEXT": 26, "WEB_PAGE": 33}
    assert plan["manifest_hash"].startswith("sha256:")
    assert plan["writes_planned"] is False


def test_corpus_ingest_plan_expected_count_gate_passes_without_writes():
    plan = build_corpus_ingest_plan(
        _palantir_full_count_manifest(),
        project="neurons",
        storage_mode="external_object_store",
        expected_source_count=65,
        expected_source_url_count=39,
        expected_manual_text_without_url_count=26,
        expected_source_type_counts={"PDF": 6, "WEB_PAGE": 33, "TEXT": 26},
    )

    assert plan["count_gate_status"] == "pass"
    assert plan["count_gate_gaps"] == []
    assert plan["expected_counts"] == {
        "source_count": 65,
        "source_url_count": 39,
        "manual_text_without_url_count": 26,
        "source_type_counts": {"PDF": 6, "TEXT": 26, "WEB_PAGE": 33},
    }
    assert plan["writes_planned"] is False


def test_corpus_ingest_plan_expected_count_gate_fails_closed_on_mismatch():
    plan = build_corpus_ingest_plan(
        _palantir_full_count_manifest(),
        project="neurons",
        storage_mode="external_object_store",
        expected_source_count=66,
        expected_source_url_count=40,
        expected_manual_text_without_url_count=25,
        expected_source_type_counts={"PDF": 7, "WEB_PAGE": 32, "TEXT": 26},
    )

    assert plan["count_gate_status"] == "fail"
    assert plan["count_gate_gaps"] == [
        {"field": "source_count", "expected": 66, "actual": 65},
        {"field": "source_url_count", "expected": 40, "actual": 39},
        {"field": "manual_text_without_url_count", "expected": 25, "actual": 26},
        {"field": "source_type_counts.PDF", "expected": 7, "actual": 6},
        {"field": "source_type_counts.WEB_PAGE", "expected": 32, "actual": 33},
    ]
    assert plan["writes_planned"] is False


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


def test_reference_corpus_manifest_maps_to_snapshot_chunk_and_freshness_objects():
    result = reference_corpus_objects_from_manifest(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )

    assert len(result["versions"]) == 2
    assert len(result["snapshots"]) == 2
    assert len(result["chunks"]) == 2
    assert len(result["freshness_checks"]) == 2
    assert all(version["schema_version"] == "document_version.v1" for version in result["versions"])
    assert all(version["authority_lane"] == "reference_only" for version in result["versions"])
    assert all(version["verification_state"] == "source_hash_verified" for version in result["versions"])
    assert result["snapshots"][0]["version_id"] == result["versions"][0]["version_id"]
    assert all(snapshot["raw_body_returnable"] is False for snapshot in result["snapshots"])
    assert all(snapshot["return_capability"] == "denied_without_explicit_approval" for snapshot in result["snapshots"])
    assert all(snapshot["retention_class"] == "user_managed_reference" for snapshot in result["snapshots"])
    assert all(snapshot["redaction_profile"] == "public_safe_summary" for snapshot in result["snapshots"])
    assert all(snapshot["deletion_policy"] == "delete_snapshot_keep_metadata" for snapshot in result["snapshots"])
    assert all(snapshot["license_source_rights"] == "operator_attested" for snapshot in result["snapshots"])
    assert all(chunk["body_storage_ref"] == "" for chunk in result["chunks"])
    assert [check["result"] for check in result["freshness_checks"]] == [
        "source_url_present",
        "source_url_missing_manual_text",
    ]


def test_reference_corpus_reingest_is_idempotent_for_stable_ids():
    first = reference_corpus_objects_from_manifest(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )
    second = reference_corpus_objects_from_manifest(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )

    assert first["corpus"]["corpus_id"] == second["corpus"]["corpus_id"]
    assert [source["source_id"] for source in first["sources"]] == [source["source_id"] for source in second["sources"]]
    assert [snapshot["snapshot_id"] for snapshot in first["snapshots"]] == [
        snapshot["snapshot_id"] for snapshot in second["snapshots"]
    ]
    assert [version["version_id"] for version in first["versions"]] == [
        version["version_id"] for version in second["versions"]
    ]
    assert [chunk["chunk_id"] for chunk in first["chunks"]] == [chunk["chunk_id"] for chunk in second["chunks"]]
    assert first["extraction_run"]["run_id"] == second["extraction_run"]["run_id"]


def test_reference_corpus_hash_mismatch_blocks_extraction_output():
    manifest = _manifest()
    manifest["sources"][0]["computed_content_hash"] = "sha256:" + "9" * 64

    result = reference_corpus_objects_from_manifest(
        manifest,
        project="neurons",
        storage_mode="managed_snapshot",
    )

    assert result["extraction_run"]["status"] == "blocked"
    assert result["extraction_run"]["evaluation"]["hash_mismatch_count"] == 1
    assert result["objects"] == []
    assert result["versions"] == []
    assert result["snapshots"] == []
    assert result["chunks"] == []
    assert result["rejected_inputs"] == [
        {
            "source_id": "palantir-ontology-001",
            "reason": "content_hash_mismatch",
        }
    ]


def test_reference_corpus_bundle_persists_to_local_test_ledger(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    bundle = reference_corpus_objects_from_manifest(
        _manifest(),
        project="neurons",
        storage_mode="managed_snapshot",
    )

    first = ledger.upsert_reference_corpus_bundle(bundle, project="neurons")
    second = ledger.upsert_reference_corpus_bundle(bundle, project="neurons")
    status = ledger.reference_corpus_status(project="neurons", corpus_id=bundle["corpus"]["corpus_id"])

    assert first["corpus_id"] == bundle["corpus"]["corpus_id"]
    assert second["write_count"] == first["write_count"]
    assert status["schema_version"] == "brain_corpus_status.v1"
    assert status["source_count"] == 2
    assert status["storage_modes"] == {"managed_snapshot": 2}
    assert status["reference_object_count"] == 2
    assert status["document_source_count"] == 2
    assert status["version_count"] == 2
    assert status["snapshot_count"] == 2
    assert status["chunk_count"] == 2
    assert status["freshness_check_count"] == 2
    assert status["extraction_run_count"] == 1
    assert status["first_class_store_counts"] == {
        "document_sources": 2,
        "document_versions": 2,
        "document_snapshots": 2,
        "document_chunks": 2,
        "freshness_checks": 2,
        "extraction_runs": 1,
    }
    assert status["document_sources"][0]["schema_version"] == "document_source.v1"
    assert status["document_sources"][0]["authority_lane"] == "reference_only"
    assert status["document_versions"][0]["schema_version"] == "document_version.v1"
    assert status["document_versions"][0]["authority_lane"] == "reference_only"
    assert status["document_snapshots"][0]["schema_version"] == "document_snapshot.v1"
    assert status["document_snapshots"][0]["raw_body_returnable"] is False
    assert status["document_chunks"][0]["schema_version"] == "document_chunk.v1"
    assert status["document_chunks"][0]["body_storage_ref"] == ""
    assert status["freshness_checks"][0]["schema_version"] == "freshness_check.v1"
    assert status["extraction_runs"][0]["status"] == "completed"
    assert status["freshness_gaps"] == [
        {
            "source_id": "palantir-ontology-002",
            "source_url_status": "missing_manual_text",
            "gap": "freshness_gap",
        }
    ]
    assert status["raw_body_policy"]["return_capability"] == "denied_without_explicit_approval"
    assert status["gaps"] == []

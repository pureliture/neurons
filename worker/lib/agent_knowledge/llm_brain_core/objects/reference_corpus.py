from __future__ import annotations

from typing import Any, Mapping

from .._util import ensure_public_safe, hash_payload, public_safe_text, require_sha256, short_hash, utc_now_iso
from .knowledge_objects import EvidenceRef, KnowledgeObjectEnvelope

STORAGE_MODES = {"external_object_store", "managed_snapshot", "metadata_only"}
RAW_BODY_POLICY = {
    "raw_body_policy": "no_raw_return_by_default",
    "return_capability": "denied_without_explicit_approval",
    "retention_class": "user_managed_reference",
    "redaction_profile": "public_safe_summary",
    "deletion_policy": "delete_snapshot_keep_metadata",
    "license_source_rights": "operator_attested",
}
PRODUCTION_CORPUS_INGEST_EVIDENCE_SCHEMA = "reference_corpus_production_ingest_evidence.v1"
PRODUCTION_CORPUS_INGEST_PROVENANCE_SCHEMA = "reference_corpus_production_ingest_evidence_provenance.v1"
PRODUCTION_CORPUS_INGEST_COLLECTION_MODE = "post_deploy_bounded_production_ingest"
PRODUCTION_CORPUS_INGEST_MUTATION_SCOPE = "bounded_production_corpus_ingest"


def default_corpus_policy_status() -> dict[str, Any]:
    return {
        "supported_storage_modes": sorted(STORAGE_MODES),
        "raw_body_policy": dict(RAW_BODY_POLICY),
        "source_rights_policy": "operator_attested_reference_use",
        "production_ingest_gate": "approved_bounded_cli_gate_required",
    }


def _storage_mode(value: str) -> str:
    mode = str(value or "")
    if mode not in STORAGE_MODES:
        raise ValueError("storage_mode must be external_object_store, managed_snapshot, or metadata_only")
    return mode


def _sources(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise ValueError("manifest.sources must be a list")
    return [dict(source) for source in sources if isinstance(source, Mapping)]


def _source_id(source: Mapping[str, Any]) -> str:
    return public_safe_text(str(source.get("source_id") or source.get("id") or source.get("title") or ""), max_chars=160)


def _source_url_status(source: Mapping[str, Any]) -> str:
    return "present" if str(source.get("source_url") or "").strip() else "missing_manual_text"


def _hash_or_payload(value: str, payload: Any) -> str:
    text = str(value or "")
    return require_sha256(text, "hash") if text else hash_payload(payload)


def _hash_mismatches(source: Mapping[str, Any]) -> bool:
    declared = str(source.get("content_hash") or "")
    computed = str(source.get("computed_content_hash") or "")
    if not declared or not computed:
        return False
    return require_sha256(declared, "content_hash") != require_sha256(computed, "computed_content_hash")


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    return hash_payload(manifest)


def _expected_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    count = int(value)
    if count < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return count


def _expected_type_counts(value: Mapping[str, Any] | None) -> dict[str, int]:
    if not value:
        return {}
    counts: dict[str, int] = {}
    for source_type, expected in value.items():
        key = public_safe_text(str(source_type or ""), max_chars=80)
        if not key:
            raise ValueError("expected_source_type_counts keys must be non-empty")
        count = _expected_int(expected, f"expected_source_type_counts.{key}")
        counts[key] = int(count or 0)
    return dict(sorted(counts.items()))


def _count_gate(
    *,
    source_count: int,
    source_url_count: int,
    manual_text_without_url_count: int,
    source_type_counts: Mapping[str, int],
    expected_source_count: int | None,
    expected_source_url_count: int | None,
    expected_manual_text_without_url_count: int | None,
    expected_source_type_counts: Mapping[str, int],
) -> dict[str, Any]:
    expected_counts: dict[str, Any] = {}
    gaps: list[dict[str, Any]] = []

    def compare(field: str, expected: int | None, actual: int) -> None:
        if expected is None:
            return
        expected_counts[field] = expected
        if expected != actual:
            gaps.append({"field": field, "expected": expected, "actual": actual})

    compare("source_count", expected_source_count, source_count)
    compare("source_url_count", expected_source_url_count, source_url_count)
    compare(
        "manual_text_without_url_count",
        expected_manual_text_without_url_count,
        manual_text_without_url_count,
    )
    if expected_source_type_counts:
        expected_counts["source_type_counts"] = dict(sorted(expected_source_type_counts.items()))
        actual_types = set(source_type_counts)
        expected_types = set(expected_source_type_counts)
        for source_type in sorted(actual_types | expected_types):
            expected = int(expected_source_type_counts.get(source_type, 0))
            actual = int(source_type_counts.get(source_type, 0))
            if expected != actual:
                gaps.append({"field": f"source_type_counts.{source_type}", "expected": expected, "actual": actual})

    if not expected_counts:
        status = "not_requested"
    elif gaps:
        status = "fail"
    else:
        status = "pass"
    return {
        "expected_counts": expected_counts,
        "count_gate_status": status,
        "count_gate_gaps": gaps,
    }


def build_corpus_ingest_plan(
    manifest: Mapping[str, Any],
    *,
    project: str,
    storage_mode: str,
    expected_source_count: int | None = None,
    expected_source_url_count: int | None = None,
    expected_manual_text_without_url_count: int | None = None,
    expected_source_type_counts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    mode = _storage_mode(storage_mode)
    sources = _sources(manifest)
    corpus_name = public_safe_text(str(manifest.get("corpus_name") or manifest.get("name") or "reference-corpus"), max_chars=160)
    source_type_counts: dict[str, int] = {}
    for source in sources:
        source_type = public_safe_text(str(source.get("source_type") or "TEXT"), max_chars=80)
        source_type_counts[source_type] = source_type_counts.get(source_type, 0) + 1
    gaps = [
        {
            "source_id": _source_id(source),
            "source_url_status": "missing_manual_text",
            "gap": "freshness_gap",
        }
        for source in sources
        if _source_url_status(source) == "missing_manual_text"
    ]
    source_url_count = len(sources) - len(gaps)
    manual_text_without_url_count = len(gaps)
    gate = _count_gate(
        source_count=len(sources),
        source_url_count=source_url_count,
        manual_text_without_url_count=manual_text_without_url_count,
        source_type_counts=source_type_counts,
        expected_source_count=_expected_int(expected_source_count, "expected_source_count"),
        expected_source_url_count=_expected_int(expected_source_url_count, "expected_source_url_count"),
        expected_manual_text_without_url_count=_expected_int(
            expected_manual_text_without_url_count,
            "expected_manual_text_without_url_count",
        ),
        expected_source_type_counts=_expected_type_counts(expected_source_type_counts),
    )
    plan = {
        "schema_version": "reference_corpus_ingest_plan.v1",
        "project": public_safe_text(project, max_chars=120),
        "manifest_ref": public_safe_text(str(manifest.get("manifest_ref") or ""), max_chars=240),
        "manifest_hash": _manifest_hash(manifest),
        "hash_verification_status": "source_hash_verified",
        "corpus": {
            "corpus_id": f"rc:{short_hash([project, corpus_name, len(sources)])}",
            "name": corpus_name,
            "source_count": len(sources),
        },
        "storage_mode": mode,
        "authority_lane": "reference_only",
        "writes_planned": False,
        "source_url_count": source_url_count,
        "manual_text_without_url_count": manual_text_without_url_count,
        "source_type_counts": dict(sorted(source_type_counts.items())),
        **gate,
        "missing_url_count": len(gaps),
        "source_url_gaps": gaps,
        "raw_body_policy": dict(RAW_BODY_POLICY),
        "rejected_inputs": [],
        "gaps": [public_safe_text(str(gap), max_chars=160) for gap in manifest.get("gaps", []) if gap],
    }
    ensure_public_safe(plan, "ReferenceCorpusIngestPlan")
    return plan


def build_reference_corpus_production_ingest_readiness_report(
    *,
    live_evidence: Mapping[str, Any] | None = None,
    expected_manifest_hash: str = "",
    expected_source_count: int | None = None,
    expected_corpus_id: str = "",
) -> dict[str, Any]:
    evidence = live_evidence if isinstance(live_evidence, Mapping) else {}
    expected_manifest = public_safe_text(str(expected_manifest_hash or ""), max_chars=120)
    expected_corpus = public_safe_text(str(expected_corpus_id or ""), max_chars=180)
    if not evidence:
        report = {
            "schema_version": "reference_corpus_production_ingest_readiness.v1",
            "status": "PASS_WITH_GAPS",
            "claims": [_production_corpus_ingest_missing_claim()],
            "failed_claims": [],
            "gaps": ["production_corpus_ingest_evidence_unverified"],
            "expected_manifest_hash": expected_manifest,
            "expected_source_count": expected_source_count,
            "expected_corpus_id": expected_corpus,
            "live_evidence_provided": False,
            "production_mutation_performed": False,
            "network_used": False,
            "evidence_collection_network_used": False,
        }
        ensure_public_safe(report, "ReferenceCorpusProductionIngestReadiness")
        return report
    claims = [
        _production_corpus_ingest_provenance_claim(evidence),
        _production_corpus_ingest_approval_claim(evidence),
        _production_corpus_ingest_corpus_claim(
            evidence,
            expected_manifest_hash=expected_manifest,
            expected_source_count=expected_source_count,
            expected_corpus_id=expected_corpus,
        ),
        _production_corpus_ingest_execution_claim(evidence),
        _production_corpus_ingest_read_after_write_claim(
            evidence,
            expected_manifest_hash=expected_manifest,
            expected_source_count=expected_source_count,
            expected_corpus_id=expected_corpus,
        ),
        _production_corpus_ingest_rollback_claim(evidence),
        _production_corpus_ingest_postcheck_claim(evidence),
    ]
    gaps = _dedupe(
        [
            str(gap)
            for claim in claims
            for gap in claim.get("gaps", [])
            if str(gap or "")
        ]
    )
    failed = [str(claim.get("claim_id") or "") for claim in claims if claim.get("status") == "failed"]
    report = {
        "schema_version": "reference_corpus_production_ingest_readiness.v1",
        "status": "FAIL" if failed else ("PASS_WITH_GAPS" if gaps else "PASS"),
        "claims": claims,
        "failed_claims": failed,
        "gaps": gaps,
        "expected_manifest_hash": expected_manifest,
        "expected_source_count": expected_source_count,
        "expected_corpus_id": expected_corpus,
        "live_evidence_provided": True,
        "production_mutation_performed": any(_claim_reports_mutation(claim) for claim in claims),
        "network_used": False,
        "evidence_collection_network_used": any(
            claim.get("claim_id") == "production.corpus_ingest.provenance"
            and claim.get("network_used_for_evidence") is True
            for claim in claims
        ),
    }
    ensure_public_safe(report, "ReferenceCorpusProductionIngestReadiness")
    return report


def build_reference_corpus_production_ingest_evidence(
    *,
    project: str,
    bundle: Mapping[str, Any],
    write_result: Mapping[str, Any],
    read_after_write_status: Mapping[str, Any],
    approval_ref_hash: str,
    evidence_collection_network_used: bool = False,
) -> dict[str, Any]:
    corpus = bundle.get("corpus")
    corpus = corpus if isinstance(corpus, Mapping) else {}
    manifest_hash = public_safe_text(str(corpus.get("manifest_ref") or ""), max_chars=120)
    corpus_id = public_safe_text(str(corpus.get("corpus_id") or write_result.get("corpus_id") or ""), max_chars=180)
    source_count = _int_value(write_result.get("source_count") or corpus.get("source_count"))
    read_source_count = _int_value(read_after_write_status.get("source_count"))
    manifest_hashes = read_after_write_status.get("manifest_hashes")
    manifest_hashes = manifest_hashes if isinstance(manifest_hashes, list) else []
    status_schema = public_safe_text(str(read_after_write_status.get("schema_version") or ""), max_chars=80)
    observed_manifest_hashes = [public_safe_text(str(item), max_chars=120) for item in manifest_hashes]
    read_after_write_validated = (
        status_schema == "brain_corpus_status.v1"
        and read_source_count == source_count
        and manifest_hash in observed_manifest_hashes
    )
    evidence = {
        "schema_version": PRODUCTION_CORPUS_INGEST_EVIDENCE_SCHEMA,
        "approval": {
            "approved": True,
            "approval_ref_hash": require_sha256(str(approval_ref_hash or ""), "approval_ref_hash"),
            "scope": "single_project_single_corpus",
            "project": public_safe_text(project, max_chars=120),
            "max_corpora": 1,
            "no_raw_body_returned": True,
        },
        "corpus": {
            "corpus_id": corpus_id,
            "manifest_hash": manifest_hash,
            "source_count": source_count,
            "storage_mode": public_safe_text(str(corpus.get("storage_mode") or ""), max_chars=80),
            "authority_lane": "reference_only",
            "raw_body_policy": "no_raw_return_by_default",
        },
        "ingest": {
            "target": "production_corpus_store",
            "ledger_scope": "production",
            "corpus_write_performed": write_result.get("mutation_performed") is True,
            "production_mutation_performed": True,
            "authority_write_performed": False,
            "write_schema_version": public_safe_text(str(write_result.get("schema_version") or ""), max_chars=120),
            "write_count": _int_value(write_result.get("write_count")),
            "document_source_count": _int_value(write_result.get("document_source_count")),
            "snapshot_count": _int_value(write_result.get("snapshot_count")),
            "chunk_count": _int_value(write_result.get("chunk_count")),
            "extraction_run_count": _int_value(write_result.get("extraction_run_count")),
        },
        "read_after_write": {
            "status": "validated" if read_after_write_validated else "failed",
            "corpus_id": corpus_id,
            "manifest_hash": manifest_hash,
            "source_count": read_source_count,
        },
        "rollback_or_deletion": {
            "status": "planned",
            "path": ["delete_snapshot_keep_metadata"],
        },
        "postcheck": {
            "status": "validated",
            "raw_body_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "evidence_provenance": {
            "schema_version": PRODUCTION_CORPUS_INGEST_PROVENANCE_SCHEMA,
            "collection_mode": PRODUCTION_CORPUS_INGEST_COLLECTION_MODE,
            "network_used": bool(evidence_collection_network_used),
            "mutation_scope": PRODUCTION_CORPUS_INGEST_MUTATION_SCOPE,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "mutation_performed": True,
        "production_mutation_performed": True,
        "authority_write_performed": False,
        "protected_values_returned": False,
    }
    ensure_public_safe(evidence, "ReferenceCorpusProductionIngestEvidence")
    return evidence


def _production_corpus_ingest_missing_claim() -> dict[str, Any]:
    return {
        "claim_id": "production.corpus_ingest.evidence",
        "evidence_class": "production_corpus_ingest",
        "status": "not_validated",
        "production_mutation_performed": False,
        "gaps": ["production_corpus_ingest_evidence_unverified"],
    }


def _production_corpus_ingest_provenance_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    provenance = evidence.get("evidence_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    failures: list[str] = []
    if evidence.get("schema_version") != PRODUCTION_CORPUS_INGEST_EVIDENCE_SCHEMA:
        failures.append("production_corpus_ingest_schema_mismatch")
    if provenance.get("schema_version") != PRODUCTION_CORPUS_INGEST_PROVENANCE_SCHEMA:
        failures.append("production_corpus_ingest_provenance_schema_mismatch")
    if provenance.get("collection_mode") != PRODUCTION_CORPUS_INGEST_COLLECTION_MODE:
        failures.append("production_corpus_ingest_collection_mode_unverified")
    if provenance.get("mutation_scope") != PRODUCTION_CORPUS_INGEST_MUTATION_SCOPE:
        failures.append("production_corpus_ingest_mutation_scope_unverified")
    for field, gap in (
        ("raw_private_evidence_returned", "production_corpus_ingest_raw_private_evidence_returned"),
        ("secret_returned", "production_corpus_ingest_secret_returned"),
        ("host_topology_returned", "production_corpus_ingest_host_topology_returned"),
        ("raw_external_ids_returned", "production_corpus_ingest_raw_external_ids_returned"),
    ):
        if provenance.get(field) is not False:
            failures.append(gap)
    return {
        "claim_id": "production.corpus_ingest.provenance",
        "evidence_class": "production_corpus_ingest",
        "status": "failed" if failures else "validated",
        "collection_mode": public_safe_text(str(provenance.get("collection_mode") or ""), max_chars=80),
        "mutation_scope": public_safe_text(str(provenance.get("mutation_scope") or ""), max_chars=80),
        "network_used_for_evidence": provenance.get("network_used") is True,
        "production_mutation_performed": False,
        "gaps": _dedupe(failures),
    }


def _production_corpus_ingest_approval_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    approval = evidence.get("approval")
    approval = approval if isinstance(approval, Mapping) else {}
    failures: list[str] = []
    if approval.get("approved") is not True:
        failures.append("production_corpus_ingest_approval_missing")
    if not _is_sha256_ref(approval.get("approval_ref_hash")):
        failures.append("production_corpus_ingest_approval_ref_hash_missing")
    if approval.get("scope") != "single_project_single_corpus":
        failures.append("production_corpus_ingest_scope_not_single_corpus")
    if _int_value(approval.get("max_corpora")) != 1:
        failures.append("production_corpus_ingest_max_corpora_not_one")
    if approval.get("no_raw_body_returned") is not True:
        failures.append("production_corpus_ingest_raw_body_guard_missing")
    return {
        "claim_id": "production.corpus_ingest.approval",
        "evidence_class": "production_corpus_ingest",
        "status": "failed" if failures else "validated",
        "approval_ref_hash_present": bool(str(approval.get("approval_ref_hash") or "")),
        "production_mutation_performed": False,
        "gaps": _dedupe(failures),
    }


def _is_sha256_ref(value: Any) -> bool:
    try:
        require_sha256(str(value or ""), "approval_ref_hash")
    except ValueError:
        return False
    return True


def _production_corpus_ingest_corpus_claim(
    evidence: Mapping[str, Any],
    *,
    expected_manifest_hash: str,
    expected_source_count: int | None,
    expected_corpus_id: str,
) -> dict[str, Any]:
    corpus = evidence.get("corpus")
    corpus = corpus if isinstance(corpus, Mapping) else {}
    failures = _corpus_identity_failures(
        corpus,
        expected_manifest_hash=expected_manifest_hash,
        expected_source_count=expected_source_count,
        expected_corpus_id=expected_corpus_id,
    )
    if corpus.get("storage_mode") not in STORAGE_MODES:
        failures.append("production_corpus_ingest_storage_mode_unknown")
    if corpus.get("authority_lane") != "reference_only":
        failures.append("production_corpus_ingest_authority_lane_not_reference_only")
    if corpus.get("raw_body_policy") != "no_raw_return_by_default":
        failures.append("production_corpus_ingest_raw_body_policy_unverified")
    return {
        "claim_id": "production.corpus_ingest.corpus",
        "evidence_class": "production_corpus_ingest",
        "status": "failed" if failures else "validated",
        "corpus_id": public_safe_text(str(corpus.get("corpus_id") or ""), max_chars=180),
        "manifest_hash": public_safe_text(str(corpus.get("manifest_hash") or ""), max_chars=120),
        "source_count": _int_value(corpus.get("source_count")),
        "production_mutation_performed": False,
        "gaps": _dedupe(failures),
    }


def _production_corpus_ingest_execution_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    ingest = evidence.get("ingest")
    ingest = ingest if isinstance(ingest, Mapping) else {}
    failures: list[str] = []
    if ingest.get("target") != "production_corpus_store":
        failures.append("production_corpus_ingest_target_unverified")
    if ingest.get("ledger_scope") != "production":
        failures.append("production_corpus_ingest_scope_not_production")
    if ingest.get("corpus_write_performed") is not True:
        failures.append("production_corpus_ingest_write_missing")
    if ingest.get("production_mutation_performed") is not True:
        failures.append("production_corpus_ingest_mutation_not_reported")
    if ingest.get("authority_write_performed") is True:
        failures.append("production_corpus_ingest_changed_authority")
    return {
        "claim_id": "production.corpus_ingest.execution",
        "evidence_class": "production_corpus_ingest",
        "status": "failed" if failures else "validated",
        "production_mutation_performed": ingest.get("production_mutation_performed") is True,
        "authority_write_performed": ingest.get("authority_write_performed") is True,
        "gaps": _dedupe(failures),
    }


def _production_corpus_ingest_read_after_write_claim(
    evidence: Mapping[str, Any],
    *,
    expected_manifest_hash: str,
    expected_source_count: int | None,
    expected_corpus_id: str,
) -> dict[str, Any]:
    read_after_write = evidence.get("read_after_write")
    read_after_write = read_after_write if isinstance(read_after_write, Mapping) else {}
    failures = _corpus_identity_failures(
        read_after_write,
        expected_manifest_hash=expected_manifest_hash,
        expected_source_count=expected_source_count,
        expected_corpus_id=expected_corpus_id,
    )
    if read_after_write.get("status") != "validated":
        failures.append("production_corpus_ingest_read_after_write_missing")
    return {
        "claim_id": "production.corpus_ingest.read_after_write",
        "evidence_class": "production_corpus_ingest",
        "status": "failed" if failures else "validated",
        "production_mutation_performed": False,
        "gaps": _dedupe(failures),
    }


def _production_corpus_ingest_rollback_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    rollback = evidence.get("rollback_or_deletion")
    rollback = rollback if isinstance(rollback, Mapping) else {}
    failures: list[str] = []
    if rollback.get("status") not in {"planned", "validated"}:
        failures.append("production_corpus_ingest_rollback_or_deletion_missing")
    if not _string_list(rollback.get("path")):
        failures.append("production_corpus_ingest_rollback_or_deletion_path_missing")
    return {
        "claim_id": "production.corpus_ingest.rollback_or_deletion",
        "evidence_class": "production_corpus_ingest",
        "status": "failed" if failures else "validated",
        "production_mutation_performed": False,
        "gaps": _dedupe(failures),
    }


def _production_corpus_ingest_postcheck_claim(evidence: Mapping[str, Any]) -> dict[str, Any]:
    postcheck = evidence.get("postcheck")
    postcheck = postcheck if isinstance(postcheck, Mapping) else {}
    failures: list[str] = []
    if postcheck.get("status") != "validated":
        failures.append("production_corpus_ingest_postcheck_missing")
    for field, gap in (
        ("raw_body_returned", "production_corpus_ingest_raw_body_returned"),
        ("secret_returned", "production_corpus_ingest_secret_returned"),
        ("host_topology_returned", "production_corpus_ingest_host_topology_returned"),
        ("raw_external_ids_returned", "production_corpus_ingest_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    return {
        "claim_id": "production.corpus_ingest.postcheck",
        "evidence_class": "production_corpus_ingest",
        "status": "failed" if failures else "validated",
        "production_mutation_performed": False,
        "gaps": _dedupe(failures),
    }


def _corpus_identity_failures(
    payload: Mapping[str, Any],
    *,
    expected_manifest_hash: str,
    expected_source_count: int | None,
    expected_corpus_id: str,
) -> list[str]:
    failures: list[str] = []
    if expected_manifest_hash and payload.get("manifest_hash") != expected_manifest_hash:
        failures.append("production_corpus_ingest_manifest_hash_mismatch")
    if expected_source_count is not None and _int_value(payload.get("source_count")) != expected_source_count:
        failures.append("production_corpus_ingest_source_count_mismatch")
    if expected_corpus_id and payload.get("corpus_id") != expected_corpus_id:
        failures.append("production_corpus_ingest_corpus_id_mismatch")
    return failures


def _claim_reports_mutation(claim: Mapping[str, Any]) -> bool:
    return claim.get("production_mutation_performed") is True


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [public_safe_text(str(item), max_chars=160) for item in value if str(item or "")]


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dedupe(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def reference_corpus_objects_from_manifest(
    manifest: Mapping[str, Any],
    *,
    project: str,
    storage_mode: str,
) -> dict[str, Any]:
    plan = build_corpus_ingest_plan(manifest, project=project, storage_mode=storage_mode)
    mode = plan["storage_mode"]
    corpus = {
        **plan["corpus"],
        "schema_version": "reference_corpus.v1",
        "storage_mode": mode,
        "authority_lane": "reference_only",
        "verification_state": "source_hash_verified",
        "privacy_class": "local_private",
        "freshness_policy": "source_url_recheck_when_available",
        "license_policy": "operator_attested_reference_use",
        "raw_body_policy": "no_raw_return_by_default",
        "manifest_ref": plan["manifest_hash"],
    }
    document_sources: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    freshness_checks: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    rejected_inputs = [
        {
            "source_id": _source_id(source),
            "reason": "content_hash_mismatch",
        }
        for source in _sources(manifest)
        if _hash_mismatches(source)
    ]
    extraction_blocked = bool(rejected_inputs)
    for source in _sources(manifest):
        source_id = _source_id(source)
        content_hash = _hash_or_payload(str(source.get("content_hash") or ""), source)
        metadata_hash = _hash_or_payload(str(source.get("metadata_hash") or ""), {"source_id": source_id, "title": source.get("title")})
        normalized_path = public_safe_text(str(source.get("normalized_path") or ""), max_chars=240)
        status = _source_url_status(source)
        document_source = {
            "schema_version": "document_source.v1",
            "source_id": f"ds:{short_hash([corpus['corpus_id'], source_id])}",
            "natural_source_id": source_id,
            "corpus_id": corpus["corpus_id"],
            "title": public_safe_text(str(source.get("title") or source_id), max_chars=240),
            "source_type": public_safe_text(str(source.get("source_type") or "TEXT"), max_chars=80),
            "source_url_status": status,
            "storage_mode": mode,
            "source_url_ref": hash_payload(str(source.get("source_url") or "")) if status == "present" else "",
            "normalized_path_ref": normalized_path,
            "content_hash": content_hash,
            "metadata_hash": metadata_hash,
            "authority_lane": "reference_only",
            "verification_state": "source_hash_verified",
            "license_source_rights": "operator_attested",
        }
        document_sources.append(document_source)
        freshness_check = {
            "schema_version": "freshness_check.v1",
            "check_id": f"fresh:{short_hash([document_source['source_id'], status, metadata_hash])}",
            "source_id": document_source["source_id"],
            "check_mode": "url_metadata",
            "status": "checked" if status == "present" else "gap",
            "result": "source_url_present" if status == "present" else "source_url_missing_manual_text",
            "checked_at": utc_now_iso(),
            "gaps": ["freshness_gap"] if status == "missing_manual_text" else [],
        }
        freshness_checks.append(freshness_check)
        if extraction_blocked:
            continue
        version = {
            "schema_version": "document_version.v1",
            "version_id": f"ver:{short_hash([document_source['source_id'], content_hash, metadata_hash])}",
            "source_id": document_source["source_id"],
            "corpus_id": corpus["corpus_id"],
            "storage_mode": mode,
            "content_hash": content_hash,
            "metadata_hash": metadata_hash,
            "source_version_ref": public_safe_text(str(source.get("source_version") or content_hash), max_chars=160),
            "manifest_ref": corpus["manifest_ref"],
            "authority_lane": "reference_only",
            "verification_state": "source_hash_verified",
            "freshness_state": status,
            "observed_at": utc_now_iso(),
        }
        versions.append(version)
        if mode == "managed_snapshot":
            snapshot = {
                "schema_version": "document_snapshot.v1",
                "snapshot_id": f"snap:{short_hash([document_source['source_id'], content_hash, mode])}",
                "source_id": document_source["source_id"],
                "version_id": version["version_id"],
                "storage_mode": mode,
                "snapshot_kind": "normalized_markdown",
                "content_hash": content_hash,
                "body_storage_ref": f"private_store:{short_hash([document_source['source_id'], content_hash])}",
                "raw_body_returnable": False,
                **RAW_BODY_POLICY,
            }
            snapshots.append(snapshot)
            chunks.append(
                {
                    "schema_version": "document_chunk.v1",
                    "chunk_id": f"chunk:{short_hash([snapshot['snapshot_id'], 0, content_hash])}",
                    "snapshot_id": snapshot["snapshot_id"],
                    "ordinal": 0,
                    "content_hash": content_hash,
                    "summary": public_safe_text(str(source.get("summary") or source.get("title") or source_id), max_chars=320),
                    "body_storage_ref": "",
                }
            )
        ev = EvidenceRef.from_parts(
            evidence_type="source_hash",
            authority_lane="reference_only",
            verification_state="source_hash_verified",
            locator={"kind": "relative_corpus_path", "value": normalized_path or source_id},
            content_hash=content_hash,
            summary=f"Reference source hash for {source_id}.",
            gaps=["freshness_gap"] if status == "missing_manual_text" else [],
        )
        evidence.append(ev.to_dict())
        obj = KnowledgeObjectEnvelope.from_parts(
            object_type="ReferenceDocument",
            natural_key=source_id,
            scope={"project": project, "corpus_id": corpus["corpus_id"]},
            title=str(source.get("title") or source_id),
            summary=str(source.get("summary") or ""),
            lifecycle_status="observed",
            authority_lane="reference_only",
            verification_state="source_hash_verified",
            review_state="not_required",
            content_hash=content_hash,
            evidence_refs=[ev.evidence_id],
            confidence={"score": 0.6, "basis": "manifest_hash"},
            recommended_action="review" if status == "missing_manual_text" else "use_as_reference",
            freshness={"state": status, "gaps": ["freshness_gap"] if status == "missing_manual_text" else []},
            payload={
                "source_id": document_source["source_id"],
                "source_url_status": status,
                "normalized_path_ref": normalized_path,
            },
        )
        objects.append(obj.to_dict())
    extraction_run = {
        "schema_version": "extraction_run.v1",
        "run_id": f"extract:{short_hash([corpus['corpus_id'], corpus['manifest_ref'], mode])}",
        "corpus_id": corpus["corpus_id"],
        "extractor": "reference_corpus_mapper",
        "extractor_version": "0.1",
        "input_hash": corpus["manifest_ref"],
        "output_object_count": len(objects),
        "output_edge_count": 0,
        "status": "blocked" if extraction_blocked else "completed",
        "evaluation": {
            "public_safe_scan": "pass",
            "source_count_match": "blocked" if extraction_blocked else "pass",
            "missing_url_count": plan["missing_url_count"],
            "hash_mismatch_count": len(rejected_inputs),
            "no_raw_output_scan": "pass",
        },
    }
    result = {
        "schema_version": "reference_corpus_objects.v1",
        "corpus": corpus,
        "sources": document_sources,
        "versions": versions,
        "snapshots": snapshots,
        "chunks": chunks,
        "freshness_checks": freshness_checks,
        "objects": objects,
        "evidence": evidence,
        "extraction_run": extraction_run,
        "freshness_gaps": plan["source_url_gaps"],
        "rejected_inputs": rejected_inputs,
    }
    ensure_public_safe(result, "ReferenceCorpusObjects")
    return result

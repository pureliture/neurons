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


def _storage_mode(value: str) -> str:
    mode = str(value or "")
    if mode not in STORAGE_MODES:
        raise ValueError("storage_mode must be external_object_store, managed_snapshot, or metadata_only")
    return mode


def _sources(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise ValueError("manifest.sources must be a list")
    return [dict(source) for source in sources]


def _source_id(source: Mapping[str, Any]) -> str:
    return public_safe_text(str(source.get("source_id") or source.get("id") or source.get("title") or ""), max_chars=160)


def _source_url_status(source: Mapping[str, Any]) -> str:
    return "present" if str(source.get("source_url") or "").strip() else "missing_manual_text"


def _hash_or_payload(value: str, payload: Any) -> str:
    text = str(value or "")
    return require_sha256(text, "hash") if text else hash_payload(payload)


def build_corpus_ingest_plan(
    manifest: Mapping[str, Any],
    *,
    project: str,
    storage_mode: str,
) -> dict[str, Any]:
    mode = _storage_mode(storage_mode)
    sources = _sources(manifest)
    corpus_name = public_safe_text(str(manifest.get("corpus_name") or manifest.get("name") or "reference-corpus"), max_chars=160)
    gaps = [
        {
            "source_id": _source_id(source),
            "source_url_status": "missing_manual_text",
            "gap": "freshness_gap",
        }
        for source in sources
        if _source_url_status(source) == "missing_manual_text"
    ]
    plan = {
        "schema_version": "reference_corpus_ingest_plan.v1",
        "project": public_safe_text(project, max_chars=120),
        "manifest_ref": public_safe_text(str(manifest.get("manifest_ref") or ""), max_chars=240),
        "corpus": {
            "corpus_id": f"rc:{short_hash([project, corpus_name, len(sources)])}",
            "name": corpus_name,
            "source_count": len(sources),
        },
        "storage_mode": mode,
        "authority_lane": "reference_only",
        "writes_planned": False,
        "missing_url_count": len(gaps),
        "source_url_gaps": gaps,
        "raw_body_policy": dict(RAW_BODY_POLICY),
        "rejected_inputs": [],
        "gaps": [public_safe_text(str(gap), max_chars=160) for gap in manifest.get("gaps", []) if str(gap)],
    }
    ensure_public_safe(plan, "ReferenceCorpusIngestPlan")
    return plan


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
        "manifest_ref": hash_payload(manifest),
    }
    document_sources: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
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
        "status": "completed",
        "evaluation": {
            "public_safe_scan": "pass",
            "source_count_match": "pass",
            "missing_url_count": plan["missing_url_count"],
            "no_raw_output_scan": "pass",
        },
    }
    result = {
        "schema_version": "reference_corpus_objects.v1",
        "corpus": corpus,
        "sources": document_sources,
        "objects": objects,
        "evidence": evidence,
        "extraction_run": extraction_run,
        "freshness_gaps": plan["source_url_gaps"],
    }
    ensure_public_safe(result, "ReferenceCorpusObjects")
    return result

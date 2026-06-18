import json

from agent_knowledge.llm_brain_core import SourceRefResolver
from agent_knowledge.llm_brain_core.models import EvidenceRequest, SourceRefRecord


def test_source_ref_policy_resolution_golden_states():
    resolver = SourceRefResolver(
        [
            _source("src_meta", "metadata_only"),
            _source("src_derived", "derived_only", derived_summary="Test result passed after config update."),
            _source("src_local", "local_only", redacted_content="See /Users/example/private.txt with TOKEN=secret"),
            _source("src_full", "full_sync", redacted_content="Bounded redacted content"),
            _source("src_revoked", "derived_only", revoked_at="2026-06-18T00:00:00Z"),
            _source("src_deleted", "derived_only", deleted_at="2026-06-18T00:00:00Z"),
        ]
    )

    assert resolver.resolve(_request("src_meta")).to_dict()["resolution_state"] == "metadata_only"
    derived = resolver.resolve(_request("src_derived")).to_dict()
    assert derived["resolution_state"] == "derived_only"
    assert derived["reason_code"] == "policy_derived_only"
    assert derived["content"] == "Test result passed after config update."
    local_foreign = resolver.resolve(_request("src_local", requesting_device_id_hash=_h("other-device"))).to_dict()
    assert local_foreign["resolution_state"] == "same_device_required"
    assert local_foreign["same_device_proof"] == "failed"
    local_no_approval = resolver.resolve(_request("src_local")).to_dict()
    assert local_no_approval["resolution_state"] == "approval_required"
    local_resolved = resolver.resolve(_request("src_local", approval_ref="approval:test")).to_dict()
    assert local_resolved["resolution_state"] == "resolved"
    assert "[redacted_path]" in local_resolved["content"]
    assert "TOKEN=secret" not in local_resolved["content"]
    assert resolver.resolve(_request("src_full")).to_dict()["resolution_state"] == "resolved"
    assert resolver.resolve(_request("src_revoked")).to_dict()["resolution_state"] == "permission_revoked"
    assert resolver.resolve(_request("src_deleted")).to_dict()["resolution_state"] == "deleted_source"
    stale = resolver.resolve(_request("src_meta", expected_content_hash=_h("different"))).to_dict()
    assert stale["resolution_state"] == "stale_hash"
    assert resolver.resolve(_request("src_unknown")).to_dict()["resolution_state"] == "unresolved"

    serialized = json.dumps(local_resolved, sort_keys=True)
    assert "/Users/" not in serialized
    assert "TOKEN" not in serialized
    assert "private.txt" not in serialized


def _source(source_ref_id, sync_policy, **overrides):
    values = {
        "source_ref_id": source_ref_id,
        "device_id_hash": _h("device-a"),
        "root_id": "project-root",
        "relative_path_hash": _h(f"{source_ref_id}:path"),
        "content_hash": _h(f"{source_ref_id}:content"),
        "mtime": "2026-06-18T00:00:00Z",
        "size": 100,
        "sync_policy": sync_policy,
        "permission_scope": "project",
        "last_seen_at": "2026-06-18T00:00:00Z",
    }
    values.update(overrides)
    return SourceRefRecord(**values)


def _request(source_ref_id, requesting_device_id_hash=None, approval_ref="", expected_content_hash=""):
    return EvidenceRequest(
        source_ref_id=source_ref_id,
        requesting_device_id_hash=requesting_device_id_hash or _h("device-a"),
        approval_ref=approval_ref,
        expected_content_hash=expected_content_hash,
    )


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

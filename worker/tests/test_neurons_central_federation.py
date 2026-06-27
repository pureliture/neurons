import pytest

from agent_knowledge.llm_brain_core.central_federation import federate_neurons_local_artifacts
from agent_knowledge.llm_brain_core.local_brain import build_neurons_local_sync_artifact
from agent_knowledge.llm_brain_core.local_evidence import local_evidence_edges_from_capture


def test_neurons_central_federation_dedupes_edges_across_devices():
    artifact_a = _artifact("device-a", content_hash=_h("content-a"))
    artifact_b = _artifact("device-b", content_hash=_h("content-a"))

    result = federate_neurons_local_artifacts([artifact_a, artifact_b])

    assert result["schema_version"] == "neurons_central_federation.v1"
    assert result["central_safe"] is True
    assert result["device_count"] == 2
    assert result["edge_count"] == 1
    assert result["conflicts"] == []
    assert result["merged_edges"][0]["device_id_hashes"] == [_h("device-a"), _h("device-b")]


def test_neurons_central_federation_preserves_sync_policy_in_merge_key():
    artifact_a = _artifact("device-a", content_hash=_h("content-a"), sync_policy="local_only")
    artifact_b = _artifact("device-b", content_hash=_h("content-a"), sync_policy="metadata_only")

    result = federate_neurons_local_artifacts([artifact_a, artifact_b])

    assert result["edge_count"] == 2
    assert [edge["sync_policy"] for edge in result["merged_edges"]] == ["local_only", "metadata_only"]


def test_neurons_central_federation_reports_file_content_conflicts():
    artifact_a = _artifact("device-a", content_hash=_h("content-a"))
    artifact_b = _artifact("device-b", content_hash=_h("content-b"))

    result = federate_neurons_local_artifacts([artifact_a, artifact_b])

    assert result["edge_count"] == 2
    assert result["conflicts"] == [
        {
            "code": "file_content_hash_conflict",
            "target_ref": f"file:{_h('worker/lib/context.py')}",
            "content_hashes": [_h("content-a"), _h("content-b")],
            "device_id_hashes": [_h("device-a"), _h("device-b")],
            "explainable": True,
        }
    ]


def test_neurons_central_federation_rejects_raw_graph_or_body_residue():
    artifact = _artifact("device-a", content_hash=_h("content-a"))
    artifact["graph_db_file"] = "neo4j.db"

    with pytest.raises(ValueError, match="raw file bodies and graph DB files must not be centrally synced"):
        federate_neurons_local_artifacts([artifact])


def _artifact(device: str, *, content_hash: str, sync_policy: str = "metadata_only"):
    device_id = _h(device)
    edges = local_evidence_edges_from_capture(
        [
            {
                "evidence_type": "session_file",
                "session_id_hash": _h(f"session-{device}"),
                "device_id_hash": device_id,
                "relative_path_hash": _h("worker/lib/context.py"),
                "content_hash": content_hash,
                "sync_policy": sync_policy,
            }
        ]
    )
    return build_neurons_local_sync_artifact(
        project="neurons",
        device_id_hash=device_id,
        evidence_edges=edges,
    )


def _h(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

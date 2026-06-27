import pytest

from agent_knowledge.llm_brain_core.local_brain import build_neurons_local_sync_artifact, resolve_neurons_local_context
from agent_knowledge.llm_brain_core.local_evidence import local_evidence_edges_from_capture

from test_context_authority_pack import _card


def test_neurons_local_sync_artifact_is_central_safe_and_hash_only():
    device_id = _h("device-a")
    edges = local_evidence_edges_from_capture(
        [
            {
                "evidence_type": "session_file",
                "session_id_hash": _h("session-a"),
                "device_id_hash": device_id,
                "relative_path_hash": _h("worker/lib/context.py"),
                "content_hash": _h("content-a"),
                "sync_policy": "metadata_only",
            }
        ]
    )

    artifact = build_neurons_local_sync_artifact(
        project="neurons",
        device_id_hash=device_id,
        evidence_edges=edges,
    )

    assert artifact == {
        "schema_version": "neurons_local_sync_artifact.v1",
        "project": "neurons",
        "device_id_hash": device_id,
        "central_safe": True,
        "raw_body_included": False,
        "evidence_edges": edges,
        "edge_count": 1,
    }


def test_neurons_local_sync_artifact_rejects_raw_body_residue():
    with pytest.raises(ValueError, match="raw file bodies must remain local"):
        build_neurons_local_sync_artifact(
            project="neurons",
            device_id_hash=_h("device-a"),
            evidence_edges=[
                {
                    "edge_type": "SessionFile",
                    "source_ref": f"session:{_h('session-a')}",
                    "target_ref": f"file:{_h('file-a')}",
                    "device_id_hash": _h("device-a"),
                    "relative_path_hash": _h("file-a"),
                    "content_hash": _h("content-a"),
                    "sync_policy": "metadata_only",
                    "raw_body": "private file body",
                    "raw_body_included": True,
                }
            ],
        )


def test_neurons_local_context_resolves_offline_with_sync_artifact():
    device_id = _h("device-a")
    task = _card(
        "mem_local_task",
        "task",
        "Answer local context offline",
        {
            "task_state": "Answer local context offline",
            "next_action": "Use local card and evidence only",
            "status": "open",
        },
    )
    result = resolve_neurons_local_context(
        project="neurons",
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_request="answer local context offline",
        current_files=[],
        device_id_hash=device_id,
        memory_cards=[task],
        evidence_records=[
            {
                "evidence_type": "session_file",
                "session_id_hash": _h("session-a"),
                "device_id_hash": device_id,
                "relative_path_hash": _h("worker/lib/context.py"),
                "content_hash": _h("content-a"),
                "sync_policy": "metadata_only",
            }
        ],
        response_mode="compact",
    )

    assert result["local_mode"] is True
    assert result["response_mode"] == "compact"
    assert result["current_task"] == "Answer local context offline"
    assert result["last_stopped_at"] == "Use local card and evidence only"
    assert result["graph_status"]["status"] == "unavailable"
    assert result["sync_artifact"]["central_safe"] is True
    assert result["sync_artifact"]["edge_count"] == 1
    assert result["sync_artifact"]["raw_body_included"] is False


def _h(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

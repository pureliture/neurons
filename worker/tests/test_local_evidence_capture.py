import pytest

from agent_knowledge.llm_brain_core import FakeGraphMemoryAdapter, GraphProjectionWorker
from agent_knowledge.llm_brain_core.local_evidence import (
    local_evidence_edges_from_capture,
    local_evidence_episodes_from_capture,
)


def test_local_evidence_capture_builds_session_and_commit_file_edges_without_raw_bodies():
    session_id = _h("session-a")
    device_id = _h("device-a")
    relative_path = _h("worker/lib/agent_knowledge/context.py")
    content = _h("file-content")

    edges = local_evidence_edges_from_capture(
        [
            {
                "evidence_type": "session_file",
                "session_id_hash": session_id,
                "device_id_hash": device_id,
                "relative_path_hash": relative_path,
                "content_hash": content,
                "sync_policy": "metadata_only",
            },
            {
                "evidence_type": "commit_file",
                "commit_id": "commit:abc123",
                "device_id_hash": device_id,
                "relative_path_hash": relative_path,
                "content_hash": content,
                "sync_policy": "derived_only",
            },
        ]
    )

    assert edges == [
        {
            "edge_type": "SessionFile",
            "source_ref": f"session:{session_id}",
            "target_ref": f"file:{relative_path}",
            "device_id_hash": device_id,
            "relative_path_hash": relative_path,
            "content_hash": content,
            "sync_policy": "metadata_only",
            "raw_body_included": False,
        },
        {
            "edge_type": "CommitFile",
            "source_ref": "commit:abc123",
            "target_ref": f"file:{relative_path}",
            "device_id_hash": device_id,
            "relative_path_hash": relative_path,
            "content_hash": content,
            "sync_policy": "derived_only",
            "raw_body_included": False,
        },
    ]


def test_local_evidence_capture_rejects_raw_file_body_fields():
    with pytest.raises(ValueError, match="raw file bodies must remain local"):
        local_evidence_edges_from_capture(
            [
                {
                    "evidence_type": "session_file",
                    "session_id_hash": _h("session-a"),
                    "device_id_hash": _h("device-a"),
                    "relative_path_hash": _h("file-a"),
                    "content_hash": _h("content-a"),
                    "sync_policy": "metadata_only",
                    "raw_body": "private source body",
                }
            ]
        )


def test_local_evidence_capture_projects_edges_without_raw_bodies():
    session_id = _h("session-a")
    relative_path = _h("worker/lib/agent_knowledge/context.py")
    records = [
        {
            "evidence_type": "session_file",
            "session_id_hash": session_id,
            "device_id_hash": _h("device-a"),
            "relative_path_hash": relative_path,
            "content_hash": _h("file-content"),
            "sync_policy": "metadata_only",
        }
    ]

    episodes = local_evidence_episodes_from_capture(records, brain_id="/project/neurons")
    graph = FakeGraphMemoryAdapter()
    report = GraphProjectionWorker(graph).project_episodes(list(episodes))
    result = graph.search_context(
        brain_id="/project/neurons",
        query="session file evidence",
        entity_types=["LocalEvidenceEdge"],
        limit=10,
    )

    assert report.status == "succeeded"
    assert len(result.episodes) == 1
    [episode] = result.episodes
    assert episode.entity_type == "LocalEvidenceEdge"
    assert episode.payload["edge_type"] == "SessionFile"
    assert episode.payload["source_ref"] == f"session:{session_id}"
    assert episode.payload["target_ref"] == f"file:{relative_path}"
    assert episode.payload["raw_body_included"] is False
    assert "raw_body" not in episode.payload


def _h(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

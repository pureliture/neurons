import json

from agent_knowledge.llm_brain_core import BrainReadService, FakeGraphMemoryAdapter
from agent_knowledge.llm_brain_core.models import OntologyEpisode


def test_contextpack_strips_raw_source_locator_fields_from_cards_and_request():
    service = BrainReadService(
        memory_cards=[
            {
                "memory_id": "mem_task_raw_ref",
                "card_type": "task",
                "project": "neurons",
                "title": "Continue source policy work",
                "summary": "Continue source policy work from redacted evidence.",
                "lifecycle_state": "accepted",
                "approval_state": "approved",
                "currentness": "current",
                "confidence": 0.8,
                "source_refs": [
                    {
                        "source_ref_id": "src_safe",
                        "content_hash": _h("content"),
                        "device_id_hash": _h("device"),
                        "path": "/Users/example/Projects/neurons/private.txt",
                        "uri": "file:///Users/example/Projects/neurons/private.txt",
                        "content": "raw file body must not leak",
                    }
                ],
                "typed_payload": {
                    "task_state": "Continue source policy work",
                    "next_action": "Prove source refs stay opaque",
                    "blocker": "",
                    "owner_hint": "neurons",
                    "status": "open",
                },
            }
        ],
        graph_adapter=FakeGraphMemoryAdapter(
            [
                OntologyEpisode.from_payload(
                    event_id="evt_file",
                    entity_type="File",
                    natural_id="file:src_safe",
                    payload={"summary": "redacted file summary"},
                    source_ref_ids=["src_safe"],
                )
            ]
        ),
    )

    pack = service.brain_context_resolve(
        repository="/Users/example/Projects/neurons",
        branch="codex/no-raw-source",
        current_files=["/Users/example/Projects/neurons/private.txt"],
        current_request="source policy",
        project="neurons",
    ).to_dict()
    serialized = json.dumps(pack, sort_keys=True)

    assert pack["source_refs"] == [
        {
            "source_ref_id": "src_safe",
            "content_hash": _h("content"),
            "device_id_hash": _h("device"),
        }
    ]
    assert "/Users/" not in serialized
    assert "file://" not in serialized
    assert "private.txt" not in serialized
    assert "raw file body" not in serialized
    assert '"path"' not in serialized
    assert '"uri"' not in serialized
    assert '"content"' not in serialized


def _h(value):
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()

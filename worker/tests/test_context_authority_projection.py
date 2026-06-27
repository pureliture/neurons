from agent_knowledge.llm_brain_core import BrainReadService, FakeGraphMemoryAdapter, GraphProjectionWorker
from agent_knowledge.llm_brain_core.authority_projection import authority_episodes_from_context_pack

from test_context_authority_pack import _card


def test_context_authority_pack_projects_workbench_nodes_to_graph_adapter():
    service = BrainReadService(
        memory_cards=[
            _card(
                "mem_design",
                "decision",
                "Approved Context Authority design",
                {
                    "decision": "Use neurons brain APIs as the default agent-facing surface.",
                    "authority_ref": "specs/context-authority-roadmap/design.md",
                },
            ),
            _card(
                "mem_workflow",
                "workflow_contract",
                "Use dedicated worktrees before edits",
                {"rule": "Use a dedicated branch/worktree before repository edits."},
            ),
            _card(
                "mem_pref",
                "preference",
                "Korean response preference",
                {"preference": "자연어 응답과 문서는 한국어로 작성한다."},
            ),
        ],
    )
    pack = service.brain_context_resolve(
        repository="neurons",
        branch="codex/context-authority-roadmap",
        current_files=["specs/context-authority-roadmap/design.md"],
        current_request="project authority graph",
        project="neurons",
    )

    episodes = authority_episodes_from_context_pack(pack)
    graph = FakeGraphMemoryAdapter()
    report = GraphProjectionWorker(graph).project_episodes(list(episodes))
    graph_result = graph.search_context(
        brain_id="/project/neurons",
        query="Context Authority design worktree Korean",
        entity_types=["Document", "WorkflowContract", "PreferenceRule", "EvidenceGap"],
        limit=10,
    )

    assert report.status == "succeeded"
    assert report.projected >= 4
    assert {episode.entity_type for episode in graph_result.episodes} >= {
        "Document",
        "WorkflowContract",
        "PreferenceRule",
        "EvidenceGap",
    }
    for episode in graph_result.episodes:
        assert episode.payload["projection_version"] == "context_authority_projection.v1"
        assert episode.payload["source_card_id"]
    document = next(episode for episode in graph_result.episodes if episode.entity_type == "Document")
    assert document.payload["status"] == "source_of_truth"
    assert document.payload["authority"] == "derived_authority_graph"
    assert document.payload["projection_version"] == "context_authority_projection.v1"
    assert document.payload["source_card_id"] == "mem_design"
    assert document.payload["evidence_edges"][0]["evidence_type"] == "memory_card"
    workflow = next(episode for episode in graph_result.episodes if episode.entity_type == "WorkflowContract")
    assert workflow.payload["evidence_refs"] == ["mem_workflow"]
    assert workflow.payload["projection_version"] == "context_authority_projection.v1"
    assert workflow.payload["source_card_id"] == "mem_workflow"
    assert workflow.payload["auto_update_allowed"] is False
    preference = next(episode for episode in graph_result.episodes if episode.entity_type == "PreferenceRule")
    assert preference.payload["source_card_id"] == "mem_pref"
    gap = next(episode for episode in graph_result.episodes if episode.entity_type == "EvidenceGap")
    assert gap.payload["source_card_id"] == "gap:graph_unavailable"

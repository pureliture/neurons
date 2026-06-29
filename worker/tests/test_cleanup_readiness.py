from agent_knowledge.session_memory.cleanup_readiness import (
    CleanupReadinessConfig,
    CleanupReadinessRunner,
)


class FakeRetiredIndexBridge:
    def __init__(self, docs_by_dataset_keyword):
        self.docs_by_dataset_keyword = docs_by_dataset_keyword

    def list_datasets(self, *, name="", **_kwargs):
        return [{"name": name, "id": f"ds_{name}"}]

    def list_documents(self, dataset_id, *, page=1, page_size=100, keywords=""):
        _ = page, page_size
        return list(self.docs_by_dataset_keyword.get((dataset_id, keywords), []))


def _doc(*, run="DONE", project="neurons", agent_id="codex-transcript-capture", provider="codex"):
    return {
        "run": run,
        "meta_fields": {
            "project": project,
            "agent_id": agent_id,
            "provider": provider,
        },
    }


def _config():
    return CleanupReadinessConfig(index_url="http://retired_index_bridge", projects=("neurons", "dendrite"))


def test_cleanup_readiness_blocks_until_corrected_coverage_is_done():
    docs = {
        ("ds_transcript-memory", "neurons"): [
            _doc(
                run="RUNNING",
                project="neurons",
                agent_id="antigravity-transcript-capture",
                provider="antigravity",
            )
        ],
        ("ds_transcript-memory", "dendrite"): [
            _doc(
                run="DONE",
                project="dendrite",
                agent_id="antigravity-transcript-capture",
                provider="antigravity",
            )
        ],
        ("ds_transcript-memory", "workspace-index-advisor"): [
            _doc(project="workspace-index-advisor", agent_id="index-advisor")
        ],
        ("ds_session-memory", "neurons"): [
            _doc(project="neurons", agent_id="index-advisor", provider="antigravity")
        ],
        ("ds_session-memory", "workspace-index-advisor"): [
            _doc(project="workspace-index-advisor", agent_id="index-advisor")
        ],
    }

    report = CleanupReadinessRunner(config=_config(), retired_index_bridge=FakeRetiredIndexBridge(docs)).run()

    assert report["status"] == "blocked"
    assert report["mutation_performed"] is False
    assert report["raw_ids_printed"] is False
    assert report["raw_content_printed"] is False
    assert "corrected_transcript_memory_done_coverage_missing" in report["gates"]["blockers"]
    assert "corrected_session_memory_done_coverage_missing" in report["gates"]["blockers"]
    assert "corrected_transcript_memory_has_non_done_runs" in report["gates"]["blockers"]


def test_cleanup_readiness_ready_requires_pollution_and_corrected_docs():
    docs = {
        ("ds_transcript-memory", "neurons"): [
            _doc(project="neurons", agent_id="codex-transcript-capture")
        ],
        ("ds_transcript-memory", "dendrite"): [
            _doc(
                project="dendrite",
                agent_id="antigravity-transcript-capture",
                provider="antigravity",
            )
        ],
        ("ds_transcript-memory", "workspace-index-advisor"): [
            _doc(project="workspace-index-advisor", agent_id="index-advisor")
        ],
        ("ds_session-memory", "neurons"): [
            _doc(project="neurons", agent_id="codex-memory-regeneration")
        ],
        ("ds_session-memory", "dendrite"): [
            _doc(
                project="dendrite",
                agent_id="antigravity-memory-regeneration",
                provider="antigravity",
            )
        ],
        ("ds_session-memory", "workspace-index-advisor"): [
            _doc(project="workspace-index-advisor", agent_id="index-advisor")
        ],
    }

    report = CleanupReadinessRunner(config=_config(), retired_index_bridge=FakeRetiredIndexBridge(docs)).run()

    assert report["status"] == "ready_for_disable_candidate_refresh"
    assert report["gates"]["ready"] is True
    assert report["gates"]["disable_delete_allowed"] is False
    assert report["preflight_requirements"]["destructive_mutation"] == "blocked_until_operator_approval"


def test_cleanup_readiness_detects_legacy_agent_inside_correct_project():
    docs = {
        ("ds_transcript-memory", "neurons"): [
            _doc(project="neurons", agent_id="codex-transcript-capture"),
            _doc(project="neurons", agent_id="index-advisor", provider="codex"),
        ],
        ("ds_transcript-memory", "dendrite"): [
            _doc(
                project="dendrite",
                agent_id="antigravity-transcript-capture",
                provider="antigravity",
            )
        ],
        ("ds_session-memory", "neurons"): [
            _doc(project="neurons", agent_id="codex-memory-regeneration")
        ],
        ("ds_session-memory", "dendrite"): [
            _doc(
                project="dendrite",
                agent_id="antigravity-memory-regeneration",
                provider="antigravity",
            )
        ],
    }

    report = CleanupReadinessRunner(config=_config(), retired_index_bridge=FakeRetiredIndexBridge(docs)).run()

    assert report["status"] == "ready_for_disable_candidate_refresh"
    assert report["gates"]["legacy_pollution_present"] is True

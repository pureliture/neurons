from __future__ import annotations

import subprocess
import sys

from agent_knowledge import cli


def test_importing_neuron_cli_does_not_load_llm_brain_runtime():
    code = (
        "import sys; import agent_knowledge.cli; "
        "loaded = sorted(name for name in sys.modules if "
        "name.startswith(("
        "'agent_knowledge.llm_brain_core', "
        "'agent_knowledge.mcp_server', "
        "'agent_knowledge.couchdb_source.migration_flow_cli', "
        "'agent_knowledge.rag_ingress.projection_invalidation_canary', "
        "'agent_knowledge.rag_ingress.temporal_revision_rebuild', "
        "'graphiti_core', 'neo4j'"
        "))); "
        "assert not loaded, loaded"
    )

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_lazy_command_handlers_import_their_targets_only_when_dispatched(monkeypatch):
    expected_targets = {
        "brain-context-resolve": (".llm_brain_core.cli", "main"),
        "object-query": (".llm_brain_core.objects.object_cli", "object_query_main"),
        "agent-context-startup": (".llm_brain_core.objects.object_cli", "agent_context_startup_main"),
        "artifact-preference-evaluate": (".llm_brain_core.objects.object_cli", "artifact_preference_evaluate_main"),
        "object-explain": (".llm_brain_core.objects.object_cli", "object_explain_main"),
        "corpus-status": (".llm_brain_core.objects.object_cli", "corpus_status_main"),
        "corpus-ingest-plan": (".llm_brain_core.objects.object_cli", "corpus_ingest_plan_main"),
        "corpus-ingest": (".llm_brain_core.objects.object_cli", "corpus_ingest_main"),
        "corpus-ingest-readiness": (".llm_brain_core.objects.object_cli", "corpus_ingest_readiness_main"),
        "object-authority-schema-ensure": (".llm_brain_core.objects.object_cli", "object_authority_schema_ensure_main"),
        "source-to-candidate-graph": (".llm_brain_core.objects.object_cli", "source_to_candidate_graph_main"),
        "candidate-review-edit": (".llm_brain_core.objects.object_cli", "candidate_review_edit_main"),
        "approval-board-decide": (".llm_brain_core.objects.object_cli", "approval_board_decide_main"),
        "golden-query-eval": (".llm_brain_core.objects.object_cli", "golden_query_eval_main"),
        "source-to-candidate-runtime-readiness": (".llm_brain_core.objects.object_cli", "source_to_candidate_runtime_readiness_main"),
        "okf-export": (".llm_brain_core.objects.object_cli", "okf_export_main"),
        "brain-regression-gate": (".llm_brain_core.regression_gate_cli", "main"),
        "brain-export": (".llm_brain_core.portable_cli", "export_main"),
        "brain-import": (".llm_brain_core.portable_cli", "import_main"),
        "brain-project": (".llm_brain_core.projection_cli", "main"),
        "couchdb-migration-flow": (".couchdb_source.migration_flow_cli", "main"),
        "couchdb-graph-trigger": (".llm_brain_core.graph_trigger_cli", "main"),
        "couchdb-graph-project": (".llm_brain_core.couchdb_projection_cli", "main"),
        "couchdb-graph-bulk-semantic": (".llm_brain_core.bulk_semantic_cli", "main"),
        "couchdb-bulk-semantic-trigger": (".llm_brain_core.bulk_semantic_trigger_cli", "main"),
        "couchdb-graph-status": (".llm_brain_core.graph_projection_status_cli", "main"),
        "couchdb-projection-invalidation-canary": (".rag_ingress.projection_invalidation_canary", "main"),
        "couchdb-temporal-revision-rebuild": (".rag_ingress.temporal_revision_rebuild", "main"),
    }
    argv = ["--sentinel"]
    imported: list[tuple[str, str | None, str, list[str]]] = []

    def fake_import(module_name: str, package: str | None = None):
        class FakeModule:
            def __getattr__(self, attribute: str):
                def handler(handler_argv: list[str]) -> int:
                    imported.append((module_name, package, attribute, handler_argv))
                    return 7

                return handler

        return FakeModule()

    monkeypatch.setattr(cli, "import_module", fake_import)

    for command in expected_targets:
        assert cli.COMMAND_HANDLERS[command](argv) == 7

    assert imported == [
        (module_name, "agent_knowledge", attribute, argv)
        for module_name, attribute in expected_targets.values()
    ]

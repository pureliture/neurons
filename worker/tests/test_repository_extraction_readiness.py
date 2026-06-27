from agent_knowledge.repository import (
    build_repository_extraction_plan,
    repository_candidate_method_matrix,
)


def test_m2_repository_extraction_plan_selects_memory_curation_candidate():
    plan = build_repository_extraction_plan()

    assert plan["schema_version"] == "agent_knowledge_repository_extraction_plan.v1"
    assert plan["milestone"] == "M2"
    assert plan["first_candidate"]["name"] == "memory_curation"
    assert plan["first_candidate"]["activation_state"] == "readiness_only"
    assert plan["first_candidate"]["public_import_contract"] is False
    assert plan["first_candidate"]["protocol_definition_stable"] is False
    assert plan["first_candidate"]["tables"] == [
        "memory_candidates",
        "memory_cards",
        "memory_card_evidence",
        "profile_facts",
    ]
    assert plan["caller_migration_order"][0] == {
        "caller": "CurationService.approve",
        "reason": "multi_write_transaction_target",
        "rollback_guard": "Ledger._transaction",
    }
    assert plan["public_compatibility_gate"]["public_api_break_allowed"] is False
    assert "tests/test_curation.py" in plan["public_compatibility_gate"]["fixtures"]
    assert "tests/test_ledger_transaction.py" in plan["rollback_guard"]["fixtures"]
    assert any("public API break" in item for item in plan["abort_criteria"])


def test_m2_repository_method_matrix_covers_curation_tables_without_mass_migration():
    matrix = repository_candidate_method_matrix()
    table_methods = {(row["table"], row["method"]) for row in matrix}

    assert ("memory_candidates", "upsert_memory_candidate") in table_methods
    assert ("memory_candidates", "update_memory_candidate_state") in table_methods
    assert ("memory_cards", "upsert_memory_card") in table_methods
    assert ("memory_card_evidence", "add_memory_card_evidence") in table_methods
    assert ("profile_facts", "upsert_profile_fact") in table_methods
    assert all(row["current_owner"].startswith("Ledger") for row in matrix)
    assert all(row["migration_action"] == "candidate_port_only" for row in matrix)

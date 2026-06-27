import copy
import json

import pytest

from agent_knowledge.dataset_contract import (
    CANONICAL_RAGFLOW_DATASET_NAMES,
    CURRENT_RUNTIME_DATASET_NAMES,
    DATASET_CONTRACT_VERSION,
    DEPRECATED_RAGFLOW_DATASET_PREFIXES,
    build_default_dataset_contract_config,
    clear_dataset_contract_config_cache,
    dataset_contract_config_schema,
    load_dataset_contract_config_once,
    validate_dataset_contract_config,
)


def test_m3_default_config_fixture_matches_current_python_constants(tmp_path):
    schema = dataset_contract_config_schema()
    config = build_default_dataset_contract_config()

    assert "logical_roles" in schema["required_fields"]
    assert "target_profile" in schema["logical_role_fields"]
    assert schema["external_activation"]["k3s_apply_in_scope"] is False
    assert schema["external_activation"]["live_mutation_allowed"] is False
    assert config["schema_version"] == "agent_knowledge_dataset_contract_config.v1"
    assert config["contract_version"] == DATASET_CONTRACT_VERSION
    assert config["current_runtime_dataset_names"] == dict(CURRENT_RUNTIME_DATASET_NAMES)
    assert config["canonical_ragflow_dataset_names"] == dict(CANONICAL_RAGFLOW_DATASET_NAMES)
    assert config["deprecated_ragflow_dataset_prefixes"] == list(DEPRECATED_RAGFLOW_DATASET_PREFIXES)
    assert config["load_policy"] == {
        "mode": "process_start_once",
        "hot_reload": False,
        "default_fallback": "code_defined",
    }

    role_names = {role["role"] for role in config["logical_roles"]}
    assert {"session_memory", "project_memory", "approved_memory_card"} <= role_names
    assert config["orchestration_rollout"]["code_default_fallback_ready"] is True
    assert config["orchestration_rollout"]["configmap_shape_ready"] is True
    assert config["orchestration_rollout"]["compose_runtime_wiring_ready"] is False
    assert config["orchestration_rollout"]["k3s_apply_in_scope"] is False
    assert config["orchestration_rollout"]["config_path_env_var"] == ""

    fixture = tmp_path / "dataset-contract.json"
    fixture.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    clear_dataset_contract_config_cache()
    assert load_dataset_contract_config_once(fixture) == config


def test_m3_dataset_contract_config_is_loaded_once_without_hot_reload(tmp_path):
    config = build_default_dataset_contract_config()
    fixture = tmp_path / "dataset-contract.json"
    fixture.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")

    clear_dataset_contract_config_cache()
    first = load_dataset_contract_config_once(fixture)

    changed = copy.deepcopy(config)
    changed["deprecated_ragflow_dataset_prefixes"].append("changed-after-startup-")
    fixture.write_text(json.dumps(changed, sort_keys=True), encoding="utf-8")

    assert load_dataset_contract_config_once(fixture) == first
    assert load_dataset_contract_config_once(fixture) == first
    with pytest.raises(ValueError, match="already loaded from a different source"):
        load_dataset_contract_config_once(None)
    clear_dataset_contract_config_cache()


def test_m3_startup_validation_rejects_incomplete_or_secret_shaped_config():
    config = build_default_dataset_contract_config()
    missing_role = copy.deepcopy(config)
    missing_role["logical_roles"] = [
        role for role in missing_role["logical_roles"] if role["role"] != "session_memory"
    ]

    with pytest.raises(ValueError, match="missing logical dataset role: session_memory"):
        validate_dataset_contract_config(missing_role)

    forbidden = copy.deepcopy(config)
    forbidden["logical_roles"][0]["dataset_id"] = "should-not-be-accepted"

    with pytest.raises(ValueError, match="forbidden dataset contract field: dataset_id"):
        validate_dataset_contract_config(forbidden)

    unknown_top_level = copy.deepcopy(config)
    unknown_top_level["runtime_secret"] = "should-not-be-accepted"

    with pytest.raises(ValueError, match="forbidden dataset contract field: runtime_secret"):
        validate_dataset_contract_config(unknown_top_level)

    extra_top_level = copy.deepcopy(config)
    extra_top_level["extra_runtime_metadata"] = {}

    with pytest.raises(ValueError, match="unknown dataset contract config field: extra_runtime_metadata"):
        validate_dataset_contract_config(extra_top_level)

    uncovered_runtime = copy.deepcopy(config)
    uncovered_runtime["current_runtime_dataset_names"]["session_memory"] = "private-runtime-name"

    with pytest.raises(ValueError, match="logical roles: session_memory") as exc_info:
        validate_dataset_contract_config(uncovered_runtime)

    assert "private-runtime-name" not in str(exc_info.value)

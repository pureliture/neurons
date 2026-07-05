from pathlib import Path

import yaml

from agent_knowledge.rag_ingress.shadow_worker import env_profile_dataset_resolver

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_env_profile_dataset_resolver_uses_retired_index_bridge_session_dataset_env_key():
    env = {"RETIRED_INDEX_BRIDGE_SESSION_MEMORY_DATASET_ID": "ds_session"}

    resolve = env_profile_dataset_resolver(env.get)
    assert resolve("index-session-memory") == "ds_session"


def test_env_profile_dataset_resolver_covers_application_profiles_and_compose_env():
    profiles = _target_profile_contract()
    expected_keys = {
        profile: entry["retiredIndexBridgeDatasetEnv"]
        for profile, entry in profiles.items()
    }
    env = {
        key: f"ds_{profile.removeprefix('index-').replace('-', '_')}"
        for profile, key in expected_keys.items()
    }

    resolve = env_profile_dataset_resolver(env.get)

    assert list(profiles) == [
        "index-transcript-memory",
        "index-session-memory",
        "index-session-summary",
        "index-project-memory",
        "index-task-summary",
        "index-approved-memory-card",
        "index-procedural-memory",
    ]
    for profile, key in expected_keys.items():
        assert resolve(profile) == env[key]

    compose = (REPO_ROOT / "compose.yaml").read_text()
    env_example = (REPO_ROOT / ".env.example").read_text()
    application_profiles = _application_target_profiles()
    for key in expected_keys.values():
        assert key in compose
        assert key in env_example
    for profile, entry in profiles.items():
        assert application_profiles[profile]["dataset-role"] == entry["datasetRole"]
        assert application_profiles[profile]["adapter"] == "retired_index_bridge"


def _target_profile_contract() -> dict[str, object]:
    contract = yaml.safe_load((REPO_ROOT / "docs/contracts/target-profiles.yaml").read_text())
    assert contract["schemaVersion"] == "neurons.target_profiles.v1"
    return contract["profiles"]


def _application_target_profiles() -> dict[str, object]:
    application = yaml.safe_load((REPO_ROOT / "src/main/resources/application.yml").read_text())
    return application["rag-ingress"]["target-profiles"]

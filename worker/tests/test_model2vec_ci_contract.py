"""Offline workflow contract for the existing Model2Vec M1-M3 test lanes."""

from pathlib import Path

import yaml


WORKFLOW = Path(__file__).parents[2] / ".github/workflows/test.yml"
OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "PYTHON_DOTENV_DISABLED": "1",
}
M2_MODULES = (
    "tests/test_local_embedding_m2_pg.py",
    "tests/test_local_embedding_m2_graphiti.py",
    "tests/test_local_embedding_journal_coordinator_pg_contract.py",
    "tests/test_local_embedding_couchdb_receipt_e2e.py",
)


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _run_text(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def _model_preparation_step(job):
    step = next(
        step
        for step in job["steps"]
        if "neuron-local-embedding install" in step.get("run", "")
    )
    assert step["env"]["NEURON_LOCAL_EMBEDDING_MODEL_DIR"] == (
        "${{ runner.temp }}/model2vec-pinned"
    )
    assert (
        'uv run --frozen --group dev --extra local-embedding '
        'neuron-local-embedding install --model-dir '
        '"$NEURON_LOCAL_EMBEDDING_MODEL_DIR" --download'
    ) in step["run"]
    assert (
        'uv run --frozen --group dev --extra local-embedding '
        'neuron-local-embedding verify --model-dir '
        '"$NEURON_LOCAL_EMBEDDING_MODEL_DIR"'
    ) in step["run"]
    return step


def _assert_offline(step):
    env = step.get("env", {})
    assert all(env.get(key) == value for key, value in OFFLINE_ENV.items())
    assert "OPENAI_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env


def test_worker_pytest_prepares_model_and_uses_cli_smoke_instead_of_duplicate_tests():
    job = _workflow()["jobs"]["worker-pytest"]
    _model_preparation_step(job)
    test_step = next(
        step for step in job["steps"] if "pytest -q" in step.get("run", "")
    )
    _assert_offline(test_step)
    commands = _run_text(job)
    assert "tests/test_local_embedding_m1.py" not in commands
    assert "tests/test_local_embedding_m3_eval.py" not in commands
    smoke = next(
        step
        for step in job["steps"]
        if "CLI verification smoke" in step.get("name", "")
    )
    assert "neuron-local-embedding verify" in smoke["run"]
    _assert_offline(smoke)


def test_pg_neo4j_lane_selects_existing_m2_modules_with_model_and_offline_env():
    workflow = _workflow()
    job = workflow["jobs"]["worker-lbrain-pg-neo4j"]
    _model_preparation_step(job)
    assert {"postgres", "neo4j", "couchdb"} <= set(job["services"])
    assert job["env"]["LLM_BRAIN_GRAPH_EXTRACT_ENTITIES"] == "false"
    assert job["env"]["NEURON_REQUIRE_MODEL2VEC_INTEGRATION"] == "1"
    assert job["env"]["LBRAIN_TEST_COUCHDB_URL"] == "http://127.0.0.1:5984"
    assert job["env"]["LBRAIN_TEST_COUCHDB_USER"]
    assert job["env"]["LBRAIN_TEST_COUCHDB_PASSWORD"]
    couchdb = job["services"]["couchdb"]
    assert couchdb["image"] == (
        "couchdb:3.4@sha256:628c4952c1cf668489fb668fbd24013bf61ff25add0a3643253dde0d47cae449"
    )
    assert "5984:5984" in couchdb["ports"]

    guard = next(
        step
        for step in job["steps"]
        if "Reject missing disposable database endpoints" in step.get("name", "")
    )
    for endpoint in (
        "LBRAIN_TEST_PG_DSN",
        "LBRAIN_TEST_NEO4J_URI",
        "LBRAIN_TEST_COUCHDB_URL",
        "LBRAIN_TEST_COUCHDB_USER",
        "LBRAIN_TEST_COUCHDB_PASSWORD",
    ):
        assert endpoint in guard["run"]

    integration = next(
        step
        for step in job["steps"]
        if "Model2Vec M2 integration" in step.get("name", "")
    )
    _assert_offline(integration)
    commands = integration["run"]
    for module in M2_MODULES:
        assert module in commands
    assert "tests/test_pg_neo4j_card_e2e.py" in commands
    assert "--junitxml" in commands
    assert "xml.etree.ElementTree" in commands
    assert ".//testcase/skipped" in commands

    pg_only = _run_text(workflow["jobs"]["worker-lbrain-pgvector"])
    assert "tests/test_local_embedding_m2_pg.py" not in pg_only


def test_workflow_defers_required_check_authority_to_live_branch_protection():
    header = WORKFLOW.read_text(encoding="utf-8").split("jobs:", 1)[0]
    assert "branch protection" in header.lower()
    assert "read the live branch-protection settings" in header.lower()
    assert "advisory" not in header.lower()

from __future__ import annotations

import re
from pathlib import Path

import pytest


WORKER_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = WORKER_ROOT.parent
LOCAL_SOURCE_SENTINEL = "0" * 40
COHORT_DOCKERFILES = (
    ("ingress-worker", "Dockerfile"),
    ("graph-trigger", "Dockerfile"),
    ("bulk-semantic-trigger", "Dockerfile"),
    ("session-memory-worker", "Dockerfile.session-memory"),
    ("llm-brain-tools", "Dockerfile.tools"),
    ("mcp-http", "Dockerfile.mcp-http"),
)
LOCAL_COMPOSE_SERVICES = {
    "ingress-worker": "ingress-worker-py",
    "graph-trigger": "llm-brain-graph-trigger",
    "bulk-semantic-trigger": "llm-brain-bulk-semantic-trigger",
    "session-memory-worker": "session-memory-worker",
    "llm-brain-tools": "llm-brain-tools",
    "mcp-http": "neuron-knowledge-mcp",
}


@pytest.mark.parametrize(("component", "dockerfile_name"), COHORT_DOCKERFILES)
def test_production_runtime_cohort_images_fail_closed_on_source_identity(
    component,
    dockerfile_name,
):
    dockerfile = (WORKER_ROOT / dockerfile_name).read_text(encoding="utf-8")
    validation = "grep -Eq '^[0-9a-f]{40}$'"
    revision_label = "org.opencontainers.image.revision=${NEURONS_SOURCE_COMMIT}"

    assert component
    assert "ARG NEURONS_SOURCE_COMMIT" in dockerfile
    assert validation in dockerfile
    assert revision_label in dockerfile
    assert (
        'org.opencontainers.image.source="https://github.com/pureliture/neurons"'
        in dockerfile
    )
    assert dockerfile.index(validation) < dockerfile.index(revision_label)


@pytest.mark.parametrize(("component", "dockerfile_name"), COHORT_DOCKERFILES)
def test_existing_local_compose_builds_get_an_explicit_non_deployable_identity(
    component,
    dockerfile_name,
):
    del dockerfile_name
    root_compose = (REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8")
    session_compose = (
        WORKER_ROOT / "deploy" / "session-memory" / "compose.yaml"
    ).read_text(encoding="utf-8")
    service = LOCAL_COMPOSE_SERVICES[component]
    source = session_compose if component == "session-memory-worker" else root_compose
    block = re.search(
        rf"(?ms)^  {re.escape(service)}:\n(?P<body>.*?)(?=^  [a-z0-9][a-z0-9-]*:\n|\Z)",
        source,
    )

    assert block is not None
    assert (
        "NEURONS_SOURCE_COMMIT: "
        "${NEURONS_SOURCE_COMMIT:-" + LOCAL_SOURCE_SENTINEL + "}"
    ) in block.group("body")

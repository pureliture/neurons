from __future__ import annotations

from pathlib import Path

import pytest


WORKER_ROOT = Path(__file__).parents[1]
COHORT_DOCKERFILES = (
    ("ingress-worker", "Dockerfile"),
    ("graph-trigger", "Dockerfile"),
    ("bulk-semantic-trigger", "Dockerfile"),
    ("session-memory-worker", "Dockerfile.session-memory"),
    ("llm-brain-tools", "Dockerfile.tools"),
    ("mcp-http", "Dockerfile.mcp-http"),
)


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

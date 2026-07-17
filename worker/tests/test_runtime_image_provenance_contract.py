from __future__ import annotations

from pathlib import Path

import pytest
import yaml


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


def _dockerfile_instructions(source: str) -> list[tuple[str, str]]:
    """Parse non-comment Dockerfile instructions, including continuations."""

    logical_lines: list[str] = []
    pending = ""
    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        instruction, _, value = pending.partition(" ")
        logical_lines.append((instruction.upper(), value.strip()))
        pending = ""
    if pending:
        instruction, _, value = pending.partition(" ")
        logical_lines.append((instruction.upper(), value.strip()))
    return logical_lines


def _compose_services(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    services = document.get("services")
    assert isinstance(services, dict)
    return services


def _identity_instruction_indices(
    instructions: list[tuple[str, str]],
) -> tuple[int, int] | None:
    validation = "grep -Eq '^[0-9a-f]{40}$'"
    revision_label = "org.opencontainers.image.revision=${NEURONS_SOURCE_COMMIT}"
    validation_index = next(
        (
            index
            for index, (instruction, value) in enumerate(instructions)
            if instruction == "RUN" and validation in value
        ),
        None,
    )
    revision_index = next(
        (
            index
            for index, (instruction, value) in enumerate(instructions)
            if instruction == "LABEL" and revision_label in value
        ),
        None,
    )
    if (
        ("ARG", "NEURONS_SOURCE_COMMIT") not in instructions
        or validation_index is None
        or revision_index is None
        or not any(
            instruction == "LABEL"
            and 'org.opencontainers.image.source="https://github.com/pureliture/neurons"'
            in value
            for instruction, value in instructions
        )
    ):
        return None
    return validation_index, revision_index


@pytest.mark.parametrize(("component", "dockerfile_name"), COHORT_DOCKERFILES)
def test_production_runtime_cohort_images_fail_closed_on_source_identity(
    component,
    dockerfile_name,
):
    instructions = _dockerfile_instructions(
        (WORKER_ROOT / dockerfile_name).read_text(encoding="utf-8")
    )
    assert component
    identity_indices = _identity_instruction_indices(instructions)
    assert identity_indices is not None
    validation_index, revision_index = identity_indices
    assert validation_index < revision_index


def test_dockerfile_parser_rejects_comment_only_identity_contract() -> None:
    instructions = _dockerfile_instructions(
        "# ARG NEURONS_SOURCE_COMMIT\n"
        "# RUN printf '%s' \"${NEURONS_SOURCE_COMMIT}\" | grep -Eq '^[0-9a-f]{40}$'\n"
        "# LABEL org.opencontainers.image.revision=${NEURONS_SOURCE_COMMIT}\n"
    )

    assert _identity_instruction_indices(instructions) is None


@pytest.mark.parametrize(("component", "dockerfile_name"), COHORT_DOCKERFILES)
def test_existing_local_compose_builds_get_an_explicit_non_deployable_identity(
    component,
    dockerfile_name,
):
    del dockerfile_name
    root_compose = _compose_services(REPOSITORY_ROOT / "compose.yaml")
    session_compose = _compose_services(
        WORKER_ROOT / "deploy" / "session-memory" / "compose.yaml"
    )
    service = LOCAL_COMPOSE_SERVICES[component]
    source = session_compose if component == "session-memory-worker" else root_compose
    service_config = source.get(service)
    assert isinstance(service_config, dict)
    build = service_config.get("build")
    assert isinstance(build, dict)
    args = build.get("args")
    assert isinstance(args, dict)
    assert args.get("NEURONS_SOURCE_COMMIT") == (
        "${NEURONS_SOURCE_COMMIT:-" + LOCAL_SOURCE_SENTINEL + "}"
    )


def test_ingress_worker_compose_revision_label_matches_build_source_identity() -> None:
    services = _compose_services(REPOSITORY_ROOT / "compose.yaml")
    ingress_worker = services.get("ingress-worker-py")
    assert isinstance(ingress_worker, dict)
    build = ingress_worker.get("build")
    assert isinstance(build, dict)
    args = build.get("args")
    assert isinstance(args, dict)
    labels = ingress_worker.get("labels")
    assert isinstance(labels, dict)

    assert labels.get("org.opencontainers.image.revision") == args.get(
        "NEURONS_SOURCE_COMMIT"
    )

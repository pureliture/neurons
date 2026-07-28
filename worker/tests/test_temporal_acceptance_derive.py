from __future__ import annotations

from pathlib import Path

import pytest

from agent_knowledge.llm_brain_core.artifact_store import InMemorySessionMemoryArtifactStore
from agent_knowledge.llm_brain_core.models import SessionMemoryArtifact
from agent_knowledge.llm_brain_core.objects.temporal_acceptance_derive import (
    derive_temporal_acceptance_baseline,
)


def _selection() -> dict[str, object]:
    return {
        "schema_version": "temporal_acceptance_selection.v3",
        "policy": "latest_relevant_bounded_artifact_revision_v1",
        "temporal_query": "migration",
        "date_a": {"as_of": "2026-07-09T10:30:00Z"},
        "date_b": {"as_of": "2026-07-15T10:30:00Z"},
        "range_boundary": {
            "date_from": "2026-07-09T00:00:00Z",
            "date_to": "2026-07-09T23:59:59Z",
        },
    }


def _artifact(*, suffix: str, start: str, end: str) -> SessionMemoryArtifact:
    return SessionMemoryArtifact.from_summary(
        session_id_hash="sha256:" + suffix * 64,
        project="neurons",
        provider="codex",
        summary="migration",
        source_event_ids=(f"event-{suffix}",),
        source_revision="sha256:" + suffix * 64,
        observed_at_start=start,
        observed_at_end=end,
        revision_observed_at_start=start,
        revision_observed_at_end=end,
        revision_observed_intervals=((start, end),),
        revision_temporal_evidence="bounded",
        materialized_at=end,
        materialization_revision=1,
    )


def test_derivation_rejects_retired_or_identifier_bearing_selection() -> None:
    selection = _selection()
    selection["schema_version"] = "temporal_acceptance_selection.v2"
    with pytest.raises(ValueError, match="schema is invalid"):
        derive_temporal_acceptance_baseline(
            artifact_store=InMemorySessionMemoryArtifactStore(),
            project="neurons",
            selection=selection,
            limit=3,
            max_runtime_seconds=5,
        )

    selection = _selection()
    selection["date_a"] = {"as_of": "2026-07-09T10:30:00Z", "source_id": "raw-id"}
    with pytest.raises(ValueError, match="raw source identifiers"):
        derive_temporal_acceptance_baseline(
            artifact_store=InMemorySessionMemoryArtifactStore(),
            project="neurons",
            selection=selection,
            limit=3,
            max_runtime_seconds=5,
        )


def test_derivation_fails_closed_when_revision_inventory_is_incomplete() -> None:
    store = InMemorySessionMemoryArtifactStore(
        [
            _artifact(
                suffix="a",
                start="2026-07-09T10:00:00Z",
                end="2026-07-09T11:00:00Z",
            ),
            _artifact(
                suffix="c",
                start="2026-07-09T10:00:00Z",
                end="2026-07-09T11:00:00Z",
            ),
            _artifact(
                suffix="d",
                start="2026-07-09T10:00:00Z",
                end="2026-07-09T11:00:00Z",
            ),
            _artifact(
                suffix="b",
                start="2026-07-15T10:00:00Z",
                end="2026-07-15T11:00:00Z",
            ),
        ]
    )
    with pytest.raises(ValueError, match="inventory is incomplete"):
        derive_temporal_acceptance_baseline(
            artifact_store=store,
            project="neurons",
            selection=_selection(),
            limit=2,
            max_runtime_seconds=5,
        )


def test_derivation_module_does_not_use_brain_read_surfaces() -> None:
    source = (
        Path(__file__).parents[1]
        / "lib/agent_knowledge/llm_brain_core/objects/temporal_acceptance_derive.py"
    ).read_text(encoding="utf-8")

    assert "BrainReadService" not in source
    assert "brain_objects_query" not in source
    assert "brain.query" not in source

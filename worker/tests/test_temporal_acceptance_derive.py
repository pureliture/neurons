from __future__ import annotations

import json

import pytest

from agent_knowledge.llm_brain_core.objects.temporal_acceptance_derive import (
    TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA,
    derive_temporal_acceptance_baseline,
)


class _MetadataOnlySourceStore:
    def __init__(self, documents: list[dict]) -> None:
        self._documents = [dict(document) for document in documents]
        self.calls: list[dict] = []

    def find_by_type(self, doc_type: str, *, fields=None, selector=None, limit=0, **_kwargs):
        self.calls.append(
            {
                "doc_type": doc_type,
                "fields": list(fields or []),
                "selector": dict(selector or {}),
                "limit": limit,
            }
        )
        result = [
            {
                key: value
                for key, value in document.items()
                if not fields or key in fields
            }
            for document in self._documents
            if document.get("doc_type") == doc_type
            and all(document.get(key) == value for key, value in (selector or {}).items())
        ]
        return result[:limit] if limit else result


class _DriftingMetadataSourceStore(_MetadataOnlySourceStore):
    def find_by_type(self, *args, **kwargs):
        result = super().find_by_type(*args, **kwargs)
        if len(self.calls) == 2:
            result[0]["_rev"] = "changed-private-revision"
        return result


def _selection() -> dict:
    return {
        "schema_version": "temporal_acceptance_selection.v2",
        "policy": "latest_bounded_source_snapshot_v1",
        "date_a": {"as_of": "2026-07-09T10:30:00Z"},
        "date_b": {"as_of": "2026-07-15T10:30:00Z"},
        "range_boundary": {
            "date_from": "2026-07-09T00:00:00Z",
            "date_to": "2026-07-09T23:59:59Z",
        },
    }


def _snapshot(*, suffix: str, start: str, end: str) -> dict:
    return {
        "_id": f"raw-source-id-{suffix}",
        "_rev": f"{suffix}-private-revision",
        "doc_type": "coverage_manifest",
        "project": "neurons",
        "source_hash": "sha256:" + suffix * 64,
        "observed_at_start": start,
        "observed_at_end": end,
        "body": "this must never be requested",
        "title": "this must never be selected",
    }


def test_derives_source_only_authority_baseline_without_raw_identifiers_or_projection_artifacts():
    store = _MetadataOnlySourceStore(
        [
            _snapshot(
                suffix="a",
                start="2026-07-09T10:00:00Z",
                end="2026-07-09T11:00:00Z",
            ),
            _snapshot(
                suffix="b",
                start="2026-07-15T10:00:00Z",
                end="2026-07-15T11:00:00Z",
            ),
        ]
    )

    result = derive_temporal_acceptance_baseline(
        source_store=store,
        project="neurons",
        selection=_selection(),
        limit=3,
        max_runtime_seconds=5,
    )

    assert result["status"] == "derived"
    baseline = result["authority_baseline"]
    assert baseline["schema_version"] == TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA
    assert baseline["source_inventory_current"] is True
    assert baseline["date_a"]["expected_authority_fingerprint"] != baseline["date_b"]["expected_authority_fingerprint"]
    assert baseline["date_a"]["authority_lane"] == "reference_only"
    assert baseline["date_a"]["source_kind"] == "session_memory_artifact"
    assert baseline["date_a"]["source_object_type"] == "SessionMemoryArtifact"
    assert baseline["date_a"]["expected_source_revision"] == "sha256:" + "a" * 64
    rendered = json.dumps(result, sort_keys=True)
    assert "raw-source-id" not in rendered
    assert "private-revision" not in rendered
    assert "this must never" not in rendered
    assert all("body" not in call["fields"] and "title" not in call["fields"] for call in store.calls)


@pytest.mark.parametrize(
    "documents, expected_error",
    [
        (
            [
                _snapshot(
                    suffix="a",
                    start="2026-07-09T10:00:00Z",
                    end="2026-07-09T11:00:00Z",
                ),
                _snapshot(
                    suffix="b",
                    start="2026-07-15T10:00:00Z",
                    end="2026-07-15T11:00:00Z",
                ),
                _snapshot(
                    suffix="c",
                    start="2026-07-16T10:00:00Z",
                    end="2026-07-16T11:00:00Z",
                ),
            ],
            "source inventory is incomplete",
        ),
        (
            [
                _snapshot(
                    suffix="a",
                    start="2026-07-09T10:00:00Z",
                    end="2026-07-09T11:00:00Z",
                ),
                _snapshot(
                    suffix="b",
                    start="2026-07-15T10:00:00Z",
                    end="",
                ),
            ],
            "bounded observed interval",
        ),
    ],
)
def test_derivation_fails_closed_for_incomplete_inventory_or_missing_bounded_time(
    documents, expected_error
):
    with pytest.raises(ValueError, match=expected_error):
        derive_temporal_acceptance_baseline(
            source_store=_MetadataOnlySourceStore(documents),
            project="neurons",
            selection=_selection(),
            limit=3,
            max_runtime_seconds=5,
        )


def test_derivation_rejects_selection_with_raw_source_identifier() -> None:
    selection = _selection()
    selection["date_a"]["source_id"] = "raw-source-id-a"

    with pytest.raises(ValueError, match="raw source identifiers"):
        derive_temporal_acceptance_baseline(
            source_store=_MetadataOnlySourceStore([]),
            project="neurons",
            selection=selection,
            limit=3,
            max_runtime_seconds=5,
        )


def test_derivation_module_cannot_import_or_call_brain_read_surfaces() -> None:
    from pathlib import Path

    source = Path(__file__).parents[1] / "lib/agent_knowledge/llm_brain_core/objects/temporal_acceptance_derive.py"
    text = source.read_text(encoding="utf-8")

    assert "BrainReadService" not in text
    assert "brain_objects_query" not in text
    assert "brain.query" not in text


def test_derivation_reuses_live_interval_overlap_for_calendar_days_and_rejects_non_overlap():
    store = _MetadataOnlySourceStore(
        [
            _snapshot(
                suffix="a",
                start="2026-07-09T10:00:00Z",
                end="2026-07-09T11:00:00Z",
            ),
            _snapshot(
                suffix="b",
                start="2026-07-15T10:00:00Z",
                end="2026-07-15T11:00:00Z",
            ),
        ]
    )
    selection = _selection()
    selection["date_a"] = {"as_of": "2026-07-09"}

    result = derive_temporal_acceptance_baseline(
        source_store=store,
        project="neurons",
        selection=selection,
        limit=3,
        max_runtime_seconds=5,
    )

    assert result["authority_baseline"]["date_a"]["expected_source_revision"] == (
        "sha256:" + "a" * 64
    )
    selection["date_a"] = {"as_of": "2026-07-09T12:00:00Z"}
    with pytest.raises(ValueError, match="date_a candidate_missing"):
        derive_temporal_acceptance_baseline(
            source_store=store,
            project="neurons",
            selection=selection,
            limit=3,
            max_runtime_seconds=5,
        )


def test_derivation_fails_closed_on_latest_start_tie_or_same_date_authority():
    documents = [
        _snapshot(
            suffix="a",
            start="2026-07-09T10:00:00Z",
            end="2026-07-09T11:00:00Z",
        ),
        _snapshot(
            suffix="c",
            start="2026-07-09T10:00:00Z",
            end="2026-07-09T10:30:00Z",
        ),
        _snapshot(
            suffix="b",
            start="2026-07-15T10:00:00Z",
            end="2026-07-15T11:00:00Z",
        ),
    ]
    with pytest.raises(ValueError, match="date_a candidate_ambiguous"):
        derive_temporal_acceptance_baseline(
            source_store=_MetadataOnlySourceStore(documents),
            project="neurons",
            selection=_selection(),
            limit=4,
            max_runtime_seconds=5,
        )

    same_revision_documents = [
        _snapshot(
            suffix="a",
            start="2026-07-09T10:00:00Z",
            end="2026-07-09T11:00:00Z",
        ),
        _snapshot(
            suffix="a",
            start="2026-07-15T10:00:00Z",
            end="2026-07-15T11:00:00Z",
        ),
    ]
    with pytest.raises(ValueError, match="date A/B source revisions must be distinct"):
        derive_temporal_acceptance_baseline(
            source_store=_MetadataOnlySourceStore(same_revision_documents),
            project="neurons",
            selection=_selection(),
            limit=3,
            max_runtime_seconds=5,
        )


def test_derivation_fails_closed_when_inventory_revision_drifts_during_read():
    store = _DriftingMetadataSourceStore(
        [
            _snapshot(
                suffix="a",
                start="2026-07-09T10:00:00Z",
                end="2026-07-09T11:00:00Z",
            ),
            _snapshot(
                suffix="b",
                start="2026-07-15T10:00:00Z",
                end="2026-07-15T11:00:00Z",
            ),
        ]
    )

    with pytest.raises(ValueError, match="source inventory drifted"):
        derive_temporal_acceptance_baseline(
            source_store=store,
            project="neurons",
            selection=_selection(),
            limit=3,
            max_runtime_seconds=5,
        )

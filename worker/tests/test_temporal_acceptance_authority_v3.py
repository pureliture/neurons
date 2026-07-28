from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_knowledge.llm_brain_core._util import hash_payload
from agent_knowledge.llm_brain_core.artifact_store import InMemorySessionMemoryArtifactStore
from agent_knowledge.llm_brain_core.models import SessionMemoryArtifact
from agent_knowledge.llm_brain_core.objects import object_cli
from agent_knowledge.llm_brain_core.objects import post_deploy_mcp_capture
from agent_knowledge.llm_brain_core.objects.temporal_acceptance_derive import (
    TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA,
    derive_temporal_acceptance_baseline,
    validate_temporal_acceptance_authority_baseline,
)


DATE_A = ("2026-07-09T10:00:00Z", "2026-07-09T11:00:00Z")
DATE_B = ("2026-07-15T10:00:00Z", "2026-07-15T11:00:00Z")


def _artifact(
    *,
    suffix: str,
    session_suffix: str,
    interval: tuple[str, str],
    revision: int,
    terms: tuple[str, ...],
) -> SessionMemoryArtifact:
    return SessionMemoryArtifact.from_summary(
        session_id_hash="sha256:" + session_suffix * 64,
        project="neurons",
        provider="codex",
        summary=" ".join(terms),
        source_event_ids=(f"event-{suffix}",),
        source_revision="sha256:" + suffix * 64,
        observed_at_start=interval[0],
        observed_at_end=interval[1],
        revision_observed_at_start=interval[0],
        revision_observed_at_end=interval[1],
        revision_observed_intervals=(interval,),
        revision_temporal_evidence="bounded",
        search_term_hashes=tuple(hash_payload(term) for term in terms),
        materialized_at=interval[1],
        materialization_revision=revision,
    )


def _selection(*, query: str = "migration") -> dict:
    return {
        "schema_version": "temporal_acceptance_selection.v3",
        "policy": "latest_relevant_bounded_artifact_revision_v1",
        "temporal_query": query,
        "date_a": {"as_of": "2026-07-09T10:30:00Z"},
        "date_b": {"as_of": "2026-07-15T10:30:00Z"},
        "range_boundary": {
            "date_from": "2026-07-09T00:00:00Z",
            "date_to": "2026-07-09T23:59:59Z",
        },
    }


def _derive(store: InMemorySessionMemoryArtifactStore, *, query: str = "migration") -> dict:
    return derive_temporal_acceptance_baseline(
        artifact_store=store,
        project="neurons",
        selection=_selection(query=query),
        limit=10,
        max_runtime_seconds=5,
    )["authority_baseline"]


def _v3_config() -> dict:
    return {
        "schema_version": "temporal_acceptance.v3",
        "temporal_query": "migration",
        "date_a": {"as_of": "2026-07-09T10:30:00Z"},
        "date_b": {"as_of": "2026-07-15T10:30:00Z"},
        "range_boundary": {
            "date_from": "2026-07-09T00:00:00Z",
            "date_to": "2026-07-09T23:59:59Z",
        },
        "mismatch": {"as_of": "2026-07-01T10:00:00Z"},
        "invalid_range": {
            "date_from": "2026-07-16T00:00:00Z",
            "date_to": "2026-07-15T00:00:00Z",
        },
        "nonsense_query": "synthetic unrelated probe",
        "semantic_query": {
            "query": "temporal source verification",
            "expected_result_fingerprint": "sha256:" + "d" * 64,
        },
        "runtime_expectations": {
            "schema_version": "temporal_correctness_runtime_expectations.v1",
            "baseline_coverage_count": 1,
            "baseline_backlog_count": 1,
            "minimum_source_session_count": 2,
            "minimum_valid_source_count": 2,
            "max_artifact_age_seconds": 60,
        },
    }


def test_v3_uses_ledger_revision_history_for_same_session_date_a_and_b() -> None:
    store = InMemorySessionMemoryArtifactStore(
        [
            _artifact(
                suffix="a",
                session_suffix="1",
                interval=DATE_A,
                revision=1,
                terms=("migration",),
            ),
            _artifact(
                suffix="b",
                session_suffix="1",
                interval=DATE_B,
                revision=2,
                terms=("migration",),
            ),
        ]
    )

    baseline = _derive(store)

    assert baseline["schema_version"] == TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA
    assert baseline["authority_source"] == "ledger_artifact_revision_history"
    assert baseline["date_a"]["expected_source_revision"] == "sha256:" + "a" * 64
    assert baseline["date_b"]["expected_source_revision"] == "sha256:" + "b" * 64
    assert baseline["date_a"]["expected_source_revision"] != baseline["date_b"][
        "expected_source_revision"
    ]
    rendered = json.dumps(baseline, sort_keys=True)
    assert "session-memory:" not in rendered
    assert "event-a" not in rendered
    assert "migration" not in rendered


def test_v3_fails_closed_when_ledger_has_no_bounded_history() -> None:
    with pytest.raises(ValueError, match="date_a candidate_missing"):
        _derive(InMemorySessionMemoryArtifactStore())


def test_v3_selects_older_relevant_revision_over_newer_unrelated_revision() -> None:
    store = InMemorySessionMemoryArtifactStore(
        [
            _artifact(
                suffix="a",
                session_suffix="1",
                interval=DATE_A,
                revision=1,
                terms=("migration",),
            ),
            _artifact(
                suffix="c",
                session_suffix="1",
                interval=DATE_A,
                revision=2,
                terms=("profile",),
            ),
            _artifact(
                suffix="b",
                session_suffix="2",
                interval=DATE_B,
                revision=1,
                terms=("migration",),
            ),
        ]
    )

    baseline = _derive(store)

    assert baseline["date_a"]["expected_source_revision"] == "sha256:" + "a" * 64


def test_v3_rejects_caller_supplied_baseline_and_retired_v2_config() -> None:
    config = _v3_config()
    config["authority_baseline"] = {"forged": True}

    with pytest.raises(ValueError, match="caller-supplied authority_baseline"):
        post_deploy_mcp_capture._validate_temporal_acceptance_config(config)

    config = _v3_config()
    config["schema_version"] = "temporal_acceptance.v2"
    with pytest.raises(ValueError, match="v2 is retired"):
        post_deploy_mcp_capture._validate_temporal_acceptance_config(config)


def test_v3_rejects_derived_baseline_when_query_hash_does_not_match() -> None:
    store = InMemorySessionMemoryArtifactStore(
        [
            _artifact(
                suffix="a",
                session_suffix="1",
                interval=DATE_A,
                revision=1,
                terms=("migration",),
            ),
            _artifact(
                suffix="b",
                session_suffix="2",
                interval=DATE_B,
                revision=1,
                terms=("migration",),
            ),
        ]
    )
    baseline = _derive(store)

    with pytest.raises(ValueError, match="temporal query hash does not match"):
        validate_temporal_acceptance_authority_baseline(
            baseline,
            temporal_query="unrelated",
        )


def test_v3_collector_derives_before_mcp_and_returns_exact_public_receipt(
    monkeypatch,
) -> None:
    store = InMemorySessionMemoryArtifactStore(
        [
            _artifact(
                suffix="a",
                session_suffix="1",
                interval=DATE_A,
                revision=1,
                terms=("migration",),
            ),
            _artifact(
                suffix="b",
                session_suffix="2",
                interval=DATE_B,
                revision=1,
                terms=("migration",),
            ),
        ]
    )
    derived = derive_temporal_acceptance_baseline(
        artifact_store=store,
        project="neurons",
        selection=_selection(),
        limit=10,
        max_runtime_seconds=5,
    )
    baseline = derived["authority_baseline"]
    receipt = {
        **derived["receipt"],
        "artifact_ledger_metadata_read_only": True,
    }
    phase = {"derived": False}

    def _derive_authority(**kwargs):
        assert kwargs["ledger_path"] == "read-only-ledger"
        assert kwargs["config"]["schema_version"] == "temporal_acceptance.v3"
        phase["derived"] = True
        return baseline, receipt

    async def _collect_checkpoint(_session, **kwargs):
        assert kwargs["authority_baseline"] == baseline
        assert kwargs["authority_derive_receipt"] == receipt
        return {"acceptance_version": "v3", "authority_baseline": baseline}

    class _Session:
        async def initialize(self) -> None:
            return None

        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(isError=False, structuredContent={})

    @asynccontextmanager
    async def _session_factory(_mcp_url: str):
        assert phase["derived"] is True
        yield _Session()

    monkeypatch.setattr(
        post_deploy_mcp_capture,
        "_derive_temporal_acceptance_authority_baseline",
        _derive_authority,
    )
    monkeypatch.setattr(
        post_deploy_mcp_capture,
        "_collect_temporal_recall_corrective_checkpoint",
        _collect_checkpoint,
    )

    capture = asyncio.run(
        post_deploy_mcp_capture.collect_temporal_recall_corrective_checkpoint(
            mcp_url="https://mcp.example.test/mcp",
            project="neurons",
            temporal_acceptance=_v3_config(),
            ledger_path="read-only-ledger",
            session_factory=_session_factory,
        )
    )

    assert capture["authority_derivation"] == receipt
    assert "authority_derivation" not in capture["temporal_recall_corrective_checkpoint"]
    rendered = json.dumps(capture, sort_keys=True)
    assert "event-a" not in rendered
    assert "migration" not in rendered


def test_v3_collector_fails_closed_before_mcp_without_ledger_path() -> None:
    with pytest.raises(ValueError, match="requires a ledger path"):
        asyncio.run(
            post_deploy_mcp_capture.collect_temporal_recall_corrective_checkpoint(
                mcp_url="https://mcp.example.test/mcp",
                project="neurons",
                temporal_acceptance=_v3_config(),
            )
        )


def test_v3_derive_cli_returns_public_blocked_receipt_without_a_ledger(
    tmp_path,
    capsys,
) -> None:
    selection_file = tmp_path / "selection.json"
    selection_file.write_text(json.dumps(_selection()), encoding="utf-8")

    status = object_cli.temporal_acceptance_derive_main(
        [
            "--selection-file",
            str(selection_file),
            "--ledger",
            str(tmp_path / "missing-ledger.db"),
            "--project",
            "neurons",
            "--limit",
            "10",
            "--max-runtime-seconds",
            "5",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 1
    assert payload["schema_version"] == "temporal_acceptance_derive_receipt.v3"
    assert payload["status"] == "blocked"
    assert payload["mutation_performed"] is False
    assert payload["network_used"] is False
    assert str(selection_file) not in json.dumps(payload, sort_keys=True)

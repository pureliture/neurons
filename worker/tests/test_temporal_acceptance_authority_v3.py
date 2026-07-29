from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_knowledge.llm_brain_core._util import hash_payload
from agent_knowledge.llm_brain_core.artifact_store import InMemorySessionMemoryArtifactStore
from agent_knowledge.llm_brain_core.context import BrainReadService
from agent_knowledge.llm_brain_core.models import SessionMemoryArtifact
from agent_knowledge.llm_brain_core.objects import object_cli
from agent_knowledge.llm_brain_core.objects import post_deploy_mcp_capture
from agent_knowledge.llm_brain_core.objects import temporal_acceptance_derive
from agent_knowledge.llm_brain_core.objects.temporal_acceptance_derive import (
    MAX_INVENTORY_LIMIT,
    SOURCE_LEDGER_BINDING_SCHEMA,
    TEMPORAL_ACCEPTANCE_BASELINE_SCHEMA,
    derive_temporal_acceptance_baseline,
    validate_temporal_acceptance_authority_baseline,
)
from agent_knowledge.ledger import Ledger


DATE_A = ("2026-07-09T10:00:00Z", "2026-07-09T11:00:00Z")
DATE_B = ("2026-07-15T10:00:00Z", "2026-07-15T11:00:00Z")


def _artifact(
    *,
    suffix: str,
    session_suffix: str,
    interval: tuple[str, str],
    revision: int,
    terms: tuple[str, ...],
    provider: str = "codex",
) -> SessionMemoryArtifact:
    return SessionMemoryArtifact.from_summary(
        session_id_hash="sha256:" + session_suffix * 64,
        project="neurons",
        provider=provider,
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


def _v3_artifact_source_constraint() -> dict[str, str]:
    return {
        "source_kind": "session_memory_artifact",
        "source_object_type": "SessionMemoryArtifact",
        "authority_lane": "reference_only",
    }


def _temporal_task_card() -> dict:
    return {
        "memory_id": "temporal-memory-card",
        "card_type": "task",
        "project": "neurons",
        "title": "memory card migration work",
        "summary": "memory card migration work",
        "lifecycle_state": "accepted",
        "approval_state": "approved",
        "currentness": "current",
        "observed_at_start": DATE_A[0],
        "observed_at_end": DATE_A[1],
        "typed_payload": {
            "task_state": "migration",
            "next_action": "resume migration work",
            "status": "open",
        },
    }


def _temporal_route_objects(
    service: BrainReadService,
    *,
    temporal_source_constraint: dict[str, str] | None = None,
) -> list[dict]:
    result = service.brain_objects_query(
        repository="pureliture/neurons",
        branch="main",
        project="neurons",
        query="migration",
        current_files=[],
        route="temporal_work_recall",
        as_of="2026-07-09T10:30:00Z",
        limit=2,
        temporal_source_constraint=temporal_source_constraint,
    )
    return result["object_pack"]["objects"]


def test_generic_temporal_route_retains_memory_card_semantics_without_constraint() -> None:
    service = BrainReadService(
        memory_cards=[_temporal_task_card()],
        artifact_store=InMemorySessionMemoryArtifactStore(
            [
                _artifact(
                    suffix="a",
                    session_suffix="1",
                    interval=DATE_A,
                    revision=1,
                    terms=("migration",),
                )
            ]
        ),
    )

    objects = _temporal_route_objects(service)

    assert any(
        item.get("payload", {}).get("source_kind") == "memory_card"
        for item in objects
    )


def test_temporal_source_constraint_selects_artifact_before_limit() -> None:
    service = BrainReadService(
        memory_cards=[_temporal_task_card()],
        artifact_store=InMemorySessionMemoryArtifactStore(
            [
                _artifact(
                    suffix="a",
                    session_suffix="1",
                    interval=DATE_A,
                    revision=1,
                    terms=("migration",),
                )
            ]
        ),
    )

    objects = _temporal_route_objects(
        service,
        temporal_source_constraint=_v3_artifact_source_constraint(),
    )

    assert len(objects) == 1
    assert objects[0]["authority_lane"] == "reference_only"
    assert objects[0]["payload"]["source_kind"] == "session_memory_artifact"
    assert objects[0]["payload"]["source_object_type"] == "SessionMemoryArtifact"


def _temporal_work_unit(
    *,
    source_kind: str,
    source_object_type: str,
    authority_lane: str,
    source_revision: str,
    interval: tuple[str, str] = DATE_A,
) -> dict:
    return {
        "object_id": f"work-unit:{source_kind}:{source_revision[-8:]}",
        "object_type": "WorkUnit",
        "content_hash": "sha256:" + "c" * 64,
        "authority_lane": authority_lane,
        "payload": {
            "source_kind": source_kind,
            "source_object_type": source_object_type,
            "source_revision": source_revision,
            "observed_at_start": interval[0],
            "observed_at_end": interval[1],
        },
    }


def _v3_probe_summary(
    objects: list[dict], *, baseline: dict
) -> dict:
    return post_deploy_mcp_capture._temporal_object_probe_summary(
        {
            "schema_version": "brain_objects_query.v1",
            "route": "temporal_work_recall",
            "object_pack": {
                "schema_version": "object_pack.v1",
                "route": "temporal_work_recall",
                "objects": objects,
                "gaps": [],
                "confidence": {"score": 0.7},
            },
        },
        selector={"as_of": "2026-07-09T10:30:00Z"},
        expected_fingerprint="",
        expected_identity_fingerprint="",
        expected_authority_fingerprint=baseline["date_a"][
            "expected_authority_fingerprint"
        ],
        expected_source_revision=baseline["date_a"]["expected_source_revision"],
        artifact_authority_only=True,
    )


def test_v3_probe_rejects_mixed_raw_response_when_server_ignored_constraint() -> None:
    baseline = _derive(
        InMemorySessionMemoryArtifactStore(
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
    )
    memory_card = _temporal_work_unit(
        source_kind="memory_card",
        source_object_type="MemoryCard:task",
        authority_lane="accepted_current",
        source_revision="sha256:" + "c" * 64,
    )
    artifact = _temporal_work_unit(
        source_kind="session_memory_artifact",
        source_object_type="SessionMemoryArtifact",
        authority_lane="reference_only",
        source_revision=baseline["date_a"]["expected_source_revision"],
    )

    summary = _v3_probe_summary([memory_card, artifact], baseline=baseline)

    assert summary["work_unit_count"] == 2
    assert summary["object_count"] == 2
    assert summary["second_result_present"] is True
    assert summary["gap_count"] > 0


@pytest.mark.parametrize(
    "objects",
    [
        [
            _temporal_work_unit(
                source_kind="memory_card",
                source_object_type="MemoryCard:task",
                authority_lane="accepted_current",
                source_revision="sha256:" + "c" * 64,
            )
        ],
        [
            _temporal_work_unit(
                source_kind="session_memory_artifact",
                source_object_type="SessionMemoryArtifact",
                authority_lane="reference_only",
                source_revision="sha256:" + "a" * 64,
            ),
            _temporal_work_unit(
                source_kind="session_memory_artifact",
                source_object_type="SessionMemoryArtifact",
                authority_lane="reference_only",
                source_revision="sha256:" + "d" * 64,
            ),
        ],
        [
            _temporal_work_unit(
                source_kind="session_memory_artifact",
                source_object_type="SessionMemoryArtifact",
                authority_lane="reference_only",
                source_revision="sha256:" + "c" * 64,
            )
        ],
    ],
)
def test_v3_probe_fails_closed_without_one_matching_artifact_candidate(
    objects: list[dict],
) -> None:
    baseline = _derive(
        InMemorySessionMemoryArtifactStore(
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
    )

    summary = _v3_probe_summary(objects, baseline=baseline)

    assert summary["gap_count"] > 0
    assert summary["observed_authority_fingerprint"] != baseline["date_a"][
        "expected_authority_fingerprint"
    ]


def test_v3_collector_sends_artifact_source_constraint(monkeypatch) -> None:
    baseline = _derive(
        InMemorySessionMemoryArtifactStore(
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
    )
    probe_artifact = _temporal_work_unit(
        source_kind="session_memory_artifact",
        source_object_type="SessionMemoryArtifact",
        authority_lane="reference_only",
        source_revision=baseline["date_a"]["expected_source_revision"],
    )
    temporal_requests: list[dict] = []

    async def _call_tool(_session, name: str, arguments: dict) -> dict:
        if name == "brain_objects_query":
            temporal_requests.append(dict(arguments))
            if (
                arguments.get("date_from") == "2026-07-16T00:00:00Z"
                and arguments.get("date_to") == "2026-07-15T00:00:00Z"
            ):
                return {
                    "collector_call_failed": True,
                    "collector_error_type": "McpToolError",
                    "collector_error_code": -32602,
                }
            return {
                "schema_version": "brain_objects_query.v1",
                "route": "temporal_work_recall",
                "object_pack": {
                    "schema_version": "object_pack.v1",
                    "route": "temporal_work_recall",
                    "objects": [probe_artifact],
                    "gaps": [],
                    "confidence": {"score": 0.7},
                },
            }
        return {}

    monkeypatch.setattr(
        post_deploy_mcp_capture,
        "_call_tool_untrusted_mapping",
        _call_tool,
    )

    asyncio.run(
        post_deploy_mcp_capture._collect_temporal_recall_corrective_checkpoint(
            object(),
            repository="pureliture/neurons",
            branch="main",
            project="neurons",
            consumer="codex",
            config=_v3_config(),
            runtime_packet={},
            authority_baseline=baseline,
            authority_derive_receipt={"source_inventory_hash": hash_payload("stable")},
        )
    )

    assert len(temporal_requests) == 5
    assert all(
        request["temporal_source_constraint"] == _v3_artifact_source_constraint()
        for request in temporal_requests
    )


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


def test_v3_excludes_synthetic_canary_from_authority_baseline() -> None:
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
            _artifact(
                suffix="c",
                session_suffix="2",
                interval=DATE_B,
                revision=2,
                terms=("migration",),
                provider="lbrain-temporal-canary",
            ),
        ]
    )

    baseline = _derive(store)

    assert baseline["date_b"]["expected_source_revision"] == "sha256:" + "b" * 64


def test_v3_config_normalizes_offset_selectors_and_forwards_inventory_limit(monkeypatch) -> None:
    config = _v3_config()
    config.update(
        {
            "date_a": {"as_of": "2026-07-09T19:30:00+09:00"},
            "date_b": {"as_of": "2026-07-15T05:30:00-05:00"},
            "range_boundary": {
                "date_from": "2026-07-09T09:00:00+09:00",
                "date_to": "2026-07-10T08:59:59+09:00",
            },
            "mismatch": {"as_of": "2026-07-01T19:00:00+09:00"},
            "invalid_range": {
                "date_from": "2026-07-16T09:00:00+09:00",
                "date_to": "2026-07-15T09:00:00+09:00",
            },
        }
    )
    validated = post_deploy_mcp_capture._validate_temporal_acceptance_config(
        config,
        inventory_limit=7,
    )
    assert validated["inventory_limit"] == 7
    assert validated["date_a"]["as_of"] == "2026-07-09T10:30:00Z"
    assert validated["date_b"]["as_of"] == "2026-07-15T10:30:00Z"
    assert validated["range_boundary"] == {
        "date_from": "2026-07-09T00:00:00Z",
        "date_to": "2026-07-09T23:59:59Z",
    }

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
    seen: dict[str, object] = {}

    def _derive_from_ledger(**kwargs):
        seen.update(kwargs)
        return {"authority_baseline": baseline, "receipt": {}}

    monkeypatch.setattr(
        post_deploy_mcp_capture,
        "derive_temporal_acceptance_baseline_from_ledger",
        _derive_from_ledger,
    )

    post_deploy_mcp_capture._derive_temporal_acceptance_authority_baseline(
        config=validated,
        ledger_path="read-only-ledger",
        project="neurons",
    )

    assert seen["limit"] == 7
    assert seen["selection"] == _selection()


@pytest.mark.parametrize("value", (1, MAX_INVENTORY_LIMIT + 1, True, "7"))
def test_v3_inventory_limit_rejects_unsafe_values(value: object) -> None:
    config = _v3_config()
    config["inventory_limit"] = value

    with pytest.raises(ValueError, match="inventory_limit"):
        post_deploy_mcp_capture._validate_temporal_acceptance_config(config)


def test_v3_inventory_limit_rejects_conflicting_cli_override() -> None:
    config = _v3_config()
    config["inventory_limit"] = 8

    with pytest.raises(ValueError, match="conflicts"):
        post_deploy_mcp_capture._validate_temporal_acceptance_config(
            config,
            inventory_limit=7,
        )


def test_inventory_limit_remains_v3_only() -> None:
    with pytest.raises(ValueError, match="only by temporal acceptance v3"):
        post_deploy_mcp_capture._validate_temporal_acceptance_config(
            {"inventory_limit": 7}
        )


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


def _minimal_derived_receipt() -> dict:
    return {"source_inventory_hash": hash_payload({"source": "stable"})}


def test_v3_from_ledger_binds_original_sqlite_source_before_and_after_open(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("NEURON_LEDGER_PG_DSN", raising=False)
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    with ledger._connect() as connection:
        connection.execute("SELECT 1")

    monkeypatch.setattr(
        temporal_acceptance_derive,
        "derive_temporal_acceptance_baseline",
        lambda **_kwargs: {"receipt": _minimal_derived_receipt()},
    )

    result = temporal_acceptance_derive.derive_temporal_acceptance_baseline_from_ledger(
        ledger_path=str(ledger_path),
        project="neurons",
        selection=_selection(),
        limit=10,
        max_runtime_seconds=5,
    )

    receipt = result["receipt"]
    binding = receipt["source_ledger_binding"]
    assert receipt["ledger_backend"] == "sqlite"
    assert receipt["network_used"] is False
    assert binding == {
        "schema_version": SOURCE_LEDGER_BINDING_SCHEMA,
        "backend": "sqlite",
        "before_fingerprint": binding["before_fingerprint"],
        "after_fingerprint": binding["before_fingerprint"],
        "stable": True,
    }
    assert str(ledger_path) not in json.dumps(result, sort_keys=True)


def test_v3_from_ledger_blocks_sqlite_drift_during_read_only_open(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("NEURON_LEDGER_PG_DSN", raising=False)
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    with ledger._connect() as connection:
        connection.execute("SELECT 1")
    original_open_read_only = temporal_acceptance_derive.Ledger.open_read_only

    def _mutating_open_read_only(path: str, *, deadline_monotonic=None):
        with ledger_path.open("ab") as handle:
            handle.write(b"drift")
        return original_open_read_only(path, deadline_monotonic=deadline_monotonic)

    monkeypatch.setattr(
        temporal_acceptance_derive,
        "derive_temporal_acceptance_baseline",
        lambda **_kwargs: {"receipt": _minimal_derived_receipt()},
    )
    monkeypatch.setattr(
        temporal_acceptance_derive.Ledger,
        "open_read_only",
        staticmethod(_mutating_open_read_only),
    )

    with pytest.raises(ValueError, match="source ledger drifted") as exc_info:
        temporal_acceptance_derive.derive_temporal_acceptance_baseline_from_ledger(
            ledger_path=str(ledger_path),
            project="neurons",
            selection=_selection(),
            limit=10,
            max_runtime_seconds=5,
        )

    assert str(ledger_path) not in str(exc_info.value)


def test_v3_from_ledger_fails_closed_when_source_binding_exceeds_runtime_budget(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("NEURON_LEDGER_PG_DSN", raising=False)
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    with ledger._connect() as connection:
        connection.execute("SELECT 1")
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(
        temporal_acceptance_derive.time,
        "monotonic",
        lambda: next(ticks),
    )

    with pytest.raises(ValueError, match="runtime limit exceeded"):
        temporal_acceptance_derive.derive_temporal_acceptance_baseline_from_ledger(
            ledger_path=str(ledger_path),
            project="neurons",
            selection=_selection(),
            limit=10,
            max_runtime_seconds=1,
        )


def test_v3_from_postgres_marks_network_used_in_receipt(monkeypatch) -> None:
    fake_ledger = SimpleNamespace(
        read_only=True,
        _db_adapter=SimpleNamespace(is_file_backed=False),
    )
    monkeypatch.setenv("NEURON_LEDGER_PG_DSN", "postgresql://test.invalid/ledger")
    monkeypatch.setattr(
        temporal_acceptance_derive.Ledger,
        "open_read_only",
        staticmethod(lambda _path, **_kwargs: fake_ledger),
    )
    monkeypatch.setattr(
        temporal_acceptance_derive,
        "derive_temporal_acceptance_baseline",
        lambda **_kwargs: {"receipt": _minimal_derived_receipt()},
    )

    result = temporal_acceptance_derive.derive_temporal_acceptance_baseline_from_ledger(
        ledger_path="configured-postgres-ledger",
        project="neurons",
        selection=_selection(),
        limit=10,
        max_runtime_seconds=5,
    )

    assert result["receipt"]["ledger_backend"] == "postgres"
    assert result["receipt"]["network_used"] is True
    assert result["receipt"]["source_ledger_binding"]["backend"] == "postgres"


def test_v3_from_postgres_passes_remaining_deadline_to_read_only_ledger(monkeypatch) -> None:
    fake_ledger = SimpleNamespace(
        read_only=True,
        _db_adapter=SimpleNamespace(is_file_backed=False),
    )
    observed = {}
    monkeypatch.setenv("NEURON_LEDGER_PG_DSN", "postgresql://test.invalid/ledger")

    def _open_read_only(_path, *, deadline_monotonic=None):
        observed["deadline_monotonic"] = deadline_monotonic
        return fake_ledger

    monkeypatch.setattr(
        temporal_acceptance_derive.Ledger,
        "open_read_only",
        staticmethod(_open_read_only),
    )
    monkeypatch.setattr(
        temporal_acceptance_derive,
        "derive_temporal_acceptance_baseline",
        lambda **_kwargs: {"receipt": _minimal_derived_receipt()},
    )

    started = temporal_acceptance_derive.time.monotonic()
    temporal_acceptance_derive.derive_temporal_acceptance_baseline_from_ledger(
        ledger_path="configured-postgres-ledger",
        project="neurons",
        selection=_selection(),
        limit=10,
        max_runtime_seconds=5,
    )

    assert observed["deadline_monotonic"] == pytest.approx(started + 5, abs=0.1)


def test_v3_from_postgres_marks_inventory_failure_as_network_attempted(monkeypatch) -> None:
    fake_ledger = SimpleNamespace(
        read_only=True,
        _db_adapter=SimpleNamespace(is_file_backed=False, network_attempted=True),
    )
    monkeypatch.setenv("NEURON_LEDGER_PG_DSN", "postgresql://test.invalid/ledger")
    monkeypatch.setattr(
        temporal_acceptance_derive.Ledger,
        "open_read_only",
        staticmethod(lambda _path, **_kwargs: fake_ledger),
    )

    def _inventory_failure(**_kwargs):
        raise RuntimeError("inventory unavailable")

    monkeypatch.setattr(
        temporal_acceptance_derive,
        "derive_temporal_acceptance_baseline",
        _inventory_failure,
    )

    with pytest.raises(RuntimeError, match="inventory unavailable") as exc_info:
        temporal_acceptance_derive.derive_temporal_acceptance_baseline_from_ledger(
            ledger_path="configured-postgres-ledger",
            project="neurons",
            selection=_selection(),
            limit=10,
            max_runtime_seconds=5,
        )

    assert exc_info.value.network_attempted is True


def test_v3_derive_cli_records_attempted_postgres_network_on_inventory_failure(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    selection_file = tmp_path / "selection.json"
    selection_file.write_text(json.dumps(_selection()), encoding="utf-8")

    def _inventory_failure(**_kwargs):
        error = RuntimeError("inventory unavailable")
        error.network_attempted = True
        raise error

    monkeypatch.setattr(
        object_cli,
        "derive_temporal_acceptance_baseline_from_ledger",
        _inventory_failure,
    )

    status = object_cli.temporal_acceptance_derive_main(
        [
            "--selection-file",
            str(selection_file),
            "--ledger",
            "configured-postgres-ledger",
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
    assert payload["network_used"] is True


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

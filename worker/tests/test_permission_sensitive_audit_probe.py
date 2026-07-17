from __future__ import annotations

import json

import pytest

from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.ledger import Ledger
from agent_knowledge.mcp_jsonrpc import dispatch_tool_call
from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message
from agent_knowledge.mcp_tools import list_tools
from agent_knowledge.permission_audit_contract import (
    build_permission_audit_operation_hash,
)
from agent_knowledge.permission_audit import IndependentProductMutationMarkerReader


BUILD_ASSOCIATION_HASH = "sha256:" + "a" * 64
EXPECTED_COMMIT = "c" * 40
OPS_REVISION = "b" * 40
OPERATION_HASH = build_permission_audit_operation_hash(
    build_association_hash=BUILD_ASSOCIATION_HASH,
    ops_revision=OPS_REVISION,
    expected_commit=EXPECTED_COMMIT,
)
PROJECTED_TOKEN = "synthetic-projected-token-fixture"
ACTOR_REF_HASH = "sha256:" + "c" * 64


def test_operation_hash_matches_ops_golden_vector_and_changes_per_jenkins_build():
    assert OPERATION_HASH == (
        "sha256:90e0a5e6043fffb0c6b36de7e4345ba02f516555ef90d7128b81bd873f08061f"
    )
    next_build_hash = "sha256:" + "f" * 64
    assert build_permission_audit_operation_hash(
        build_association_hash=next_build_hash,
        ops_revision=OPS_REVISION,
        expected_commit=EXPECTED_COMMIT,
    ) != OPERATION_HASH


def _token_review_response():
    return {
        "apiVersion": "authentication.k8s.io/v1",
        "kind": "TokenReview",
        "status": {
            "authenticated": True,
            "audiences": ["neurons-permission-audit"],
            "user": {
                "username": (
                    "system:serviceaccount:jenkins:"
                    "neurons-release-production-evidence"
                ),
                "extra": {
                    "authentication.kubernetes.io/pod-name": ["evidence-pod"],
                    "authentication.kubernetes.io/pod-uid": ["fixture-pod-uid"],
                },
            },
        },
    }


def _sentinel():
    return {
        "schema_version": "product_mutation_sentinel.v1",
        "authority_ledger": {"count": 5, "hash": "sha256:" + "1" * 64},
        "corpus": {"count": 7, "hash": "sha256:" + "2" * 64},
        "queue": {"count": 0, "hash": "sha256:" + "3" * 64},
        "index": {"count": 9, "hash": "sha256:" + "4" * 64},
        "product_db": {"count": 11, "hash": "sha256:" + "5" * 64},
    }


def _marker_snapshot():
    in_flight_statuses = (
        "clear",
        "atomic_commit_boundary",
        "atomic_commit_boundary",
        "clear",
        "atomic_commit_boundary",
    )
    markers = []
    for index, (plane, in_flight_status) in enumerate(
        zip(
            ("authority_ledger", "corpus", "queue", "index", "product_db"),
            in_flight_statuses,
            strict=True,
        ),
        start=1,
    ):
        markers.append(
            {
                "plane": plane,
                "generation_hash": "sha256:" + "a" * 64,
                "event_position_hash": "sha256:" + format(index, "x") * 64,
                "marker_hash": "sha256:" + format(index + 5, "x") * 64,
                "in_flight_count": 0,
                "in_flight_status": in_flight_status,
                "coverage_hash": "sha256:" + format(index + 10, "x") * 64,
                "coverage_status": "validated",
                "read_scope_status": "read_only",
                "reset_or_decrease_count": 0,
                "read_call_count": 1,
            }
        )
    return {
        "schema_version": "product_mutation_marker_snapshot.v1",
        "marker_count": 5,
        "markers": markers,
        "reset_or_decrease_count": 0,
        "production_mutation_performed": False,
    }


def _exact_marker_reader(*, events=None):
    events = events if events is not None else []

    def record(plane):
        return next(
            item for item in _marker_snapshot()["markers"] if item["plane"] == plane
        )

    class Fence:
        def read_marker(self):
            events.append("authority_ledger")
            return record("authority_ledger")

        def release(self):
            events.append("release")

    class Provider:
        def __init__(self, plane):
            self.plane = plane

        def __call__(self):
            events.append(self.plane)
            return record(self.plane)

    def acquire_fence():
        events.append("acquire")
        return Fence()

    return IndependentProductMutationMarkerReader(
        authority_fence_factory=acquire_fence,
        providers={
            plane: Provider(plane)
            for plane in ("corpus", "queue", "index", "product_db")
        },
    )


def _bounded_denial_result(**kwargs):
    return {
        "schema_version": "bounded_permission_denial_result.v1",
        "action": "single_bounded_denial.v1",
        "ledger_scope": "production",
        "permission": "denied",
        "authority_write_performed": False,
        "production_mutation_performed": False,
        "actor_ref_hash": kwargs["actor_ref_hash"],
        "request_hash": kwargs["request_hash"],
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }


def _store_result(*, append_count=1, **kwargs):
    return {
        "status": "recorded",
        "append_count": append_count,
        "stored_row_count": 1,
        "read_after_write_status": "validated",
        **_bounded_denial_result(**kwargs),
    }


def _service(tmp_path, **kwargs):
    return KnowledgeSearchService(
        ledger=Ledger(tmp_path / "ledger.sqlite3"),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        **kwargs,
    )


def test_independent_exact_marker_reader_holds_postgres_fence_across_one_action():
    events = []
    records = {
        marker["plane"]: marker for marker in _marker_snapshot()["markers"]
    }

    class Fence:
        def read_marker(self):
            events.append("authority_ledger")
            return records["authority_ledger"]

        def release(self):
            events.append("release")

    class Provider:
        def __init__(self, plane):
            self.plane = plane

        def __call__(self):
            events.append(self.plane)
            return records[self.plane]

    def acquire_fence():
        events.append("acquire")
        return Fence()

    reader = IndependentProductMutationMarkerReader(
        authority_fence_factory=acquire_fence,
        providers={
            plane: Provider(plane)
            for plane in ("corpus", "queue", "index", "product_db")
        },
    )

    before, action_result, after = reader.run_audit_window(
        lambda: events.append("action") or {"status": "recorded"}
    )

    assert before["schema_version"] == "product_mutation_marker_snapshot.v1"
    assert after["schema_version"] == "product_mutation_marker_snapshot.v1"
    assert action_result == {"status": "recorded"}
    assert events == [
        "acquire",
        "authority_ledger",
        "corpus",
        "queue",
        "index",
        "product_db",
        "action",
        "authority_ledger",
        "corpus",
        "queue",
        "index",
        "product_db",
        "release",
    ]


def test_permission_probe_executes_store_inside_exact_marker_audit_window(tmp_path):
    calls = []

    class WindowReader:
        def run_audit_window(self, action):
            calls.append("window_before")
            result = action()
            calls.append("window_after")
            return _marker_snapshot(), result, _marker_snapshot()

    def append(**kwargs):
        calls.append("store")
        return _store_result(**kwargs)

    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_token_reviewer=lambda _token: _token_review_response(),
        permission_audit_store_append=append,
        permission_audit_product_sentinel_reader=WindowReader(),
    )

    result = service.brain_permission_sensitive_audit_probe(
        mode="deny_once",
        operation_hash=OPERATION_HASH,
        build_association_hash=BUILD_ASSOCIATION_HASH,
        projected_service_account_token=PROJECTED_TOKEN,
    )

    assert calls == ["window_before", "store", "window_after"]
    assert result["postcheck"]["product_mutation_markers_match"] is True


def test_permission_probe_rejects_unfenced_marker_callable_before_store(tmp_path):
    calls = {"store": 0}

    def append(**kwargs):
        calls["store"] += 1
        return _store_result(**kwargs)

    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_token_reviewer=lambda _token: _token_review_response(),
        permission_audit_store_append=append,
        permission_audit_product_sentinel_reader=_marker_snapshot,
    )

    with pytest.raises(
        RuntimeError,
        match="permission audit exact marker window is required",
    ):
        service.brain_permission_sensitive_audit_probe(
            mode="deny_once",
            operation_hash=OPERATION_HASH,
            build_association_hash=BUILD_ASSOCIATION_HASH,
            projected_service_account_token=PROJECTED_TOKEN,
        )

    assert calls == {"store": 0}


def test_permission_sensitive_audit_probe_contract_is_single_purpose_and_strict():
    tools = {tool["name"]: tool for tool in list_tools()}

    contract = tools["brain_permission_sensitive_audit_probe"]

    assert contract["inputSchema"] == {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["deny_once"]},
            "operation_hash": {
                "type": "string",
                "pattern": "^sha256:[0-9a-f]{64}$",
                "description": (
                    "build association hash, packet ops revision, external expected commit, "
                    "fixed action의 canonical SHA-256이다."
                ),
            },
            "build_association_hash": {
                "type": "string",
                "pattern": "^sha256:[0-9a-f]{64}$",
                "description": (
                    "current Jenkins build externalizable id의 SHA-256이며 "
                    "raw job id는 허용하지 않는다."
                ),
            },
            "projected_service_account_token": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16384,
            },
        },
        "required": [
            "mode",
            "operation_hash",
            "build_association_hash",
            "projected_service_account_token",
        ],
        "additionalProperties": False,
    }
    assert "opt-in" in contract["description"]
    assert "Jenkins" in contract["description"]
    assert "Jenkins" in contract["inputSchema"]["properties"]["build_association_hash"][
        "description"
    ]


def test_runtime_evidence_tool_names_are_exported_from_mcp_server():
    from agent_knowledge import mcp_server

    assert (
        mcp_server.BRAIN_PERMISSION_SENSITIVE_AUDIT_PROBE_TOOL_NAME
        == "brain_permission_sensitive_audit_probe"
    )
    assert mcp_server.BRAIN_RUNTIME_BUILD_IDENTITY_TOOL_NAME == "brain_runtime_build_identity"


def test_runtime_readiness_evaluator_accepts_external_build_association_anchor():
    tools = {tool["name"]: tool for tool in list_tools()}

    association = tools["brain_source_to_candidate_runtime_readiness"]["inputSchema"][
        "properties"
    ]["expected_build_association_hash"]

    assert association == {
        "type": "string",
        "pattern": "^sha256:[0-9a-f]{64}$",
        "description": (
            "controller Run action에서 packet과 별도로 전달한 external build "
            "association anchor다."
        ),
    }


def test_runtime_readiness_service_forwards_external_build_association_anchor(
    monkeypatch,
    tmp_path,
):
    from agent_knowledge import knowledge_search_service as service_module

    captured = {}

    def report_builder(**kwargs):
        captured.update(kwargs)
        return {"status": "PASS"}

    monkeypatch.setattr(
        service_module,
        "build_source_to_candidate_runtime_readiness_report",
        report_builder,
    )

    result = _service(tmp_path).brain_source_to_candidate_runtime_readiness(
        live_evidence={},
        expected_commit=EXPECTED_COMMIT,
        expected_build_association_hash=BUILD_ASSOCIATION_HASH,
    )

    assert result == {"status": "PASS"}
    assert captured["expected_build_association_hash"] == BUILD_ASSOCIATION_HASH


def test_runtime_readiness_service_audit_off_collects_no_synthetic_or_runtime_audit(
    monkeypatch,
    tmp_path,
):
    from agent_knowledge.llm_brain_core.objects import runtime_readiness

    calls = {"synthetic": 0, "provider": 0, "store": 0, "network": 0}

    def counted(name):
        def invoke(*_args, **_kwargs):
            calls[name] += 1
            raise AssertionError(f"audit-off invoked {name}")

        return invoke

    monkeypatch.setattr(
        runtime_readiness,
        "build_permission_sensitive_audit_shadow_evidence",
        counted("synthetic"),
    )
    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=False,
        permission_audit_token_reviewer=counted("network"),
        permission_audit_store_append=counted("store"),
        permission_audit_product_sentinel_reader=counted("provider"),
    )

    packet = service.brain_source_to_candidate_runtime_readiness(
        collect_shadow_evidence=True,
        evidence_collection_mode="local_test_replay",
        evidence_collection_network_used=False,
        repository="pureliture/neurons",
        branch="codex/post-deploy-evidence-runtime-contract",
        consumer="codex",
    )
    report = runtime_readiness.build_source_to_candidate_runtime_readiness_report(
        live_evidence=packet
    )
    permission_claim = next(
        claim
        for claim in report["claims"]
        if claim["claim_id"] == "live.production.permission_sensitive_audit"
    )

    assert calls == {"synthetic": 0, "provider": 0, "store": 0, "network": 0}
    assert "permission_sensitive_audit" not in packet
    assert packet["collector"]["permission_sensitive_audit_collected"] is False
    assert (
        packet["collector"]["permission_sensitive_audit_collection_status"]
        == "not_collected"
    )
    assert packet["collector"]["permission_sensitive_audit_schema"] == ""
    assert permission_claim["status"] == "not_validated"
    assert permission_claim["gaps"] == [
        "permission_sensitive_audit_unverified",
        "product_marker_audit_unverified",
    ]


def test_runtime_readiness_dispatch_forwards_external_build_association_anchor(
    monkeypatch,
    tmp_path,
):
    from agent_knowledge import knowledge_search_service as service_module

    captured = {}

    def report_builder(**kwargs):
        captured.update(kwargs)
        return {"status": "PASS"}

    monkeypatch.setattr(
        service_module,
        "build_source_to_candidate_runtime_readiness_report",
        report_builder,
    )

    dispatch_tool_call(
        {
            "name": "brain_source_to_candidate_runtime_readiness",
            "arguments": {
                "live_evidence": {},
                "expected_commit": EXPECTED_COMMIT,
                "expected_build_association_hash": BUILD_ASSOCIATION_HASH,
            },
        },
        _service(tmp_path),
    )

    assert captured["expected_build_association_hash"] == BUILD_ASSOCIATION_HASH


def test_permission_sensitive_audit_probe_is_default_off_with_zero_calls(tmp_path):
    calls = {"review": 0, "store": 0, "sentinel": 0}

    def review(_token):
        calls["review"] += 1
        return _token_review_response()

    def append(**_kwargs):
        calls["store"] += 1
        return {}

    def sentinel():
        calls["sentinel"] += 1
        return _sentinel()

    service = _service(
        tmp_path,
        permission_audit_token_reviewer=review,
        permission_audit_store_append=append,
        permission_audit_product_sentinel_reader=sentinel,
    )

    with pytest.raises(RuntimeError, match="permission audit probe is disabled"):
        service.brain_permission_sensitive_audit_probe(
            mode="deny_once",
            operation_hash=OPERATION_HASH,
            build_association_hash=BUILD_ASSOCIATION_HASH,
            projected_service_account_token=PROJECTED_TOKEN,
        )

    assert calls == {"review": 0, "store": 0, "sentinel": 0}


def test_enabled_probe_rejects_legacy_count_hash_sentinel_before_denied_request(
    tmp_path,
):
    calls = {"store": 0, "sentinel": 0}

    def append(**kwargs):
        calls["store"] += 1
        return _store_result(**kwargs)

    class LegacyWindow:
        def run_audit_window(self, _action):
            calls["sentinel"] += 1
            return _sentinel(), {}, _sentinel()

    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_token_reviewer=lambda _token: _token_review_response(),
        permission_audit_store_append=append,
        permission_audit_product_sentinel_reader=LegacyWindow(),
    )

    with pytest.raises(
        ValueError,
        match="permission audit exact marker snapshot is malformed",
    ):
        service.brain_permission_sensitive_audit_probe(
            mode="deny_once",
            operation_hash=OPERATION_HASH,
            build_association_hash=BUILD_ASSOCIATION_HASH,
            projected_service_account_token=PROJECTED_TOKEN,
        )

    assert calls == {"store": 0, "sentinel": 1}


def test_enabled_probe_returns_one_sanitized_denial_and_validated_readback(tmp_path):
    reviewed_tokens = []
    store_calls = []
    marker_events = []

    def review(token):
        reviewed_tokens.append(token)
        return _token_review_response()

    def append(**kwargs):
        store_calls.append(kwargs)
        return _store_result(**kwargs)

    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_token_reviewer=review,
        permission_audit_store_append=append,
        permission_audit_product_sentinel_reader=_exact_marker_reader(
            events=marker_events
        ),
    )

    result = service.brain_permission_sensitive_audit_probe(
        mode="deny_once",
        operation_hash=OPERATION_HASH,
        build_association_hash=BUILD_ASSOCIATION_HASH,
        projected_service_account_token=PROJECTED_TOKEN,
    )

    assert result["schema_version"] == "permission_sensitive_runtime_audit_evidence.v2"
    assert result["policy"] == "single_bounded_denial.v1"
    assert result["build_association_hash"] == BUILD_ASSOCIATION_HASH
    assert result["product_marker_evidence"]["schema_version"] == (
        "product_mutation_marker_evidence.v1"
    )
    assert result["product_marker_evidence"]["external_build_association_hash"] == (
        BUILD_ASSOCIATION_HASH
    )
    assert result["product_marker_evidence"]["marker_count"] == 5
    assert [
        marker["plane"] for marker in result["product_marker_evidence"]["markers"]
    ] == ["authority_ledger", "corpus", "queue", "index", "product_db"]
    assert {
        marker["pre_post_status"]
        for marker in result["product_marker_evidence"]["markers"]
    } == {"equal"}
    assert result["transport_call_count"] == 1
    assert result["permission_action_count"] == 1
    assert len(result["audit_events"]) == 1
    assert result["audit_events"][0] == {
        "schema_version": "runtime_permission_audit_event.v2",
        "event_type": "permission_sensitive_runtime_action",
        "action": "single_bounded_denial.v1",
        "ledger_scope": "production",
        "permission": "denied",
        "authority_write_performed": False,
        "production_mutation_performed": False,
        "actor_ref_hash": store_calls[0]["actor_ref_hash"],
        "request_hash": OPERATION_HASH,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    assert result["audit_store"]["append_count"] == 1
    assert result["audit_store"]["stored_row_count"] == 1
    assert result["postcheck"] == {
        "status": "validated",
        "product_mutation_markers_match": True,
        "unexpected_runtime_mutation_count": 0,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }
    assert result["production_mutation_performed"] is False
    assert reviewed_tokens == [PROJECTED_TOKEN]
    assert store_calls == [
        {
            "request_hash": OPERATION_HASH,
            "actor_ref_hash": store_calls[0]["actor_ref_hash"],
            "action": "single_bounded_denial.v1",
        }
    ]
    assert marker_events.count("authority_ledger") == 2
    assert marker_events.count("corpus") == 2
    assert marker_events[-1] == "release"
    serialized = json.dumps(result, sort_keys=True)
    assert PROJECTED_TOKEN not in serialized
    assert "system:serviceaccount:jenkins:neurons-release-production-evidence" not in serialized
    assert "evidence-pod" not in serialized
    assert "fixture-pod-uid" not in serialized


def test_enabled_probe_executes_one_atomic_store_action_and_records_only_its_result(
    tmp_path,
):
    store_calls = []

    def append(**kwargs):
        store_calls.append(kwargs)
        return _store_result(**kwargs)

    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_token_reviewer=lambda _token: _token_review_response(),
        permission_audit_store_append=append,
        permission_audit_product_sentinel_reader=_exact_marker_reader(),
    )

    result = service.brain_permission_sensitive_audit_probe(
        mode="deny_once",
        operation_hash=OPERATION_HASH,
        build_association_hash=BUILD_ASSOCIATION_HASH,
        projected_service_account_token=PROJECTED_TOKEN,
    )

    assert store_calls == [
        {
            "request_hash": OPERATION_HASH,
            "actor_ref_hash": result["audit_events"][0]["actor_ref_hash"],
            "action": "single_bounded_denial.v1",
        }
    ]
    assert result["permission_action_count"] == 1
    assert result["audit_events"] == [
        {
            "schema_version": "runtime_permission_audit_event.v2",
            "event_type": "permission_sensitive_runtime_action",
            "action": "single_bounded_denial.v1",
            "ledger_scope": "production",
            "permission": "denied",
            "authority_write_performed": False,
            "production_mutation_performed": False,
            "actor_ref_hash": result["audit_events"][0]["actor_ref_hash"],
            "request_hash": OPERATION_HASH,
            "protected_values_returned": False,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        }
    ]
    assert len(store_calls) == 1


def test_probe_rejects_allowed_atomic_store_action_result(tmp_path):
    calls = {"store": 0}

    def append(**kwargs):
        calls["store"] += 1
        return {**_store_result(**kwargs), "permission": "allowed"}

    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_token_reviewer=lambda _token: _token_review_response(),
        permission_audit_store_append=append,
        permission_audit_product_sentinel_reader=_exact_marker_reader(),
    )

    with pytest.raises(
        ValueError,
        match="permission audit bounded denial result is malformed",
    ):
        service.brain_permission_sensitive_audit_probe(
            mode="deny_once",
            operation_hash=OPERATION_HASH,
            build_association_hash=BUILD_ASSOCIATION_HASH,
            projected_service_account_token=PROJECTED_TOKEN,
        )

    assert calls == {"store": 1}


def test_probe_dispatch_rejects_extra_arguments_before_reading_token(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="unexpected permission audit probe arguments"):
        dispatch_tool_call(
            {
                "name": "brain_permission_sensitive_audit_probe",
                "arguments": {
                    "mode": "deny_once",
                    "operation_hash": OPERATION_HASH,
                    "build_association_hash": BUILD_ASSOCIATION_HASH,
                    "projected_service_account_token": PROJECTED_TOKEN,
                    "raw_claims": "forbidden",
                },
            },
            service,
        )


def test_probe_rejects_wrong_tokenreview_subject_without_store_or_sentinel(tmp_path):
    calls = {"store": 0, "sentinel": 0}
    review_response = _token_review_response()
    review_response["status"]["user"]["username"] = "system:serviceaccount:default:other"

    def append(**_kwargs):
        calls["store"] += 1
        return {}

    def sentinel():
        calls["sentinel"] += 1
        return _sentinel()

    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_token_reviewer=lambda _token: review_response,
        permission_audit_store_append=append,
        permission_audit_product_sentinel_reader=sentinel,
    )

    with pytest.raises(ValueError, match="permission audit authentication failed"):
        service.brain_permission_sensitive_audit_probe(
            mode="deny_once",
            operation_hash=OPERATION_HASH,
            build_association_hash=BUILD_ASSOCIATION_HASH,
            projected_service_account_token=PROJECTED_TOKEN,
        )

    assert calls == {"store": 0, "sentinel": 0}


def test_disabled_probe_masks_projected_token_in_jsonrpc_error(tmp_path):
    service = _service(tmp_path)

    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "brain_permission_sensitive_audit_probe",
                "arguments": {
                    "mode": "deny_once",
                    "operation_hash": OPERATION_HASH,
                    "build_association_hash": BUILD_ASSOCIATION_HASH,
                    "projected_service_account_token": PROJECTED_TOKEN,
                },
            },
        },
        service,
    )

    serialized = json.dumps(response, sort_keys=True)
    assert response["error"]["message"] == "internal error"
    assert PROJECTED_TOKEN not in serialized


def test_duplicate_operation_reports_zero_action_and_no_new_event(tmp_path):
    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_token_reviewer=lambda _token: _token_review_response(),
        permission_audit_store_append=lambda **kwargs: _store_result(
            append_count=0,
            **kwargs,
        ),
        permission_audit_product_sentinel_reader=_exact_marker_reader(),
    )

    result = service.brain_permission_sensitive_audit_probe(
        mode="deny_once",
        operation_hash=OPERATION_HASH,
        build_association_hash=BUILD_ASSOCIATION_HASH,
        projected_service_account_token=PROJECTED_TOKEN,
    )

    assert result["transport_call_count"] == 1
    assert result["permission_action_count"] == 0
    assert result["audit_events"] == []
    assert result["audit_store"]["append_count"] == 0

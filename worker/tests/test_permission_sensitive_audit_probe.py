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


def _service(tmp_path, **kwargs):
    return KnowledgeSearchService(
        ledger=Ledger(tmp_path / "ledger.sqlite3"),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        **kwargs,
    )


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


def test_enabled_probe_returns_one_sanitized_denial_and_validated_readback(tmp_path):
    reviewed_tokens = []
    store_calls = []
    sentinel_calls = []

    def review(token):
        reviewed_tokens.append(token)
        return _token_review_response()

    def append(**kwargs):
        store_calls.append(kwargs)
        return {
            "status": "recorded",
            "append_count": 1,
            "stored_row_count": 1,
            "read_after_write_status": "validated",
            "request_hash": kwargs["request_hash"],
            "production_mutation_performed": False,
        }

    def sentinel():
        sentinel_calls.append(True)
        return _sentinel()

    service = _service(
        tmp_path,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_token_reviewer=review,
        permission_audit_store_append=append,
        permission_audit_product_sentinel_reader=sentinel,
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
        "product_mutation_sentinels_match": True,
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
    assert len(sentinel_calls) == 2
    serialized = json.dumps(result, sort_keys=True)
    assert PROJECTED_TOKEN not in serialized
    assert "system:serviceaccount:jenkins:neurons-release-production-evidence" not in serialized
    assert "evidence-pod" not in serialized
    assert "fixture-pod-uid" not in serialized


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
        permission_audit_store_append=lambda **kwargs: {
            "status": "recorded",
            "append_count": 0,
            "stored_row_count": 1,
            "read_after_write_status": "validated",
            "request_hash": kwargs["request_hash"],
            "production_mutation_performed": False,
        },
        permission_audit_product_sentinel_reader=_sentinel,
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

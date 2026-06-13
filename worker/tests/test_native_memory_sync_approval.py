from __future__ import annotations

import json

import pytest

from agent_knowledge.session_memory.native_memory_sync_approval import (
    ApprovalError,
    validate_native_memory_sync_approval,
)


SYNC_ARGV = ["agent-knowledge", "native-memory-sync"]


def _write_approval(tmp_path, *, operation="native_memory_sync", memory_id="mem_main", argv=None, approved=True):
    payload = {
        "schema_version": "agent_knowledge_live_approval.v1",
        "operation": operation,
        "operator_approval": {"approved": approved},
        "redaction_required": True,
        "rollback_or_abort_criteria": ["abort on error"],
        "timeout_seconds": 60,
        "target": {"memory_id": memory_id},
        "command": {"argv": argv if argv is not None else SYNC_ARGV},
    }
    path = tmp_path / "approval.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_validator_accepts_matching_memory_id(tmp_path):
    path = _write_approval(tmp_path)

    payload = validate_native_memory_sync_approval(
        path,
        operation="native_memory_sync",
        memory_id="mem_main",
        command_argv=SYNC_ARGV,
    )

    assert payload["operation"] == "native_memory_sync"


def test_validator_accepts_operations_list(tmp_path):
    path = _write_approval(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["operation"] = "other"
    payload["operations"] = ["other", "native_memory_sync"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    accepted = validate_native_memory_sync_approval(
        path,
        operation="native_memory_sync",
        memory_id="mem_main",
        command_argv=SYNC_ARGV,
    )

    assert accepted["operations"] == ["other", "native_memory_sync"]


def test_validator_rejects_memory_id_mismatch(tmp_path):
    path = _write_approval(tmp_path, memory_id="other_mem")

    with pytest.raises(ApprovalError, match="memory_id mismatch"):
        validate_native_memory_sync_approval(
            path,
            operation="native_memory_sync",
            memory_id="mem_main",
            command_argv=SYNC_ARGV,
        )


def test_validator_rejects_operation_mismatch(tmp_path):
    path = _write_approval(tmp_path, operation="something_else")

    with pytest.raises(ApprovalError, match="operation mismatch"):
        validate_native_memory_sync_approval(
            path,
            operation="native_memory_sync",
            memory_id="mem_main",
            command_argv=SYNC_ARGV,
        )


def test_validator_rejects_argv_mismatch(tmp_path):
    path = _write_approval(tmp_path, argv=["agent-knowledge", "native-memory-sync", "--extra"])

    with pytest.raises(ApprovalError, match="argv mismatch"):
        validate_native_memory_sync_approval(
            path,
            operation="native_memory_sync",
            memory_id="mem_main",
            command_argv=SYNC_ARGV,
        )


def test_validator_rejects_unapproved_payload(tmp_path):
    path = _write_approval(tmp_path, approved=False)

    with pytest.raises(ApprovalError, match="not approved"):
        validate_native_memory_sync_approval(
            path,
            operation="native_memory_sync",
            memory_id="mem_main",
            command_argv=SYNC_ARGV,
        )

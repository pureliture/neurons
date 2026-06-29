from __future__ import annotations

import hashlib
import json
import shlex
from typing import Any, Mapping


SCHEMA_VERSION = "agent_knowledge_rag_ingress_m9_product_surface_switch_plan.v1"
APPROVAL_PACKET_SCHEMA = (
    "agent_knowledge_rag_ingress_m9_product_surface_switch_approval_packet.v1"
)
ROLLBACK_MATERIAL_MANIFEST_SCHEMA = (
    "agent_knowledge_rag_ingress_m9_product_surface_switch_rollback_manifest.v1"
)
VALID_SESSION_ENTRY_STATES = {"state_db_recall_configured", "disabled"}


def build_m9_product_surface_switch_plan(
    *,
    dry_run: bool,
    redact_paths: bool,
    reason: str,
    agent_knowledge_command: str,
    project: str,
    ledger_path: str,
    state_db_recall: str,
    dataset_ids: tuple[str, ...],
    mcp_server_name: str = "agent_memory",
    index_url: str = "",
    token_env: str = "",
    policy_proxy_url: str = "",
    allow_private_results: bool = True,
    max_items: int = 8,
    session_entry_hook_state: str = "state_db_recall_configured",
) -> dict[str, object]:
    if not dry_run:
        raise ValueError("product-surface-switch-plan requires --dry-run")
    if not redact_paths:
        raise ValueError("product-surface-switch-plan requires --redact-paths")
    if not reason:
        raise ValueError("product-surface-switch-plan requires --reason")
    if max_items <= 0:
        raise ValueError("max-items must be positive")

    blockers: list[dict[str, object]] = []
    normalized_dataset_ids = _normalize_dataset_ids(dataset_ids)
    if not normalized_dataset_ids:
        _block(blockers, "dataset_id_required")
    if session_entry_hook_state not in VALID_SESSION_ENTRY_STATES:
        _block(blockers, "session_entry_hook_state_invalid")
    redacted_dataset_ids = _redacted_dataset_ids(normalized_dataset_ids)

    redaction_map = {
        "ledger_path": "<redacted:ledger-path>",
        "state_db_recall": "<redacted:state-db-recall>",
        "index_url": "<redacted:index-url>",
        "policy_proxy_url": "<redacted:policy-proxy-url>",
    }
    mcp_argv = _mcp_argv(
        agent_knowledge_command=agent_knowledge_command,
        ledger_path=redaction_map["ledger_path"],
        dataset_ids=redacted_dataset_ids,
        index_url=redaction_map["index_url"] if index_url else "",
        token_env=token_env,
        policy_proxy_url=redaction_map["policy_proxy_url"] if policy_proxy_url else "",
        allow_private_results=allow_private_results,
        state_db_recall=redaction_map["state_db_recall"],
    )
    codex_hook_argv = _codex_context_hook_argv(
        agent_knowledge_command=agent_knowledge_command,
        ledger_path=redaction_map["ledger_path"],
        dataset_ids=redacted_dataset_ids,
        project=project,
        index_url=redaction_map["index_url"] if index_url else "",
        token_env=token_env,
        policy_proxy_url=redaction_map["policy_proxy_url"] if policy_proxy_url else "",
        allow_private_results=allow_private_results,
        max_items=max_items,
        state_db_recall=redaction_map["state_db_recall"],
    )
    session_entry_hook_argv = (
        _session_entry_codex_adapter_argv(
            agent_knowledge_command=agent_knowledge_command,
            ledger_path=redaction_map["ledger_path"],
            dataset_ids=redacted_dataset_ids,
            project=project,
            allow_private_results=allow_private_results,
            max_items=max_items,
            state_db_recall=redaction_map["state_db_recall"],
        )
        if session_entry_hook_state == "state_db_recall_configured"
        else ()
    )
    target = {
        "mcp_state_db_recall_configured": bool(normalized_dataset_ids),
        "codex_hook_state_db_recall_configured": bool(normalized_dataset_ids),
        "session_entry_hook_state": (
            session_entry_hook_state if session_entry_hook_state in VALID_SESSION_ENTRY_STATES else "invalid"
        ),
    }
    status = "blocked" if blockers else "ready_to_approve"
    target_digest = _digest(target)
    mcp_config_toml_plan = {
        "server_name": mcp_server_name,
        "table": f"mcp_servers.{mcp_server_name}",
        "command": agent_knowledge_command,
        "args": list(mcp_argv[1:]),
        "redacted_argv": list(mcp_argv),
        "redacted_command": _shell_join(mcp_argv),
    }
    codex_hooks_json_plan = {
        "write_target": "~/.codex/hooks.json",
        "UserPromptSubmit": [
            {
                "surface": "codex_context_hook",
                "type": "command",
                "command": _shell_join(codex_hook_argv),
                "redacted_argv": list(codex_hook_argv),
                "timeout": 10,
                "statusMessage": "Checking approved context",
            }
        ],
    }
    session_entry_hook_plan = _session_entry_hook_plan_payload(
        session_entry_hook_state=session_entry_hook_state,
        session_entry_hook_argv=session_entry_hook_argv,
    )
    planned_config_digest = _digest(
        {
            "mcp_config_toml_plan": mcp_config_toml_plan,
            "codex_hooks_json_plan": codex_hooks_json_plan,
            "session_entry_hook_plan": session_entry_hook_plan,
        }
    )
    rollback_material_manifest = _rollback_material_manifest(
        session_entry_hook_state=session_entry_hook_state
    )
    postcheck = [
        "regenerate product-surface-evidence from current live MCP config TOML",
        "regenerate product-surface-evidence from current live Codex hooks JSON",
        "run retirement-readiness with accepted product-surface and authority evidence packets",
    ]
    approval_packet = _approval_packet(
        reason=reason,
        status=status,
        target_digest=target_digest,
        planned_config_digest=planned_config_digest,
        rollback_manifest_digest=str(rollback_material_manifest["manifest_digest"]),
        postcheck_digest=_digest({"postcheck": postcheck}),
        blockers=blockers,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "dry_run": True,
        "redacted_paths": True,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "mutation_performed": False,
        "provider_config_mutation_performed": False,
        "network_used": False,
        "target": target,
        "target_digest": target_digest,
        "mcp_config_toml_plan": mcp_config_toml_plan,
        "codex_hooks_json_plan": codex_hooks_json_plan,
        "session_entry_hook_plan": session_entry_hook_plan,
        "planned_config_digest": planned_config_digest,
        "rollback_material_manifest": rollback_material_manifest,
        "approval_packet": approval_packet,
        "approval_required_before_live_mutation": True,
        "approval_packet_requirements": [
            "operator approval must name live MCP config target",
            "operator approval must name live Codex hooks target",
            "operator approval must bind the final unredacted argv bytes",
            "operator approval must preserve rollback bytes before mutation",
            "post-switch evidence must be regenerated from current live config",
        ],
        "postcheck": postcheck,
        "blockers": blockers,
        "blocking_codes": sorted(
            {str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")}
        ),
    }


def _mcp_argv(
    *,
    agent_knowledge_command: str,
    ledger_path: str,
    dataset_ids: tuple[str, ...],
    index_url: str,
    token_env: str,
    policy_proxy_url: str,
    allow_private_results: bool,
    state_db_recall: str,
) -> tuple[str, ...]:
    argv = [agent_knowledge_command, "mcp-stdio", "--ledger", ledger_path]
    for dataset_id in dataset_ids:
        argv.extend(["--dataset-id", dataset_id])
    if index_url:
        argv.extend(["--retired-index-bridge-url", index_url])
    if token_env:
        argv.extend(["--retired-index-bridge-token-env", token_env])
    if policy_proxy_url:
        argv.extend(["--policy-proxy-url", policy_proxy_url])
    if allow_private_results:
        argv.append("--allow-private-results")
    argv.extend(["--state-db-recall", state_db_recall])
    return tuple(argv)


def _codex_context_hook_argv(
    *,
    agent_knowledge_command: str,
    ledger_path: str,
    dataset_ids: tuple[str, ...],
    project: str,
    index_url: str,
    token_env: str,
    policy_proxy_url: str,
    allow_private_results: bool,
    max_items: int,
    state_db_recall: str,
) -> tuple[str, ...]:
    argv = [agent_knowledge_command, "codex-context-hook", "--ledger", ledger_path]
    for dataset_id in dataset_ids:
        argv.extend(["--dataset-id", dataset_id])
    if index_url:
        argv.extend(["--retired-index-bridge-url", index_url])
    if token_env:
        argv.extend(["--retired-index-bridge-token-env", token_env])
    if policy_proxy_url:
        argv.extend(["--policy-proxy-url", policy_proxy_url])
    argv.extend(["--project", project, "--stdin-json"])
    if allow_private_results:
        argv.append("--allow-private-results")
    argv.extend(["--max-items", str(max_items), "--state-db-recall", state_db_recall])
    return tuple(argv)


def _session_entry_codex_adapter_argv(
    *,
    agent_knowledge_command: str,
    ledger_path: str,
    dataset_ids: tuple[str, ...],
    project: str,
    allow_private_results: bool,
    max_items: int,
    state_db_recall: str,
) -> tuple[str, ...]:
    argv = [
        agent_knowledge_command,
        "session-entry-recall",
        "codex-adapter",
        "--ledger",
        ledger_path,
    ]
    for dataset_id in dataset_ids:
        argv.extend(["--dataset-id", dataset_id])
    argv.extend(["--project", project, "--stdin-json", "--recall-enabled"])
    if allow_private_results:
        argv.append("--allow-private-results")
    argv.extend(["--max-items", str(max_items), "--state-db-recall", state_db_recall])
    return tuple(argv)


def _session_entry_hook_plan_payload(
    *,
    session_entry_hook_state: str,
    session_entry_hook_argv: tuple[str, ...],
) -> dict[str, object]:
    if session_entry_hook_state == "disabled":
        return {
            "state": "disabled",
            "action": "remove_or_leave_disabled",
            "redacted_argv": [],
            "command": "",
        }
    return {
        "state": session_entry_hook_state,
        "action": "install_or_update",
        "redacted_argv": list(session_entry_hook_argv),
        "command": _shell_join(session_entry_hook_argv),
    }


def _rollback_material_manifest(*, session_entry_hook_state: str) -> dict[str, object]:
    entries: list[dict[str, object]] = [
        {
            "surface": "mcp_config_toml",
            "target": "<operator-bound:mcp-config-target>",
            "required_before_mutation": True,
            "backup_bytes_digest": "<operator-bound:sha256:pre-switch-mcp-config-bytes>",
            "backup_metadata_digest": "<operator-bound:sha256:pre-switch-mcp-config-metadata>",
        },
        {
            "surface": "codex_hooks_json",
            "target": "<operator-bound:codex-hooks-target>",
            "required_before_mutation": True,
            "backup_bytes_digest": "<operator-bound:sha256:pre-switch-codex-hooks-bytes>",
            "backup_metadata_digest": "<operator-bound:sha256:pre-switch-codex-hooks-metadata>",
        },
    ]
    entries.append(
        {
            "surface": "session_entry_hook",
            "target": "<operator-bound:session-entry-hook-target>",
            "required_before_mutation": (
                session_entry_hook_state == "state_db_recall_configured"
            ),
            "backup_bytes_digest": "<operator-bound:sha256:pre-switch-session-entry-hook-bytes>",
            "backup_metadata_digest": "<operator-bound:sha256:pre-switch-session-entry-hook-metadata>",
            "planned_state": session_entry_hook_state,
        }
    )
    manifest_body = {"entries": entries}
    return {
        "schema_version": ROLLBACK_MATERIAL_MANIFEST_SCHEMA,
        "redacted_paths": True,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "mutation_performed": False,
        "network_used": False,
        "entries": entries,
        "manifest_digest": _digest(manifest_body),
    }


def _approval_packet(
    *,
    reason: str,
    status: str,
    target_digest: str,
    planned_config_digest: str,
    rollback_manifest_digest: str,
    postcheck_digest: str,
    blockers: list[dict[str, object]],
) -> dict[str, object]:
    blocking_codes = sorted(
        {str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")}
    )
    canonical_approval = {
        "operation": "m9_product_surface_config_switch",
        "reason": reason,
        "status": status,
        "target_digest": target_digest,
        "planned_config_digest": planned_config_digest,
        "rollback_manifest_digest": rollback_manifest_digest,
        "postcheck_digest": postcheck_digest,
        "blocking_codes": blocking_codes,
    }
    canonical_approval_json = json.dumps(
        canonical_approval,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": APPROVAL_PACKET_SCHEMA,
        "approval_operation": "m9_product_surface_config_switch",
        "approval_status": status,
        "requires_unredacted_operator_binding": True,
        "canonical_approval": canonical_approval,
        "canonical_approval_json": canonical_approval_json,
        "packet_digest": "sha256:"
        + hashlib.sha256(canonical_approval_json.encode("utf-8")).hexdigest(),
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "mutation_performed": False,
        "network_used": False,
    }


def _normalize_dataset_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _redacted_dataset_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        f"<redacted:dataset-id:{index}>"
        for index, _value in enumerate(values, start=1)
    )


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _shell_join(argv: tuple[str, ...]) -> str:
    return shlex.join(argv)


def _block(blockers: list[dict[str, object]], code: str, **details: object) -> None:
    blocker: dict[str, object] = {"code": code}
    blocker.update(details)
    blockers.append(blocker)

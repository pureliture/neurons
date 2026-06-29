"""Read-only M9 legacy-retirement readiness gate.

This module does not retire, rename, delete, or mutate the legacy ledger. It
only aggregates the evidence that must be true before an operator can approve a
separate live retirement step.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from .product_surface_switch_plan import (
    APPROVAL_PACKET_SCHEMA as PRODUCT_SURFACE_SWITCH_APPROVAL_PACKET_SCHEMA,
    ROLLBACK_MATERIAL_MANIFEST_SCHEMA as PRODUCT_SURFACE_SWITCH_ROLLBACK_MANIFEST_SCHEMA,
    SCHEMA_VERSION as PRODUCT_SURFACE_SWITCH_PLAN_SCHEMA,
)
from .state_shadow_readiness import build_state_shadow_readiness_report


SCHEMA_VERSION = "agent_knowledge_rag_ingress_m9_retirement_readiness.v1"
PRODUCT_SURFACE_EVIDENCE_SCHEMA = (
    "agent_knowledge_rag_ingress_m9_product_surface_evidence.v1"
)
DERIVED_MEMORY_AUTHORITY_EVIDENCE_SCHEMA = (
    "agent_knowledge_rag_ingress_m9_derived_memory_authority_evidence.v1"
)
LEGACY_RETIREMENT_PLAN_SCHEMA = (
    "agent_knowledge_rag_ingress_m9_legacy_retirement_plan.v1"
)
LEGACY_RETIREMENT_APPROVAL_PACKET_SCHEMA = (
    "agent_knowledge_rag_ingress_m9_legacy_retirement_approval_packet.v1"
)
LEGACY_RETIREMENT_ROLLBACK_MANIFEST_SCHEMA = (
    "agent_knowledge_rag_ingress_m9_legacy_retirement_rollback_manifest.v1"
)
M9_CLOSURE_BUNDLE_SCHEMA = "agent_knowledge_rag_ingress_m9_closure_bundle.v1"
M9_CLOSURE_APPROVAL_RECORD_SCHEMA = (
    "agent_knowledge_rag_ingress_m9_closure_approval_record.v1"
)
SUPPORTED_PRODUCT_RESULT_CLASSES = {"conversation_chunk"}
VALID_DERIVED_MEMORY_AUTHORITIES = {"state-db", "renamed-ledger-owned"}
VALID_SESSION_ENTRY_HOOK_STATES = {"state_db_recall_configured", "disabled"}
VALID_LEGACY_RETIREMENT_ACTIONS = {"rename_legacy_ledger", "archive_legacy_ledger_read_only"}


def build_m9_legacy_retirement_readiness_report(
    *,
    state_db_path: Path | str,
    legacy_ledger_path: Path | str,
    queue_root: Path | str,
    dry_run: bool,
    redact_paths: bool,
    mcp_state_db_recall_configured: bool = False,
    codex_hook_state_db_recall_configured: bool = False,
    session_entry_hook_state_db_recall_configured: bool = False,
    session_entry_hook_disabled: bool = False,
    derived_memory_authority: str = "not-evaluated",
    product_surface_result_classes: tuple[str, ...] = ("conversation_chunk",),
    product_surface_evidence: dict | None = None,
    derived_memory_authority_evidence: dict | None = None,
    legacy_disposition: dict | None = None,
    max_runtime_seconds: float = 300.0,
) -> dict[str, object]:
    if not dry_run:
        raise ValueError("retirement-readiness requires --dry-run")
    if not redact_paths:
        raise ValueError("retirement-readiness requires --redact-paths")
    if max_runtime_seconds <= 0:
        raise ValueError("max-runtime-seconds must be positive")
    if session_entry_hook_state_db_recall_configured and session_entry_hook_disabled:
        raise ValueError(
            "session-entry hook cannot be both state-db configured and disabled"
        )

    started = time.monotonic()
    blockers: list[dict[str, object]] = []
    normalized_classes = _normalize_product_surface_classes(product_surface_result_classes)
    product_evidence = _evaluate_product_surface_evidence(product_surface_evidence)
    if not product_evidence["supplied"]:
        _block(blockers, "product_surface_evidence_packet_required_for_live_closure")
    elif product_evidence["status"] == "invalid":
        _block(
            blockers,
            "product_surface_evidence_packet_invalid",
            reason=product_evidence["reason"],
        )
    elif product_evidence["supplied"] and not product_evidence["accepted"]:
        _block(
            blockers,
            "product_surface_evidence_packet_not_accepted",
            reason=product_evidence["reason"],
        )
    elif not product_evidence["accepted_for_live_closure"]:
        _block(
            blockers,
            "product_surface_evidence_packet_not_closure_grade",
            reason=product_evidence["closure_grade_reason"],
        )
    if product_evidence["accepted"]:
        product_target = dict(product_evidence.get("target") or {})
        mcp_state_db_recall_configured = bool(
            product_target.get("mcp_state_db_recall_configured")
        )
        codex_hook_state_db_recall_configured = bool(
            product_target.get("codex_hook_state_db_recall_configured")
        )
        session_entry_hook_state = str(product_target.get("session_entry_hook_state") or "")
        session_entry_hook_state_db_recall_configured = (
            session_entry_hook_state == "state_db_recall_configured"
        )
        session_entry_hook_disabled = session_entry_hook_state == "disabled"
    derived_memory_evidence = _evaluate_derived_memory_authority_evidence(
        derived_memory_authority_evidence
    )
    if not derived_memory_evidence["supplied"]:
        _block(blockers, "derived_memory_authority_evidence_packet_required_for_live_closure")
    elif derived_memory_evidence["status"] == "invalid":
        _block(
            blockers,
            "derived_memory_authority_evidence_packet_invalid",
            reason=derived_memory_evidence["reason"],
        )
    elif derived_memory_evidence["supplied"] and not derived_memory_evidence["accepted"]:
        _block(
            blockers,
            "derived_memory_authority_evidence_packet_not_accepted",
            reason=derived_memory_evidence["reason"],
        )
    if derived_memory_evidence["accepted"]:
        derived_target = dict(derived_memory_evidence.get("target") or {})
        derived_memory_authority = str(
            derived_target.get("derived_memory_authority") or "not-evaluated"
        )

    state_shadow = build_state_shadow_readiness_report(
        state_db_path=state_db_path,
        legacy_ledger_path=legacy_ledger_path,
        queue_root=queue_root,
        dry_run=True,
        redact_paths=True,
        max_runtime_seconds=max_runtime_seconds,
        legacy_disposition=legacy_disposition,
    )
    shadow_blocking_codes = tuple(str(code) for code in state_shadow.get("blocking_codes") or ())
    if shadow_blocking_codes:
        _block(
            blockers,
            "state_shadow_readiness_blocked",
            shadow_blocking_codes=list(shadow_blocking_codes),
        )

    state_db_coverage = _inspect_state_db_product_coverage(
        Path(state_db_path),
        expected_product_result_classes=normalized_classes,
        blockers=blockers,
    )

    product_surface = _build_product_surface_summary(
        mcp_state_db_recall_configured=mcp_state_db_recall_configured,
        codex_hook_state_db_recall_configured=codex_hook_state_db_recall_configured,
        session_entry_hook_state_db_recall_configured=session_entry_hook_state_db_recall_configured,
        session_entry_hook_disabled=session_entry_hook_disabled,
        expected_result_classes=normalized_classes,
        evidence_source="packet" if product_evidence["accepted"] else "flags",
        blockers=blockers,
    )

    derived_memory = _build_derived_memory_summary(
        derived_memory_authority=derived_memory_authority,
        evidence_source="packet" if derived_memory_evidence["accepted"] else "flag",
        blockers=blockers,
    )

    blocking_codes = sorted({str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")})
    readiness_status = (
        "retirement_readiness_blocked"
        if blocking_codes
        else "retirement_ready_for_operator_approval"
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "dry_run": True,
        "redacted_paths": True,
        "network_used": False,
        "mutation_performed": False,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "max_runtime_seconds": max_runtime_seconds,
        "retirement_readiness_status": readiness_status,
        "legacy_retirement_status": (
            "NO-GO" if blocking_codes else "ready_for_operator_approval"
        ),
        "product_surface_closure_status": (
            "blocked" if blocking_codes else "ready_for_operator_approval"
        ),
        "state_shadow_readiness": {
            "schema_version": state_shadow.get("schema_version"),
            "shadow_readiness_status": state_shadow.get("shadow_readiness_status"),
            "production_authority_status": state_shadow.get("production_authority_status"),
            "cutover_status": state_shadow.get("cutover_status"),
            "blocking_codes": list(shadow_blocking_codes),
            "state_db_candidate": state_shadow.get("state_db_candidate"),
            "legacy_ledger": state_shadow.get("legacy_ledger"),
            "source_queue": state_shadow.get("source_queue"),
            "parity_summary": state_shadow.get("parity_summary"),
            "legacy_disposition": state_shadow.get("legacy_disposition"),
        },
        "state_db_product_coverage": state_db_coverage,
        "product_surface": product_surface,
        "product_surface_evidence": product_evidence,
        "derived_memory_authority": derived_memory,
        "derived_memory_authority_evidence": derived_memory_evidence,
        "external_gates": [
            {
                "gate": "operator_approval_for_live_config_switch",
                "status": "not_executed_by_this_command",
                "required_before_live_mutation": True,
            },
            {
                "gate": "operator_approval_for_legacy_ledger_physical_retirement",
                "status": "not_executed_by_this_command",
                "required_before_live_mutation": True,
            },
            {
                "gate": "postcheck_after_legacy_ledger_physical_retirement",
                "status": "not_executed_by_this_command",
                "required_after_live_mutation": True,
            },
        ],
        "blockers": blockers,
        "blocking_codes": blocking_codes,
    }


def build_m9_product_surface_evidence_packet(
    *,
    dry_run: bool,
    redact_paths: bool,
    reason: str,
    mcp_state_db_recall_configured: bool = False,
    codex_hook_state_db_recall_configured: bool = False,
    session_entry_hook_state_db_recall_configured: bool = False,
    session_entry_hook_disabled: bool = False,
    mcp_argv: tuple[str, ...] | None = None,
    codex_hook_argv: tuple[str, ...] | None = None,
    session_entry_hook_argv: tuple[str, ...] | None = None,
    mcp_argvs: tuple[tuple[str, ...], ...] = (),
    codex_hook_argvs: tuple[tuple[str, ...], ...] = (),
    session_entry_hook_argvs: tuple[tuple[str, ...], ...] = (),
    mcp_evidence_mode: str | None = None,
    codex_hook_evidence_mode: str | None = None,
    session_entry_hook_evidence_mode: str | None = None,
) -> dict[str, object]:
    if not dry_run:
        raise ValueError("product-surface-evidence requires --dry-run")
    if not redact_paths:
        raise ValueError("product-surface-evidence requires --redact-paths")
    if not reason:
        raise ValueError("product-surface-evidence requires --reason")
    if session_entry_hook_argv is not None and session_entry_hook_disabled:
        raise ValueError("session-entry hook argv cannot combine with disabled state")
    if session_entry_hook_state_db_recall_configured and session_entry_hook_disabled:
        raise ValueError(
            "session-entry hook cannot be both state-db configured and disabled"
        )

    mcp_candidates = _argv_candidates(mcp_argv, mcp_argvs)
    codex_hook_candidates = _argv_candidates(codex_hook_argv, codex_hook_argvs)
    session_entry_hook_candidates = _argv_candidates(
        session_entry_hook_argv,
        session_entry_hook_argvs,
    )
    mcp_mode = mcp_evidence_mode or ("argv" if mcp_candidates else "flag")
    codex_hook_mode = codex_hook_evidence_mode or (
        "argv" if codex_hook_candidates else "flag"
    )
    session_entry_mode = (
        session_entry_hook_evidence_mode
        if session_entry_hook_evidence_mode
        else "argv"
        if session_entry_hook_candidates
        else "disabled_flag"
        if session_entry_hook_disabled
        else "flag"
    )
    if mcp_candidates:
        mcp_state_db_recall_configured = _any_argv_proves_state_db_recall(
            mcp_candidates,
            required_tokens=("mcp-stdio",),
        )
    if codex_hook_candidates:
        codex_hook_state_db_recall_configured = _any_argv_proves_state_db_recall(
            codex_hook_candidates,
            required_tokens=("codex-context-hook",),
        )
    if session_entry_hook_candidates:
        session_entry_hook_state_db_recall_configured = _any_argv_proves_state_db_recall(
            session_entry_hook_candidates,
            required_tokens=("session-entry-recall", "codex-adapter"),
        )

    session_entry_hook_state = (
        "state_db_recall_configured"
        if session_entry_hook_state_db_recall_configured
        else "disabled"
        if session_entry_hook_disabled
        else "unresolved"
    )
    target = {
        "mcp_state_db_recall_configured": bool(mcp_state_db_recall_configured),
        "codex_hook_state_db_recall_configured": bool(codex_hook_state_db_recall_configured),
        "session_entry_hook_state": session_entry_hook_state,
    }
    input_modes = {
        "mcp": mcp_mode,
        "codex_hook": codex_hook_mode,
        "session_entry_hook": session_entry_mode,
    }
    closure_evidence = _product_surface_closure_evidence(
        target=target,
        input_modes=input_modes,
    )
    blockers: list[dict[str, object]] = []
    if not target["mcp_state_db_recall_configured"]:
        _block(blockers, "live_mcp_state_db_recall_not_configured")
    if not target["codex_hook_state_db_recall_configured"]:
        _block(blockers, "live_codex_hook_state_db_recall_not_configured")
    if session_entry_hook_state == "unresolved":
        _block(blockers, "live_session_entry_hook_state_unresolved")

    return {
        "schema_version": PRODUCT_SURFACE_EVIDENCE_SCHEMA,
        "evidence_status": "blocked" if blockers else "accepted",
        "reason": reason,
        "redacted_paths": True,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "evidence_input_modes": input_modes,
        "closure_evidence": closure_evidence,
        "target": target,
        "target_digest": _digest(target),
        "blockers": blockers,
        "blocking_codes": sorted(
            {str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")}
        ),
    }


def build_m9_derived_memory_authority_evidence_packet(
    *,
    dry_run: bool,
    redact_paths: bool,
    reason: str,
    authority: str,
    reviewed_authority_disposition: bool = False,
) -> dict[str, object]:
    if not dry_run:
        raise ValueError("derived-memory-authority-evidence requires --dry-run")
    if not redact_paths:
        raise ValueError("derived-memory-authority-evidence requires --redact-paths")
    if not reason:
        raise ValueError("derived-memory-authority-evidence requires --reason")
    normalized = str(authority or "not-evaluated")
    target = {
        "derived_memory_authority": normalized,
        "reviewed_authority_disposition": bool(reviewed_authority_disposition),
    }
    blockers: list[dict[str, object]] = []
    if normalized not in VALID_DERIVED_MEMORY_AUTHORITIES:
        _block(blockers, "derived_memory_authority_not_dispositioned")
    if not reviewed_authority_disposition:
        _block(blockers, "derived_memory_authority_not_reviewed")
    return {
        "schema_version": DERIVED_MEMORY_AUTHORITY_EVIDENCE_SCHEMA,
        "evidence_status": "blocked" if blockers else "accepted",
        "reason": reason,
        "redacted_paths": True,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "target": target,
        "target_digest": _digest(target),
        "blockers": blockers,
        "blocking_codes": sorted(
            {str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")}
        ),
    }


def build_m9_legacy_retirement_plan(
    *,
    dry_run: bool,
    redact_paths: bool,
    reason: str,
    readiness_report: dict | None,
    retirement_action: str = "rename_legacy_ledger",
) -> dict[str, object]:
    if not dry_run:
        raise ValueError("legacy-retirement-plan requires --dry-run")
    if not redact_paths:
        raise ValueError("legacy-retirement-plan requires --redact-paths")
    if not reason:
        raise ValueError("legacy-retirement-plan requires --reason")

    blockers: list[dict[str, object]] = []
    if retirement_action not in VALID_LEGACY_RETIREMENT_ACTIONS:
        _block(blockers, "legacy_retirement_action_invalid")
    readiness = _evaluate_retirement_readiness_report(readiness_report)
    if readiness["status"] == "invalid":
        _block(
            blockers,
            "retirement_readiness_report_invalid",
            reason=readiness["reason"],
        )
    elif not readiness["ready"]:
        _block(
            blockers,
            "retirement_readiness_report_not_ready",
            readiness_status=readiness["retirement_readiness_status"],
            legacy_retirement_status=readiness["legacy_retirement_status"],
            product_surface_closure_status=readiness["product_surface_closure_status"],
            blocking_codes=readiness["blocking_codes"],
        )

    target = {
        "retirement_action": retirement_action,
        "readiness_digest": readiness["readiness_digest"],
        "readiness_status": readiness["retirement_readiness_status"],
        "legacy_retirement_status": readiness["legacy_retirement_status"],
        "product_surface_closure_status": readiness["product_surface_closure_status"],
    }
    status = "blocked" if blockers else "ready_to_approve"
    target_digest = _digest(target)
    rollback_manifest = _legacy_retirement_rollback_manifest(
        retirement_action=retirement_action
    )
    postcheck = [
        "run retirement-readiness against current state DB, legacy ledger target, and source queue",
        "confirm product-surface evidence packet is accepted from current live config",
        "confirm derived-memory authority evidence remains accepted",
        "confirm no raw ids, paths, transcript content, or tokens are printed",
    ]
    approval_packet = _legacy_retirement_approval_packet(
        reason=reason,
        status=status,
        retirement_action=retirement_action,
        target_digest=target_digest,
        readiness_digest=str(readiness["readiness_digest"]),
        rollback_manifest_digest=str(rollback_manifest["manifest_digest"]),
        postcheck_digest=_digest({"postcheck": postcheck}),
        blockers=blockers,
    )
    return {
        "schema_version": LEGACY_RETIREMENT_PLAN_SCHEMA,
        "status": status,
        "reason": reason,
        "dry_run": True,
        "redacted_paths": True,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "mutation_performed": False,
        "network_used": False,
        "target": target,
        "target_digest": target_digest,
        "readiness_report": readiness,
        "planned_action": {
            "action": retirement_action,
            "target": "<redacted:legacy-ledger>",
            "mode": "operator_approval_required",
            "live_mutation_performed_by_this_command": False,
        },
        "rollback_material_manifest": rollback_manifest,
        "approval_packet": approval_packet,
        "approval_required_before_live_mutation": True,
        "approval_packet_requirements": [
            "operator approval must bind final unredacted legacy ledger path",
            "operator approval must bind current readiness report digest",
            "operator approval must preserve rollback bytes and file metadata before mutation",
            "operator approval must prove no legacy ledger write process remains configured as product authority",
            "operator approval must define abort criteria and postcheck timeout",
        ],
        "rollback": [
            "restore preserved legacy ledger bytes and file metadata",
            "restore previous live MCP/Codex/session-entry config bytes if switch rollback is required",
            "rerun product-surface evidence generation from restored live config",
            "rerun retirement-readiness and confirm it returns blocked rather than partially retired",
        ],
        "postcheck": postcheck,
        "blockers": blockers,
        "blocking_codes": sorted(
            {str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")}
        ),
    }


def build_m9_closure_bundle(
    *,
    dry_run: bool,
    redact_paths: bool,
    reason: str,
    product_surface_evidence: dict | None,
    derived_memory_authority_evidence: dict | None,
    product_surface_switch_plan: dict | None,
    retirement_readiness_report: dict | None,
    legacy_retirement_plan: dict | None,
) -> dict[str, object]:
    if not dry_run:
        raise ValueError("closure-bundle requires --dry-run")
    if not redact_paths:
        raise ValueError("closure-bundle requires --redact-paths")
    if not reason:
        raise ValueError("closure-bundle requires --reason")

    blockers: list[dict[str, object]] = []
    product_evidence = _evaluate_product_surface_evidence(product_surface_evidence)
    if not product_evidence["supplied"]:
        _block(blockers, "product_surface_evidence_packet_required")
    elif product_evidence["status"] == "invalid":
        _block(blockers, "product_surface_evidence_packet_invalid", reason=product_evidence["reason"])
    elif not product_evidence["accepted"]:
        _block(blockers, "product_surface_evidence_packet_not_accepted", reason=product_evidence["reason"])
    elif not product_evidence["accepted_for_live_closure"]:
        _block(
            blockers,
            "product_surface_evidence_packet_not_closure_grade",
            reason=product_evidence["closure_grade_reason"],
        )

    derived_evidence = _evaluate_derived_memory_authority_evidence(
        derived_memory_authority_evidence
    )
    if not derived_evidence["supplied"]:
        _block(blockers, "derived_memory_authority_evidence_packet_required")
    elif derived_evidence["status"] == "invalid":
        _block(
            blockers,
            "derived_memory_authority_evidence_packet_invalid",
            reason=derived_evidence["reason"],
        )
    elif not derived_evidence["accepted"]:
        _block(
            blockers,
            "derived_memory_authority_evidence_packet_not_accepted",
            reason=derived_evidence["reason"],
        )
    elif not derived_evidence["accepted_for_live_closure"]:
        _block(blockers, "derived_memory_authority_evidence_packet_not_closure_grade")

    switch_plan = _evaluate_product_surface_switch_plan(product_surface_switch_plan)
    if switch_plan["status"] == "invalid":
        _block(blockers, "product_surface_switch_plan_invalid", reason=switch_plan["reason"])
    elif not switch_plan["ready"]:
        _block(blockers, "product_surface_switch_plan_not_ready", reason=switch_plan["reason"])

    readiness = _evaluate_retirement_readiness_report(retirement_readiness_report)
    if readiness["status"] == "invalid":
        _block(blockers, "retirement_readiness_report_invalid", reason=readiness["reason"])
    elif not readiness["ready"]:
        _block(
            blockers,
            "retirement_readiness_report_not_ready",
            readiness_status=readiness["retirement_readiness_status"],
            blocking_codes=readiness["blocking_codes"],
        )

    legacy_plan = _evaluate_legacy_retirement_plan(
        legacy_retirement_plan,
        expected_readiness_digest=str(readiness["readiness_digest"]),
    )
    if legacy_plan["status"] == "invalid":
        _block(blockers, "legacy_retirement_plan_invalid", reason=legacy_plan["reason"])
    elif not legacy_plan["ready"]:
        _block(blockers, "legacy_retirement_plan_not_ready", reason=legacy_plan["reason"])

    product_digest = str(product_evidence["target_digest"])
    if switch_plan["target_digest"] and switch_plan["target_digest"] != product_digest:
        _block(
            blockers,
            "product_surface_switch_target_digest_mismatch",
            product_surface_evidence_target_digest=product_digest,
            switch_plan_target_digest=switch_plan["target_digest"],
        )
    readiness_product = (
        (retirement_readiness_report or {}).get("product_surface_evidence") or {}
    )
    if (
        product_digest
        and readiness_product.get("target_digest")
        and readiness_product.get("target_digest") != product_digest
    ):
        _block(
            blockers,
            "retirement_readiness_product_surface_digest_mismatch",
            product_surface_evidence_target_digest=product_digest,
            readiness_product_surface_target_digest=readiness_product.get("target_digest"),
        )
    readiness_derived = (
        (retirement_readiness_report or {}).get("derived_memory_authority_evidence")
        or {}
    )
    derived_digest = str(derived_evidence["target_digest"])
    if (
        derived_digest
        and readiness_derived.get("target_digest")
        and readiness_derived.get("target_digest") != derived_digest
    ):
        _block(
            blockers,
            "retirement_readiness_derived_authority_digest_mismatch",
            derived_memory_authority_target_digest=derived_digest,
            readiness_derived_authority_target_digest=readiness_derived.get("target_digest"),
        )

    bundle_target = {
        "product_surface_evidence_target_digest": product_digest,
        "derived_memory_authority_target_digest": derived_digest,
        "product_surface_switch_target_digest": str(switch_plan["target_digest"]),
        "product_surface_switch_approval_packet_digest": str(
            switch_plan["approval_packet_digest"]
        ),
        "retirement_readiness_digest": str(readiness["readiness_digest"]),
        "legacy_retirement_target_digest": str(legacy_plan["target_digest"]),
        "legacy_retirement_approval_packet_digest": str(
            legacy_plan["approval_packet_digest"]
        ),
    }
    status = "blocked" if blockers else "ready_to_approve"
    return {
        "schema_version": M9_CLOSURE_BUNDLE_SCHEMA,
        "status": status,
        "reason": reason,
        "dry_run": True,
        "redacted_paths": True,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "mutation_performed": False,
        "provider_config_mutation_performed": False,
        "legacy_retirement_mutation_performed": False,
        "network_used": False,
        "target": bundle_target,
        "target_digest": _digest(bundle_target),
        "chain": {
            "product_surface_evidence": product_evidence,
            "derived_memory_authority_evidence": derived_evidence,
            "product_surface_switch_plan": switch_plan,
            "retirement_readiness_report": readiness,
            "legacy_retirement_plan": legacy_plan,
        },
        "approval_required_before_live_mutation": True,
        "postcheck_required_after_live_mutation": True,
        "blockers": blockers,
        "blocking_codes": sorted(
            {str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")}
        ),
    }


def build_m9_closure_approval_record(
    *,
    dry_run: bool,
    redact_paths: bool,
    reason: str,
    closure_bundle: dict | None,
    operator_label: str = "operator",
) -> dict[str, object]:
    if not dry_run:
        raise ValueError("closure-approval-record requires --dry-run")
    if not redact_paths:
        raise ValueError("closure-approval-record requires --redact-paths")
    if not reason:
        raise ValueError("closure-approval-record requires --reason")

    blockers: list[dict[str, object]] = []
    bundle = _evaluate_closure_bundle(closure_bundle)
    if bundle["status"] == "invalid":
        _block(blockers, "closure_bundle_invalid", reason=bundle["reason"])
    elif not bundle["ready"]:
        _block(
            blockers,
            "closure_bundle_not_ready",
            bundle_status=bundle["bundle_status"],
            blocking_codes=bundle["blocking_codes"],
        )

    canonical_approval = {
        "operation": "m9_product_surface_closure_and_legacy_retirement",
        "reason": reason,
        "operator_label": str(operator_label or "operator"),
        "closure_bundle_digest": bundle["closure_bundle_digest"],
        "product_surface_switch_approval_packet_digest": bundle[
            "product_surface_switch_approval_packet_digest"
        ],
        "legacy_retirement_approval_packet_digest": bundle[
            "legacy_retirement_approval_packet_digest"
        ],
        "blocking_codes": sorted(
            {str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")}
        ),
    }
    canonical_approval_json = json.dumps(
        canonical_approval,
        sort_keys=True,
        separators=(",", ":"),
    )
    status = "blocked" if blockers else "ready_to_approve"
    return {
        "schema_version": M9_CLOSURE_APPROVAL_RECORD_SCHEMA,
        "status": status,
        "reason": reason,
        "dry_run": True,
        "redacted_paths": True,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "mutation_performed": False,
        "provider_config_mutation_performed": False,
        "legacy_retirement_mutation_performed": False,
        "network_used": False,
        "approval_operation": "m9_product_surface_closure_and_legacy_retirement",
        "operator_label": str(operator_label or "operator"),
        "requires_unredacted_operator_binding": True,
        "approval_required_before_live_mutation": True,
        "closure_bundle": bundle,
        "canonical_approval": canonical_approval,
        "canonical_approval_json": canonical_approval_json,
        "approval_record_digest": "sha256:"
        + hashlib.sha256(canonical_approval_json.encode("utf-8")).hexdigest(),
        "required_unredacted_bindings": [
            "live MCP config target and exact post-switch bytes",
            "live Codex hooks target and exact post-switch bytes",
            "session-entry hook target or reviewed disabled disposition",
            "pre-switch product config backup bytes and metadata digests",
            "legacy ledger target path and pre-retirement bytes/metadata digests",
            "legacy ledger WAL/sidecar backup bytes and metadata digests",
            "post-switch product-surface evidence packet generated from current live config",
            "post-retirement readiness report and closure-bundle rerun",
        ],
        "apply_sequence": [
            {
                "phase": "product_surface_config_switch",
                "approval_packet_digest": bundle[
                    "product_surface_switch_approval_packet_digest"
                ],
                "mutation_performed_by_this_command": False,
                "postcheck": "regenerate closure-grade product-surface evidence from current live config",
            },
            {
                "phase": "retirement_readiness_refresh",
                "mutation_performed_by_this_command": False,
                "postcheck": "rerun retirement-readiness with current live evidence packets",
            },
            {
                "phase": "legacy_ledger_physical_retirement",
                "approval_packet_digest": bundle[
                    "legacy_retirement_approval_packet_digest"
                ],
                "mutation_performed_by_this_command": False,
                "postcheck": "rerun closure-bundle and confirm ready_to_approve before final record",
            },
        ],
        "blockers": blockers,
        "blocking_codes": sorted(
            {str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")}
        ),
    }


def _legacy_retirement_rollback_manifest(*, retirement_action: str) -> dict[str, object]:
    entries = [
        {
            "surface": "legacy_ledger_bytes",
            "target": "<operator-bound:legacy-ledger-target>",
            "required_before_mutation": True,
            "backup_bytes_digest": "<operator-bound:sha256:pre-retirement-ledger-bytes>",
            "backup_metadata_digest": "<operator-bound:sha256:pre-retirement-ledger-metadata>",
            "planned_action": retirement_action,
        },
        {
            "surface": "legacy_ledger_wal_and_sidecars",
            "target": "<operator-bound:legacy-ledger-sidecars>",
            "required_before_mutation": True,
            "backup_bytes_digest": "<operator-bound:sha256:pre-retirement-ledger-sidecar-bytes>",
            "backup_metadata_digest": "<operator-bound:sha256:pre-retirement-ledger-sidecar-metadata>",
            "planned_action": retirement_action,
        },
        {
            "surface": "product_surface_config_bytes",
            "target": "<operator-bound:product-surface-config-targets>",
            "required_before_mutation": True,
            "backup_bytes_digest": "<operator-bound:sha256:pre-retirement-product-config-bytes>",
            "backup_metadata_digest": "<operator-bound:sha256:pre-retirement-product-config-metadata>",
            "planned_action": "preserve_for_cross_surface_rollback",
        },
    ]
    manifest_body = {
        "retirement_action": retirement_action,
        "entries": entries,
    }
    return {
        "schema_version": LEGACY_RETIREMENT_ROLLBACK_MANIFEST_SCHEMA,
        "redacted_paths": True,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "mutation_performed": False,
        "network_used": False,
        "retirement_action": retirement_action,
        "entries": entries,
        "manifest_digest": _digest(manifest_body),
    }


def _legacy_retirement_approval_packet(
    *,
    reason: str,
    status: str,
    retirement_action: str,
    target_digest: str,
    readiness_digest: str,
    rollback_manifest_digest: str,
    postcheck_digest: str,
    blockers: list[dict[str, object]],
) -> dict[str, object]:
    blocking_codes = sorted(
        {str(blocker.get("code") or "") for blocker in blockers if blocker.get("code")}
    )
    canonical_approval = {
        "operation": "m9_legacy_ledger_physical_retirement",
        "reason": reason,
        "status": status,
        "retirement_action": retirement_action,
        "target_digest": target_digest,
        "readiness_digest": readiness_digest,
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
        "schema_version": LEGACY_RETIREMENT_APPROVAL_PACKET_SCHEMA,
        "approval_operation": "m9_legacy_ledger_physical_retirement",
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


def _evaluate_product_surface_switch_plan(plan: dict | None) -> dict[str, object]:
    if plan is None:
        return _invalid_switch_plan("plan_not_supplied")
    if not isinstance(plan, dict):
        return _invalid_switch_plan("plan_not_object")
    if plan.get("schema_version") != PRODUCT_SURFACE_SWITCH_PLAN_SCHEMA:
        return _invalid_switch_plan("schema_version_mismatch")
    for key in ("dry_run", "redacted_paths"):
        if plan.get(key) is not True:
            return _invalid_switch_plan(f"{key}_required")
    for key in (
        "raw_paths_printed",
        "raw_ids_printed",
        "raw_content_printed",
        "mutation_performed",
        "provider_config_mutation_performed",
        "network_used",
    ):
        if plan.get(key) is not False:
            return _invalid_switch_plan(f"{key}_required_false")
    status = str(plan.get("status") or "")
    if status not in {"ready_to_approve", "blocked"}:
        return _invalid_switch_plan("status_invalid")
    target = plan.get("target")
    if not isinstance(target, dict):
        return _invalid_switch_plan("target_not_object")
    normalized_target = {
        "mcp_state_db_recall_configured": target.get("mcp_state_db_recall_configured")
        is True,
        "codex_hook_state_db_recall_configured": target.get(
            "codex_hook_state_db_recall_configured"
        )
        is True,
        "session_entry_hook_state": str(target.get("session_entry_hook_state") or ""),
    }
    target_digest = _digest(normalized_target)
    if plan.get("target_digest") != target_digest:
        return _invalid_switch_plan("target_digest_mismatch", target_digest=target_digest)

    planned_config = {
        "mcp_config_toml_plan": plan.get("mcp_config_toml_plan"),
        "codex_hooks_json_plan": plan.get("codex_hooks_json_plan"),
        "session_entry_hook_plan": plan.get("session_entry_hook_plan"),
    }
    planned_config_digest = _digest(planned_config)
    if plan.get("planned_config_digest") != planned_config_digest:
        return _invalid_switch_plan(
            "planned_config_digest_mismatch",
            target_digest=target_digest,
            planned_config_digest=planned_config_digest,
        )

    rollback_manifest = plan.get("rollback_material_manifest")
    if not isinstance(rollback_manifest, dict):
        return _invalid_switch_plan("rollback_manifest_not_object")
    if (
        rollback_manifest.get("schema_version")
        != PRODUCT_SURFACE_SWITCH_ROLLBACK_MANIFEST_SCHEMA
    ):
        return _invalid_switch_plan("rollback_manifest_schema_mismatch")
    rollback_digest = _digest({"entries": rollback_manifest.get("entries") or []})
    if rollback_manifest.get("manifest_digest") != rollback_digest:
        return _invalid_switch_plan(
            "rollback_manifest_digest_mismatch",
            target_digest=target_digest,
            planned_config_digest=planned_config_digest,
        )

    approval = plan.get("approval_packet")
    approval_digest = _validate_approval_packet(
        approval,
        schema_version=PRODUCT_SURFACE_SWITCH_APPROVAL_PACKET_SCHEMA,
        operation="m9_product_surface_config_switch",
        status=status,
        expected_values={
            "target_digest": target_digest,
            "planned_config_digest": planned_config_digest,
            "rollback_manifest_digest": rollback_digest,
        },
    )
    if not approval_digest["valid"]:
        return _invalid_switch_plan(
            f"approval_packet_{approval_digest['reason']}",
            target_digest=target_digest,
            planned_config_digest=planned_config_digest,
            rollback_manifest_digest=rollback_digest,
        )

    ready = status == "ready_to_approve"
    return {
        "supplied": True,
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "reason": "" if ready else "switch_plan_blocked",
        "schema_valid": True,
        "target": normalized_target,
        "target_digest": target_digest,
        "planned_config_digest": planned_config_digest,
        "rollback_manifest_digest": rollback_digest,
        "approval_packet_digest": approval_digest["packet_digest"],
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _invalid_switch_plan(
    reason: str,
    *,
    target_digest: str = "",
    planned_config_digest: str = "",
    rollback_manifest_digest: str = "",
) -> dict[str, object]:
    return {
        "supplied": True,
        "status": "invalid",
        "ready": False,
        "reason": reason,
        "schema_valid": False,
        "target": {},
        "target_digest": target_digest,
        "planned_config_digest": planned_config_digest,
        "rollback_manifest_digest": rollback_manifest_digest,
        "approval_packet_digest": "",
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _evaluate_legacy_retirement_plan(
    plan: dict | None,
    *,
    expected_readiness_digest: str,
) -> dict[str, object]:
    if plan is None:
        return _invalid_legacy_plan("plan_not_supplied")
    if not isinstance(plan, dict):
        return _invalid_legacy_plan("plan_not_object")
    if plan.get("schema_version") != LEGACY_RETIREMENT_PLAN_SCHEMA:
        return _invalid_legacy_plan("schema_version_mismatch")
    for key in ("dry_run", "redacted_paths"):
        if plan.get(key) is not True:
            return _invalid_legacy_plan(f"{key}_required")
    for key in (
        "raw_paths_printed",
        "raw_ids_printed",
        "raw_content_printed",
        "mutation_performed",
        "network_used",
    ):
        if plan.get(key) is not False:
            return _invalid_legacy_plan(f"{key}_required_false")
    status = str(plan.get("status") or "")
    if status not in {"ready_to_approve", "blocked"}:
        return _invalid_legacy_plan("status_invalid")
    target = plan.get("target")
    if not isinstance(target, dict):
        return _invalid_legacy_plan("target_not_object")
    normalized_target = {
        "retirement_action": str(target.get("retirement_action") or ""),
        "readiness_digest": str(target.get("readiness_digest") or ""),
        "readiness_status": str(target.get("readiness_status") or ""),
        "legacy_retirement_status": str(target.get("legacy_retirement_status") or ""),
        "product_surface_closure_status": str(
            target.get("product_surface_closure_status") or ""
        ),
    }
    target_digest = _digest(normalized_target)
    if plan.get("target_digest") != target_digest:
        return _invalid_legacy_plan("target_digest_mismatch", target_digest=target_digest)
    if normalized_target["readiness_digest"] != expected_readiness_digest:
        return _invalid_legacy_plan(
            "readiness_digest_mismatch",
            target_digest=target_digest,
            readiness_digest=normalized_target["readiness_digest"],
        )

    rollback_manifest = plan.get("rollback_material_manifest")
    if not isinstance(rollback_manifest, dict):
        return _invalid_legacy_plan("rollback_manifest_not_object")
    if (
        rollback_manifest.get("schema_version")
        != LEGACY_RETIREMENT_ROLLBACK_MANIFEST_SCHEMA
    ):
        return _invalid_legacy_plan("rollback_manifest_schema_mismatch")
    rollback_digest = _digest(
        {
            "retirement_action": rollback_manifest.get("retirement_action"),
            "entries": rollback_manifest.get("entries") or [],
        }
    )
    if rollback_manifest.get("manifest_digest") != rollback_digest:
        return _invalid_legacy_plan(
            "rollback_manifest_digest_mismatch",
            target_digest=target_digest,
            readiness_digest=normalized_target["readiness_digest"],
        )

    approval = plan.get("approval_packet")
    approval_digest = _validate_approval_packet(
        approval,
        schema_version=LEGACY_RETIREMENT_APPROVAL_PACKET_SCHEMA,
        operation="m9_legacy_ledger_physical_retirement",
        status=status,
        expected_values={
            "retirement_action": normalized_target["retirement_action"],
            "target_digest": target_digest,
            "readiness_digest": normalized_target["readiness_digest"],
            "rollback_manifest_digest": rollback_digest,
        },
    )
    if not approval_digest["valid"]:
        return _invalid_legacy_plan(
            f"approval_packet_{approval_digest['reason']}",
            target_digest=target_digest,
            readiness_digest=normalized_target["readiness_digest"],
            rollback_manifest_digest=rollback_digest,
        )

    ready = status == "ready_to_approve"
    return {
        "supplied": True,
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "reason": "" if ready else "legacy_plan_blocked",
        "schema_valid": True,
        "target": normalized_target,
        "target_digest": target_digest,
        "readiness_digest": normalized_target["readiness_digest"],
        "rollback_manifest_digest": rollback_digest,
        "approval_packet_digest": approval_digest["packet_digest"],
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _invalid_legacy_plan(
    reason: str,
    *,
    target_digest: str = "",
    readiness_digest: str = "",
    rollback_manifest_digest: str = "",
) -> dict[str, object]:
    return {
        "supplied": True,
        "status": "invalid",
        "ready": False,
        "reason": reason,
        "schema_valid": False,
        "target": {},
        "target_digest": target_digest,
        "readiness_digest": readiness_digest,
        "rollback_manifest_digest": rollback_manifest_digest,
        "approval_packet_digest": "",
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _evaluate_closure_bundle(bundle: dict | None) -> dict[str, object]:
    if bundle is None:
        return _invalid_closure_bundle("bundle_not_supplied")
    if not isinstance(bundle, dict):
        return _invalid_closure_bundle("bundle_not_object")
    if bundle.get("schema_version") != M9_CLOSURE_BUNDLE_SCHEMA:
        return _invalid_closure_bundle("schema_version_mismatch")
    for key in ("dry_run", "redacted_paths"):
        if bundle.get(key) is not True:
            return _invalid_closure_bundle(f"{key}_required")
    for key in (
        "raw_paths_printed",
        "raw_ids_printed",
        "raw_content_printed",
        "mutation_performed",
        "provider_config_mutation_performed",
        "legacy_retirement_mutation_performed",
        "network_used",
    ):
        if bundle.get(key) is not False:
            return _invalid_closure_bundle(f"{key}_required_false")
    status = str(bundle.get("status") or "")
    if status not in {"ready_to_approve", "blocked"}:
        return _invalid_closure_bundle("status_invalid")
    target = bundle.get("target")
    if not isinstance(target, dict):
        return _invalid_closure_bundle("target_not_object")
    normalized_target = {
        "product_surface_evidence_target_digest": str(
            target.get("product_surface_evidence_target_digest") or ""
        ),
        "derived_memory_authority_target_digest": str(
            target.get("derived_memory_authority_target_digest") or ""
        ),
        "product_surface_switch_target_digest": str(
            target.get("product_surface_switch_target_digest") or ""
        ),
        "product_surface_switch_approval_packet_digest": str(
            target.get("product_surface_switch_approval_packet_digest") or ""
        ),
        "retirement_readiness_digest": str(
            target.get("retirement_readiness_digest") or ""
        ),
        "legacy_retirement_target_digest": str(
            target.get("legacy_retirement_target_digest") or ""
        ),
        "legacy_retirement_approval_packet_digest": str(
            target.get("legacy_retirement_approval_packet_digest") or ""
        ),
    }
    target_digest = _digest(normalized_target)
    if bundle.get("target_digest") != target_digest:
        return _invalid_closure_bundle(
            "target_digest_mismatch",
            target_digest=target_digest,
            target=normalized_target,
        )
    blocking_codes = [str(code) for code in bundle.get("blocking_codes") or []]
    chain = bundle.get("chain")
    if not isinstance(chain, dict):
        return _invalid_closure_bundle(
            "chain_not_object",
            target_digest=target_digest,
            target=normalized_target,
        )
    expected_chain_statuses = {
        "product_surface_evidence": "accepted",
        "derived_memory_authority_evidence": "accepted",
        "product_surface_switch_plan": "ready",
        "retirement_readiness_report": "ready",
        "legacy_retirement_plan": "ready",
    }
    chain_statuses: dict[str, str] = {}
    for key, expected_status in expected_chain_statuses.items():
        item = chain.get(key)
        if not isinstance(item, dict):
            return _invalid_closure_bundle(
                f"{key}_not_object",
                target_digest=target_digest,
                target=normalized_target,
            )
        actual_status = str(item.get("status") or "")
        chain_statuses[key] = actual_status
        if status == "ready_to_approve" and actual_status != expected_status:
            return _invalid_closure_bundle(
                f"{key}_status_mismatch",
                target_digest=target_digest,
                target=normalized_target,
            )
    ready = status == "ready_to_approve" and not blocking_codes
    return {
        "supplied": True,
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "reason": "" if ready else "closure_bundle_blocked",
        "schema_valid": True,
        "bundle_status": status,
        "blocking_codes": blocking_codes,
        "target": normalized_target,
        "target_digest": target_digest,
        "closure_bundle_digest": target_digest,
        "chain_statuses": chain_statuses,
        "product_surface_switch_approval_packet_digest": normalized_target[
            "product_surface_switch_approval_packet_digest"
        ],
        "legacy_retirement_approval_packet_digest": normalized_target[
            "legacy_retirement_approval_packet_digest"
        ],
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _invalid_closure_bundle(
    reason: str,
    *,
    target_digest: str = "",
    target: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "supplied": True,
        "status": "invalid",
        "ready": False,
        "reason": reason,
        "schema_valid": False,
        "bundle_status": "",
        "blocking_codes": [],
        "target": target or {},
        "target_digest": target_digest,
        "closure_bundle_digest": target_digest,
        "chain_statuses": {},
        "product_surface_switch_approval_packet_digest": "",
        "legacy_retirement_approval_packet_digest": "",
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _validate_approval_packet(
    packet: object,
    *,
    schema_version: str,
    operation: str,
    status: str,
    expected_values: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(packet, dict):
        return {"valid": False, "reason": "not_object", "packet_digest": ""}
    if packet.get("schema_version") != schema_version:
        return {"valid": False, "reason": "schema_mismatch", "packet_digest": ""}
    if packet.get("approval_operation") != operation:
        return {"valid": False, "reason": "operation_mismatch", "packet_digest": ""}
    if packet.get("approval_status") != status:
        return {"valid": False, "reason": "status_mismatch", "packet_digest": ""}
    for key in (
        "raw_paths_printed",
        "raw_ids_printed",
        "raw_content_printed",
        "mutation_performed",
        "network_used",
    ):
        if packet.get(key) is not False:
            return {"valid": False, "reason": f"{key}_required_false", "packet_digest": ""}
    canonical = packet.get("canonical_approval")
    if not isinstance(canonical, dict):
        return {"valid": False, "reason": "canonical_not_object", "packet_digest": ""}
    if canonical.get("operation") != operation:
        return {
            "valid": False,
            "reason": "canonical_operation_mismatch",
            "packet_digest": "",
        }
    if canonical.get("status") != status:
        return {
            "valid": False,
            "reason": "canonical_status_mismatch",
            "packet_digest": "",
        }
    for key, value in expected_values.items():
        if canonical.get(key) != value:
            return {
                "valid": False,
                "reason": f"canonical_{key}_mismatch",
                "packet_digest": "",
            }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    if packet.get("canonical_approval_json") != canonical_json:
        return {
            "valid": False,
            "reason": "canonical_json_mismatch",
            "packet_digest": "",
        }
    packet_digest = "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    if packet.get("packet_digest") != packet_digest:
        return {"valid": False, "reason": "packet_digest_mismatch", "packet_digest": ""}
    return {"valid": True, "reason": "", "packet_digest": packet_digest}


def _normalize_product_surface_classes(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values or ("conversation_chunk",):
        item = str(value or "").strip()
        if item and item not in normalized:
            normalized.append(item)
    return tuple(normalized or ["conversation_chunk"])


def _build_product_surface_summary(
    *,
    mcp_state_db_recall_configured: bool,
    codex_hook_state_db_recall_configured: bool,
    session_entry_hook_state_db_recall_configured: bool,
    session_entry_hook_disabled: bool,
    expected_result_classes: tuple[str, ...],
    evidence_source: str,
    blockers: list[dict[str, object]],
) -> dict[str, object]:
    unsupported = sorted(
        result_class
        for result_class in expected_result_classes
        if result_class not in SUPPORTED_PRODUCT_RESULT_CLASSES
    )
    for result_class in unsupported:
        _block(blockers, "unsupported_product_surface_result_class", result_class=result_class)
    if not mcp_state_db_recall_configured:
        _block(blockers, "live_mcp_state_db_recall_not_configured")
    if not codex_hook_state_db_recall_configured:
        _block(blockers, "live_codex_hook_state_db_recall_not_configured")
    if not session_entry_hook_state_db_recall_configured and not session_entry_hook_disabled:
        _block(blockers, "live_session_entry_hook_state_unresolved")

    return {
        "expected_result_classes": list(expected_result_classes),
        "supported_result_classes": sorted(SUPPORTED_PRODUCT_RESULT_CLASSES),
        "unsupported_result_classes": unsupported,
        "mcp_state_db_recall_configured": mcp_state_db_recall_configured,
        "codex_hook_state_db_recall_configured": codex_hook_state_db_recall_configured,
        "evidence_source": evidence_source,
        "session_entry_hook_state": (
            "state_db_recall_configured"
            if session_entry_hook_state_db_recall_configured
            else "disabled"
            if session_entry_hook_disabled
            else "unresolved"
        ),
    }


def _evaluate_product_surface_evidence(packet: dict | None) -> dict[str, object]:
    if packet is None:
        return {
            "supplied": False,
            "accepted": False,
            "status": "not_supplied",
            "reason": "",
            "schema_valid": False,
            "target_digest_matches": False,
            "target_digest": "",
            "raw_paths_printed": False,
            "raw_ids_printed": False,
            "raw_content_printed": False,
            "accepted_for_live_closure": False,
            "closure_grade_reason": "packet_not_supplied",
            "target": {},
        }
    if not isinstance(packet, dict):
        return _invalid_product_surface_evidence("packet_not_object")
    if packet.get("schema_version") != PRODUCT_SURFACE_EVIDENCE_SCHEMA:
        return _invalid_product_surface_evidence("schema_version_mismatch")
    evidence_status = str(packet.get("evidence_status") or "")
    if evidence_status not in {"accepted", "blocked"}:
        return _invalid_product_surface_evidence("evidence_status_invalid")
    if packet.get("redacted_paths") is not True:
        return _invalid_product_surface_evidence("redacted_paths_required")
    if packet.get("raw_paths_printed") is not False:
        return _invalid_product_surface_evidence("raw_paths_flag_required_false")
    if packet.get("raw_ids_printed") is not False:
        return _invalid_product_surface_evidence("raw_ids_flag_required_false")
    if packet.get("raw_content_printed") is not False:
        return _invalid_product_surface_evidence("raw_content_flag_required_false")

    target = packet.get("target")
    if not isinstance(target, dict):
        return _invalid_product_surface_evidence("target_not_object")
    normalized_target = {
        "mcp_state_db_recall_configured": target.get("mcp_state_db_recall_configured") is True,
        "codex_hook_state_db_recall_configured": target.get("codex_hook_state_db_recall_configured")
        is True,
        "session_entry_hook_state": str(target.get("session_entry_hook_state") or ""),
    }
    if (
        evidence_status == "accepted"
        and normalized_target["session_entry_hook_state"] not in VALID_SESSION_ENTRY_HOOK_STATES
    ):
        return _invalid_product_surface_evidence("session_entry_hook_state_invalid")

    input_modes = packet.get("evidence_input_modes")
    if not isinstance(input_modes, dict):
        input_modes = {}
    normalized_input_modes = {
        "mcp": str(input_modes.get("mcp") or ""),
        "codex_hook": str(input_modes.get("codex_hook") or ""),
        "session_entry_hook": str(input_modes.get("session_entry_hook") or ""),
    }
    closure_evidence = _product_surface_closure_evidence(
        target=normalized_target,
        input_modes=normalized_input_modes,
    )
    expected_digest = str(packet.get("target_digest") or "")
    actual_digest = _digest(normalized_target)
    if not expected_digest or expected_digest != actual_digest:
        return _invalid_product_surface_evidence(
            "target_digest_mismatch",
            target=normalized_target,
            target_digest=actual_digest,
        )

    accepted = evidence_status == "accepted"
    return {
        "supplied": True,
        "accepted": accepted,
        "status": "accepted" if accepted else "not_accepted",
        "reason": str(packet.get("reason") or ""),
        "schema_valid": True,
        "target_digest_matches": True,
        "target_digest": actual_digest,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "evidence_input_modes": normalized_input_modes,
        "accepted_for_live_closure": bool(
            closure_evidence["accepted_for_live_closure"]
        ),
        "closure_grade_reason": str(closure_evidence["reason"]),
        "closure_evidence": closure_evidence,
        "target": normalized_target,
    }


def _invalid_product_surface_evidence(
    reason: str,
    *,
    target: dict[str, object] | None = None,
    target_digest: str = "",
) -> dict[str, object]:
    return {
        "supplied": True,
        "accepted": False,
        "status": "invalid",
        "reason": reason,
        "schema_valid": False,
        "target_digest_matches": False,
        "target_digest": target_digest,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "evidence_input_modes": {},
        "accepted_for_live_closure": False,
        "closure_grade_reason": reason,
        "closure_evidence": {
            "accepted_for_live_closure": False,
            "reason": reason,
            "required_modes": _product_surface_required_closure_modes(),
        },
        "target": target or {},
    }


def _product_surface_closure_evidence(
    *,
    target: Mapping[str, object],
    input_modes: Mapping[str, object],
) -> dict[str, object]:
    mcp_mode = str(input_modes.get("mcp") or "")
    codex_hook_mode = str(input_modes.get("codex_hook") or "")
    session_entry_hook_mode = str(input_modes.get("session_entry_hook") or "")
    session_entry_state = str(target.get("session_entry_hook_state") or "")
    session_entry_mode_ok = (
        session_entry_state == "state_db_recall_configured"
        and session_entry_hook_mode in {"config", "plan"}
    ) or (
        session_entry_state == "disabled"
        and session_entry_hook_mode == "plan"
    )
    mode_status = {
        "mcp": target.get("mcp_state_db_recall_configured") is True
        and mcp_mode == "config",
        "codex_hook": target.get("codex_hook_state_db_recall_configured") is True
        and codex_hook_mode == "config",
        "session_entry_hook": session_entry_mode_ok,
    }
    missing = sorted(surface for surface, ok in mode_status.items() if not ok)
    return {
        "accepted_for_live_closure": not missing,
        "reason": "" if not missing else "non_closure_grade_input_modes",
        "missing_closure_grade_surfaces": missing,
        "input_modes": {
            "mcp": mcp_mode,
            "codex_hook": codex_hook_mode,
            "session_entry_hook": session_entry_hook_mode,
        },
        "required_modes": _product_surface_required_closure_modes(),
    }


def _product_surface_required_closure_modes() -> dict[str, object]:
    return {
        "mcp": ["config"],
        "codex_hook": ["config"],
        "session_entry_hook": {
            "state_db_recall_configured": ["config", "plan"],
            "disabled": ["plan"],
        },
    }


def _build_derived_memory_summary(
    *,
    derived_memory_authority: str,
    evidence_source: str,
    blockers: list[dict[str, object]],
) -> dict[str, object]:
    normalized = str(derived_memory_authority or "not-evaluated")
    accepted = normalized in VALID_DERIVED_MEMORY_AUTHORITIES
    if not accepted:
        _block(blockers, "derived_memory_authority_not_dispositioned")
    return {
        "authority": normalized,
        "accepted_for_retirement_gate": accepted,
        "evidence_source": evidence_source,
        "accepted_values": sorted(VALID_DERIVED_MEMORY_AUTHORITIES),
    }


def _evaluate_derived_memory_authority_evidence(packet: dict | None) -> dict[str, object]:
    if packet is None:
        return {
            "supplied": False,
            "accepted": False,
            "status": "not_supplied",
            "reason": "",
            "schema_valid": False,
            "target_digest_matches": False,
            "target_digest": "",
            "raw_paths_printed": False,
            "raw_ids_printed": False,
            "raw_content_printed": False,
            "accepted_for_live_closure": False,
            "reviewed_authority_disposition": False,
            "target": {},
        }
    if not isinstance(packet, dict):
        return _invalid_derived_memory_authority_evidence("packet_not_object")
    if packet.get("schema_version") != DERIVED_MEMORY_AUTHORITY_EVIDENCE_SCHEMA:
        return _invalid_derived_memory_authority_evidence("schema_version_mismatch")
    evidence_status = str(packet.get("evidence_status") or "")
    if evidence_status not in {"accepted", "blocked"}:
        return _invalid_derived_memory_authority_evidence("evidence_status_invalid")
    if packet.get("redacted_paths") is not True:
        return _invalid_derived_memory_authority_evidence("redacted_paths_required")
    if packet.get("raw_paths_printed") is not False:
        return _invalid_derived_memory_authority_evidence(
            "raw_paths_flag_required_false"
        )
    if packet.get("raw_ids_printed") is not False:
        return _invalid_derived_memory_authority_evidence(
            "raw_ids_flag_required_false"
        )
    if packet.get("raw_content_printed") is not False:
        return _invalid_derived_memory_authority_evidence(
            "raw_content_flag_required_false"
        )
    target = packet.get("target")
    if not isinstance(target, dict):
        return _invalid_derived_memory_authority_evidence("target_not_object")
    normalized_target = {
        "derived_memory_authority": str(
            target.get("derived_memory_authority") or "not-evaluated"
        ),
        "reviewed_authority_disposition": target.get("reviewed_authority_disposition")
        is True,
    }
    if (
        evidence_status == "accepted"
        and normalized_target["derived_memory_authority"]
        not in VALID_DERIVED_MEMORY_AUTHORITIES
    ):
        return _invalid_derived_memory_authority_evidence(
            "derived_memory_authority_invalid"
        )
    if evidence_status == "accepted" and not normalized_target["reviewed_authority_disposition"]:
        return _invalid_derived_memory_authority_evidence(
            "reviewed_authority_disposition_required"
        )

    expected_digest = str(packet.get("target_digest") or "")
    actual_digest = _digest(normalized_target)
    if not expected_digest or expected_digest != actual_digest:
        return _invalid_derived_memory_authority_evidence(
            "target_digest_mismatch",
            target=normalized_target,
            target_digest=actual_digest,
        )

    accepted = evidence_status == "accepted"
    return {
        "supplied": True,
        "accepted": accepted,
        "status": "accepted" if accepted else "not_accepted",
        "reason": str(packet.get("reason") or ""),
        "schema_valid": True,
        "target_digest_matches": True,
        "target_digest": actual_digest,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "accepted_for_live_closure": accepted,
        "reviewed_authority_disposition": normalized_target[
            "reviewed_authority_disposition"
        ],
        "target": normalized_target,
    }


def _invalid_derived_memory_authority_evidence(
    reason: str,
    *,
    target: dict[str, object] | None = None,
    target_digest: str = "",
) -> dict[str, object]:
    return {
        "supplied": True,
        "accepted": False,
        "status": "invalid",
        "reason": reason,
        "schema_valid": False,
        "target_digest_matches": False,
        "target_digest": target_digest,
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
        "accepted_for_live_closure": False,
        "reviewed_authority_disposition": False,
        "target": target or {},
    }


def _evaluate_retirement_readiness_report(report: dict | None) -> dict[str, object]:
    if report is None:
        return _invalid_retirement_readiness_report("report_not_supplied")
    if not isinstance(report, dict):
        return _invalid_retirement_readiness_report("report_not_object")
    if report.get("schema_version") != SCHEMA_VERSION:
        return _invalid_retirement_readiness_report("schema_version_mismatch")
    if report.get("dry_run") is not True:
        return _invalid_retirement_readiness_report("dry_run_required")
    if report.get("redacted_paths") is not True:
        return _invalid_retirement_readiness_report("redacted_paths_required")
    if report.get("mutation_performed") is not False:
        return _invalid_retirement_readiness_report("mutation_flag_required_false")
    if report.get("network_used") is not False:
        return _invalid_retirement_readiness_report("network_flag_required_false")
    if report.get("raw_paths_printed") is not False:
        return _invalid_retirement_readiness_report("raw_paths_flag_required_false")
    if report.get("raw_ids_printed") is not False:
        return _invalid_retirement_readiness_report("raw_ids_flag_required_false")
    if report.get("raw_content_printed") is not False:
        return _invalid_retirement_readiness_report("raw_content_flag_required_false")

    blocking_codes = tuple(str(code) for code in report.get("blocking_codes") or ())
    retirement_readiness_status = str(report.get("retirement_readiness_status") or "")
    legacy_retirement_status = str(report.get("legacy_retirement_status") or "")
    product_surface_closure_status = str(report.get("product_surface_closure_status") or "")
    ready = (
        retirement_readiness_status == "retirement_ready_for_operator_approval"
        and legacy_retirement_status == "ready_for_operator_approval"
        and product_surface_closure_status == "ready_for_operator_approval"
        and not blocking_codes
    )
    normalized = {
        "retirement_readiness_status": retirement_readiness_status,
        "legacy_retirement_status": legacy_retirement_status,
        "product_surface_closure_status": product_surface_closure_status,
        "blocking_codes": list(blocking_codes),
        "product_surface_evidence_status": str(
            (report.get("product_surface_evidence") or {}).get("status") or ""
        ),
        "derived_memory_authority_evidence_status": str(
            (report.get("derived_memory_authority_evidence") or {}).get("status") or ""
        ),
        "derived_memory_authority": str(
            (report.get("derived_memory_authority") or {}).get("authority") or ""
        ),
    }
    return {
        "supplied": True,
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "reason": "",
        "schema_valid": True,
        "retirement_readiness_status": retirement_readiness_status,
        "legacy_retirement_status": legacy_retirement_status,
        "product_surface_closure_status": product_surface_closure_status,
        "blocking_codes": list(blocking_codes),
        "readiness_digest": _digest(normalized),
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _invalid_retirement_readiness_report(reason: str) -> dict[str, object]:
    return {
        "supplied": True,
        "status": "invalid",
        "ready": False,
        "reason": reason,
        "schema_valid": False,
        "retirement_readiness_status": "",
        "legacy_retirement_status": "",
        "product_surface_closure_status": "",
        "blocking_codes": [],
        "readiness_digest": "",
        "raw_paths_printed": False,
        "raw_ids_printed": False,
        "raw_content_printed": False,
    }


def _inspect_state_db_product_coverage(
    path: Path,
    *,
    expected_product_result_classes: tuple[str, ...],
    blockers: list[dict[str, object]],
) -> dict[str, object]:
    exists = path.exists()
    read_only_open = False
    delivery_payloads_table_exists = False
    delivery_job_status_counts: dict[str, int] = {}
    succeeded_document_kind_counts: dict[str, int] = {}
    matched_payload_type_counts: dict[str, int] = {}
    delivery_payload_count = 0
    succeeded_delivery_count = 0
    payload_missing_count = 0
    payload_hash_mismatch_count = 0
    missing_index_ref_count = 0

    if not exists:
        return {
            "state_db_exists": False,
            "read_only_open": False,
            "delivery_payloads_table_exists": False,
            "delivery_payload_count": 0,
            "delivery_job_status_counts": {},
            "succeeded_delivery_count": 0,
            "succeeded_document_kind_counts": {},
            "matched_payload_type_counts": {},
            "payload_missing_count": 0,
            "payload_hash_mismatch_count": 0,
            "missing_index_ref_count": 0,
            "expected_product_result_classes": list(expected_product_result_classes),
        }

    try:
        with _connect_sqlite_immutable(path) as connection:
            read_only_open = True
            delivery_payloads_table_exists = _table_exists(connection, "delivery_payloads")
            if not delivery_payloads_table_exists:
                _block(blockers, "state_db_schema_missing_delivery_payloads")
            if _table_exists(connection, "delivery_jobs"):
                delivery_job_status_counts = _group_counts(connection, "delivery_jobs", "status")
                rows = connection.execute(
                    """
                    SELECT *
                    FROM delivery_jobs
                    WHERE status = 'succeeded'
                    ORDER BY updated_at ASC, rowid ASC
                    """
                ).fetchall()
                succeeded_delivery_count = len(rows)
                for row in rows:
                    job = _row_to_dict(row)
                    document_kind = str(job.get("document_kind") or "")
                    succeeded_document_kind_counts[document_kind] = (
                        succeeded_document_kind_counts.get(document_kind, 0) + 1
                    )
                    if not job.get("index_target_id") or not job.get("index_document_id"):
                        missing_index_ref_count += 1
                    payload = _payload_for_job(connection, job) if delivery_payloads_table_exists else None
                    if payload is None:
                        payload_missing_count += 1
                        continue
                    if not _payload_matches_job(payload, job):
                        payload_hash_mismatch_count += 1
                        continue
                    payload_type = _payload_type(payload, fallback=document_kind)
                    matched_payload_type_counts[payload_type] = (
                        matched_payload_type_counts.get(payload_type, 0) + 1
                    )
            if delivery_payloads_table_exists:
                delivery_payload_count = _count_rows(connection, "delivery_payloads")
    except sqlite3.Error:
        _block(blockers, "state_db_product_coverage_read_failed")

    if payload_missing_count:
        _block(blockers, "state_db_payload_missing_for_succeeded_delivery")
    if payload_hash_mismatch_count:
        _block(blockers, "state_db_payload_integrity_mismatch")
    if missing_index_ref_count:
        _block(blockers, "state_db_succeeded_delivery_missing_index_refs")

    return {
        "state_db_exists": exists,
        "read_only_open": read_only_open,
        "delivery_payloads_table_exists": delivery_payloads_table_exists,
        "delivery_payload_count": delivery_payload_count,
        "delivery_job_status_counts": dict(sorted(delivery_job_status_counts.items())),
        "succeeded_delivery_count": succeeded_delivery_count,
        "succeeded_document_kind_counts": dict(sorted(succeeded_document_kind_counts.items())),
        "matched_payload_type_counts": dict(sorted(matched_payload_type_counts.items())),
        "payload_missing_count": payload_missing_count,
        "payload_hash_mismatch_count": payload_hash_mismatch_count,
        "missing_index_ref_count": missing_index_ref_count,
        "expected_product_result_classes": list(expected_product_result_classes),
    }


def _connect_sqlite_immutable(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=False)
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro&immutable=1",
        uri=True,
        factory=_ClosingSqliteConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON;")
    return connection


class _ClosingSqliteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _count_rows(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _group_counts(connection: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    rows = connection.execute(
        f"""
        SELECT COALESCE({column}, '') AS bucket, COUNT(*) AS count
        FROM {table}
        GROUP BY COALESCE({column}, '')
        """
    ).fetchall()
    return {str(row["bucket"]): int(row["count"]) for row in rows}


def _payload_for_job(connection: sqlite3.Connection, job: Mapping[str, Any]) -> dict[str, object] | None:
    row = connection.execute(
        "SELECT payload_json FROM delivery_payloads WHERE idempotency_key = ?",
        (str(job.get("idempotency_key") or ""),),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload_json"] or ""))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _payload_matches_job(payload: Mapping[str, Any], job: Mapping[str, Any]) -> bool:
    expected = str(job.get("payload_hash") or "")
    if not expected:
        return False
    content_hash = str(payload.get("contentHash") or "")
    body = str(_payload_document(payload).get("body") or "")
    recomputed = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    return content_hash == expected and recomputed == expected


def _payload_type(payload: Mapping[str, Any], *, fallback: str = "") -> str:
    metadata = _payload_metadata(payload)
    return str(metadata.get("type") or metadata.get("result_type") or fallback or "")


def _payload_metadata(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    document = _payload_document(payload)
    metadata = document.get("metadata") if isinstance(document, dict) else {}
    return metadata if isinstance(metadata, Mapping) else {}


def _payload_document(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = payload.get("payload")
    if not isinstance(nested, Mapping):
        return {}
    document = nested.get("document")
    return document if isinstance(document, Mapping) else {}


def _row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _argv_proves_state_db_recall(
    argv: tuple[str, ...],
    *,
    required_tokens: tuple[str, ...],
) -> bool:
    tokens = tuple(str(part) for part in argv)
    if not all(required in tokens for required in required_tokens):
        return False
    try:
        index = tokens.index("--state-db-recall")
    except ValueError:
        return False
    if index + 1 >= len(tokens):
        return False
    value = tokens[index + 1]
    return bool(value) and not value.startswith("--")


def _argv_candidates(
    primary: tuple[str, ...] | None,
    extras: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    candidates: list[tuple[str, ...]] = []
    if primary is not None:
        candidates.append(tuple(primary))
    for argv in extras:
        candidates.append(tuple(argv))
    return tuple(candidates)


def _any_argv_proves_state_db_recall(
    candidates: tuple[tuple[str, ...], ...],
    *,
    required_tokens: tuple[str, ...],
) -> bool:
    return any(
        _argv_proves_state_db_recall(argv, required_tokens=required_tokens)
        for argv in candidates
    )


def _digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _block(blockers: list[dict[str, object]], code: str, **details: object) -> None:
    blocker: dict[str, object] = {"code": code}
    blocker.update(details)
    blockers.append(blocker)

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping, MutableSet
from datetime import datetime, timedelta, timezone
from typing import Any

from .._util import ensure_public_safe, hash_payload, public_safe_text


AGENT_CONTEXT_CONSUMER_CHALLENGE_SCHEMA = "agent_context_consumer_challenge.v1"
AGENT_CONTEXT_CONSUMER_STARTUP_RECEIPT_SCHEMA = "agent_context_consumer_startup_receipt.v2"
AGENT_CONTEXT_ROUTE_BINDING_SCHEMA = "agent_context_route_binding.v1"
AGENT_CONTEXT_POLICY_DECISION_RECEIPT_SCHEMA = "agent_context_policy_decision_receipt.v1"
AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA = "agent_context_startup_runtime_evidence.v1"
AGENT_CONTEXT_PRODUCT_SCHEMA = "agent_context_product_pack.v1"
CONTEXT_PACK_SCHEMA = "llm_brain_context_resolve.v1"
CODEX_BOUNDED_ACTIVATION_SCOPE = "codex_bounded_startup_read_only.v1"
CODEX_CONTEXT_ADAPTER = "neurons_codex_context_adapter"
CODEX_CONTEXT_ADAPTER_ENTRYPOINT = "neuron-knowledge agent-context-startup"
AGENT_CONTEXT_STARTUP_CURRENT_REQUEST = "agent context startup before task dispatch"
AGENT_CONTEXT_STARTUP_RESPONSE_MODE = "full"
AGENT_CONTEXT_STARTUP_CONTEXT_LIMIT = 8
AGENT_CONTEXT_STARTUP_ROUTE_LIMIT = 5
MAX_CHALLENGE_TTL_SECONDS = 300
REQUIRED_STARTUP_SECTIONS = (
    "current_authority",
    "style_preference",
    "active_work",
    "required_verification",
)
REQUIRED_STARTUP_ROUTES = (
    "authority_archive_separation",
    "code_style_preference",
    "temporal_work_recall",
    "code_change_impact",
    "html_visualization_preference",
    "deployment_runtime_truth",
)
REQUIRED_POLICY_DECISIONS = {
    "context.execute_direct": ("deny", "DIRECT_EXECUTION_FORBIDDEN"),
    "context.read_private_raw": ("deny", "RAW_PRIVATE_CONTEXT_FORBIDDEN"),
    "authority.promote_without_approval_scope": ("deny", "APPROVAL_SCOPE_REQUIRED"),
    "context.suggest_change": ("allow", "SUGGESTION_ALLOWED"),
}
REQUIRED_PRIVATE_PROPERTY_OMISSIONS = {
    "raw_body",
    "raw_source",
    "private_deploy_value",
    "secret",
}
_RECEIPT_KEYS = {
    "schema_version",
    "issuer",
    "challenge_binding",
    "scope_binding",
    "startup_events",
    "context_binding",
    "startup_binding_hash",
    "policy_decisions",
    "policy_decision_hashes",
    "io_audit",
    "postcheck",
    "production_mutation_performed",
    "receipt_hash",
    "proof",
}
_CHALLENGE_KEYS = {
    "schema_version",
    "challenge_id",
    "nonce",
    "issued_at",
    "expires_at",
    "scope_binding",
    "challenge_hash",
}
_SCOPE_KEYS = {
    "activation_scope",
    "consumer",
    "project",
    "repository_hash",
    "branch_hash",
    "expected_commit",
    "expected_commit_binding_kind",
    "endpoint_origin_hash",
    "read_tool",
    "request_hash",
    "route_request_hashes",
    "scope_hash",
}
_ROUTE_BINDING_KEYS = {
    "schema_version",
    "route",
    "route_request_hash",
    "semantic_projection_hash",
    "observed_source_payload_hash",
}


def build_agent_context_startup_context_request(
    *,
    repository: str,
    branch: str,
    project: str,
    consumer: str = "codex",
) -> dict[str, Any]:
    return {
        "repository": repository,
        "branch": branch,
        "project": project,
        "current_files": [],
        "current_request": AGENT_CONTEXT_STARTUP_CURRENT_REQUEST,
        "limit": AGENT_CONTEXT_STARTUP_CONTEXT_LIMIT,
        "response_mode": AGENT_CONTEXT_STARTUP_RESPONSE_MODE,
        "consumer": consumer,
    }


def build_agent_context_startup_route_request(
    *,
    repository: str,
    branch: str,
    project: str,
    route: str,
    consumer: str = "codex",
) -> dict[str, Any]:
    return {
        "repository": repository,
        "branch": branch,
        "project": project,
        "query": f"agent context startup route smoke: {route}",
        "current_files": [],
        "route": route,
        "limit": AGENT_CONTEXT_STARTUP_ROUTE_LIMIT,
        "response_mode": AGENT_CONTEXT_STARTUP_RESPONSE_MODE,
        "consumer": consumer,
    }


def build_agent_context_consumer_challenge(
    *,
    consumer: str,
    project: str,
    repository: str,
    branch: str,
    expected_commit: str,
    endpoint_origin: str,
    now: datetime | str | None = None,
    nonce: str = "",
    ttl_seconds: int = MAX_CHALLENGE_TTL_SECONDS,
) -> dict[str, Any]:
    issued = _as_utc_datetime(now)
    bounded_ttl = max(1, min(int(ttl_seconds), MAX_CHALLENGE_TTL_SECONDS))
    safe_consumer = public_safe_text(str(consumer or ""), max_chars=80)
    if safe_consumer != "codex":
        raise ValueError("bounded startup challenge only supports consumer=codex")
    safe_nonce = public_safe_text(nonce or secrets.token_hex(32), max_chars=160)
    scope = _scope_binding(
        consumer=safe_consumer,
        project=project,
        repository=repository,
        branch=branch,
        expected_commit=expected_commit,
        endpoint_origin=endpoint_origin,
    )
    challenge_id = f"challenge:{hash_payload([scope, safe_nonce, issued.isoformat()]).split(':', 1)[1][:24]}"
    challenge = {
        "schema_version": AGENT_CONTEXT_CONSUMER_CHALLENGE_SCHEMA,
        "challenge_id": challenge_id,
        "nonce": safe_nonce,
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(seconds=bounded_ttl)).isoformat(),
        "scope_binding": scope,
    }
    challenge["challenge_hash"] = hash_payload(challenge)
    ensure_public_safe(challenge, "AgentContextConsumerChallenge")
    return challenge


def build_agent_context_consumer_startup_receipt(
    *,
    challenge: Mapping[str, Any],
    proof_key: bytes,
    context_pack: Mapping[str, Any],
    route_smokes: list[Mapping[str, Any]],
    now: datetime | str | None = None,
    process_instance_seed: str = "",
    io_audit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    key = _proof_key(proof_key)
    duplicate_routes = _duplicate_route_names(route_smokes)
    if duplicate_routes:
        raise ValueError(
            "duplicate agent context startup route: " + ",".join(duplicate_routes)
        )
    observed = _as_utc_datetime(now)
    safe_challenge = dict(challenge)
    product = _agent_context_product(context_pack)
    process_instance_hash = hash_payload(
        process_instance_seed or secrets.token_hex(32)
    )
    issuer = {
        "kind": "external_consumer_process",
        "consumer": "codex",
        "implementation": CODEX_CONTEXT_ADAPTER,
        "process_instance_hash": process_instance_hash,
        "entrypoint_hash": hash_payload(
            {
                "entrypoint": CODEX_CONTEXT_ADAPTER_ENTRYPOINT,
                "receipt_schema": AGENT_CONTEXT_CONSUMER_STARTUP_RECEIPT_SCHEMA,
            }
        ),
    }
    challenge_binding = {
        "challenge_id": public_safe_text(str(safe_challenge.get("challenge_id") or ""), max_chars=160),
        "challenge_hash": public_safe_text(str(safe_challenge.get("challenge_hash") or ""), max_chars=80),
        "issued_at": public_safe_text(str(safe_challenge.get("issued_at") or ""), max_chars=80),
        "expires_at": public_safe_text(str(safe_challenge.get("expires_at") or ""), max_chars=80),
    }
    scope_binding = _public_mapping(safe_challenge.get("scope_binding"))
    startup_events = _startup_event_chain(observed)
    context_binding = {
        "response_schema": public_safe_text(str(context_pack.get("schema_version") or ""), max_chars=80),
        "product_schema": public_safe_text(str(product.get("schema_version") or ""), max_chars=80),
        "product_hash": hash_payload(product),
        "section_manifest": _section_manifest(product),
        "route_manifest": _route_manifest(
            route_smokes,
            _public_mapping(scope_binding.get("route_request_hashes")),
        ),
        "disclosed_gaps_hash": hash_payload(
            {
                "degraded_mode": _public_mapping(product.get("degraded_mode")),
                "missing_evidence_before_promotion": _public_list(
                    product.get("missing_evidence_before_promotion")
                ),
            }
        ),
    }
    startup_binding_hash = hash_payload(
        {
            "issuer": issuer,
            "challenge_binding": challenge_binding,
            "scope_binding": scope_binding,
            "startup_events": startup_events,
            "context_binding": context_binding,
        }
    )
    policy_decisions = _policy_decision_receipts(
        product,
        startup_binding_hash=startup_binding_hash,
        context_product_hash=context_binding["product_hash"],
        process_instance_hash=process_instance_hash,
        decided_at=observed.isoformat(),
    )
    observed_io = dict(
        io_audit
        or {
            "brain_context_resolve_calls": 1,
            "brain_objects_query_calls": len(route_smokes),
            "write_tool_calls": 0,
            "task_dispatch_count_before_load": 0,
        }
    )
    observed_io["observation_basis"] = "bounded_adapter_call_accounting"
    receipt_core = {
        "schema_version": AGENT_CONTEXT_CONSUMER_STARTUP_RECEIPT_SCHEMA,
        "issuer": issuer,
        "challenge_binding": challenge_binding,
        "scope_binding": scope_binding,
        "startup_events": startup_events,
        "context_binding": context_binding,
        "startup_binding_hash": startup_binding_hash,
        "policy_decisions": policy_decisions,
        "policy_decision_hashes": [str(item.get("decision_hash") or "") for item in policy_decisions],
        "io_audit": observed_io,
        "postcheck": {
            "status": "validated",
            "observation_basis": "public_safe_adapter_output_check",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }
    receipt_hash = hash_payload(receipt_core)
    proof_tag = _proof_tag(
        key,
        challenge_hash=str(challenge_binding.get("challenge_hash") or ""),
        receipt_hash=receipt_hash,
    )
    receipt = {
        **receipt_core,
        "receipt_hash": receipt_hash,
        "proof": {"algorithm": "HMAC-SHA-256", "tag": proof_tag},
    }
    ensure_public_safe(receipt, "AgentContextConsumerStartupReceipt")
    return receipt


def validate_agent_context_consumer_startup_receipt(
    receipt: Mapping[str, Any],
    *,
    challenge: Mapping[str, Any],
    proof_key: bytes,
    context_pack: Mapping[str, Any],
    route_smokes: list[Mapping[str, Any]],
    now: datetime | str | None = None,
    consumed_challenge_hashes: MutableSet[str] | None = None,
) -> list[str]:
    failures: list[str] = []
    safe_receipt = dict(receipt) if isinstance(receipt, Mapping) else {}
    safe_challenge = dict(challenge) if isinstance(challenge, Mapping) else {}
    try:
        ensure_public_safe(safe_receipt, "AgentContextConsumerStartupReceipt")
    except ValueError:
        failures.append("agent_context_startup_receipt_not_public_safe")
    if set(safe_receipt) != _RECEIPT_KEYS:
        failures.append("agent_context_startup_receipt_shape_mismatch")
    if safe_receipt.get("schema_version") != AGENT_CONTEXT_CONSUMER_STARTUP_RECEIPT_SCHEMA:
        failures.append("agent_context_startup_receipt_schema_mismatch")

    try:
        ensure_public_safe(safe_challenge, "AgentContextConsumerChallenge")
    except ValueError:
        failures.append("agent_context_startup_challenge_not_public_safe")
    if set(safe_challenge) != _CHALLENGE_KEYS:
        failures.append("agent_context_startup_challenge_shape_mismatch")
    if safe_challenge.get("schema_version") != AGENT_CONTEXT_CONSUMER_CHALLENGE_SCHEMA:
        failures.append("agent_context_startup_challenge_schema_mismatch")
    if not str(safe_challenge.get("challenge_id") or "").startswith("challenge:"):
        failures.append("agent_context_startup_challenge_id_missing")
    if not str(safe_challenge.get("nonce") or ""):
        failures.append("agent_context_startup_challenge_nonce_missing")

    issuer = _public_mapping(safe_receipt.get("issuer"))
    if issuer.get("kind") != "external_consumer_process":
        failures.append("agent_context_startup_issuer_not_external_consumer")
    if issuer.get("consumer") != "codex":
        failures.append("agent_context_startup_consumer_mismatch")
    if issuer.get("implementation") != CODEX_CONTEXT_ADAPTER:
        failures.append("agent_context_startup_adapter_mismatch")

    expected_challenge_hash = _challenge_hash(safe_challenge)
    actual_challenge_hash = str(safe_challenge.get("challenge_hash") or "")
    if not actual_challenge_hash or actual_challenge_hash != expected_challenge_hash:
        failures.append("agent_context_startup_challenge_hash_mismatch")
    challenge_binding = _public_mapping(safe_receipt.get("challenge_binding"))
    for field in ("challenge_id", "challenge_hash", "issued_at", "expires_at"):
        if str(challenge_binding.get(field) or "") != str(safe_challenge.get(field) or ""):
            failures.append(f"agent_context_startup_challenge_binding_mismatch:{field}")
    failures.extend(_challenge_time_failures(safe_challenge, now=_as_utc_datetime(now)))
    if consumed_challenge_hashes is not None and actual_challenge_hash in consumed_challenge_hashes:
        failures.append("agent_context_startup_challenge_replayed")

    expected_scope = _public_mapping(safe_challenge.get("scope_binding"))
    scope = _public_mapping(safe_receipt.get("scope_binding"))
    if set(expected_scope) != _SCOPE_KEYS:
        failures.append("agent_context_startup_scope_shape_mismatch")
    route_request_hashes = _public_mapping(expected_scope.get("route_request_hashes"))
    if set(route_request_hashes) != set(REQUIRED_STARTUP_ROUTES):
        failures.append("agent_context_startup_scope_route_request_shape_mismatch")
    for field in ("repository_hash", "branch_hash", "endpoint_origin_hash", "request_hash"):
        if not _is_sha256_ref(expected_scope.get(field)):
            failures.append(f"agent_context_startup_scope_hash_missing:{field}")
    for route in REQUIRED_STARTUP_ROUTES:
        if not _is_sha256_ref(route_request_hashes.get(route)):
            failures.append(f"agent_context_startup_scope_route_hash_missing:{route}")
    if expected_scope.get("expected_commit_binding_kind") != "requested_source_identity_only":
        failures.append("agent_context_startup_expected_commit_binding_kind_mismatch")
    if expected_scope.get("read_tool") != "brain_context_resolve":
        failures.append("agent_context_startup_scope_read_tool_mismatch")
    expected_challenge_id = (
        "challenge:"
        + hash_payload(
            [
                expected_scope,
                str(safe_challenge.get("nonce") or ""),
                str(safe_challenge.get("issued_at") or ""),
            ]
        ).split(":", 1)[1][:24]
    )
    if safe_challenge.get("challenge_id") != expected_challenge_id:
        failures.append("agent_context_startup_challenge_id_mismatch")
    if scope != expected_scope:
        failures.append("agent_context_startup_scope_mismatch")
    if scope.get("scope_hash") != hash_payload({key: value for key, value in scope.items() if key != "scope_hash"}):
        failures.append("agent_context_startup_scope_hash_mismatch")
    if scope.get("activation_scope") != CODEX_BOUNDED_ACTIVATION_SCOPE:
        failures.append("agent_context_startup_activation_scope_mismatch")

    product = _agent_context_product(context_pack)
    for route in _duplicate_route_names(route_smokes):
        failures.append(f"agent_context_startup_route_duplicate:{route}")
    expected_route_manifest = _route_manifest(route_smokes, route_request_hashes)
    expected_binding = {
        "response_schema": public_safe_text(str(context_pack.get("schema_version") or ""), max_chars=80),
        "product_schema": public_safe_text(str(product.get("schema_version") or ""), max_chars=80),
        "product_hash": hash_payload(product),
        "section_manifest": _section_manifest(product),
        "route_manifest": expected_route_manifest,
        "disclosed_gaps_hash": hash_payload(
            {
                "degraded_mode": _public_mapping(product.get("degraded_mode")),
                "missing_evidence_before_promotion": _public_list(
                    product.get("missing_evidence_before_promotion")
                ),
            }
        ),
    }
    context_binding = _public_mapping(safe_receipt.get("context_binding"))
    if context_binding.get("product_hash") != expected_binding["product_hash"]:
        failures.append("agent_context_startup_product_hash_mismatch")
    actual_non_route_binding = {
        key: value for key, value in context_binding.items() if key != "route_manifest"
    }
    expected_non_route_binding = {
        key: value for key, value in expected_binding.items() if key != "route_manifest"
    }
    if actual_non_route_binding != expected_non_route_binding:
        failures.append("agent_context_startup_context_binding_mismatch")
    section_manifest = _public_mapping(context_binding.get("section_manifest"))
    for section in REQUIRED_STARTUP_SECTIONS:
        section_view = _public_mapping(section_manifest.get(section))
        if not _public_list(section_view.get("item_hashes")):
            failures.append(f"agent_context_startup_section_missing:{section}")
    for section in ("current_authority", "style_preference"):
        lanes = set(str(item) for item in _public_list(_public_mapping(section_manifest.get(section)).get("authority_lanes")))
        if lanes != {"accepted_current"}:
            failures.append(f"agent_context_startup_authority_lane_mismatch:{section}")
    route_manifest = _public_mapping(context_binding.get("route_manifest"))
    route_binding_failed = set(route_manifest) != set(REQUIRED_STARTUP_ROUTES)
    if route_binding_failed:
        failures.append("agent_context_startup_route_manifest_shape_mismatch")
    for route in REQUIRED_STARTUP_ROUTES:
        if route not in route_manifest:
            failures.append(f"agent_context_startup_route_missing:{route}")
            continue
        binding = _public_mapping(route_manifest.get(route))
        expected_route_binding = _public_mapping(expected_route_manifest.get(route))
        if set(binding) != _ROUTE_BINDING_KEYS:
            failures.append(f"agent_context_startup_route_binding_shape_mismatch:{route}")
            route_binding_failed = True
        if binding.get("schema_version") != AGENT_CONTEXT_ROUTE_BINDING_SCHEMA:
            failures.append(f"agent_context_startup_route_binding_schema_mismatch:{route}")
            route_binding_failed = True
        if binding.get("route") != route:
            failures.append(f"agent_context_startup_route_binding_route_mismatch:{route}")
            route_binding_failed = True
        if binding.get("route_request_hash") != route_request_hashes.get(route):
            failures.append(f"agent_context_startup_route_request_binding_mismatch:{route}")
            route_binding_failed = True
        if binding.get("semantic_projection_hash") != expected_route_binding.get(
            "semantic_projection_hash"
        ):
            failures.append(f"agent_context_startup_route_semantic_binding_mismatch:{route}")
            route_binding_failed = True
        if not _is_sha256_ref(binding.get("semantic_projection_hash")):
            failures.append(f"agent_context_startup_route_semantic_hash_invalid:{route}")
            route_binding_failed = True
        if not _is_sha256_ref(binding.get("observed_source_payload_hash")):
            failures.append(f"agent_context_startup_route_observed_hash_invalid:{route}")
            route_binding_failed = True
    if route_binding_failed and "agent_context_startup_context_binding_mismatch" not in failures:
        failures.append("agent_context_startup_context_binding_mismatch")

    failures.extend(_startup_event_failures(_public_list(safe_receipt.get("startup_events"))))
    startup_binding_hash = hash_payload(
        {
            "issuer": issuer,
            "challenge_binding": challenge_binding,
            "scope_binding": scope,
            "startup_events": _public_list(safe_receipt.get("startup_events")),
            "context_binding": context_binding,
        }
    )
    if safe_receipt.get("startup_binding_hash") != startup_binding_hash:
        failures.append("agent_context_startup_binding_hash_mismatch")
    failures.extend(
        _policy_decision_failures(
            _public_list(safe_receipt.get("policy_decisions")),
            startup_binding_hash=startup_binding_hash,
            context_product_hash=str(context_binding.get("product_hash") or ""),
            process_instance_hash=str(issuer.get("process_instance_hash") or ""),
        )
    )
    decision_hashes = [
        str(item.get("decision_hash") or "")
        for item in _public_list(safe_receipt.get("policy_decisions"))
        if isinstance(item, Mapping)
    ]
    if _public_list(safe_receipt.get("policy_decision_hashes")) != decision_hashes:
        failures.append("agent_context_startup_policy_decision_hashes_mismatch")

    io_audit = _public_mapping(safe_receipt.get("io_audit"))
    if io_audit.get("observation_basis") != "bounded_adapter_call_accounting":
        failures.append("agent_context_startup_io_observation_basis_missing")
    if io_audit.get("brain_context_resolve_calls") != 1:
        failures.append("agent_context_startup_context_read_count_mismatch")
    if io_audit.get("brain_objects_query_calls") != len(REQUIRED_STARTUP_ROUTES):
        failures.append("agent_context_startup_object_query_count_mismatch")
    if io_audit.get("write_tool_calls") != 0:
        failures.append("agent_context_startup_write_tool_called")
    if io_audit.get("task_dispatch_count_before_load") != 0:
        failures.append("agent_context_startup_task_dispatched_before_load")
    if safe_receipt.get("production_mutation_performed") is not False:
        failures.append("agent_context_startup_production_mutation_performed")
    failures.extend(_postcheck_failures(_public_mapping(safe_receipt.get("postcheck"))))

    receipt_core = {
        key: value
        for key, value in safe_receipt.items()
        if key not in {"receipt_hash", "proof"}
    }
    expected_receipt_hash = hash_payload(receipt_core)
    receipt_hash = str(safe_receipt.get("receipt_hash") or "")
    if receipt_hash != expected_receipt_hash:
        failures.append("agent_context_startup_receipt_hash_mismatch")
    proof = _public_mapping(safe_receipt.get("proof"))
    if proof.get("algorithm") != "HMAC-SHA-256":
        failures.append("agent_context_startup_proof_algorithm_mismatch")
    try:
        expected_tag = _proof_tag(
            _proof_key(proof_key),
            challenge_hash=str(challenge_binding.get("challenge_hash") or ""),
            receipt_hash=expected_receipt_hash,
        )
    except ValueError:
        expected_tag = ""
        failures.append("agent_context_startup_proof_key_invalid")
    if not expected_tag or not hmac.compare_digest(str(proof.get("tag") or ""), expected_tag):
        failures.append("agent_context_startup_proof_mismatch")

    failures = _dedupe(failures)
    if not failures and consumed_challenge_hashes is not None:
        consumed_challenge_hashes.add(actual_challenge_hash)
    return failures


def build_agent_context_startup_runtime_evidence(
    *,
    receipt: Mapping[str, Any],
    challenge: Mapping[str, Any],
    proof_key: bytes,
    context_pack: Mapping[str, Any],
    route_smokes: list[Mapping[str, Any]],
    now: datetime | str | None = None,
) -> dict[str, Any]:
    failures = validate_agent_context_consumer_startup_receipt(
        receipt,
        challenge=challenge,
        proof_key=proof_key,
        context_pack=context_pack,
        route_smokes=route_smokes,
        now=now,
    )
    valid = not failures
    product = _agent_context_product(context_pack)
    context_binding = _public_mapping(receipt.get("context_binding"))
    manifest = _public_mapping(context_binding.get("section_manifest"))
    section_counts = {
        section: len(_public_list(_public_mapping(manifest.get(section)).get("item_hashes")))
        for section in REQUIRED_STARTUP_SECTIONS
    }
    decisions = {
        str(_public_mapping(item.get("request")).get("capability") or ""): _public_mapping(
            item.get("decision")
        )
        for item in _public_list(receipt.get("policy_decisions"))
        if isinstance(item, Mapping)
    }
    route_manifest = _public_mapping(context_binding.get("route_manifest"))
    degraded = _public_mapping(product.get("degraded_mode"))
    evidence = {
        "schema_version": AGENT_CONTEXT_STARTUP_RUNTIME_SCHEMA,
        "evidence_origin": "external_consumer_process",
        "activation_scope": CODEX_BOUNDED_ACTIVATION_SCOPE,
        "startup_receipt": dict(receipt),
        "receipt_validation": {
            "status": "validated" if valid else "failed",
            "failures": failures,
        },
        "startup_context": {
            "schema_version": public_safe_text(str(product.get("schema_version") or ""), max_chars=80),
            "consumer": public_safe_text(str(product.get("consumer") or ""), max_chars=80),
            "loaded_on_startup": valid,
            "section_counts": section_counts,
            "section_authority_lanes": {
                section: _public_list(_public_mapping(manifest.get(section)).get("authority_lanes"))
                for section in REQUIRED_STARTUP_SECTIONS
            },
            "surface_policy": {
                "mutation_allowed": _public_mapping(product.get("surface_policy")).get(
                    "mutation_allowed"
                )
            },
            "degraded_gap_disclosure_present": isinstance(degraded.get("gaps"), list),
            "missing_evidence_before_promotion_present": isinstance(
                product.get("missing_evidence_before_promotion"), list
            ),
        },
        "read_path_smoke": {
            "tool": "brain_objects_query",
            "read_only": True,
            "routes_checked": list(route_manifest),
            "production_mutation_performed": False,
        },
        "runtime_enforcement": {
            "evidence_kind": "context_pack_policy_projection",
            "runtime_interception_observed": False,
            "executor_invocation_count": 0,
            "direct_execution_allowed": _decision_outcome(
                decisions, "context.execute_direct"
            )
            == "allow",
            "production_mutation_allowed": False,
            "raw_private_context_blocked": _decision_outcome(
                decisions, "context.read_private_raw"
            )
            == "deny",
            "approval_scope_blocker_enforced": _decision_outcome(
                decisions, "authority.promote_without_approval_scope"
            )
            == "deny",
            "suggest_change_allowed": _decision_outcome(
                decisions, "context.suggest_change"
            )
            == "allow",
            "stale_or_degraded_disclosure_present": isinstance(degraded.get("gaps"), list),
        },
        "consumer_statuses": {
            "codex": {
                "scope": CODEX_BOUNDED_ACTIVATION_SCOPE,
                "status": "validated" if valid else "failed",
                "host_startup_hook_status": "not_validated",
            },
            "claude-code": {"status": "not_validated"},
            "gemini": {"status": "not_validated"},
            "hermes": {"status": "not_validated"},
        },
        "postcheck": {
            "status": "validated" if valid else "failed",
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }
    ensure_public_safe(evidence, "AgentContextStartupRuntimeEvidence")
    return evidence


def _scope_binding(
    *,
    consumer: str,
    project: str,
    repository: str,
    branch: str,
    expected_commit: str,
    endpoint_origin: str,
) -> dict[str, Any]:
    scope = {
        "activation_scope": CODEX_BOUNDED_ACTIVATION_SCOPE,
        "consumer": public_safe_text(consumer, max_chars=80),
        "project": public_safe_text(project, max_chars=120),
        "repository_hash": hash_payload(repository),
        "branch_hash": hash_payload(branch),
        "expected_commit": public_safe_text(expected_commit, max_chars=80),
        "expected_commit_binding_kind": "requested_source_identity_only",
        "endpoint_origin_hash": hash_payload(endpoint_origin),
        "read_tool": "brain_context_resolve",
        "request_hash": hash_payload(
            build_agent_context_startup_context_request(
                repository=repository,
                branch=branch,
                project=project,
                consumer=consumer,
            )
        ),
        "route_request_hashes": {
            route: hash_payload(
                build_agent_context_startup_route_request(
                    repository=repository,
                    branch=branch,
                    project=project,
                    route=route,
                    consumer=consumer,
                )
            )
            for route in REQUIRED_STARTUP_ROUTES
        },
    }
    scope["scope_hash"] = hash_payload(scope)
    return scope


def _agent_context_product(context_pack: Mapping[str, Any]) -> dict[str, Any]:
    authority = _public_mapping(context_pack.get("authority"))
    product = authority.get("agent_context_product")
    if not isinstance(product, Mapping):
        product = context_pack.get("agent_context_product")
    return dict(product) if isinstance(product, Mapping) else {}


def _section_manifest(product: Mapping[str, Any]) -> dict[str, Any]:
    sections = _public_mapping(product.get("sections"))
    manifest: dict[str, Any] = {}
    for name in REQUIRED_STARTUP_SECTIONS:
        section = _public_mapping(sections.get(name))
        items = [dict(item) for item in _public_list(section.get("items")) if isinstance(item, Mapping)]
        lanes = sorted(
            {
                public_safe_text(str(item.get("authority_lane") or ""), max_chars=80)
                for item in items
                if str(item.get("authority_lane") or "")
            }
        )
        manifest[name] = {
            "item_hashes": [hash_payload(item) for item in items],
            "authority_lanes": lanes,
        }
    return manifest


def _route_manifest(
    route_smokes: list[Mapping[str, Any]],
    route_request_hashes: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    manifest: dict[str, dict[str, str]] = {}
    for smoke in route_smokes:
        if not isinstance(smoke, Mapping):
            continue
        route = public_safe_text(str(smoke.get("route") or ""), max_chars=120)
        if route and route not in manifest:
            manifest[route] = {
                "schema_version": AGENT_CONTEXT_ROUTE_BINDING_SCHEMA,
                "route": route,
                "route_request_hash": public_safe_text(
                    str(route_request_hashes.get(route) or ""), max_chars=80
                ),
                "semantic_projection_hash": public_safe_text(
                    str(smoke.get("semantic_payload_hash") or ""), max_chars=80
                ),
                "observed_source_payload_hash": public_safe_text(
                    str(smoke.get("source_payload_hash") or ""), max_chars=80
                ),
            }
    return manifest


def _duplicate_route_names(route_smokes: list[Mapping[str, Any]]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for smoke in route_smokes:
        if not isinstance(smoke, Mapping):
            continue
        route = public_safe_text(str(smoke.get("route") or ""), max_chars=120)
        if not route:
            continue
        if route in seen and route not in duplicates:
            duplicates.append(route)
        seen.add(route)
    return duplicates


def _startup_event_chain(observed: datetime) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous = ""
    for seq, event_type in enumerate(
        (
            "process_started",
            "context_requested",
            "context_loaded_before_task_dispatch",
        ),
        start=1,
    ):
        event = {
            "seq": seq,
            "type": event_type,
            "at": observed.isoformat(),
            "prev_hash": previous,
            "observation_basis": "bounded_adapter_control_flow",
        }
        event["event_hash"] = hash_payload(event)
        previous = event["event_hash"]
        events.append(event)
    return events


def _policy_decision_receipts(
    product: Mapping[str, Any],
    *,
    startup_binding_hash: str,
    context_product_hash: str,
    process_instance_hash: str,
    decided_at: str,
) -> list[dict[str, Any]]:
    surface = _public_mapping(product.get("surface_policy"))
    action_hints = [item for item in _public_list(product.get("action_hints")) if isinstance(item, Mapping)]
    tool_hints = [item for item in _public_list(product.get("tool_hints")) if isinstance(item, Mapping)]
    direct_forbidden = (
        surface.get("mutation_allowed") is False
        and all(item.get("execute_allowed") is False for item in action_hints)
        and all(item.get("execute_allowed") is False for item in tool_hints)
    )
    private_forbidden = REQUIRED_PRIVATE_PROPERTY_OMISSIONS.issubset(
        {str(item) for item in _public_list(surface.get("property_omissions"))}
    )
    promotion = next(
        (
            item
            for item in action_hints
            if str(item.get("action") or "") == "promote_authority"
        ),
        {},
    )
    approval_required = (
        promotion.get("execute_allowed") is False
        and "approved_scope_required" in _public_list(promotion.get("blocked_by"))
    )
    suggestion_allowed = "suggest_change" in _public_list(surface.get("allowed_actions"))
    observations = {
        "context.execute_direct": (
            "deny" if direct_forbidden else "allow",
            "DIRECT_EXECUTION_FORBIDDEN" if direct_forbidden else "DIRECT_EXECUTION_ALLOWED",
        ),
        "context.read_private_raw": (
            "deny" if private_forbidden else "allow",
            "RAW_PRIVATE_CONTEXT_FORBIDDEN" if private_forbidden else "RAW_PRIVATE_CONTEXT_ALLOWED",
        ),
        "authority.promote_without_approval_scope": (
            "deny" if approval_required else "allow",
            "APPROVAL_SCOPE_REQUIRED" if approval_required else "APPROVAL_SCOPE_NOT_REQUIRED",
        ),
        "context.suggest_change": (
            "allow" if suggestion_allowed else "deny",
            "SUGGESTION_ALLOWED" if suggestion_allowed else "SUGGESTION_FORBIDDEN",
        ),
    }
    policy_hash = hash_payload(
        {
            "surface_policy": surface,
            "action_hints": action_hints,
            "tool_hints": tool_hints,
        }
    )
    receipts: list[dict[str, Any]] = []
    for capability, (outcome, reason_code) in observations.items():
        request = {
            "request_hash": hash_payload(
                {
                    "capability": capability,
                    "startup_binding_hash": startup_binding_hash,
                }
            ),
            "capability": capability,
            "target_scope": "private"
            if capability == "context.read_private_raw"
            else "production"
            if capability == "authority.promote_without_approval_scope"
            else "context",
            "approval_scope_hash": "",
        }
        decision = {
            "outcome": outcome,
            "reason_code": reason_code,
            "policy_hash": policy_hash,
            "evidence_kind": "context_pack_policy_projection",
            "executor_invoked": False,
            "side_effect_count": 0,
        }
        receipt = {
            "schema_version": AGENT_CONTEXT_POLICY_DECISION_RECEIPT_SCHEMA,
            "startup_binding": {
                "startup_binding_hash": startup_binding_hash,
                "context_product_hash": context_product_hash,
                "process_instance_hash": process_instance_hash,
            },
            "request": request,
            "decision": decision,
            "decided_at": decided_at,
        }
        receipt["decision_hash"] = hash_payload(receipt)
        receipts.append(receipt)
    return receipts


def _challenge_hash(challenge: Mapping[str, Any]) -> str:
    return hash_payload({key: value for key, value in challenge.items() if key != "challenge_hash"})


def _challenge_time_failures(challenge: Mapping[str, Any], *, now: datetime) -> list[str]:
    failures: list[str] = []
    try:
        issued = _as_utc_datetime(str(challenge.get("issued_at") or ""))
        expires = _as_utc_datetime(str(challenge.get("expires_at") or ""))
    except ValueError:
        return ["agent_context_startup_challenge_time_invalid"]
    ttl = (expires - issued).total_seconds()
    if ttl <= 0 or ttl > MAX_CHALLENGE_TTL_SECONDS:
        failures.append("agent_context_startup_challenge_ttl_invalid")
    if now < issued:
        failures.append("agent_context_startup_challenge_not_yet_valid")
    if now > expires:
        failures.append("agent_context_startup_challenge_expired")
    return failures


def _startup_event_failures(events: list[Any]) -> list[str]:
    failures: list[str] = []
    expected_types = (
        "process_started",
        "context_requested",
        "context_loaded_before_task_dispatch",
    )
    if len(events) != len(expected_types):
        return ["agent_context_startup_event_sequence_incomplete"]
    previous = ""
    for index, (raw, expected_type) in enumerate(zip(events, expected_types, strict=True), start=1):
        if not isinstance(raw, Mapping):
            failures.append(f"agent_context_startup_event_invalid:{index}")
            continue
        event = dict(raw)
        if event.get("seq") != index or event.get("type") != expected_type:
            failures.append(f"agent_context_startup_event_order_mismatch:{index}")
        if event.get("prev_hash") != previous:
            failures.append(f"agent_context_startup_event_chain_mismatch:{index}")
        expected_hash = hash_payload({key: value for key, value in event.items() if key != "event_hash"})
        if event.get("event_hash") != expected_hash:
            failures.append(f"agent_context_startup_event_hash_mismatch:{index}")
        previous = str(event.get("event_hash") or "")
    return failures


def _policy_decision_failures(
    decisions: list[Any],
    *,
    startup_binding_hash: str,
    context_product_hash: str,
    process_instance_hash: str,
) -> list[str]:
    failures: list[str] = []
    by_capability: dict[str, Mapping[str, Any]] = {}
    for raw in decisions:
        if not isinstance(raw, Mapping):
            failures.append("agent_context_startup_policy_decision_invalid")
            continue
        request = _public_mapping(raw.get("request"))
        capability = str(request.get("capability") or "")
        if not capability or capability in by_capability:
            failures.append("agent_context_startup_policy_decision_duplicate_or_missing")
            continue
        by_capability[capability] = raw
    for capability, (expected_outcome, expected_reason) in REQUIRED_POLICY_DECISIONS.items():
        raw = by_capability.get(capability)
        if raw is None:
            failures.append(f"agent_context_startup_policy_decision_missing:{capability}")
            continue
        decision = _public_mapping(raw.get("decision"))
        binding = _public_mapping(raw.get("startup_binding"))
        if raw.get("schema_version") != AGENT_CONTEXT_POLICY_DECISION_RECEIPT_SCHEMA:
            failures.append(f"agent_context_startup_policy_schema_mismatch:{capability}")
        if binding != {
            "startup_binding_hash": startup_binding_hash,
            "context_product_hash": context_product_hash,
            "process_instance_hash": process_instance_hash,
        }:
            failures.append(f"agent_context_startup_policy_binding_mismatch:{capability}")
        if decision.get("outcome") != expected_outcome or decision.get("reason_code") != expected_reason:
            failures.append(f"agent_context_startup_policy_outcome_mismatch:{capability}")
        if decision.get("evidence_kind") != "context_pack_policy_projection":
            failures.append(f"agent_context_startup_policy_evidence_kind_mismatch:{capability}")
        if decision.get("executor_invoked") is not False or decision.get("side_effect_count") != 0:
            failures.append(f"agent_context_startup_policy_side_effect:{capability}")
        expected_hash = hash_payload({key: value for key, value in raw.items() if key != "decision_hash"})
        if raw.get("decision_hash") != expected_hash:
            failures.append(f"agent_context_startup_policy_hash_mismatch:{capability}")
    unknown = sorted(set(by_capability) - set(REQUIRED_POLICY_DECISIONS))
    failures.extend(f"agent_context_startup_policy_decision_unknown:{item}" for item in unknown)
    return failures


def _postcheck_failures(postcheck: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if postcheck.get("status") != "validated":
        failures.append("agent_context_startup_postcheck_missing")
    if postcheck.get("observation_basis") != "public_safe_adapter_output_check":
        failures.append("agent_context_startup_postcheck_observation_basis_missing")
    for field, gap in (
        ("raw_private_evidence_returned", "agent_context_startup_raw_private_evidence_returned"),
        ("secret_returned", "agent_context_startup_secret_returned"),
        ("host_topology_returned", "agent_context_startup_host_topology_returned"),
        ("raw_external_ids_returned", "agent_context_startup_raw_external_ids_returned"),
    ):
        if postcheck.get(field) is not False:
            failures.append(gap)
    return failures


def _proof_key(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise ValueError("proof key must contain at least 32 bytes")
    return value


def _proof_tag(key: bytes, *, challenge_hash: str, receipt_hash: str) -> str:
    digest = hmac.new(
        key,
        f"{challenge_hash}:{receipt_hash}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256:{digest}"


def _is_sha256_ref(value: Any) -> bool:
    text = str(value or "")
    if not text.startswith("sha256:"):
        return False
    digest = text.removeprefix("sha256:")
    return len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)


def _decision_outcome(decisions: Mapping[str, Mapping[str, Any]], capability: str) -> str:
    return public_safe_text(str(_public_mapping(decisions.get(capability)).get("outcome") or ""), max_chars=40)


def _as_utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("timestamp must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _public_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _public_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))

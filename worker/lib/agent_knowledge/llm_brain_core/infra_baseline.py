from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe, public_safe_text


COMPOSE_BASELINE_REPORT_SCHEMA = "compose_baseline_report.v1"
K3S_POC_CANARY_PLAN_SCHEMA = "k3s_poc_canary_plan.v1"
K3S_POC_MANIFEST_BUNDLE_SCHEMA = "k3s_poc_manifest_bundle.v1"
K3S_POC_OPERATOR_APPROVAL_PACKET_SCHEMA = "k3s_poc_operator_approval_packet.v1"
K3S_POC_EXECUTION_EVIDENCE_SCHEMA = "k3s_poc_execution_evidence.v1"
K3S_SCALE_OUT_BUNDLE_SCHEMA = "k3s_scale_out_bundle.v1"

_PASS_OUTCOMES = {"ok", "pass", "passed", "success", "succeeded"}
_K3S_CANARY_ACCESS_POLICIES = {"tailscale_private"}

# Manifest keys whose value is a capacity count owned by the private ops overlay.
# Public scale-out templates must not embed real counts; a 2+ digit literal under any
# of these keys is a leak. Single-digit policy invariants (replicas: 1, minAvailable: 1)
# are allowed; ports/weights live under other keys and are never inspected here.
_CAPACITY_KEYS = {
    "replicas",
    "minReplicas",
    "maxReplicas",
    "averageUtilization",
    "minAvailable",
    "maxUnavailable",
}

# The scale-out classification vocabulary. workload-inventory.yaml is the single source of
# truth; load_scale_out_workloads validates every workload against this set, so a missing or
# mistyped scaleCategory fails closed instead of silently drifting from the generator.
_SCALE_CATEGORIES = {
    "horizontally-scalable",
    "serialized-worker",
    "singleton-stateful",
    "not-a-target",
}


def compose_baseline_report(
    compose: Mapping[str, Any],
    *,
    dockerfiles: list[str],
) -> dict[str, Any]:
    services = compose.get("services") if isinstance(compose.get("services"), Mapping) else {}
    service_names = sorted(str(name) for name in services)
    profile_gated = sorted(
        str(name)
        for name, service in services.items()
        if isinstance(service, Mapping) and bool(service.get("profiles"))
    )
    healthchecked = sorted(
        str(name)
        for name, service in services.items()
        if isinstance(service, Mapping) and bool(service.get("healthcheck"))
    )
    restarted = sorted(
        str(name)
        for name, service in services.items()
        if isinstance(service, Mapping) and bool(service.get("restart"))
    )
    volume_services = sorted(
        str(name)
        for name, service in services.items()
        if isinstance(service, Mapping) and bool(service.get("volumes"))
    )
    warnings = [f"service_missing_healthcheck:{name}" for name in service_names if name not in healthchecked]
    safe_defaults = _safe_delivery_defaults(services)
    loopback_ports = _loopback_published_ports(services)
    ready = bool(service_names) and loopback_ports and safe_defaults["allow_live_queue_default"] == "0" and not warnings
    report = {
        "schema_version": COMPOSE_BASELINE_REPORT_SCHEMA,
        "status": "ready" if ready else "needs_attention",
        "runtime_target": "compose",
        "k3s_migration_implied": False,
        "dockerfiles": [public_safe_text(path, max_chars=180) for path in dockerfiles],
        "services": service_names,
        "profile_gated_services": profile_gated,
        "healthchecked_services": healthchecked,
        "restart_policy_services": restarted,
        "volume_services": volume_services,
        "loopback_published_ports": loopback_ports,
        "safe_delivery_defaults": safe_defaults,
        "warnings": warnings,
    }
    ensure_public_safe(report, "ComposeBaselineReport")
    return report


def k3s_poc_canary_plan(
    *,
    namespace: str,
    canary_workloads: list[Mapping[str, Any]],
    access_policy: str,
    rollback_target: str,
) -> dict[str, Any]:
    safe_namespace = public_safe_text(namespace, max_chars=120)
    safe_access = public_safe_text(access_policy, max_chars=120).strip().lower()
    safe_rollback = public_safe_text(rollback_target, max_chars=120)
    if safe_namespace in {"default", "prod", "production"} or safe_access not in _K3S_CANARY_ACCESS_POLICIES:
        raise ValueError("production k3s migration is not part of this roadmap")
    if safe_rollback != "compose":
        raise ValueError("k3s PoC rollback target must be compose")
    workloads = [_canary_workload(item) for item in canary_workloads]
    if any(item["stateful"] for item in workloads):
        raise ValueError("stateful DB migration is not part of the k3s PoC")
    plan = {
        "schema_version": K3S_POC_CANARY_PLAN_SCHEMA,
        "status": "ready_to_review",
        "namespace": safe_namespace,
        "production_migration_implied": False,
        "stateful_db_migration_allowed": False,
        "access_policy": safe_access,
        "canary_order": [item["name"] for item in workloads],
        "canary_workloads": workloads,
        "rollback": {
            "target": "compose",
            "steps": [
                "scale_down_k3s_canary",
                "restore_compose_service",
                "verify_compose_baseline_report",
            ],
        },
        "requires_operator_approval": True,
    }
    ensure_public_safe(plan, "K3sPocCanaryPlan")
    return plan


def k3s_poc_canary_manifest_bundle(
    plan: Mapping[str, Any],
    *,
    image_by_workload: Mapping[str, str],
    container_port_by_workload: Mapping[str, int],
) -> dict[str, Any]:
    if plan.get("schema_version") != K3S_POC_CANARY_PLAN_SCHEMA:
        raise ValueError("k3s PoC manifest bundle requires a k3s PoC canary plan")
    if plan.get("production_migration_implied") is not False:
        raise ValueError("production k3s migration is not part of this roadmap")
    if plan.get("stateful_db_migration_allowed") is not False:
        raise ValueError("stateful DB migration is not part of the k3s PoC")
    namespace = public_safe_text(str(plan.get("namespace") or ""), max_chars=120)
    workloads = plan.get("canary_workloads") if isinstance(plan.get("canary_workloads"), list) else []
    resources = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": {
                    "neurons.openclaw.dev/purpose": "context-authority-canary",
                    "neurons.openclaw.dev/production": "false",
                },
            },
        }
    ]
    rollback_commands: list[str] = []
    for workload in workloads:
        if not isinstance(workload, Mapping):
            continue
        if workload.get("stateful") is not False:
            raise ValueError("stateful DB migration is not part of the k3s PoC")
        name = public_safe_text(str(workload.get("name") or ""), max_chars=120)
        image = public_safe_text(str(image_by_workload.get(name) or ""), max_chars=240)
        if not image:
            raise ValueError("canary workload image is required")
        port = int(container_port_by_workload.get(name) or 0)
        if port <= 0:
            raise ValueError("canary workload port is required")
        resources.append(_deployment_resource(name=name, namespace=namespace, image=image, container_port=port))
        rollback_commands.append(f"kubectl -n {namespace} scale deployment/{name} --replicas=0")
    if plan.get("access_policy") == "tailscale_private":
        resources.append(_tailscale_private_network_policy(namespace))
    rollback_commands.extend(
        [
            "docker compose up -d",
        ]
    )
    bundle = {
        "schema_version": K3S_POC_MANIFEST_BUNDLE_SCHEMA,
        "status": "ready_for_operator_review",
        "namespace": namespace,
        "production_migration_implied": False,
        "stateful_db_migration_allowed": False,
        "resources": resources,
        "rollback_commands": rollback_commands,
        "requires_operator_approval": True,
    }
    ensure_public_safe(bundle, "K3sPocManifestBundle")
    return bundle


def k3s_poc_operator_approval_packet(
    bundle: Mapping[str, Any],
    *,
    manifest_path: str,
    compose_check_command: str,
) -> dict[str, Any]:
    if bundle.get("schema_version") != K3S_POC_MANIFEST_BUNDLE_SCHEMA:
        raise ValueError("operator approval packet requires a k3s PoC manifest bundle")
    if bundle.get("requires_operator_approval") is not True:
        raise ValueError("operator approval packet requires approval-gated bundle")
    safe_manifest_path = public_safe_text(manifest_path, max_chars=240)
    safe_compose_check = public_safe_text(compose_check_command, max_chars=240)
    namespace = public_safe_text(str(bundle.get("namespace") or ""), max_chars=120)
    deployments = [
        str(resource.get("metadata", {}).get("name") or "")
        for resource in bundle.get("resources") or []
        if isinstance(resource, Mapping) and resource.get("kind") == "Deployment"
    ]
    packet = {
        "schema_version": K3S_POC_OPERATOR_APPROVAL_PACKET_SCHEMA,
        "status": "awaiting_operator_approval",
        "approved": False,
        "manifest_path": safe_manifest_path,
        "dry_run_commands": [
            f"kubectl apply --dry-run=server -f {safe_manifest_path}",
        ],
        "apply_commands": [
            f"kubectl apply -f {safe_manifest_path}",
        ],
        "postcheck_commands": [
            *[f"kubectl -n {namespace} rollout status deployment/{name}" for name in deployments],
            f"kubectl -n {namespace} get pods",
        ],
        "rollback_commands": [
            *list(bundle.get("rollback_commands") or []),
            safe_compose_check,
        ],
        "rollback_proof_required": True,
        "external_mutation": True,
        "requires_operator_approval": True,
    }
    ensure_public_safe(packet, "K3sPocOperatorApprovalPacket")
    return packet


def k3s_poc_execution_evidence(
    packet: Mapping[str, Any],
    *,
    approval_record: Mapping[str, Any],
    command_results: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if packet.get("schema_version") != K3S_POC_OPERATOR_APPROVAL_PACKET_SCHEMA:
        raise ValueError("k3s PoC execution evidence requires an operator approval packet")
    safe_approval = _safe_approval_record(approval_record)
    namespace = _namespace_from_packet(packet)
    blocking_codes: list[str] = []
    executed_commands: list[str] = []
    approved = safe_approval.get("approved") is True
    if not approved:
        blocking_codes.append("operator_approval_missing")
    dry_run_blockers = _phase_blockers(
        packet,
        command_results,
        phase="dry_run",
        command_key="dry_run_commands",
        missing_code="dry_run_proof_missing",
        failed_code="dry_run_command_failed",
        executed_commands=executed_commands,
    )
    apply_blockers = _phase_blockers(
        packet,
        command_results,
        phase="apply",
        command_key="apply_commands",
        missing_code="apply_proof_missing",
        failed_code="apply_command_failed",
        executed_commands=executed_commands,
    )
    postcheck_blockers = _phase_blockers(
        packet,
        command_results,
        phase="postcheck",
        command_key="postcheck_commands",
        missing_code="postcheck_proof_missing",
        failed_code="postcheck_command_failed",
        executed_commands=executed_commands,
    )
    rollback_blockers = _phase_blockers(
        packet,
        command_results,
        phase="rollback",
        command_key="rollback_commands",
        missing_code="rollback_proof_missing",
        failed_code="rollback_command_failed",
        executed_commands=executed_commands,
    )
    if approved:
        blocking_codes.extend(dry_run_blockers)
        blocking_codes.extend(apply_blockers)
        blocking_codes.extend(postcheck_blockers)
        blocking_codes.extend(rollback_blockers)
    canary_proved = approved and not dry_run_blockers and not apply_blockers and not postcheck_blockers
    rollback_proved = approved and canary_proved and not rollback_blockers
    evidence = {
        "schema_version": K3S_POC_EXECUTION_EVIDENCE_SCHEMA,
        "status": "proved" if canary_proved and rollback_proved else "blocked",
        "namespace": namespace,
        "approval_record": safe_approval,
        "canary_proved": canary_proved,
        "rollback_proved": rollback_proved,
        "external_mutation": packet.get("external_mutation") is True,
        "proof_required": {
            "approval": True,
            "dry_run": True,
            "apply": True,
            "postcheck": True,
            "rollback": packet.get("rollback_proof_required") is True,
        },
        "executed_commands": executed_commands,
        "blocking_codes": blocking_codes,
    }
    ensure_public_safe(evidence, "K3sPocExecutionEvidence")
    return evidence


def load_scale_out_workloads(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Classify workloads from a parsed workload-inventory.yaml mapping.

    Reads each ``workloads[].id`` with its ``scaleCategory``/``replicaPolicy`` and validates
    the category against ``_SCALE_CATEGORIES``. This is the seam that ties the inventory SoT to
    ``scale_out_manifest_bundle``: an unclassified or mistyped workload raises here rather than
    drifting from the generator's routing.
    """
    entries = inventory.get("workloads") if isinstance(inventory.get("workloads"), list) else []
    classified: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = public_safe_text(str(entry.get("id") or ""), max_chars=120)
        category = public_safe_text(str(entry.get("scaleCategory") or ""), max_chars=80)
        policy = public_safe_text(str(entry.get("replicaPolicy") or ""), max_chars=40)
        if not name:
            continue
        if category not in _SCALE_CATEGORIES:
            raise ValueError(f"workload {name} has unknown or missing scaleCategory: {category!r}")
        classified.append({"name": name, "scaleCategory": category, "replicaPolicy": policy})
    return classified


def reject_capacity_integers(resource: Any) -> None:
    """Reject 2+ digit integer literals under capacity keys in a public manifest.

    Real replica/HPA/PDB counts are owned by the private ops overlay and must never
    appear in a public scale-out template. This is a key-scoped guard (not a blanket
    integer scan) so legitimate ``containerPort: 8080`` / ``weight: 100`` values pass.
    """
    if isinstance(resource, Mapping):
        for key, value in resource.items():
            if key in _CAPACITY_KEYS and _is_leaked_capacity_count(value):
                raise ValueError(
                    f"public scale-out manifest must not embed capacity counts: "
                    f"{key}={value!r} is overlay-owned"
                )
            reject_capacity_integers(value)
    elif isinstance(resource, (list, tuple)):
        for item in resource:
            reject_capacity_integers(item)


def _is_leaked_capacity_count(value: Any) -> bool:
    """True if value is a 2+ digit capacity count in int, float, or quoted-string form.

    Covers bypass shapes (``"10"``, ``10.0``) as well as plain ints. bool is excluded
    (True/False are not counts) and non-numeric strings (e.g. a policy marker) pass.
    """
    if isinstance(value, bool):
        return False
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return False
    return as_float.is_integer() and as_float >= 10


def scale_out_manifest_bundle(
    *,
    workloads: list[Mapping[str, Any]],
    namespace: str,
    access_policy: str,
    image_by_workload: Mapping[str, str],
    container_port_by_workload: Mapping[str, int],
) -> dict[str, Any]:
    """Build a public, count-free scale-out manifest skeleton from classified workloads.

    ``workloads`` is the classification (name/scaleCategory/replicaPolicy) produced by
    ``load_scale_out_workloads``; images and ports are supplied separately (the same
    overlay-owned mapping pattern as the canary bundle). Separate from the canary bundle: it
    never embeds real replica/HPA/PDB counts, routes each workload by its scaleCategory,
    excludes not-a-target, and makes singleton-stateful a single-writer StatefulSet (never a
    multi-replica Deployment).
    """
    safe_namespace = public_safe_text(str(namespace or ""), max_chars=120)
    if safe_namespace in {"default", "prod", "production"}:
        raise ValueError("production k3s migration is not part of this roadmap")
    safe_access = public_safe_text(str(access_policy or ""), max_chars=120).strip().lower()
    if safe_access not in _K3S_CANARY_ACCESS_POLICIES:
        # tailnet-only NetworkPolicy is the isolation gate; an unsupported access policy must
        # fail closed rather than emit a bundle without isolation (mirrors the canary plan).
        raise ValueError("scale-out access_policy must be tailscale_private")
    resources: list[dict[str, Any]] = [_namespace_resource(safe_namespace)]
    for workload in workloads:
        if not isinstance(workload, Mapping):
            continue
        name = public_safe_text(str(workload.get("name") or ""), max_chars=120)
        category = public_safe_text(str(workload.get("scaleCategory") or ""), max_chars=80)
        replica_policy = public_safe_text(str(workload.get("replicaPolicy") or ""), max_chars=40)
        if category == "not-a-target":
            continue
        if not name:
            raise ValueError("scale-out workload name is required")
        if category not in _SCALE_CATEGORIES:
            raise ValueError(f"unknown scaleCategory: {category!r}")
        image = public_safe_text(str(image_by_workload.get(name) or ""), max_chars=240)
        port = int(container_port_by_workload.get(name) or 0)
        if category == "singleton-stateful":
            _require_image_and_port(image, port)
            resources.append(_statefulset_resource(name=name, namespace=safe_namespace, image=image, container_port=port))
            resources.append(_headless_service_resource(name=name, namespace=safe_namespace, container_port=port))
            resources.append(_pdb_resource(name=name, namespace=safe_namespace, mode="minAvailable"))
        elif category == "serialized-worker":
            _require_image_and_port(image, port)
            resources.append(
                _deployment_resource(
                    name=name, namespace=safe_namespace, image=image, container_port=port, replica_policy="single"
                )
            )
            resources.append(_pdb_resource(name=name, namespace=safe_namespace, mode="minAvailable"))
        elif category == "horizontally-scalable":
            _require_image_and_port(image, port)
            # One effective policy drives both the Deployment shape and the HPA/PDB choice,
            # so a blank policy can't yield an ops-defined Deployment without its HPA.
            effective_policy = replica_policy or "ops-defined"
            if effective_policy not in {"ops-defined", "single"}:
                # A typo (e.g. "ops-define") would silently fall through to single-replica;
                # fail closed instead so a horizontally-scalable workload can't be demoted.
                raise ValueError(
                    f"unknown replicaPolicy for horizontally-scalable workload {name}: {effective_policy!r}"
                )
            deployment = _deployment_resource(
                name=name,
                namespace=safe_namespace,
                image=image,
                container_port=port,
                replica_policy=effective_policy,
            )
            deployment["spec"]["template"]["spec"]["affinity"] = _pod_anti_affinity(name)
            resources.append(deployment)
            if effective_policy == "ops-defined":
                resources.append(_hpa_resource(name=name, namespace=safe_namespace))
                # multi-replica: maxUnavailable keeps at least N-1 Pods during disruption.
                resources.append(_pdb_resource(name=name, namespace=safe_namespace, mode="maxUnavailable"))
            else:
                # single replica (e.g. mcp-http until host networking is removed):
                # maxUnavailable:1 would permit evicting the only Pod, so guard with minAvailable.
                resources.append(_pdb_resource(name=name, namespace=safe_namespace, mode="minAvailable"))
    if safe_access == "tailscale_private":
        # NOTE: the default k3s flannel backend parses but does NOT enforce NetworkPolicy.
        # Enforcement (CNI selection) is a private ops-overlay concern (cniSelection).
        resources.append(_tailscale_private_network_policy(safe_namespace))
    for resource in resources:
        reject_capacity_integers(resource)
    bundle = {
        "schema_version": K3S_SCALE_OUT_BUNDLE_SCHEMA,
        "status": "ready_for_operator_review",
        "namespace": safe_namespace,
        "production_migration_implied": False,
        "resources": resources,
        "requires_operator_approval": True,
    }
    ensure_public_safe(bundle, "K3sScaleOutBundle")
    return bundle


def _require_image_and_port(image: str, port: int) -> None:
    if not image:
        raise ValueError("scale-out workload image is required")
    if port <= 0:
        raise ValueError("scale-out workload port is required")


def _namespace_resource(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {
                "neurons.openclaw.dev/purpose": "context-authority-scale-out",
                "neurons.openclaw.dev/production": "false",
            },
        },
    }


def _deployment_resource(
    *,
    name: str,
    namespace: str,
    image: str,
    container_port: int,
    replica_policy: str = "canary",
) -> dict[str, Any]:
    runtime_mode = "k3s-canary" if replica_policy == "canary" else "k3s"
    metadata: dict[str, Any] = {"name": name, "namespace": namespace}
    spec: dict[str, Any] = {
        "selector": {"matchLabels": {"app": name}},
        "template": {
            "metadata": {"labels": {"app": name}},
            "spec": {
                "containers": [
                    {
                        "name": name,
                        "image": image,
                        "ports": [{"containerPort": container_port}],
                        "env": [
                            {"name": "NEURONS_RUNTIME_MODE", "value": runtime_mode},
                            {"name": "NEURONS_STATEFUL_DB_MIGRATION", "value": "false"},
                        ],
                    }
                ]
            },
        },
    }
    if replica_policy == "ops-defined":
        # replicas count is overlay-owned; mark policy and let the overlay patch it in.
        metadata["annotations"] = {"neurons.scale/replica-policy": "ops-defined"}
    else:
        spec = {"replicas": 1, **spec}
        if replica_policy != "canary":
            metadata["annotations"] = {"neurons.scale/replica-policy": replica_policy}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata, "spec": spec}


def _statefulset_resource(*, name: str, namespace: str, image: str, container_port: int) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {"neurons.scale/replica-policy": "singleton"},
        },
        "spec": {
            "serviceName": f"{name}-headless",
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": image,
                            "ports": [{"containerPort": container_port}],
                        }
                    ]
                },
            },
            "volumeClaimTemplates": [
                {
                    "metadata": {
                        "name": f"{name}-data",
                        "annotations": {"neurons.scale/storage": "ops-defined"},
                    },
                    # storageClassName + real request size are overlay-owned (PVC sizing). A
                    # safe-default 1Gi request keeps the PVC schema valid; overlay patches the
                    # real size.
                    "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
                }
            ],
        },
    }


def _headless_service_resource(*, name: str, namespace: str, container_port: int) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"{name}-headless", "namespace": namespace},
        "spec": {
            "clusterIP": "None",
            "selector": {"app": name},
            "ports": [{"port": container_port, "targetPort": container_port}],
        },
    }


def _hpa_resource(*, name: str, namespace: str) -> dict[str, Any]:
    # Skeleton only: minReplicas/metric targets are overlay-owned counts and are omitted.
    # maxReplicas is required by the autoscaling/v2 schema, so a single-digit safe default (1)
    # keeps the template valid (kubectl/ArgoCD) while leaking no production count; the real
    # bound is overlay-patched.
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {"neurons.scale/hpa": "ops-defined"},
        },
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": name},
            "maxReplicas": 1,
        },
    }


def _pdb_resource(*, name: str, namespace: str, mode: str) -> dict[str, Any]:
    if mode not in {"minAvailable", "maxUnavailable"}:
        raise ValueError("pdb mode must be minAvailable or maxUnavailable")
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {mode: 1, "selector": {"matchLabels": {"app": name}}},
    }


def _pod_anti_affinity(name: str) -> dict[str, Any]:
    # preferred (not required): on a single node, required anti-affinity would leave
    # replicas Pending. Spread is best-effort until ops adds agent nodes.
    return {
        "podAntiAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "weight": 100,
                    "podAffinityTerm": {
                        "labelSelector": {"matchLabels": {"app": name}},
                        "topologyKey": "kubernetes.io/hostname",
                    },
                }
            ]
        }
    }


def _tailscale_private_network_policy(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "tailscale-private-only", "namespace": namespace},
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": "tailscale"}
                            }
                        }
                    ]
                }
            ],
        },
    }


def _canary_workload(workload: Mapping[str, Any]) -> dict[str, Any]:
    name = public_safe_text(str(workload.get("name") or ""), max_chars=120)
    kind = public_safe_text(str(workload.get("kind") or "Deployment"), max_chars=80)
    stateful = bool(workload.get("stateful")) or kind == "StatefulSet"
    if not stateful and kind != "Deployment":
        raise ValueError("k3s PoC canary workloads currently support Deployment only")
    return {"name": name, "kind": kind, "stateful": stateful}


def _safe_approval_record(approval_record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "approved": approval_record.get("approved") is True,
        "approved_by": public_safe_text(str(approval_record.get("approved_by") or ""), max_chars=120),
        "target": public_safe_text(str(approval_record.get("target") or ""), max_chars=160),
    }


def _namespace_from_packet(packet: Mapping[str, Any]) -> str:
    for command_key in ("postcheck_commands", "rollback_commands"):
        for command in packet.get(command_key) or []:
            match = re.search(r"\bkubectl\s+-n\s+([a-zA-Z0-9_.-]+)\b", str(command))
            if match:
                return public_safe_text(match.group(1), max_chars=120)
    return ""


def _phase_blockers(
    packet: Mapping[str, Any],
    command_results: list[Mapping[str, Any]],
    *,
    phase: str,
    command_key: str,
    missing_code: str,
    failed_code: str,
    executed_commands: list[str],
) -> list[str]:
    blockers: list[str] = []
    result_by_command = _results_by_command(command_results, phase=phase)
    for expected in packet.get(command_key) or []:
        command = public_safe_text(str(expected), max_chars=280)
        result = result_by_command.get(command)
        if result is None:
            blockers.append(f"{missing_code}:{command}")
            continue
        if _result_passed(result):
            executed_commands.append(command)
            continue
        blockers.append(f"{failed_code}:{command}")
    return blockers


def _results_by_command(command_results: list[Mapping[str, Any]], *, phase: str) -> dict[str, Mapping[str, Any]]:
    results: dict[str, Mapping[str, Any]] = {}
    for result in command_results:
        if public_safe_text(str(result.get("phase") or ""), max_chars=40) != phase:
            continue
        command = public_safe_text(str(result.get("command") or ""), max_chars=280)
        if command:
            results[command] = result
    return results


def _result_passed(result: Mapping[str, Any]) -> bool:
    outcome = public_safe_text(str(result.get("outcome") or ""), max_chars=40).lower()
    return result.get("exit_code") == 0 and outcome in _PASS_OUTCOMES


def _safe_delivery_defaults(services: Mapping[str, Any]) -> dict[str, str]:
    worker = services.get("ingress-worker-py")
    env = worker.get("environment") if isinstance(worker, Mapping) and isinstance(worker.get("environment"), Mapping) else {}
    return {
        "shadow_stream_default": _env_default(str(env.get("SHADOW_STREAM") or ""), fallback=""),
        "allow_live_queue_default": _env_default(str(env.get("ALLOW_LIVE_QUEUE") or ""), fallback=""),
        "deliver_default": _env_default(str(env.get("SHADOW_DELIVER") or ""), fallback=""),
    }


def _env_default(value: str, *, fallback: str) -> str:
    match = re.fullmatch(r"\$\{[^:}]+:-(.*)\}", value)
    return match.group(1) if match else (value or fallback)


def _loopback_published_ports(services: Mapping[str, Any]) -> bool:
    for service in services.values():
        if not isinstance(service, Mapping):
            continue
        for port in service.get("ports") or []:
            text = str(port)
            if not text.startswith("127.0.0.1:"):
                return False
    return True

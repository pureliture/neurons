from pathlib import Path

import pytest
import yaml

from agent_knowledge.llm_brain_core.infra_baseline import (
    compose_baseline_report,
    k3s_poc_canary_manifest_bundle,
    k3s_poc_execution_evidence,
    k3s_poc_operator_approval_packet,
    k3s_poc_canary_plan,
    load_scale_out_workloads,
    reject_capacity_integers,
    scale_out_manifest_bundle,
)

_INVENTORY = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "k3s"
    / "public-contract"
    / "workload-inventory.yaml"
)


def _resource(bundle, kind, name):
    for resource in bundle["resources"]:
        if resource.get("kind") == kind and resource.get("metadata", {}).get("name") == name:
            return resource
    return None


def test_compose_baseline_report_keeps_compose_target_and_safe_delivery_defaults():
    compose = {
        "services": {
            "nats-jetstream": {
                "image": "nats:2-alpine",
                "restart": "unless-stopped",
                "healthcheck": {"test": ["CMD", "wget", "http://127.0.0.1:8222/healthz"]},
                "ports": ["127.0.0.1:4222:4222"],
                "volumes": ["nats_data:/data"],
            },
            "ingress-worker-py": {
                "build": "./worker",
                "restart": "unless-stopped",
                "environment": {
                    "SHADOW_STREAM": "${RAG_INGRESS_STREAM:-RAG_INGRESS_SHADOW}",
                    "ALLOW_LIVE_QUEUE": "${RAG_INGRESS_ALLOW_LIVE_QUEUE:-0}",
                    "SHADOW_DELIVER": "${RAG_INGRESS_DELIVER:-0}",
                },
                "security_opt": ["no-new-privileges:true"],
                "volumes": ["rag-ingress-live-state:/var/lib/agent-knowledge/ingest-state"],
            },
            "qdrant": {
                "profiles": ["searchable-mirror"],
                "image": "qdrant/qdrant:latest",
                "restart": "unless-stopped",
                "ports": ["127.0.0.1:6333:6333"],
                "volumes": ["qdrant_data:/qdrant/storage"],
            },
        }
    }

    report = compose_baseline_report(
        compose,
        dockerfiles=["Dockerfile", "worker/Dockerfile"],
    )

    assert report == {
        "schema_version": "compose_baseline_report.v1",
        "status": "needs_attention",
        "runtime_target": "compose",
        "k3s_migration_implied": False,
        "dockerfiles": ["Dockerfile", "worker/Dockerfile"],
        "services": ["ingress-worker-py", "nats-jetstream", "qdrant"],
        "profile_gated_services": ["qdrant"],
        "healthchecked_services": ["nats-jetstream"],
        "restart_policy_services": ["ingress-worker-py", "nats-jetstream", "qdrant"],
        "volume_services": ["ingress-worker-py", "nats-jetstream", "qdrant"],
        "loopback_published_ports": True,
        "safe_delivery_defaults": {
            "shadow_stream_default": "RAG_INGRESS_SHADOW",
            "allow_live_queue_default": "0",
            "deliver_default": "0",
        },
        "warnings": ["service_missing_healthcheck:ingress-worker-py", "service_missing_healthcheck:qdrant"],
    }


def test_compose_baseline_report_requires_healthchecks_for_ready_status():
    compose = {
        "services": {
            "ingress-worker-py": {
                "healthcheck": {"test": ["CMD", "true"]},
                "environment": {
                    "ALLOW_LIVE_QUEUE": "${RAG_INGRESS_ALLOW_LIVE_QUEUE:-0}",
                },
                "ports": ["127.0.0.1:8080:8080"],
            }
        }
    }

    report = compose_baseline_report(compose, dockerfiles=["worker/Dockerfile"])

    assert report["status"] == "ready"
    assert report["warnings"] == []


def test_k3s_poc_canary_plan_is_non_production_stateless_and_rollbackable():
    plan = k3s_poc_canary_plan(
        namespace="neurons-canary",
        canary_workloads=[
            {"name": "llm-brain-mcp", "kind": "Deployment", "stateful": False},
            {"name": "context-resolver-worker", "kind": "Deployment", "stateful": False},
        ],
        access_policy="tailscale_private",
        rollback_target="compose",
    )

    assert plan == {
        "schema_version": "k3s_poc_canary_plan.v1",
        "status": "ready_to_review",
        "namespace": "neurons-canary",
        "production_migration_implied": False,
        "stateful_db_migration_allowed": False,
        "access_policy": "tailscale_private",
        "canary_order": ["llm-brain-mcp", "context-resolver-worker"],
        "canary_workloads": [
            {"name": "llm-brain-mcp", "kind": "Deployment", "stateful": False},
            {"name": "context-resolver-worker", "kind": "Deployment", "stateful": False},
        ],
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


def test_k3s_poc_canary_plan_rejects_stateful_or_production_migration():
    with pytest.raises(ValueError, match="stateful DB migration is not part of the k3s PoC"):
        k3s_poc_canary_plan(
            namespace="neurons-canary",
            canary_workloads=[{"name": "postgres", "kind": "StatefulSet", "stateful": True}],
            access_policy="tailscale_private",
            rollback_target="compose",
        )

    with pytest.raises(ValueError, match="production k3s migration is not part of this roadmap"):
        k3s_poc_canary_plan(
            namespace="production",
            canary_workloads=[{"name": "llm-brain-mcp", "kind": "Deployment", "stateful": False}],
            access_policy="public",
            rollback_target="compose",
        )

    with pytest.raises(ValueError, match="production k3s migration is not part of this roadmap"):
        k3s_poc_canary_plan(
            namespace="neurons-canary",
            canary_workloads=[{"name": "llm-brain-mcp", "kind": "Deployment", "stateful": False}],
            access_policy="nodeport",
            rollback_target="compose",
        )

    with pytest.raises(ValueError, match="k3s PoC canary workloads currently support Deployment only"):
        k3s_poc_canary_plan(
            namespace="neurons-canary",
            canary_workloads=[{"name": "batch-canary", "kind": "Job", "stateful": False}],
            access_policy="tailscale_private",
            rollback_target="compose",
        )


def test_k3s_poc_canary_manifest_bundle_keeps_canary_stateless_private_and_rollbackable():
    plan = k3s_poc_canary_plan(
        namespace="neurons-canary",
        canary_workloads=[{"name": "llm-brain-mcp", "kind": "Deployment", "stateful": False}],
        access_policy="tailscale_private",
        rollback_target="compose",
    )

    bundle = k3s_poc_canary_manifest_bundle(
        plan,
        image_by_workload={"llm-brain-mcp": "neurons-worker:canary"},
        container_port_by_workload={"llm-brain-mcp": 8080},
    )

    assert bundle == {
        "schema_version": "k3s_poc_manifest_bundle.v1",
        "status": "ready_for_operator_review",
        "namespace": "neurons-canary",
        "production_migration_implied": False,
        "stateful_db_migration_allowed": False,
        "resources": [
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": "neurons-canary",
                    "labels": {
                        "neurons.openclaw.dev/purpose": "context-authority-canary",
                        "neurons.openclaw.dev/production": "false",
                    },
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "llm-brain-mcp", "namespace": "neurons-canary"},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "llm-brain-mcp"}},
                    "template": {
                        "metadata": {"labels": {"app": "llm-brain-mcp"}},
                        "spec": {
                            "containers": [
                                {
                                    "name": "llm-brain-mcp",
                                    "image": "neurons-worker:canary",
                                    "ports": [{"containerPort": 8080}],
                                    "env": [
                                        {"name": "NEURONS_RUNTIME_MODE", "value": "k3s-canary"},
                                        {"name": "NEURONS_STATEFUL_DB_MIGRATION", "value": "false"},
                                    ],
                                }
                            ]
                        },
                    },
                },
            },
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "tailscale-private-only", "namespace": "neurons-canary"},
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
            },
        ],
        "rollback_commands": [
            "kubectl -n neurons-canary scale deployment/llm-brain-mcp --replicas=0",
            "docker compose up -d",
        ],
        "requires_operator_approval": True,
    }


def test_k3s_poc_operator_approval_packet_lists_dry_run_apply_postcheck_and_rollback_proof():
    plan = k3s_poc_canary_plan(
        namespace="neurons-canary",
        canary_workloads=[{"name": "llm-brain-mcp", "kind": "Deployment", "stateful": False}],
        access_policy="tailscale_private",
        rollback_target="compose",
    )
    bundle = k3s_poc_canary_manifest_bundle(
        plan,
        image_by_workload={"llm-brain-mcp": "neurons-worker:canary"},
        container_port_by_workload={"llm-brain-mcp": 8080},
    )

    packet = k3s_poc_operator_approval_packet(
        bundle,
        manifest_path="k8s/canary/context-authority.yaml",
        compose_check_command="docker compose ps",
    )

    assert packet == {
        "schema_version": "k3s_poc_operator_approval_packet.v1",
        "status": "awaiting_operator_approval",
        "approved": False,
        "manifest_path": "k8s/canary/context-authority.yaml",
        "dry_run_commands": [
            "kubectl apply --dry-run=server -f k8s/canary/context-authority.yaml",
        ],
        "apply_commands": [
            "kubectl apply -f k8s/canary/context-authority.yaml",
        ],
        "postcheck_commands": [
            "kubectl -n neurons-canary rollout status deployment/llm-brain-mcp",
            "kubectl -n neurons-canary get pods",
        ],
        "rollback_commands": [
            "kubectl -n neurons-canary scale deployment/llm-brain-mcp --replicas=0",
            "docker compose up -d",
            "docker compose ps",
        ],
        "rollback_proof_required": True,
        "external_mutation": True,
        "requires_operator_approval": True,
    }


def test_k3s_poc_execution_evidence_proves_approved_canary_and_rollback():
    packet = _k3s_operator_packet()

    evidence = k3s_poc_execution_evidence(
        packet,
        approval_record={
            "approved": True,
            "approved_by": "operator",
            "target": "non-production k3s lab",
        },
        command_results=[
            {"phase": "dry_run", "command": packet["dry_run_commands"][0], "exit_code": 0, "outcome": "pass"},
            {"phase": "apply", "command": packet["apply_commands"][0], "exit_code": 0, "outcome": "pass"},
            {
                "phase": "postcheck",
                "command": packet["postcheck_commands"][0],
                "exit_code": 0,
                "outcome": "pass",
            },
            {
                "phase": "postcheck",
                "command": packet["postcheck_commands"][1],
                "exit_code": 0,
                "outcome": "pass",
            },
            {
                "phase": "rollback",
                "command": packet["rollback_commands"][0],
                "exit_code": 0,
                "outcome": "pass",
            },
            {
                "phase": "rollback",
                "command": packet["rollback_commands"][1],
                "exit_code": 0,
                "outcome": "pass",
            },
            {
                "phase": "rollback",
                "command": packet["rollback_commands"][2],
                "exit_code": 0,
                "outcome": "pass",
            },
        ],
    )

    assert evidence == {
        "schema_version": "k3s_poc_execution_evidence.v1",
        "status": "proved",
        "namespace": "neurons-canary",
        "approval_record": {
            "approved": True,
            "approved_by": "operator",
            "target": "non-production k3s lab",
        },
        "canary_proved": True,
        "rollback_proved": True,
        "external_mutation": True,
        "proof_required": {
            "approval": True,
            "dry_run": True,
            "apply": True,
            "postcheck": True,
            "rollback": True,
        },
        "executed_commands": [
            "kubectl apply --dry-run=server -f k8s/canary/context-authority.yaml",
            "kubectl apply -f k8s/canary/context-authority.yaml",
            "kubectl -n neurons-canary rollout status deployment/llm-brain-mcp",
            "kubectl -n neurons-canary get pods",
            "kubectl -n neurons-canary scale deployment/llm-brain-mcp --replicas=0",
            "docker compose up -d",
            "docker compose ps",
        ],
        "blocking_codes": [],
    }


def test_k3s_poc_execution_evidence_requires_rollback_proof():
    packet = _k3s_operator_packet()

    evidence = k3s_poc_execution_evidence(
        packet,
        approval_record={"approved": True, "approved_by": "operator", "target": "non-production k3s lab"},
        command_results=[
            {"phase": "dry_run", "command": packet["dry_run_commands"][0], "exit_code": 0, "outcome": "pass"},
            {"phase": "apply", "command": packet["apply_commands"][0], "exit_code": 0, "outcome": "pass"},
            {
                "phase": "postcheck",
                "command": packet["postcheck_commands"][0],
                "exit_code": 0,
                "outcome": "pass",
            },
            {
                "phase": "postcheck",
                "command": packet["postcheck_commands"][1],
                "exit_code": 0,
                "outcome": "pass",
            },
        ],
    )

    assert evidence["status"] == "blocked"
    assert evidence["canary_proved"] is True
    assert evidence["rollback_proved"] is False
    assert evidence["blocking_codes"] == [
        "rollback_proof_missing:kubectl -n neurons-canary scale deployment/llm-brain-mcp --replicas=0",
        "rollback_proof_missing:docker compose up -d",
        "rollback_proof_missing:docker compose ps",
    ]


def test_k3s_poc_execution_evidence_requires_explicit_operator_approval():
    packet = _k3s_operator_packet()

    evidence = k3s_poc_execution_evidence(
        packet,
        approval_record={"approved": False, "approved_by": "operator", "target": "non-production k3s lab"},
        command_results=[
            {"phase": "dry_run", "command": packet["dry_run_commands"][0], "exit_code": 0, "outcome": "pass"},
            {"phase": "apply", "command": packet["apply_commands"][0], "exit_code": 0, "outcome": "pass"},
            {
                "phase": "postcheck",
                "command": packet["postcheck_commands"][0],
                "exit_code": 0,
                "outcome": "pass",
            },
            {
                "phase": "postcheck",
                "command": packet["postcheck_commands"][1],
                "exit_code": 0,
                "outcome": "pass",
            },
            {
                "phase": "rollback",
                "command": packet["rollback_commands"][0],
                "exit_code": 0,
                "outcome": "pass",
            },
            {
                "phase": "rollback",
                "command": packet["rollback_commands"][1],
                "exit_code": 0,
                "outcome": "pass",
            },
            {
                "phase": "rollback",
                "command": packet["rollback_commands"][2],
                "exit_code": 0,
                "outcome": "pass",
            },
        ],
    )

    assert evidence["status"] == "blocked"
    assert evidence["canary_proved"] is False
    assert evidence["rollback_proved"] is False
    assert evidence["blocking_codes"] == ["operator_approval_missing"]


def test_reject_capacity_integers_blocks_multi_digit_counts_but_allows_ports():
    bad_resources = [
        {"spec": {"replicas": 12}},
        {"spec": {"minReplicas": 10}},
        {"spec": {"maxReplicas": 50}},
        {"spec": {"minAvailable": 12}},
        {"spec": {"maxUnavailable": 33}},
        {"spec": {"metrics": [{"resource": {"target": {"averageUtilization": 80}}}]}},
    ]
    for resource in bad_resources:
        with pytest.raises(ValueError, match="capacity"):
            reject_capacity_integers(resource)

    # Ports and single-digit policy values are legitimate and must pass.
    reject_capacity_integers(
        {
            "spec": {
                "replicas": 1,
                "minAvailable": 1,
                "template": {"spec": {"containers": [{"ports": [{"containerPort": 8080}]}]}},
            }
        }
    )
    reject_capacity_integers({"ports": [{"containerPort": 6333}]})


def test_scale_out_manifest_bundle_classifies_workloads_without_leaking_counts():
    bundle = scale_out_manifest_bundle(
        workloads=[
            {"name": "ingress-api", "scaleCategory": "horizontally-scalable", "replicaPolicy": "ops-defined"},
            {"name": "mcp-http", "scaleCategory": "horizontally-scalable", "replicaPolicy": "single"},
            {"name": "ingress-worker", "scaleCategory": "serialized-worker", "replicaPolicy": "single"},
            {"name": "ledger-postgres", "scaleCategory": "singleton-stateful", "replicaPolicy": "singleton"},
            {"name": "llm-brain-tools", "scaleCategory": "not-a-target", "replicaPolicy": "singleton"},
        ],
        namespace="neurons-scale",
        access_policy="tailscale_private",
        image_by_workload={
            "ingress-api": "neurons-api:scale",
            "mcp-http": "neurons-mcp:scale",
            "ingress-worker": "neurons-worker:scale",
            "ledger-postgres": "postgres:17-alpine",
        },
        container_port_by_workload={
            "ingress-api": 8080,
            "mcp-http": 8765,
            "ingress-worker": 8080,
            "ledger-postgres": 5432,
        },
    )

    assert bundle["schema_version"] == "k3s_scale_out_bundle.v1"
    assert bundle["production_migration_implied"] is False
    assert bundle["requires_operator_approval"] is True

    # not-a-target is excluded entirely.
    assert all(
        resource["metadata"].get("name") != "llm-brain-tools" for resource in bundle["resources"]
    )

    # ops-defined horizontally-scalable: Deployment without a replicas count, policy marker,
    # anti-affinity, plus an HPA skeleton and a maxUnavailable PDB.
    api_deploy = _resource(bundle, "Deployment", "ingress-api")
    assert "replicas" not in api_deploy["spec"]
    assert api_deploy["metadata"]["annotations"]["neurons.scale/replica-policy"] == "ops-defined"
    assert "podAntiAffinity" in api_deploy["spec"]["template"]["spec"]["affinity"]
    affinity = api_deploy["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]
    assert "preferredDuringSchedulingIgnoredDuringExecution" in affinity
    assert "requiredDuringSchedulingIgnoredDuringExecution" not in affinity
    assert _resource(bundle, "HorizontalPodAutoscaler", "ingress-api") is not None
    api_pdb = _resource(bundle, "PodDisruptionBudget", "ingress-api")
    assert api_pdb["spec"].get("maxUnavailable") == 1

    # single horizontally-scalable (mcp-http until host-networking removed): replicas:1, no HPA,
    # and a minAvailable PDB (maxUnavailable:1 on a single replica gives no protection).
    mcp_deploy = _resource(bundle, "Deployment", "mcp-http")
    assert mcp_deploy["spec"]["replicas"] == 1
    assert _resource(bundle, "HorizontalPodAutoscaler", "mcp-http") is None
    assert _resource(bundle, "PodDisruptionBudget", "mcp-http")["spec"].get("minAvailable") == 1

    # serialized-worker: fixed single Deployment + minAvailable PDB, no HPA.
    worker_deploy = _resource(bundle, "Deployment", "ingress-worker")
    assert worker_deploy["spec"]["replicas"] == 1
    assert _resource(bundle, "HorizontalPodAutoscaler", "ingress-worker") is None
    assert _resource(bundle, "PodDisruptionBudget", "ingress-worker")["spec"].get("minAvailable") == 1

    # singleton-stateful: StatefulSet single-writer + headless Service, never a Deployment.
    assert _resource(bundle, "StatefulSet", "ledger-postgres") is not None
    assert _resource(bundle, "Deployment", "ledger-postgres") is None
    assert _resource(bundle, "Service", "ledger-postgres-headless")["spec"]["clusterIP"] == "None"

    # tailscale_private still attaches the namespace-scoped NetworkPolicy.
    assert _resource(bundle, "NetworkPolicy", "tailscale-private-only") is not None
    assert _resource(bundle, "Namespace", "neurons-scale") is not None


def test_inventory_classification_round_trips_to_a_clean_scale_out_bundle():
    # The real workload-inventory.yaml is the single source of truth: every workload must
    # classify into a known scaleCategory, and that classification must build a public-safe,
    # count-free bundle. This wires the inventory directly to the generator so a drift
    # (missing/typo scaleCategory, a new unclassified workload) fails closed here.
    inventory = yaml.safe_load(_INVENTORY.read_text(encoding="utf-8"))
    classified = load_scale_out_workloads(inventory)

    assert len(classified) == len(inventory["workloads"])
    assert {w["scaleCategory"] for w in classified} <= {
        "horizontally-scalable",
        "serialized-worker",
        "singleton-stateful",
        "not-a-target",
    }
    # ingress-worker stays serialized (no competing-consumer); mcp-http stays single.
    by_name = {w["name"]: w for w in classified}
    assert by_name["ingress-worker"]["scaleCategory"] == "serialized-worker"
    assert by_name["mcp-http"]["replicaPolicy"] == "single"

    bundle = scale_out_manifest_bundle(
        workloads=classified,
        namespace="neurons-scale",
        access_policy="tailscale_private",
        image_by_workload={w["name"]: f"neurons-{w['name']}:scale" for w in classified},
        container_port_by_workload={w["name"]: 8080 for w in classified},
    )
    assert bundle["schema_version"] == "k3s_scale_out_bundle.v1"
    # not-a-target workloads never produce resources.
    not_targets = {w["name"] for w in classified if w["scaleCategory"] == "not-a-target"}
    resource_names = {r["metadata"].get("name") for r in bundle["resources"]}
    assert not (not_targets & resource_names)


def test_load_scale_out_workloads_rejects_unknown_category():
    with pytest.raises(ValueError, match="scaleCategory"):
        load_scale_out_workloads(
            {"workloads": [{"id": "mystery", "scaleCategory": "warp-drive", "replicaPolicy": "single"}]}
        )


def test_scale_out_horizontally_scalable_blank_policy_defaults_to_ops_defined_with_hpa():
    # A blank replicaPolicy must resolve consistently: the Deployment and the HPA decision
    # use the SAME effective policy (no ops-defined Deployment without its HPA).
    bundle = scale_out_manifest_bundle(
        workloads=[{"name": "ingress-api", "scaleCategory": "horizontally-scalable", "replicaPolicy": ""}],
        namespace="neurons-scale",
        access_policy="tailscale_private",
        image_by_workload={"ingress-api": "neurons-api:scale"},
        container_port_by_workload={"ingress-api": 8080},
    )
    deploy = _resource(bundle, "Deployment", "ingress-api")
    assert "replicas" not in deploy["spec"]
    assert deploy["metadata"]["annotations"]["neurons.scale/replica-policy"] == "ops-defined"
    assert _resource(bundle, "HorizontalPodAutoscaler", "ingress-api") is not None
    assert _resource(bundle, "PodDisruptionBudget", "ingress-api")["spec"].get("maxUnavailable") == 1


def _k3s_operator_packet():
    plan = k3s_poc_canary_plan(
        namespace="neurons-canary",
        canary_workloads=[{"name": "llm-brain-mcp", "kind": "Deployment", "stateful": False}],
        access_policy="tailscale_private",
        rollback_target="compose",
    )
    bundle = k3s_poc_canary_manifest_bundle(
        plan,
        image_by_workload={"llm-brain-mcp": "neurons-worker:canary"},
        container_port_by_workload={"llm-brain-mcp": 8080},
    )
    return k3s_poc_operator_approval_packet(
        bundle,
        manifest_path="k8s/canary/context-authority.yaml",
        compose_check_command="docker compose ps",
    )

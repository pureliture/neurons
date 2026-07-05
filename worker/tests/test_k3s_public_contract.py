from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
K3S_ROOT = REPO_ROOT / "deploy/k3s"


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_k3s_public_contract_keeps_canary_and_workqueue_gates() -> None:
    inventory = _yaml(K3S_ROOT / "public-contract/workload-inventory.yaml")
    canary = inventory["canarySafety"]
    workqueue = canary["workQueueIsolation"]

    assert inventory["cutoverMode"] == "compose-maintained-k3s-canary"
    assert inventory["coexistence"] == "short-safety-window"
    assert workqueue["required"] is True
    assert workqueue["sameLiveDurableAsCompose"] == "forbidden"
    assert set(workqueue["allowedModes"]) == {
        "shadow stream",
        "separate durable",
        "worker disabled health-only validation",
    }
    assert canary["safetyWindow"]["maxCanaryWindowHours"] == 24
    assert canary["safetyWindow"]["noDecisionAction"] == "abort-to-compose-primary"


def test_k3s_public_contract_records_scale_out_preconditions_without_private_counts() -> None:
    inventory = _yaml(K3S_ROOT / "public-contract/workload-inventory.yaml")
    workloads = {item["id"]: item for item in inventory["workloads"]}

    assert workloads["ingress-worker"]["scaleCategory"] == "serialized-worker"
    assert workloads["ingress-worker"]["replicaPolicy"] == "single"
    assert workloads["ingress-worker"]["scaleOutPrecondition"] == (
        "workqueue-fanout-and-shared-state-store-required"
    )
    assert workloads["mcp-http"]["scaleCategory"] == "horizontally-scalable"
    assert workloads["mcp-http"]["replicaPolicy"] == "single"
    assert workloads["mcp-http"]["scaleOutPrecondition"] == "host-networking-removal-required"
    assert workloads["retired-java-ingress-worker"]["migrateToK3s"] is False

    forbidden_private_capacity_keys = {"replicas", "nodeSpecs", "hpaTarget", "hpaMin", "hpaMax"}
    for workload in workloads.values():
        assert forbidden_private_capacity_keys.isdisjoint(workload)


def test_k3s_public_contract_keeps_backup_restore_and_network_policy_gates() -> None:
    inventory = _yaml(K3S_ROOT / "public-contract/workload-inventory.yaml")
    config = _yaml(K3S_ROOT / "public-contract/base/config-contract.yaml")
    ops = _yaml(K3S_ROOT / "public-contract/ops-overlay-contract.yaml")
    data = config["data"]

    stateful = [item for item in inventory["workloads"] if item.get("stateful")]
    assert stateful
    assert all(item["backupRestoreRequired"] is True for item in stateful)
    assert data["stateful-gate"] == "backup-restore-rehearsal-required"
    assert data["workqueue-isolation-gate"] == "shadow-stream-or-separate-durable-required"
    assert data["network-policy-gate"] == "required-before-promotion"
    assert data["kube-apiserver-access-gate"] == "private-ops-operator-allowlist-required"
    assert data["live-apply-gate"] == "explicit-approval-required"
    assert data["compose-retire-gate"] == "explicit-approval-required"

    cni_description = ops["requiredPrivateInputs"]["cniSelection"]["description"]
    assert "flannel" in cni_description
    assert "does not" in cni_description
    assert "NetworkPolicy" in cni_description
    assert ops["gates"]["backupRestore"]["requiredBeforePrimaryCutover"] is True
    assert ops["gates"]["network"]["networkPolicy"] == "required before promotion"


def test_k3s_public_contract_does_not_contain_live_apply_or_private_values() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in K3S_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".md", ".yaml", ".yml"}
    )
    readme = (K3S_ROOT / "README.md").read_text(encoding="utf-8")
    ops = _yaml(K3S_ROOT / "public-contract/ops-overlay-contract.yaml")

    assert "No live apply before dry-run evidence and explicit approval" in readme
    assert "flannel 기본 backend는 NetworkPolicy를 집행하지 않는다" in readme
    assert "kubectl apply" not in combined
    assert "helm install" not in combined
    assert "docker compose stop" not in combined
    for forbidden in (
        "real hostnames",
        "private filesystem paths",
        "token values",
        "bearer values",
        "api key values",
        "raw transcript bodies",
        "raw dataset identifiers",
        "raw document identifiers",
    ):
        assert forbidden in ops["forbiddenPublicData"]

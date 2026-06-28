# Milestones — Neurons k3s Migration

## M1 Inventory and Public/Private Boundary Contract

- status: done
- evidence: workload-inventory contract passes and covers root compose, worker compose, and
  removed legacy external-memory/dendrite exclusions.

## M2 Validation/Redaction Contract Test

- status: done
- evidence: contract tests were first observed failing on missing/weak contract artifacts, then
  passing after safe public artifacts were added.

## M3 k3s Contract Artifacts

- status: done
- evidence: `deploy/k3s/README.md`, `deploy/k3s/public-contract/workload-inventory.yaml`,
  `deploy/k3s/public-contract/ops-overlay-contract.yaml`, and secret-free public base
  manifests exist and are checked for public/private boundary and forbidden public data patterns.

## M4 Backup/Restore Rehearsal Runbook

- status: done
- evidence: `backup-restore-rehearsal.md` defines CouchDB, Postgres ledger, Neo4j, Qdrant,
  restore rehearsal, and redacted evidence gates, verified by `K3sMigrationContractTest`.

## M5 Canary/Cutover Runbook

- status: done
- evidence: `canary-cutover-runbook.md` defines client dry-run, server dry-run, explicit
  approval, WorkQueue isolation, bounded safety window, NetworkPolicy/kube-apiserver gates,
  read/write canary, public-safe synthetic data, rollback, and compose retire gates.

## M6 Single-Goal Cutover Control

- status: done
- evidence: `single-goal-cutover-control.md` defines Gates 0-6 for running the whole migration as
  one agentic-execution goal without redefining completion around a narrower canary step.

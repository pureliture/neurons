# Neurons k3s Public Contract

이 디렉터리는 `neurons` public repo가 소유하는 k3s migration contract다. 실제
production overlay, secret/config, Tailscale route/ACL, backup evidence, approval record는
private `neurons-ops` repo가 소유한다.

## Scope

- Target: local Ubuntu primary k3s
- Migration shape: `neurons` repo-owned compose surface 전체
- Cutover shape: compose 유지 + k3s canary, 짧은 safety window 후 compose retire
- Stateful strategy: backup/restore rehearsal first
- Network boundary: Tailscale subnet router with limited routes and access policy

## Public Repo Owns

- Safe workload inventory
- Public/private overlay contract
- Non-secret validation expectations
- Redacted runbooks
- Tests that fail closed when the boundary is weakened

## Private Ops Repo Owns

- Real env files and secret material
- Host-specific k3s overlay values
- Tailscale routes and ACL/grants
- Backup/restore execution records
- Live apply, cutover, rollback, and compose retire approval records

## Hard Gates

- No live apply before dry-run evidence and explicit approval
- No secret mutation before explicit approval
- No compose stop before cutover approval
- No primary promotion before backup/restore rehearsal evidence
- No k3s worker canary may share the live WorkQueue durable with the compose worker
- No safety window may exceed 24h without aborting back to compose primary
- No promotion without kube-apiserver access allowlist and NetworkPolicy expectations
- No public artifact may contain real hostnames, private paths, secret values, raw transcript
  bodies, or raw dataset/document identifiers

## Current Artifacts

- `public-contract/workload-inventory.yaml`
- `public-contract/ops-overlay-contract.yaml`
- `public-contract/base/kustomization.yaml`
- `public-contract/base/namespace.yaml`
- `public-contract/base/config-contract.yaml`
- `docs/specs/2026-06-27-neurons-k3s-migration/backup-restore-rehearsal.md`
- `docs/specs/2026-06-27-neurons-k3s-migration/canary-cutover-runbook.md`
- `docs/specs/2026-06-27-neurons-k3s-migration/single-goal-cutover-control.md`

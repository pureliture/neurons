# k3s Public Contract Hardening

Status: done.

Tracker: GitHub issue #40.

## Scope

Strengthen public static checks for `deploy/k3s` contract files without applying anything to a cluster.

## Non-Goals

- No live k3s apply.
- No Docker/systemd/firewall mutation.
- No secret loading or private ops overlay values.

## Required Invariants

- Public workload inventory and config contract stay public-safe.
- Scale-out preconditions and NetworkPolicy/CNI caveat are represented.
- WorkQueue isolation and backup/restore rehearsal gates are represented.
- Private ops values stay outside public repo.

## Test Plan

- Add static tests over `deploy/k3s/**`.
- Verify no raw secret/private value is introduced.
- Verify public contract references required safety gates.

## Done Criteria

- Focused static tests pass.
- Evidence is recorded in `milestones.md`.

## Evidence

- `worker/tests/test_k3s_public_contract.py` reads public contract YAML files and checks safety gates without live apply.
- Static tests cover canary/workqueue isolation, scale-out preconditions, NetworkPolicy/CNI caveat, backup/restore gates, and forbidden public data.

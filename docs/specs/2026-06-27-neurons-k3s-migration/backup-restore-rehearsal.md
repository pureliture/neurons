# Backup/Restore Rehearsal

이 runbook은 local Ubuntu compose에서 k3s로 primary를 넘기기 전에 필요한
backup/restore rehearsal gate를 정의한다. 실행 명령과 실제 저장 위치는 private
`neurons-ops` repo가 소유한다.

## Scope

- CouchDB
- Postgres ledger
- Neo4j
- Qdrant
- NATS and worker/runtime state where the workload inventory marks `backupRestoreRequired: true`

Profile-gated stores are required only when the corresponding source compose profile is enabled
for the migration target. Disabled profiles must record an explicit non-migration decision instead
of blocking cutover with an impossible restore rehearsal.

## Preconditions

- Current compose runtime remains primary.
- k3s live apply has not been promoted to primary.
- Secret/config material is available only through the private ops overlay.
- Rehearsal output must be redacted evidence.
- No raw transcript body, secret value, private path, or raw dataset/document identifier is printed.

## Required Proof

For each stateful store:

- backup artifact exists in the private ops boundary
- restore rehearsal runs against an isolated target
- restored service reaches health/readiness
- redacted object counts match expected bounds
- schema/index readiness is checked where the store supports it
- representative query behavior succeeds with public-safe or redacted inputs
- rollback decision is recorded as pass/fail

## Store Gates

| Store | Rehearsal evidence |
| --- | --- |
| CouchDB | health, database presence, redacted document counts, representative read behavior |
| Postgres ledger | health, schema readiness, migration/table presence, representative ledger query |
| Neo4j | health, graph connectivity, index/constraint readiness, representative graph read |
| Qdrant | if source profile is enabled: health, collection readiness, vector config readiness, representative search/read; otherwise explicit non-migration decision |
| NATS/runtime state | durable stream/consumer readiness or explicit non-migration decision |

## Promotion Rule

Primary cutover is blocked until backup/restore rehearsal passes for every required stateful
store. A failed rehearsal keeps compose primary and sends the work back to the agentic-execution
loop for correction or to grill-to-spec if the approved design must change.

## Evidence Shape

Allowed:

- redacted counts
- pass/fail gate status
- sanitized service names
- schema/index readiness summaries
- public-safe synthetic canary ids

Forbidden:

- raw transcript bodies
- secret values
- private filesystem paths
- raw dataset identifiers
- raw document identifiers
- full environment dumps

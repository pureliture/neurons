# Graph Trigger Postgres Ledger Cutover Design Spec

## Overview

`neurons-graph-trigger`의 ledger engine을 SQLite fallback에서 cluster PostgreSQL로 되돌린다. 구현은 두 부분이다: (1) `ledger.py`의 PostgreSQL dialect table/column introspection을 고쳐 Postgres adapter path에서 `sqlite_master`를 조회하지 않게 한다. (2) 새 Secret 생성이나 shell DSN 조립 없이, 기존 runtime Secret의 `NEURON_LEDGER_PG_DSN` key를 graph-trigger Deployment가 명시적으로 참조하도록 바꾼다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- 승인 상태: 사용자 사전 승인
- 핵심 요구사항:
  - 새 K8s Secret을 만들지 않는다.
  - `NEURON_LEDGER_PG_DSN` 빈 값 override를 제거한다.
  - 기존 runtime Secret의 `NEURON_LEDGER_PG_DSN` key를 `secretKeyRef`로 사용한다.
  - raw DSN/password/token은 출력하지 않는다.
  - rollout 후 graph-trigger `status: ok`, pod ready, restart 0을 확인한다.

## Approach Proposal

### 선택한 접근: 기존 Secret key의 explicit `secretKeyRef`

```yaml
- name: NEURON_LEDGER_PG_DSN
  valueFrom:
    secretKeyRef:
      name: <runtime-secret-name>
      key: NEURON_LEDGER_PG_DSN
```

장점:

- 새 Secret duplication이 없다.
- manifest에 raw DSN이 없다.
- `envFrom`에 묻혀 있는 key보다 의도가 명확하다.
- 기존 runtime Secret rotation path를 그대로 쓴다.
- Postgres adapter code는 이미 `is_file_backed = False`라 code change가 작다.

단점:

- Secret key가 누락되면 pod env resolution이 실패하거나 app이 시작되지 않는다. 단, live key 존재를 사전 확인했다.

### 대안 1: 새 `neurons-ledger-dsn` Secret 생성

장점: DSN 전용 Secret이라 이름이 직관적이다.

단점: 이미 기존 runtime Secret에 동일 key가 있으므로 SoT가 중복되고 rotation/audit surface가 늘어난다. 이번 design에서는 채택하지 않는다.

### 대안 2: shell command에서 `POSTGRES_*` env로 DSN 조립

장점: 기존 stateful auth Secret의 개별 credential만으로 작동한다.

단점: shell escaping과 accidental logging 위험이 있고, Deployment command가 secret construction logic을 소유하게 된다. 이번 design에서는 채택하지 않는다.

## Architecture

```text
Kubernetes Secret: <runtime-secret-name>
  key: NEURON_LEDGER_PG_DSN
        |
        | secretKeyRef
        v
Deployment: neurons-graph-trigger
  env.NEURON_LEDGER_PG_DSN = [REDACTED]
        |
        v
Ledger.__init__
  os.environ["NEURON_LEDGER_PG_DSN"] non-empty
        |
        v
PostgresLedgerDbAdapter(is_file_backed=False)
        |
        v
ledger._table_exists/_column_names use information_schema
        |
        v
No SQLite parent permission validation and no sqlite_master/PRAGMA introspection
```

`LLM_BRAIN_LEDGER_PATH` remains present as the required `--ledger` CLI argument/fallback path. It is not the active storage authority when `NEURON_LEDGER_PG_DSN` is non-empty.

## Data Flow

1. Kubernetes resolves `NEURON_LEDGER_PG_DSN` from the existing runtime Secret into the graph-trigger container environment.
2. The shell loop invokes:

   ```text
   neuron-knowledge couchdb-graph-trigger --ledger "$LLM_BRAIN_LEDGER_PATH" ... --execute
   ```

3. `Ledger` sees non-empty `NEURON_LEDGER_PG_DSN` and instantiates `PostgresLedgerDbAdapter`.
4. `file_backed` is `False`, so `_prepare_parent_directory`, `_validate_existing_file_backed_schema`, and read-only snapshot copy do not run.
5. `ledger.py` helper introspection uses `information_schema` for PostgreSQL and preserves `sqlite_master`/`PRAGMA` behavior for SQLite.
6. Graph projection state writes go through Postgres ledger tables.

## Data State And Migration

Sanitized private ops evidence before final cutover showed existing PostgreSQL graph projection state; PostgreSQL is not a fresh migration target.

Before treating PostgreSQL projection state as the active resume state, verify parity/completeness against SQLite or another definitive private ops completeness check. If parity is not proven, do not skip reconciliation and do not resume from PostgreSQL alone; require a safe reconciliation path that preserves the existing graph projection/resume flow.

Do not run an unconditional SQLite-to-Postgres graph projection state migration as part of this cutover. Use existing PostgreSQL projection state as active resume state only after the parity/completeness gate passes, then let graph-trigger catch up any sessions missing from PostgreSQL through the normal projection flow.

## Component Details

### Private ops graph-trigger Deployment manifest

Change only the `neurons-graph-trigger` Deployment env section:

- Replace:

  ```yaml
  - name: NEURON_LEDGER_PG_DSN
    value: ''
  ```

- With:

  ```yaml
  - name: NEURON_LEDGER_PG_DSN
    valueFrom:
      secretKeyRef:
        name: <runtime-secret-name>
        key: NEURON_LEDGER_PG_DSN
  ```

Keep:

```yaml
- name: LLM_BRAIN_LEDGER_PATH
  value: $LLM_BRAIN_LEDGER_PATH
```

### `neurons` public repo

Production code change is required in `worker/lib/agent_knowledge/ledger.py`:

- `_table_exists(connection, table)` must branch on `getattr(connection, "dialect", "sqlite")`.
- PostgreSQL branch uses `information_schema.tables`.
- `_column_names(connection, table)` must use `information_schema.columns` for PostgreSQL.
- SQLite branch keeps `sqlite_master` and `PRAGMA table_info` behavior unchanged.

Regression tests live in `worker/tests/test_ledger_postgres_dialect_helpers.py` and use a fake PostgreSQL-dialect connection so no live credential is needed.

### Private ops rollout

Apply the same env shape to the live Deployment, then check rollout and graph-trigger output. Do not print the DSN value.

## Error Handling

- Secret key missing:
  - Expected failure: pod cannot resolve `secretKeyRef` or graph-trigger has no `NEURON_LEDGER_PG_DSN`.
  - Detection: env presence check returns false or rollout/logs fail.
  - Rollback: restore explicit `NEURON_LEDGER_PG_DSN=''` and private `LLM_BRAIN_LEDGER_PATH` fallback.

- Postgres connectivity/auth failure:
  - Expected failure: graph-trigger log `status: failed` or child command non-zero.
  - Detection: wrapper report, pod logs, restart count.
  - Rollback: same SQLite fallback override.

- Schema mismatch:
  - Expected failure: Postgres adapter raises during projection write/read.
  - Detection: graph-trigger report `failed > 0`, stderr present, or exception class in logs.
  - Rollback: do not run destructive migrations from this task; revert env and plan schema repair separately.

## Testing Strategy

Static / source checks:

- Add regression tests proving PostgreSQL dialect helpers use `information_schema` and never `sqlite_master`/`PRAGMA table_info`.
- Parse the private ops manifest as multi-document YAML and assert `neurons-graph-trigger` has `NEURON_LEDGER_PG_DSN.valueFrom.secretKeyRef.name == <runtime-secret-name>` and `key == NEURON_LEDGER_PG_DSN`.
- Assert `NEURON_LEDGER_PG_DSN.value == ''` no longer appears in the graph-trigger env list.
- Run `kubectl apply --dry-run=client --validate=false -f ...`.

Live checks:

- `kubectl` key existence check against the runtime Secret only, no value output.
- `kubectl -n neurons set env deployment/neurons-graph-trigger NEURON_LEDGER_PG_DSN-` or equivalent manifest apply/patch to remove the empty override and use secret ref.
- `kubectl -n neurons rollout status deployment/neurons-graph-trigger --timeout=240s`.
- Pod env existence check:

  ```text
  bool(os.environ.get("NEURON_LEDGER_PG_DSN")) == True
  ```

  The value is never printed.

- Graph trigger smoke:

  ```text
  neuron-knowledge couchdb-graph-trigger --limit 1 --execute --graph-required
  ```

  Expected: `status: ok`, `failed: 0`, no traceback.

## TDD Strategy

This work has two code-changing/static milestones:

1. Add focused `ledger.py` PostgreSQL dialect helper tests first and observe RED: current `_table_exists()` calls `sqlite_master` for PostgreSQL connections.
2. Patch `_table_exists()` and `_column_names()` to use `information_schema` for PostgreSQL while preserving SQLite behavior.
3. Run focused helper tests and relevant ledger/graph tests.
4. Run a manifest assertion against current ops manifest and observe RED because `NEURON_LEDGER_PG_DSN.value == ''`.
5. Patch the manifest to `valueFrom.secretKeyRef`.
6. Re-run the assertion and Kubernetes dry-run until both pass.
7. Build/deploy a graph-trigger image containing the code fix, then apply bounded live rollout and verify runtime evidence.

## Milestones

- M1: Spec lock
  - Done: `requirements.md` and `design.md` exist under `docs/specs/2026-07-05-graph-trigger-postgres-ledger/` with preapproved status.
- M2: Ledger dialect fix
  - Done: PostgreSQL helper tests fail first, then pass after `ledger.py` uses `information_schema` for PostgreSQL table/column introspection.
- M3: Manifest cutover
  - Done: graph-trigger env references the existing runtime Secret's `NEURON_LEDGER_PG_DSN` via `secretKeyRef`; static assertion and `kubectl --dry-run` pass.
- M4: Live rollout
  - Done: graph-trigger image includes the code fix, Deployment rolls out with Secret reference, pod ready 1/1, restart 0, env presence true without printing value.
- M5: Runtime proof
  - Done: graph-trigger smoke/log report shows `status: ok`, `failed: 0`, no `ledger parent must be private`, no `sqlite_master` PostgreSQL error.
- M6: Review and source control
  - Done: requested review agents have been considered, git branches/commits/PRs or direct ops main updates are reported with evidence.

## Open Questions

없음.

## Design Self-Review

- Secret duplication avoided: yes.
- Raw secret value exposure avoided: yes.
- Rollback path documented: yes.
- Scope limited to graph-trigger env: yes.
- Postgres adapter code precondition verified: partial. `PostgresLedgerDbAdapter.is_file_backed = False` exists, and this design adds the missing `ledger.py` dialect introspection fix.
- Remaining risk: live Postgres schema/connectivity can only be fully proven by rollout/smoke after the fixed image is deployed.

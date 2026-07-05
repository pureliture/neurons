# Graph Trigger Postgres Ledger Cutover Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Design companion: `design.md`
- 승인 상태: 사용자 사전 승인. 본 문서는 `/grill-to-spec` 요청에 따라 자문자답, repo/private ops evidence, 병렬 review agent 리서치 요청을 바탕으로 확정한 Phase 1 source다.

## 질문-답변 흐름

### Q: 왜 지금 graph-trigger ledger를 Postgres로 돌려야 하나?

현재 `neurons-graph-trigger`는 `NEURON_LEDGER_PG_DSN`을 빈 값으로 override하고, `LLM_BRAIN_LEDGER_PATH`가 가리키는 private SQLite fallback path를 사용한다. 이 우회는 SQLite parent permission 문제를 피하기 위해 안전하게 적용된 runtime workaround였지만, 장기 ledger SoT는 cluster PostgreSQL이어야 한다.

sanitized private ops evidence:

- cluster ledger service가 존재한다.
- 기존 runtime Secret에는 `NEURON_LEDGER_PG_DSN` key가 존재한다.
- 기존 stateful auth Secret에는 필요한 PostgreSQL auth key들이 존재한다.
- `PostgresLedgerDbAdapter`는 이미 `is_file_backed = False`를 선언한다.
- private smoke에서 Postgres DSN 전환만 적용하면 `ledger.py`의 SQLite-only `_table_exists()`가 `sqlite_master`를 조회해 `UndefinedTable`을 내는 것이 확인됐다.

### Q: 새 K8s Secret을 만들어야 하나?

아니다. 새 Secret을 만들지 않는다.

이미 기존 runtime Secret이 `NEURON_LEDGER_PG_DSN` key를 소유한다. 새 ledger DSN Secret을 만들면 credential SoT가 둘로 갈라지고 rotation/audit surface가 늘어난다. 장기적으로는 graph-trigger Deployment가 기존 Secret key를 `secretKeyRef`로 명시 참조하는 것이 가장 작고 명확한 구조다.

### Q: command에서 DSN을 shell로 조립해야 하나?

아니다. command에서 `POSTGRES_PASSWORD` 등을 조합하지 않는다.

DSN 조합은 escaping, logging, shell expansion 실수 위험이 있다. 이미 완성된 DSN key가 Secret에 있으므로 manifest는 secret value를 직접 다루지 않고 `valueFrom.secretKeyRef`만 선언한다.

### Q: `LLM_BRAIN_LEDGER_PATH`는 제거해야 하나?

이번 범위에서는 제거하지 않는다.

Postgres DSN이 설정되면 `Ledger`는 `PostgresLedgerDbAdapter`를 사용하고 file-backed validation을 건너뛴다. `--ledger` path는 CLI compatibility/fallback argument로 남지만, active storage engine은 `NEURON_LEDGER_PG_DSN`이 결정한다. `LLM_BRAIN_LEDGER_PATH`는 DSN 누락 시 private SQLite fallback path로도 안전하다.

### Q: live migration은 어떤 방식으로 검증해야 하나?

작은 rolling update로 검증한다.

1. manifest에서 `NEURON_LEDGER_PG_DSN: ''` override를 제거하고, 기존 runtime Secret의 `NEURON_LEDGER_PG_DSN` `secretKeyRef`로 바꾼다.
2. `kubectl apply --dry-run=client --validate=false`로 manifest shape를 검증한다.
3. live Deployment에 같은 env 변경을 적용한다.
4. rollout 후 pod에서 `NEURON_LEDGER_PG_DSN`이 non-empty인지 확인하고 값은 출력하지 않는다.
5. `couchdb-graph-trigger --limit 1 --execute` 또는 wrapper log에서 `status: ok`, `failed: 0`, `stderr_present: false`를 확인한다.

## 기능 요구사항

- `neurons-graph-trigger`는 더 이상 `NEURON_LEDGER_PG_DSN`을 빈 값으로 override하지 않아야 한다.
- `neurons-graph-trigger`는 기존 runtime Secret의 `NEURON_LEDGER_PG_DSN` key를 `secretKeyRef`로 받아야 한다.
- 새 K8s Secret은 만들지 않는다.
- Secret value, raw DSN, password, token은 manifest, docs, logs, final report에 출력하지 않는다.
- `LLM_BRAIN_LEDGER_PATH`는 CLI/fallback path로 유지할 수 있다.
- graph-trigger rollout 후 pod는 ready 1/1, restart 0, graph trigger report `status: ok`를 보여야 한다.
- Postgres cutover 후 SQLite `ledger parent must be private` workaround에 의존하지 않아야 한다.
- `ledger.py`의 `_table_exists()`와 `_column_names()`는 PostgreSQL dialect에서 `information_schema`를 사용하고 `sqlite_master`/`PRAGMA table_info`를 사용하지 않아야 한다.
- 기존 PostgreSQL graph projection state가 있으므로 이 cutover 중 SQLite-to-Postgres projection-state migration을 실행하지 않아야 한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Source control | `main`이 아니라 전용 worktree/branch에서 수정한다. |
| Secret hygiene | 새 secret duplication 금지. 기존 Secret key 참조만 사용. |
| Redaction | raw DSN/password/token 출력 금지. 존재 여부만 확인. |
| Blast radius | `neurons-graph-trigger` Deployment env 변경으로 scope를 제한한다. |
| Rollback | `NEURON_LEDGER_PG_DSN=''` + private `LLM_BRAIN_LEDGER_PATH` override로 되돌릴 수 있어야 한다. |
| Evidence | dry-run, rollout status, env 존재 여부, graph-trigger status, pod restart count를 남긴다. |
| Review | `codebase_architecture_manager`와 `code_simplifier` review를 반영한다. |
| Language | 자연어 문서와 보고는 한국어, code/path/env/key는 영어 원문 유지. |

## 사용자 시나리오

- 운영자는 graph-trigger가 SQLite PVC path permission workaround 없이 cluster Postgres ledger를 사용한다는 것을 Secret reference와 live env presence로 확인한다.
- 운영자는 raw DSN을 보지 않고도 기존 runtime Secret의 `NEURON_LEDGER_PG_DSN`이 graph-trigger에 주입되는지 확인한다.
- 운영자는 문제가 생기면 manifest/env를 이전 SQLite fallback override로 되돌려 graph-trigger를 복구할 수 있다.

## 미결정 항목

없음. 사용자가 `requirements.md`와 `design.md`를 사전 승인했고, Secret duplication 여부는 repo/private ops evidence에 따라 `새 Secret 생성 안 함`으로 닫았다.

# RAGFlow Retirement Cleanup Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`
- Phase 1 status: approved

## 질문-답변 흐름

### Q: 클린업 목표를 어디까지로 잡을까?

RAGFlow를 `neurons`의 active/runtime 선택지로 남기지 않는 퇴역 캠페인으로 진행한다.

### Q: RAGFlow 흔적은 어디까지 보존할까?

퇴역 기록만 보존한다. active code, config, docs, CLI, tests, local MCP 표면에서는 제거한다.

### Q: 첫 성공 기준은 무엇으로 둘까?

repo 문서/코드뿐 아니라 stale local MCP 프로세스와 설정까지 정리해서, 실제 로컬에서도 RAGFlow 인자가 보이지 않는 상태를 성공 기준으로 둔다.

### Q: 정리 실행 방식은 어떻게 둘까?

감사 먼저, 실행은 승인 후로 한다. 프로세스 종료, 설정 수정, 파일 삭제는 먼저 목록화하고 `requirements.md`와 `design.md` 승인 뒤 수행한다.

### Q: 1차 감사 범위는 어디까지 포함할까?

`neurons` repo와 로컬 MCP 표면만 우선 포함한다. `workspace-index-advisor`, ops/private overlay, RAGFlow management capability는 1차 범위 밖이다.

### Q: 남기는 예외는 무엇으로 제한할까?

퇴역 기록 문서 1개만 남긴다. 과거 migration evidence, disabled tests, reusable legacy code는 보존 예외가 아니다.

### Q: 로컬 stale MCP 프로세스는 어떻게 다룰까?

승인 후 종료까지 포함한다. `--ragflow-url` 또는 `--ragflow-token-env RAGFLOW_API_KEY`로 떠 있는 stale local MCP 프로세스는 kill list에 넣고, 실행 단계에서 종료한다.

### Q: 최종 검색 기준은 얼마나 강하게 둘까?

`neurons` repo-wide 검색 결과는 퇴역 기록 문서 1개만 허용한다. `RAGFlow`, `RAGFLOW_API_KEY`, `--ragflow-url` 등 active 의존으로 오해될 수 있는 문자열은 그 문서 외에 남기지 않는다.

### Q: 기존 CLI/API 호환성은 어떻게 볼까?

RAGFlow 관련 flag, command, env 호환성은 깨져도 된다. deprecation이나 retired error shim보다 제거를 우선한다.

### Q: 기존 state/schema에 남은 RAGFlow 이름은 어떻게 처리할까?

active DB/state/model field의 `index_*` 이름도 backend-neutral 이름으로 바꾼다.

### Q: 기존 데이터는 어떻게 처리할까?

보존 migration을 수행한다. 기존 `index_*` 데이터는 backend-neutral 이름으로 옮기고, 검증 뒤 옛 이름을 제거한다.

### Q: Phase 1 요구사항 파일 위치는 어디로 둘까?

공식 repo spec track으로 둔다: `docs/specs/2026-06-29-index-retirement-cleanup/requirements.md`.

### Q: 캠페인 진행 단위는 어떻게 나눌까?

감사 -> 삭제 -> migration -> local cleanup 순서로 진행한다. 큰 위험을 단계별로 닫고, schema/data 변경과 local MCP 종료를 한 번에 섞지 않는다.

### Q: 감사 단계의 승인 산출물은 무엇이어야 할까?

삭제/변경/종료 목록표를 만든다. 파일, 코드 경로, schema, 프로세스, 설정을 `remove`, `rename`, `migrate`, `kill`, `keep-as-retirement-record`로 분류한다.

### Q: 최종 검색 기준에 포함할 문자열은 어떻게 잡을까?

강한 핵심 키워드 세트를 사용한다. `RAGFlow`, `ragflow`, `RAGFLOW_API_KEY`, `RAGFLOW_`, `--ragflow-url`, `--ragflow-token-env`는 퇴역 기록 문서 외에 남기지 않는다.

### Q: 퇴역 기록 문서 1개는 어디에 둘까?

퇴역 기록 문서는 `docs/retired/index-retirement.md` 하나만 둔다. 이 문서만 repo-wide 검색 예외로 허용한다.

### Q: backend-neutral schema 이름은 어떤 원칙으로 정할까?

active schema, model, table, column, variable 이름에는 backend/vendor 이름을 쓰지 않는다. 기존 `index_*` 이름은 `index_*` 또는 `ragflow_*` 계열의 backend-neutral 이름으로 바꾼다. 대표 매핑은 `index_target_id` -> `index_target_id`, `index_document_id` -> `index_document_id`, `index_run_id` -> `index_run_id`, `index_progress` -> `index_progress`, `index_targets` -> `index_targets`로 둔다.

### Q: migration 성공/실패 기준과 rollback 조건은 어떻게 둘까?

보존 migration은 사전 백업 또는 되돌릴 수 있는 snapshot을 전제로 한다. 성공 기준은 기존 행 수와 핵심 참조값이 backend-neutral schema로 보존되고, 옛 `index_*` schema 이름이 active DB/schema/model에 남지 않으며, 관련 read/write path가 Qdrant/graph runtime 기준으로 통과하는 것이다. 데이터 누락, row count mismatch, old schema 잔존, runtime regression이 발견되면 migration을 중단하고 백업/snapshot 또는 이전 배포 상태로 rollback한다.

### Q: local MCP 설정 수정 대상과 stale 프로세스 식별 기준은 어떻게 둘까?

local cleanup 대상은 Mac local process/config surface로 제한한다. `neuron-knowledge mcp-stdio` command line 또는 MCP config가 `--ragflow-url`, `--ragflow-token-env`, `RAGFLOW_API_KEY`, `ragflow-direct-recall` 중 하나를 포함하면 stale RAGFlow MCP 표면으로 분류한다. Kubernetes live service, SSH remote exec, `workspace-index-advisor`, ops/private overlay는 1차 cleanup 대상이 아니다.

### Q: Phase 1 preview companion은 어떻게 생성할까?

같은 spec directory에 static `requirements.html`을 generated companion으로 둔다. 승인 대상은 항상 `requirements.md`이며, HTML은 검토 편의용이다.

## 기능 요구사항

- `neurons` active runtime, CLI, config, docs, tests, and local MCP setup에서 RAGFlow를 현재 선택지로 노출하지 않는다.
- RAGFlow 관련 code path, command, flag, env, test, docs는 삭제 후보로 감사한다.
- RAGFlow 퇴역 이유와 현재 대체 runtime 기준은 `docs/retired/index-retirement.md`에만 남긴다.
- `index_*` schema/state/model 이름은 `index_*` 또는 `ragflow_*` 계열의 backend-neutral 이름으로 migration한다.
- 기존 state 데이터는 삭제하지 않고 backend-neutral schema로 보존 migration한다.
- stale local MCP 프로세스와 설정은 kill/edit list에 포함한다.
- 캠페인은 감사, 삭제, migration, local cleanup 단위로 진행한다.
- 감사 단계는 `remove`, `rename`, `migrate`, `kill`, `keep-as-retirement-record` 분류가 있는 승인 가능한 목록표를 산출한다.
- 최종 repo-wide 검색 기준은 `RAGFlow`, `ragflow`, `RAGFLOW_API_KEY`, `RAGFLOW_`, `--ragflow-url`, `--ragflow-token-env`로 한다.
- local stale MCP 식별 기준은 `neuron-knowledge mcp-stdio`와 forbidden RAGFlow keyword 조합으로 한다.
- migration은 rollback 가능한 백업/snapshot을 전제로 한다.
- 실제 종료, 수정, 삭제, migration은 승인된 `design.md` 이후에만 수행한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Source of truth | `requirements.md` |
| Approval gate | `requirements.md` 승인 전 `design.md` 작성 금지 |
| Execution gate | `design.md` 승인 전 kill/edit/delete/migration 실행 금지 |
| Runtime 기준 | Qdrant/graph 기반 `neurons` live runtime |
| 보존 예외 | 퇴역 기록 문서 1개 |
| 검색 기준 | 퇴역 기록 문서 외 RAGFlow 관련 검색 결과 없음 |
| 퇴역 기록 경로 | `docs/retired/index-retirement.md` |
| 데이터 정책 | 기존 데이터 보존 migration |
| schema naming | backend/vendor 이름 금지, `index_*` 또는 `ragflow_*` 사용 |
| rollback | 데이터 누락, old schema 잔존, runtime regression 시 백업/snapshot 또는 이전 배포 상태로 복구 |
| 1차 범위 | `neurons` repo + local MCP surface |
| 1차 제외 | `workspace-index-advisor`, ops/private overlay, RAGFlow management capability |
| 진행 단위 | 감사 -> 삭제 -> migration -> local cleanup |
| 감사 산출물 | 삭제/변경/종료 목록표 |
| 검색 키워드 | `RAGFlow`, `ragflow`, `RAGFLOW_API_KEY`, `RAGFLOW_`, `--ragflow-url`, `--ragflow-token-env` |
| preview | 같은 spec directory의 static `requirements.html` |

## 사용자 시나리오

- 운영자가 `neurons` repo에서 RAGFlow 관련 문자열을 검색했을 때, 현재 runtime 선택지로 오해할 수 있는 결과가 나오지 않는다.
- 운영자가 local MCP 프로세스를 확인했을 때, stale RAGFlow direct recall 인자가 남아 있지 않다.
- 개발자가 CLI help, README, AGENTS류 문서를 읽었을 때, `neurons`가 Qdrant/graph 기반 live runtime이라는 점을 바로 이해한다.
- 데이터 migration 후 기존 state 내용은 backend-neutral 이름으로 계속 조회 가능하다.

## 미결정 항목

- 없음. 사용자가 `requirements.md`를 승인하면 Phase 2에서 접근안과 `design.md`를 작성한다.

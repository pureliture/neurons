# LBrain 사실 교정 구현 및 격리 배포 검증

검증일: 2026-10-25. FR13의 초기 `registered_json` 범위에 대해 구현, 설치 artifact, Ubuntu 격리 배포 MCP, 실제 LLM 작업 흐름을 검증했다. 아래 PASS는 이 범위의 결과다. main merge, 운영 서비스 교체, 실제 사용자 memory 정책 활성화는 수행하지 않았다.

## 구현 범위

`brain_object_correct`는 서버에 등록된 source/field 정책으로 사실을 독립 추출한다. 요청자는 새 값이나 승인자를 전달하지 않는다. project/repository, 원본 카드 계보, source revision/hash, 최신 관측, freshness와 필드 대응이 통과하면 PostgreSQL에서 successor 승인, 이전 카드 supersession, `correct_fact` 감사, edge와 projection outbox를 원자 commit한다. cache를 무효화하고 public read route로 재조회한다.

초기 adapter는 등록된 JSON source의 명시적 scalar 필드에 한정한다. 자유로운 코드 의미 해석, Git 상태 판정, runtime probe는 별도 adapter가 필요하다. registry의 관측 이후 외부 파일 변경까지 증명하지는 않으므로 정책의 freshness window가 적용된다. preference·intent·평가는 자동 교정 대상이 아니다.

HTTP correction surface는 별도 identity/bearer와 두 도구만 제공한다. stdio는 유효한 정책과 feature flag가 설정된 프로세스에서 도구를 노출한다. 기능은 기본 비활성이며 admin 권한을 LLM에게 넘기지 않는다.

## source와 배포 artifact 결속

| 항목 | 검증값 |
| --- | --- |
| 실행 source commit | `f552bf53f76320aeb215dad7dd6926e65f3de4c4` |
| 통합한 origin/main | `3d85ef493cc9af36ac99d124164dffc59d0a3d2b` |
| 이미지 digest | `sha256:09ba57575a8f301f540fb9d075a573cbfc71d7c6c650d4b60d21c555ca918b91` |
| build content manifest | `sha256:45925bbc2e0a323e778d19c1bcd6016fc76fc14c173ccc6aea8e2975991c9dd3` |

`worker/Dockerfile.mcp-http`로 source archive를 빌드하고 Ubuntu 격리 컨테이너에 배포했다. 실행 이미지 ID, source commit, manifest가 일치했다. 설치된 전체 Python tree와 schema SQL이 이미지의 source와 byte 단위로 일치했다. 이후 문서와 cache 회귀 assertion만 추가했으며 실행 코드의 기준은 위 commit이다.

첫 설치 artifact 검사에서 wheel에 schema SQL이 빠진 것을 발견했다. `pyproject.toml`에 SQL package data를 명시하고 wheel을 다시 만들었다. 최종 검증은 source 경로 override 없이 설치된 패키지로 새 PostgreSQL schema를 초기화했다. source checkout의 import 성공만으로 배포 가능 판정을 내리지 않았다.

## 실제 LLM 및 MCP 증거

현재 Codex root LLM(`gpt-6.1-sol`)이 transport 전용 MCP client를 사용해 실제 Streamable HTTP MCP를 호출했다. 준비 script는 synthetic 상태를 설정·관측·정리하며 교정 도구를 호출하지 않는다. 기존 기본 LBrain 연결을 운영 교체한 검증은 아니다.

1. `tools/list`에서 `brain.resolve`, `brain_object_correct`와 source registry에 정의된 동일 input schema를 확인했다.
2. synthetic project의 `brain.resolve(mode=context)` 및 `mode=list`가 accepted/current 값 `missing`을 반환했다.
3. LLM이 별도로 두 synthetic JSON 파일을 읽어 `ready`와 동일 content hash를 확인했다. 서버 정책은 두 source 모두를 필수로 요구했다.
4. LLM이 조회한 target/hash와 evidence refs로 `brain_object_correct`를 호출했다. 요청에 새 값이나 사람 승인 필드는 없었다.
5. 응답의 authority write, cache invalidation, read-after-write, supersession readback이 모두 참이었다. 같은 context route와 list route가 successor의 `ready`를 반환했고 freshness는 `verified_current`였다.
6. 같은 이미지의 실제 `neuron-knowledge mcp-stdio` 프로세스에서 initialize, tools/list, read, 동일 correction 재호출, read를 수행했다. stdio 목록은 `brain.resolve`, `memory_candidate_create`, `brain_object_correct`였다. 재호출은 duplicate이며 추가 authority write 없이 동일 successor를 반환했다. canonical hash는 유지됐고 `correct_fact` 결정은 1건이었다.

| 실제 부정 경로 | reason code | canonical 전후 동일 | denial audit |
| --- | --- | --- | --- |
| stale source | `source_stale` | PASS | PASS |
| hash 검증 불가 | `source_hash_unverifiable` | PASS | PASS |
| 필수 source 간 충돌 | `authoritative_sources_conflict` | PASS | PASS |
| 다른 project | `policy_scope_mismatch` | PASS | PASS |
| 잘못된 expected revision | `target_revision_conflict` | PASS | PASS |

모든 부정 응답은 `authority_write_performed=false`였다. bearer 없는 요청은 401, correction identity의 admin 도구 호출은 거부됐다. 실제 PostgreSQL 회귀 검사에서는 source 누락·malformed·조작, 재시도 충돌, 동시 호출, transaction 중 target 변경과 부분 SQL 실패 rollback도 검증했다.

## 검사 결과

| 검사 | 결과 |
| --- | --- |
| worker 전체 `uv run pytest -q` | 5200 passed, 607 skipped |
| 실제 PostgreSQL 교정 test 파일 | 32 passed |
| 기존 PostgreSQL steward 회귀 포함 별도 selection | 41 passed |
| 최신 main freshness 검사, 전용 빈 DB | 8 passed |
| root Java `gradle test` | BUILD SUCCESSFUL |
| 최종 cache assertion, 실제 PostgreSQL | 1 passed, 31 deselected |
| wheel 설치 schema, 배포 source 일치, HTTP/stdio 및 receipt 검증 | PASS |

worker의 skip은 환경 선택형 검사이며 전체 live backend 통과를 뜻하지 않는다. freshness 검사를 기존 synthetic 행이 있는 DB에 함께 실행했을 때 빈 DB를 전제한 fixture 1건이 실패했다. 별도 빈 DB에서 8건 모두 통과했다. 이 실패를 숨기거나 skip으로 바꾸지 않았다.

## 정리 및 감사 보존

synthetic current 카드를 기존 stewardship 경로로 non-current 전환했다. 같은 context/list 조회가 빈 목록을 반환했다. 전용 DB의 교정 결정 1건, 거부 감사 5건, supersession edge와 outbox를 export하고 DB backup과 source 상태 archive를 보존했다. 해당 DB의 non-synthetic 카드 수는 계속 0이었다. 운영 DB 및 사용자 memory는 검증용 컨테이너에 연결하지 않았다.

task label을 확인한 검증용 컨테이너 두 개만 제거했다. 전용 SSH forwarding을 종료하고 임시 bearer/env 파일을 로컬과 서버에서 제거했다. audit와 receipt, archive는 private storage에 보존한다. 공개 문서에는 private path, endpoint, credential, 원문 source, 운영 식별자를 넣지 않는다.

| 보존 증거 | SHA-256 |
| --- | --- |
| audit canonical payload | `8eaa907350fd0b043ac99ba0462d628a509f20d163540e8388f996e01b243e8f` |
| PostgreSQL archive | `05dd6082097ff4f02ff927b6e5674e68a13cc9a4f863438808f69b4b905fdd94` |
| source 상태 archive | `660c8ff39011fb870af547be952c09dba7ccd863aab1d3dee4c6a76d7e95d3a2` |
| 최종 receipt bundle | `1bb7a15edcc06c9691d566797f4d900395d609c25d9e9be14a32d06e0d7dbd50` |

## 남은 운영 범위

main merge와 운영 desired state 변경, 운영 correction identity/정책 활성화는 별도 delivery 단계다. 현재 기본 LBrain MCP에 도구가 활성화됐다는 의미가 아니다. 이 canary는 PostgreSQL authority와 outbox 기록을 검증했으며 graph backend의 실제 projection 소비와 graph recall까지 검증하지 않았다. 지원 adapter 확장이나 운영 활성화 시에는 그 범위에 맞는 추가 증거가 필요하다.

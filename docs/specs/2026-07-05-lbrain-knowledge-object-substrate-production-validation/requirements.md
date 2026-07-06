# LBrain Knowledge Object Substrate Production Validation Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: 생성하지 않음
- 승인 상태: 사용자 사전 승인
- 후속 design: 이 문서 기준으로 `design.md` 작성 및 `agentic-execution` 수행

## 문제 정의

1차 구현은 local/test evidence로 LBrain Knowledge Object Substrate의 모델, MCP surface, CLI, golden query baseline, OKF export, proposal/review separation을 통과시켰다. 그러나 local test green은 production-ready 또는 runtime-verified claim이 아니다.

이번 goal은 구현 결과를 production 환경과 운영 read path 관점에서 검증 가능한 claim과 gap으로 분리하는 것이다. 핵심은 production mutation 없이 다음을 판정하는 것이다.

- 새 substrate가 local/package/runtime entrypoint에서 로드 가능한가
- MCP/CLI contract가 실제 호출에서 public-safe output을 주는가
- production target write가 fail-closed 되는가
- live LBrain read path가 현재 새 substrate를 포함하는지, 또는 아직 미배포 gap인지
- 어떤 evidence가 있어야 `runtime-verified`라고 말할 수 있는가

## 실패 사례

- local `pytest` 통과만으로 production MCP가 새 object tools를 제공한다고 말한다.
- production corpus ingest denied가 아니라 실제 write를 시도한다.
- live production에 배포되지 않은 branch 기능을 “운영 검증 완료”로 표현한다.
- LBrain current memory read가 stale인데도 object substrate 품질 개선을 accepted/current로 주장한다.
- raw private path, secret, raw transcript, raw external ids를 validation report에 출력한다.

## 사용자 기대

검증 결과는 release note가 아니라 evidence ledger 형태여야 한다.

- `validated`: 실제 증거가 있는 claim
- `not_validated`: 증거가 없거나 현 production에 아직 없는 claim
- `denied_as_expected`: mutation/write가 안전하게 막힌 claim
- `gap`: production 검증을 위해 남은 조건
- `stop_condition`: 실행 중 멈춰야 하는 조건

## Scope

포함:

- local worker full regression
- root Gradle regression
- CLI smoke for object/query/corpus/golden/OKF surfaces
- JSON-RPC/MCP tool-list and call smoke against local test service
- production write-denial proof
- source-to-candidate runtime evidence collection plan/template smoke
- read-only LBrain MCP authority/context check if available
- live production deployment/read-path 확인은 read-only로만 수행
- validation report 작성

제외:

- production ledger write
- corpus ingest 실행 write
- deployment/GitOps mutation
- branch merge, image build/push, Argo sync
- raw corpus body 또는 raw transcript 읽기
- accepted/current memory promotion

## Safety And Privacy Requirements

- 모든 production-facing command는 read-only 또는 deny-smoke여야 한다.
- production target write command는 성공하면 실패로 간주한다. 기대 결과는 static denial이다.
- secret, token, private path, raw transcript, raw dataset/document id는 출력하지 않는다.
- live host/runtime evidence는 public-safe summary 또는 redacted digest만 기록한다.
- production deployment 여부를 확인할 때 merge/deploy/runtime-loaded를 분리한다.
- runtime evidence collection plan/template는 live evidence 자체가 아니며, 생성 성공만으로 production readiness를 주장하지 않는다.

## Acceptance Criteria

- local/package claims는 worker/root tests와 CLI smoke로 검증한다.
- runtime/MCP claims는 실제 JSON-RPC 또는 configured MCP read path evidence 없이는 `runtime-verified`로 표시하지 않는다.
- production mutation denial evidence를 최소 2개 포함한다: corpus ingest production denial, object decision/restricted denial.
- live production이 새 branch code를 포함하지 않으면 `not_validated: not_deployed_to_production`으로 기록한다.
- validation report는 validated/not_validated/denied_as_expected/gaps를 분리한다.
- source-to-candidate runtime evidence collection plan은 필요한 MCP tool list, `brain_objects_query` route smoke, deployed identity, production denied/no-mutation smoke, authority gate policy, evidence provenance/redaction 조건을 public-safe/read-only로 열거해야 한다.
- final status는 `PASS`, `PASS_WITH_GAPS`, `NO_GO`, `BLOCKED` 중 하나로 끝난다.

# 의미 기반 source 판단의 실제 교정 연결

전체 goal은 진행 중이다. 이 문서는 서버에 등록한 Git 파일의 내용에 대한 의미 판단을 실제 기존 사실 교정과 연결한 증거다. 일반 native 관측의 현재성 admission, 임의 target 의미 검색, 운영 배포/가동 여부의 범용 검증까지 완료한 것은 아니다.

## 처리 계약

`GitSemanticFactPolicy`는 고정 branch·파일·fact 범위·필드·표시문과 2~4개의 값별 주장을 서버에서 정한다. MCP 호출자가 근거 종류나 확인할 주장 집합을 바꿀 수 없다. `required_capability`는 source reader가 실제 발급한 종류·context·resource와 일치해야 한다. repository 파일을 읽었다는 사실로 graph 저장·HTTP 응답·실행한 test 결과를 인증하지 않는다. 새 경로의 reader는 고정 branch의 전체 committed UTF-8 파일이며, 8192 byte 초과·redaction 변경·잘림은 거부한다. dirty worktree는 이 관측이 아니다.

worker는 원천 receipt와 관측을 먼저 보존하고 DB authority transaction 밖에서 선택 모델 `perplexity/pplx-decider-flash-v1.2`에 값별 주장의 지지 여부를 묻는다. 충분히 지지된 값이 정확히 하나일 때만 그 값을 선택한다. 하나도 없거나 둘 이상이면 내구성 재검토 작업으로 남긴다. 호출자의 제안과 독립 선택 값이 달라도 각각 반환한다. 직접 JSON 대응은 모델을 호출하지 않는다. 자동 모델 fallback은 없다.

판단 전후와 적용 직전에 source를 다시 확인한다. 판단 cache는 source·질문·모델 revision·정책·입력 hash에 결속하며 적용 때도 재검증한다. 오래된 요청의 원천은 별도 receipt로 보존한 뒤 등록된 현재 source를 독립 재조회한다. 도착 시각으로 최신 값을 정하지 않는다. 적용 시 source가 바뀌거나 profile이 바뀌면 authority를 변경하지 않는다.

`correction_semantic.load_validated_profile`은 서버가 신뢰하는 평가 artifact의 byte hash와 실행 코드·질문·모델 identity를 검사하고 결과를 다시 집계한다. `pass=true`만 받는 경로는 없다. 기준은 독립 표본의 정상 사례 20건 이상·거부 사례 20건 이상, 이 중 모델 의미 판단까지 수행한 거부 사례 10건 이상, 잘못된 값 허용 0건, 정상 값 선택 80% 이상이다. 개발 자료와 평가 case hash의 중복을 거부한다. artifact의 실호출 표시는 암호학적 provider 서명이 아니다. 실제 실행 로그와 고정 계획을 운영자가 검증하고 그 artifact hash를 서버 설정으로 신뢰하는 경계다. 단위 검사의 대역 artifact를 출시 증거로 사용하지 않는다.

실제 적용 결과에는 profile hash·judgment hash·task ID·snapshot hash가 남는다. canonical card·supersession·fact head·resolution/audit/outbox·완료 참조는 기존 공통 transaction에서 저장한다. API 오류·profile 부재/불일치·근거 범위 불일치는 다음 재시도 예약을 가진 차단 상태다. 모델 판단이 부족한 상태는 source나 검증 정책이 바뀌기 전 같은 cache를 재사용하며 우연히 높은 답이 나올 때까지 반복 추첨하지 않는다.

## 실제 모델의 독립 평가

최초 `correction_semantic_holdout.json` 40건은 2건이 처리 예외로 끝나 미통과했다. 완료된 정상 선택은 19/20, 잘못된 선택은 0건이었다. 두 예외는 모델 답이 아니라 JSON으로 감싼 YAML 줄바꿈을 Windows 경로로 오인한 기존 유출 방지 검사였다. 같은 고정 계획의 예외 진단에서도 provider 호출 전에 같은 검사에서 실패함을 확인했다. 이 40건을 수정 뒤의 독립 평가로 재사용하지 않았다.

`CorrectionRelationClient`는 알려진 structured premise의 JSON을 풀어 원래 문자열 leaf를 검사한다. 실제 Windows/private 경로와 Bearer 값은 계속 provider 호출 전에 차단한다. API에 보낼 질문과 관측 내용의 의미는 바꾸지 않았다. 새 독립 표본은 `correction_semantic_v2_holdout.json`으로 분리했다.

- Ubuntu 실제 Git reader + 실제 OpenRouter의 새 40건: **정상 선택 20/20, 거부 사례 20건 중 잘못된 선택 0건**. 의미 판단을 수행한 거부 12건, 코드로 source 종류/context/resource를 선차단한 사례 8건이다. typed 모델 응답 64건을 사용했다.
- threshold는 평가 전에 **0.8**로 고정했다. 질문은 `correction_relation.v4`, 실제 모델 revision은 `perplexity/pplx-decider-flash-v1.2-20261016`이다. 이 표본에서 threshold를 다시 맞추지 않았다.
- 고정 계획 hash: `sha256:39fae1a0c0475c07a17ed796d4aec438d2cc1dcc69939bd5fcc1330540a78f70`.
- 실제 검증 artifact hash: `sha256:9cae8dfc9d32b362e33240625b25b32f17ba5516817166a79b20e50b83767e59`. 원문 artifact와 인증은 public repo에 넣지 않는다.
- provider가 보고한 평가 비용 합계는 **USD 0.00025264**다. 전체 세션 비용이나 후속 통합 검사 비용이 아니다.
- 평가 source bundle SHA-256: `74860d0b7e58d482dbf4fbb0d6c7882b3809c8d77cb3ed07f2f333fdb5481c29`.

이는 이 범위의 유한한 표본에서 기준을 충족한 결과다. 임의 문장·모든 근거 종류에서 오교정이 없다는 보장은 아니다. 기존 원시 모델 평가의 잘못된 허용 결과를 이 평가로 삭제하거나 원시 모델 자체의 통과로 바꾸지 않는다.

## 실제 교정과 회귀 검증

- Ubuntu 실제 OpenRouter + Git + PG + 실제 MCP HTTP CLI: **2 passed, 1 warning in 15.67s**. client 대역 없이 검증 artifact를 로드했다. 잘못된 제안과 다른 현재 값 선택, 지연 수집의 근거 합류와 단일 assertion, 후속 실제 source 변경, 현재 값의 새 조회를 확인했다. 원래 결정 당시 값과 이후 현재 값도 구분했다.
- 저장 직후 응답 유실은 실제 authority commit 뒤 예외를 주입했다. source를 내려도 같은 내구성 작업이 기존 결과로 복구하며 추가 successor를 만들지 않았다. 실제 OS process kill을 이 검사에서 수행한 것은 아니다.
- 최초 실호출 통합 검사는 마지막 context 응답의 `typed_payload`를 기대해 실패했다. 실제 context의 `statuses/status`와 list의 `typed_payload` 계약을 각각 검사하도록 수정하고 두 검사를 재실행했다. 제품 응답 필드를 삭제하거나 검사 자체를 skip하지 않았다.
- 이 최종 실제 교정 검사의 source bundle SHA-256: `0172da7893af817535debc1749168efc2749ed4abbf3dc6fb2d271547a6941db`. 평가 bundle 이후 변경은 test harness이며 loader는 최종 실행에서도 제품 코드 identity 일치를 확인했다.
- source/profile 변경·옛 snapshot·일반 writer 차단과 Couch/PG/Neo4j 관련 검사: **79 passed, 1 skipped, 1 warning in 52.68s**. 모델 대역을 쓰는 검사는 위 실호출과 구분한다. skip은 별도 graph 종료 제어가 필요한 검사이며 이번 통과에 포함하지 않는다.
- 직전 source의 기존 PG CI 25모듈 회귀: **781 passed, 1 warning in 162.14s**. 최종 유출 방지 client 변경은 이후 별도 검사와 실제 평가에서 확인했다.
- 최종 worker 전체 `uv run pytest -q`: **5305 passed, 675 skipped, 4 warnings in 55.67s**. opt-in 실호출 검사는 위 Ubuntu 실행에서 별도로 확인했다. 환경 선택형 skip을 실제 통과로 계산하지 않는다.
- Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`: **BUILD SUCCESSFUL**, task는 UP-TO-DATE였다. `git diff --check`: 통과.

검증은 작업 전용 Ubuntu runtime과 합성 source만 사용했다. 의존성 image digest `sha256:ee9b21c07899568b49342da9b884adb84847fd02c0f7583f0464d000fa155f6a`에 source를 읽기 전용 주입했으므로 최종 배포 image identity 증거가 아니다. 일시 인증 파일은 실호출 종료 후 제거하고 부재를 확인했다. 운영 서비스·운영 DB 활성화나 merge는 수행하지 않았다.

## 설정과 남은 범위

기존 `fact_correction_config.v1`의 rule에 `adapter="git_semantic.v1"`, `scope_description`, `claims`, `required_capability`, `validation_profile_path`, `validation_profile_hash`를 지정한다. `json_pointer` 대신 값별 주장 집합을 사용한다. profile 파일은 검증한 artifact 그대로여야 한다. worker와 MCP HTTP process는 기존 환경의 `OPENROUTER_API_KEY`를 사용한다. 경로와 인증값은 public 설정이나 응답에 넣지 않는다. 직접 대응 rule의 기존 설정과 단건 도구 계약은 유지한다.

일반 native evidence의 admission·동일 관측 합류, 의미 target 검색과 미식별 중복, source 철회·parser 변경의 기존 사실/파생물 영향, retention/GC, 실제 embedding provider, 운영 자동 실행/감시, 최종 image·전체 LLM 작업·백업 복원은 남아 있다. 이 profile은 Git 파일 내용에 대한 범위이며 운영 가동 주장을 인증하는 대체 수단이 아니다. B 자동 숨김 정책은 미결정 상태를 유지한다. rollback은 관리 대상 쓰기를 정지하거나 호환 resolver/DB guard를 유지하고 근거·판단 이력을 보존한다.

# LBrain 전체 교정 구현의 첫 보완 기록

전체 목표는 활성 상태다. 이 문서는 부분 구현의 증거이며 최종 완료 보고서가 아니다. [달성 추적](../specs/2026-07-05-lbrain-knowledge-object-substrate/correction-acceptance.md)의 G01~G10과 C01~C36을 계속 충족해야 한다.

## 구현한 변경

- 초기 `summary` 교정도 같은 사실의 typed payload를 함께 갱신한다. 지정된 사실의 정확한 표시 구간만 summary/render에서 바꾸며, 같은 카드의 다른 사실은 보존한다. 표시 구간을 유일하게 확인할 수 없으면 변경하지 않는다.
- 단일 필드 검증으로 카드 전체를 최신으로 인증하지 않는다. 검증한 필드와 표시 구간을 별도로 기록하고, 다른 사실의 관측 시각·confidence·freshness를 올리지 않는다. 일반 freshness 재검증도 부분 검증을 전체 검증으로 승격하지 못한다. 처리 시각 차이가 멱등 candidate hash를 바꾸지 않는 것도 확인한다.
- Codex native call ID와 session에 기반한 관측 anchor를, 분류·요약·내용 version에 따른 evidence ID와 분리했다. 필터링 순서나 chunk 관련 위치가 바뀌어도 같은 호출의 관측 identity는 유지한다. 실제 다른 호출이 같은 결과를 내면 독립 관측으로 유지한다. 같은 관측의 수정된 source와 해석은 별도 version으로 남긴다. SQLite·PostgreSQL ledger에 추가 column으로 보존하며 기존 anchor 없는 기록을 문장 유사성으로 합치지 않는다.
- Git 근거 adapter는 server에 설정된 repository에서 immutable commit과 regular blob을 직접 읽어 SHA-256을 확인한다. traversal·symlink·과도한 크기·hash 불일치를 거부한다. 현재 branch/head를 확인하고, 같은 source를 쓰기 전 다시 확인할 수 있다. 오래된 source와 실제 후속 변경은 시각 대신 commit ancestry로 비교한다. clock이 거꾸로 가는 경우와 갈라진 branch도 실제 로컬 Git으로 검사했다. 이 adapter의 Ubuntu 배포·교정 연결은 아직 수행하지 않았다.
- OpenRouter Decisions의 typed 연결과 검증을 제품 module로 구현했다. timeout·인증·quota·rate limit·잘못된 JSON·확률·model 치환을 명시적 실패로 처리한다. 자동 fallback과 authority write는 없다. 근거 관련성·직접 지지·명시적 반증을 독립 명제로 묻고, 질문·정책·source revision·model을 fingerprint에 결속한다. 적용 임계값에는 검증한 profile을 별도로 제공해야 한다.

native anchor는 source 인증 자체가 아니다. 여전히 기존 source admission/권한 검증이 필요하며, 이번 변경만으로 일반 수집과 교정이 공통 사실 resolution에 합류했다고 처리하지 않는다.

## 실제 모델의 두 번째 평가

질문 version은 `correction_entailment.v2`, 모델은 기존 선택인 `perplexity/pplx-decider-flash-v1.2`다. 응답 model은 `perplexity/pplx-decider-flash-v1.2-20261016`으로 확인했다. 새 연결 확인 1건·조정용 12건·독립 확인용 16건을 실제 호출했다. 원본 키와 운영 설정은 변경하지 않았다.

평가 자료는 `worker/eval/correction_judgment_cases.json`, 실행기는 `worker/eval/correction_judgment_eval.py`다. 독립 확인 자료는 호출 전에 고정했다. 출력은 기존 파일을 덮어쓰지 않는 private receipt로 기록하며 질문 version·입력 hash·연결 코드 hash·응답 확률을 함께 보존한다.

| 범위 | 결과 |
| --- | --- |
| 조정용 12건 | 12건 응답 완료, 진단 분류 9/12 일치 |
| 적용 후보 | 관련성 ≥0.95, 직접 지지 ≥0.85, 반증 ≤0.05. 운영 정책으로 채택하지 않은 조정용 후보 |
| 독립 확인 16건 | 16건 응답 완료, 진단 분류 13/16 일치 |
| 정상 변경안 4건 | 적용 후보 허용 1건, 보류 3건 |
| 허용하면 안 되는 12건 | 잘못된 허용 0건 |

**모델 적용 기준은 아직 미통과다.** 작은 표본의 잘못된 허용 0건만으로 안전성을 일반화하지 않으며, 정상 변경안 1/4 허용도 기능의 충분한 동작으로 판정하지 않는다. 근거 부족을 반증으로 혼동하는 경우도 남았다. 독립 확인 결과에 맞춰 임계값을 바꾸고 같은 자료를 다시 독립 시험으로 포장하지 않는다. 실제 교정 경로에서는 source 종류·권한·환경·범위를 코드로 먼저 검증해야 하며, 부정적인 모델 답 하나만으로 사실이 반증됐다고 확정하지 않는다.

## 검증 범위와 한계

실제 PG에서 초기 교정·freshness 회귀를 실행했고, 독립 DB의 SQLite/PostgreSQL parity에서도 관측 version 두 개가 하나의 관측 identity로 보존되는 것을 확인했다. 서로 public schema를 초기화하는 parity 시험 두 개를 같은 DB에서 겹쳐 실행한 첫 시도는 충돌해 실패했다. 또한 그 DB에 남은 legacy ledger의 동명 table 때문에 canonical freshness DDL 검사가 실패했다. parity와 canonical 검사를 각각 전용 DB로 분리한 재시험만 최종 증거로 사용한다. 운영 DB를 시험 대상으로 사용하지 않았다.

현재 검사는 source·단위 함수·실제 Git CLI·실제 PG·실제 OpenRouter 호출을 구분한다. 일반 batch/설명 검색 API, 관측 admission과 공통 사실 resolution, 정규 intake/autopilot/교정 작업자 연결, source retention 참조 보호, context/list/query와 graph 순서 수렴은 남아 있다. 충돌 처리 B도 아직 선택하지 않았다. 전체 목표를 이 보완 작업으로 축소하거나 완료 처리하지 않는다.

검사 결과:

- `uv run pytest -q tests/test_evidence_correction.py`: 별도 실제 PG에서 **40 passed**. 표시 구간 보존까지 포함한 검사다.
- `uv run pytest -q tests/test_evidence_correction.py::test_pg_summary_correction_does_not_mark_unverified_fields_current tests/test_pg_freshness_revalidate_cli.py`: canonical 전용 DB에서 **10 passed**. 부분 검증 대상의 전체 freshness 승격 거부와 제외 건수 보고도 확인했다.
- `uv run pytest -q tests/test_pg_parity.py`: 별도 ledger 전용 DB에서 **9 passed**. native 관측의 두 source version 보존도 포함한다.
- worker 전체 검사: **5243 passed, 624 skipped, 4 warnings**. 환경 선택형 skip은 실증으로 세지 않는다. opt-in PG 검사는 위와 같이 별도 실행했다.
- Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`: **BUILD SUCCESSFUL**.
- `git diff --check`: 통과.

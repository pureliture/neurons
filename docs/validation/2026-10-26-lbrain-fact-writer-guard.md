# 공통 사실의 일반 writer 우회 차단

전체 교정 goal은 미완료다. 이 변경은 서버가 정확히 식별한 기존 사실에 대한 일반 MemoryCard writer의 우회를 막는다. 아직 식별하지 않은 다른 제목의 의미상 중복까지 해결했다는 뜻은 아니다.

## 재현한 문제와 변경

실제 Ubuntu PostgreSQL에서 관리 중인 카드의 summary를 직접 SQL로 바꾸거나, `insert_card`/`upsert_card`로 같은 project·card_type·title의 별도 active/current 카드를 만들 수 있었다. 과거의 정상 resolution ID를 transaction 설정에 넣어도 직접 SQL을 차단하는 규칙은 없었다. 최초 재현은 **4 failed, 1 passed**였다. 이 중 이력 삭제는 기존 FK가 이미 막았으나 명시적인 교정 이력 보호 오류는 없었다.

`memory_cards`의 DB trigger가 관리 대상의 내용·상태 변경을 검사한다. 공통 교정은 현재 transaction ID와 실제 target/successor ID를 묶은 `lbrain_fact_mutation_intents`를 먼저 만들고, 같은 transaction에서 최종 resolution을 보존한다. deferred FK와 결과 검사는 successor·fact key·실제 적용 결과의 연결을 commit 시 확인한다. 과거 resolution의 설정값만 다시 제시해서는 새 transaction에서 수정할 수 없다. 저장된 intent의 변경·삭제도 거부한다.

최초 교정이 queue에서 대기할 때는 fact head가 아직 없을 수 있다. coordinator가 기존 대상을 확인하고 요청을 수락하는 transaction에서 정확한 서버 정책과 target을 `lbrain_fact_admission_rules`에 등록한다. 이때부터 target 수정과 같은 식별자의 새 current 승인이 차단된다. constructor 같은 읽기 경로에서는 정책을 등록하지 않는다. 기존 등록과 다른 설정이나 철회된 설정을 옛 submitter가 덮어쓰지 못하며, worker는 저장된 정책 version과 활성 여부도 재검증한다. 설정 변경/철회의 관리 API와 기존 교정 영향 재평가는 이 변경으로 완료되지 않는다.

관리 범위 등록 시 guard epoch를 갱신한다. writer는 이 행을 공유 잠금으로 읽으므로 신규 등록 전의 Repeatable Read snapshot이 등록을 놓친 채 중복 current를 생성하려 하면 serialization 오류가 발생한다. 일반적인 lock 순서는 기존 authority의 memory table→전역 advisory→좁은 잠금을 유지한다. 짧은 lock timeout을 늘리지 않는다.

embedding lease/CAS가 embedding·모델·상태·revision·갱신 시각만 바꾸는 것은 허용한다. 사실의 payload·summary·현재 상태나 근거를 함께 바꾸면 이 예외에 해당하지 않는다. 다른 project나 card_type의 일반 기록을 전부 차단하지 않는다.

이 규칙은 제품 writer와 일반 DML의 무결성 보호다. DB 소유자가 trigger를 끄거나 schema를 변경하는 관리 권한을 통제하는 보안 경계가 아니다. fact 제어 테이블의 임의 조작까지 허용한 적대적 DB 소유자를 방어한다고 주장하지 않는다.

## 검증 범위

- 최초 보호 규칙의 단독 Ubuntu 실행: **27 passed, 1 warning in 30.93s**.
- queue 대기·옛 snapshot·철회 설정 검사 추가 후 실제 PG 실행: **33 passed, 1 warning in 34.09s**.
- 이 두 번째 source bundle SHA-256: `fd01ce043b4da6b6c41b3b5a6fc22355438cef810da1017f3cbaa6ec3d57412a`.
- intent 불변성·schema 재적용·최종 결과의 다른 successor 거부까지 포함한 최종 Ubuntu Couch/PG/Git/Neo4j 실행: **72 passed, 1 skipped, 1 warning in 48.16s**. `uv run --no-project pytest -q -rs --tb=short`로 `test_fact_writer_guard.py`, `test_fact_resolution.py`, `test_fact_resolution_graph.py`, `test_correction_capabilities.py`, `test_native_fact_intake.py`, `test_observation_provenance.py`, `test_pg_schema_apply_cli.py`를 실행했다. graph 종료 제어가 필요한 한 검사는 이번 실행에서 skip했으며 통과로 세지 않는다.
- 최종 source bundle SHA-256: `87ee6fd5e9a4c1d629e65c2e376186436a322996b8edc0087b6c5725364284eb`.
- 직전 source의 기존 PG CI 25모듈 회귀: `ATLAS_QRET_SYNTHETIC_SQL=1`과 `uv run --no-project pytest -q --tb=short` → **781 passed, 1 warning in 162.02s**. 최종 source에서 추가한 intent 변경/삭제 trigger와 검사는 위 72건에서 별도로 확인했다.
- 최종 worker 전체 `uv run pytest -q`: **5294 passed, 669 skipped, 4 warnings in 58.29s**. 환경 선택형 skip은 실제 통과로 계산하지 않는다.
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`: **BUILD SUCCESSFUL**, Gradle test task는 UP-TO-DATE였다. `git diff --check`: 통과.

처음 두 검증을 같은 DB에서 겹쳐 실행한 작업 실수로 advisory lock timeout이 발생했다. 한 실행을 중단한 뒤 단독 실행한 결과를 위에 기록했다. 이 혼합 실행의 오류를 제품 실패나 통과 근거로 사용하지 않았다.

Ubuntu 작업 전용 PG와 읽기 전용 source overlay를 사용했다. 검증 의존성 image digest는 `sha256:ee9b21c07899568b49342da9b884adb84847fd02c0f7583f0464d000fa155f6a`이며 최종 배포 image의 identity 증거가 아니다. 운영 DB·서비스는 변경하지 않았다. core 검사의 source admission callback은 명시적 대역이며 coordinator 검사는 실제 Git과 PG를 사용한다. 실제 의미 모델·embedding provider의 검증을 대신하지 않는다.

CI의 실제 PostgreSQL lane에도 공통 resolution 및 writer guard 검사를 추가했다. 미식별 의미상 중복, 일반 native 근거 admission, 기존 교정의 철회 전파, 전체 retention/GC, 백업·복원과 최종 제품 identity는 계속 남아 있다.

운영 rollback 시 trigger를 끄고 이전 writer로 단순 복귀하면 같은 우회가 재개된다. 관리 대상 쓰기를 중지하거나 guard와 호환되는 공통 resolver를 유지해야 한다. fact·근거·판단·intent 이력은 보존한다. 실제 운영 적용/rollback은 수행하지 않았다.

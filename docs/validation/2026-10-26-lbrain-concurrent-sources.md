# 동일 필드의 복수 원천과 실제 동시 교정

전체 goal은 진행 중이다. 이 변경은 같은 카드의 같은 사실을 검증하는 독립 원천 정책과, 같은 revision에서 시작한 실제 동시 요청을 다룬다. 일반 native admission·의미 target 검색·모든 writer의 사실 식별까지 완료한 것은 아니다. B 자동 숨김은 계속 미결정이다.

## 발견한 제약과 변경

기존 `lbrain_fact_admission_rules`의 `UNIQUE(project,target_title,card_type,field_path)` 때문에 같은 카드의 같은 필드에는 원천 정책을 하나만 등록할 수 있었다. 독립 source가 같은 fact를 검증하려 해도 두 번째 등록이 `fact_policy_changed`로 거부됐다. 앞선 서로 다른 title의 alias 실증은 이 경우의 증거가 아니었다. 실제 Git·PG 동시성 검사에서 **1 failed, 2 passed in 3.50s**, 거부 사유를 추가한 재실행에서 **1 failed, 3 passed in 4.23s**로 확인했다. 두 번째 묶음의 나머지 1건은 기존 import 기한 검사다.

필드의 사실 소유권과 원천 정책 수를 분리했다. `lbrain_fact_admission_targets`는 같은 project·title·card_type·field를 하나의 fact에만 연결한다. 원천 정책은 이 소유권을 FK로 참조하므로 같은 사실에 여러 정책을 등록할 수 있다. 다른 사실로 필드를 재분류하는 요청은 등록하지 않으며 기존 structured 거부 계약을 유지한다. PK 충돌과 행 잠금은 같은 대상의 동시 등록도 하나의 소유권으로 합류시킨다. 새 current 기억을 생성하는 기능은 추가하지 않았다.

복수 정책의 최초 입력에서는 상대 원천의 재검토 예약이 공통 fact head 생성보다 먼저 필요할 수 있다. 이 예약→head FK는 같은 transaction의 commit 시점에 검사한다. transaction이 끝날 때 head가 없으면 실제 FK 오류로 전체 rollback한다. 없는 fact의 예약을 허용한 것이 아니다.

기존 등록으로 소유권을 채우고 기존 단일 정책 UNIQUE를 교체한다. 기존 등록·queue·근거·current 값을 재작성하지 않는다. 동일 대상에 서로 다른 fact가 연결된 손상된 기존 데이터는 FK validation에서 migration 전체를 중단하며 어느 한쪽을 임의 선택하지 않는다. 재적용은 멱등이다.

## 실제 동시 요청

두 worker가 source snapshot을 모두 읽은 뒤 Barrier를 통과해 실제 Git·PG 교정을 동시에 실행한다. 같은 source가 `ready`인데 호출자 제안이 각각 `ready`와 `failed`여도 실제 값 `ready`만 반영하고 authority 변경은 1건이다. 두 제안은 각각의 결과에 남는다.

같은 revision의 서로 다른 두 파일이 `ready`와 `failed`를 지지하면, 동일 카드의 두 원천 정책을 모두 등록하고 충돌로 보류한다. 어느 요청이 잠금을 먼저 얻었는지로 값을 선택하지 않는다. 이전 값은 확정된 새 값으로 승격하지 않으며 두 assertion을 보존한다.

같은 원래 카드 revision에서 다른 두 필드를 동시에 교정하는 검사도 추가했다. 실제 PostgreSQL backend PID가 다른 두 connection에서 각각 기능 상태와 배포 상태를 바꾸고, 최종 단일 successor가 두 변경을 모두 담는지 확인한다. 이 복수 필드 코어 검사의 admission callback은 명시적 대역이며 실제 모델 검증으로 계산하지 않는다.

## 검증과 경계

초기 복수 정책 등록 수정 뒤에는 첫 공통 head 생성 실패도 드러나 **1 failed, 24 passed in 12.41s**였다. 예약 FK의 commit 시점 검사를 포함한 후속 bundle `db305a9bfc6713aa64360230dc41a4cd9d6b6b3de786f3c13aca4df770c63629`에서 동시성·writer guard·schema·기존 import **129 passed, 2 skipped in 40.39s**를 확인했다. legacy 단일 정책의 upgrade·멱등 재적용·상충 기존 데이터의 중단과 실제 orphan 예약 commit 거부도 포함한다. skip은 import의 별도 opt-in lane이다.

최종 bundle `05c82db0042ee50eac5759c2e6123e58d3c3cdbc868fc8da321598c6fe4270c6`에서 실제 PG의 resolution/writer guard/semantic/source lifecycle/verification/competition/scheduler/concurrency/schema/import **232 passed, 2 skipped in 153.29s**를 확인했다. `uv run --no-project pytest -q --tb=short`로 해당 11개 모듈을 실행했다.

같은 bundle의 실제 OpenRouter·Git·PG·MCP HTTP·CLI는 **6 passed, 1 warning in 89.96s**다. 새 6번째 검사는 동일 카드의 두 독립 상반 원천을 실제 모델로 판단하고 두 worker를 동시에 실행해 충돌 보류를 확인했다. 앞선 자동 합의 검사에는 살아 있는 worker의 장기 미해결 감지와 실제 완료 후 경고 해제도 포함한다. model client 대역 없이 실행했으며 API 일시 인증 파일은 제거 후 부재를 확인했다. 모델 질문·선택 threshold·coordinator identity는 이전 v3 검증 profile과 같고, 이 6건을 새 독립 평가 40건으로 합산하지 않는다.

최종 source의 `cd worker && uv run pytest -q`는 **5306 passed, 736 skipped in 56.18s**, Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`는 **BUILD SUCCESSFUL**, test task는 UP-TO-DATE였다. 선택형 skip은 실제 통과로 계산하지 않는다. `git diff --check`는 통과했다.

검증은 작업 전용 Ubuntu runtime과 합성 source를 사용했다. source overlay를 주입한 의존성 image는 최종 배포 image identity와 다르다. 운영 migration·서비스 활성화·merge·사용자 기억·운영 GC는 수행하지 않았다. 복수 정책이 등록된 뒤에는 근거를 삭제하고 옛 UNIQUE를 다시 추가하는 방식으로 rollback하지 않는다. 문제가 있으면 관리 대상 writer를 중지하고 호환 reader와 소유권·근거·판단·이력을 보존한다.

## CI에서 발견한 기한 검사 누락

직전 `fe2aaa1`의 worker CI는 기존 `test_deadline_interrupts_blocking_boundary`에서 2회 실패했다. 로그의 `Exception ignored in ... WeakSet ... _remove`에서 SIGALRM의 `DeadlineExpired`가 unraisable로 처리됐고, 그 뒤 기한이 지난 새 boundary 호출이 완료됐다. 무관한 통과 결과나 단순 재실행으로 이 실패를 대체하지 않았다.

`pg_qdrant_import_cli`는 비싼 코드 identity 확인 뒤와 I/O 진입 전후에 monotonic budget을 다시 확인한다. I/O 차단 검사는 source 전체 hashing과 분리했고, alarm 예외가 무시된 preflight가 기한 경과 후 새 I/O를 시작하지 못하는 검사도 추가했다. 로컬 해당 모듈은 **102 passed, 2 skipped in 6.03s**다. 비협조적인 cleanup까지 포함하는 실제 process supervisor의 검사를 낮추거나 skip하지 않았다.

수정 commit `7fb1ffe`의 CI 8개는 모두 성공했다. 실제 worker job은 **5306 passed, 728 skipped in 110.14s**, 실제 PG job은 기존 계약 **782 passed**, schema **8 passed**, 공통 교정 **115 passed**였다. 이는 뒤의 복수 정책 schema 변경 전 commit의 결과이므로 최종 commit의 CI와 구분한다.

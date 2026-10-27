# 교정된 사실의 PostgreSQL 백업 복원과 재생

이 검사는 합성 Git source와 작업 전용 PostgreSQL에서만 수행했다. 교정된 PostgreSQL의 단일 테스트 schema를 실제 `pg_dump` custom archive로 저장하고, 새 임시 데이터베이스에 `pg_restore --exit-on-error`로 복원한다. 운영 백업 전체, Couch 원문 저장소, graph 재구축이나 일반 session backfill까지 검증한 것은 아니다.

원래 저장소에서 `missing → ready → failed`를 실제 Git revision과 교정 worker로 반영했다. 복원 후 과거 `ready` 교정의 결과는 결정 당시 값 `ready`와 현재 값 `failed`를 따로 반환했고, 같은 요청 key를 다시 제출해도 기존 결과를 사용했다. 직접 SQL로 관리 카드 내용을 변경하려는 시도도 복원된 guard가 거부했다. 과거 `ready` source를 제안하는 수집 요청은 복원된 현재 값 `failed`를 기준으로 제출했으며, worker는 오래된 관측을 `retain_history`로 처리했다. 현재 조회는 `failed`를 반환하고 추가 steward 결정은 발생하지 않았다.

첫 시험에서는 수집 요청의 `old_value`에 과거 `ready`를 넣어 현재 값 충돌로 거부됐다. 복원 자체의 실패가 아니며, 해당 시도를 보존하고 요청이 실제로 조회한 현재 값 `failed`와 오래된 제안 `ready`를 구분하도록 수정했다. 수정 후 독립 재실행과 저장소의 driver를 사용한 재실행이 각각 통과했다.

최종 Ubuntu source bundle `sha256:195201b46a0bd1eee875ba3bf9b000f74354e8b178aac36bd2ac11c8651248bf`의 `test_fact_backup_restore_live.py`는 **1 passed, 0 skipped in 5.66s**다. 테스트의 임시 schema와 복원 데이터베이스, driver의 임시 dump가 모두 제거된 것을 따로 확인했다. 작업 전용 OpenRouter 임시 인증은 이 검사에 사용하지 않았다. 로컬 worker 전체는 **5387 passed, 819 skipped in 57.33s**이며 이 복원 검사는 opt-in이 없어 skip됐다.

테스트는 `LBRAIN_TEST_BACKUP_CONTROL_DIR`과 `LBRAIN_TEST_RESTORE_ALLOW_DB_CREATION=1`이 없으면 skip된다. 외부 `fact_backup_restore_driver.sh`에는 작업 전용 PostgreSQL 컨테이너와 빈 절대 제어 디렉터리를 명시해야 한다. driver는 지정된 합성 source database와 무작위 테스트 schema·복원 database 이름을 검증하고 각 `pg_dump`/`pg_restore`에 90초 timeout을 둔다. 시험 컨테이너에 Docker 권한이나 운영 credential을 전달하지 않는다. 비정상 종료 시 테스트가 만든 `lb_restore_` database와 임시 archive만 소유권을 확인해 정리해야 한다.

별도의 실제 Neo4j 검사에서는 다른 합성 project의 새 graph 영역에 PG에 남은 교정 전후 projection 작업을 직접 재생했다. 최신 작업을 먼저 처리한 뒤 이전 작업을 역순·중복 처리해도 graph를 거친 질의는 현재 `ready` 카드만 반환했다. source bundle `sha256:cef5aaad1de88eea85a23d3ea6ec22567c1dfc0b6cfa3fd350838033bcc59cf5`의 graph 계약 묶음은 **5 passed, 1 skipped in 9.07s**다. skip은 별도 graph 종료·복구 제어가 필요한 검사다. 실제 outbox 처리기를 직접 호출한 부분 검사이며, 자동으로 전체 범위를 열거·재구축하는 운영 도구의 완전성은 검증하지 않았다. 이 graph 검사 묶음을 CI의 실제 PG·Neo4j step에도 추가했다.

기존 일회성 `run_graph_replay`는 비활성 adapter가 `skipped_disabled`를 반환하거나 이전 실행이 dead letter로 끝난 경우에도 다음 결과를 성공으로 표시했다. 합성 재현을 통해 두 결함을 확인하고, 저장 응답이 없으면 재시도하고 이전 dead letter는 계속 실패로 보고하도록 수정했다. 이미 완료한 key라도 내용 hash가 다르면 충돌로 차단한다. 예전 checkpoint에 내용 hash가 없으면 같은 내용이라고 추측하지 않고 명시적으로 차단한다. 기존의 명시적인 성공 응답은 계속 지원한다. checkpoint 파일은 같은 디렉터리에서 완성한 뒤 교체하여 부분 쓰기로 이전 파일을 손상하지 않게 했고, 교체 실패를 주입해 이전 파일의 보존을 확인했다. 동시 worker 간 checkpoint 조정은 제공하지 않는다.

수정한 source bundle `sha256:d7da934a770c3bd691fa95e495f6028e3b332d003db230c9abfad93a87943aa3`에서 실제 PG·Neo4j와 replay 계약 묶음은 **16 passed, 1 skipped in 9.19s**다. 두 실제 모델 profile도 이 bundle의 코드 identity로 다시 로드했다. checkpoint를 새로운 graph 인스턴스에 결속하고 전체 authority를 빠짐없이 열거하는 기능은 아직 없다.

C33의 일반 session backfill·전체 graph 재구축·Couch와 PG의 일관된 시점 복원은 남아 있다. 이 검사를 전체 백업 복원 또는 운영 재해 복구 완료로 승격하지 않는다.

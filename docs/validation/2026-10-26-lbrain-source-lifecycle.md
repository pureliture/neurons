# 교정 근거 보존과 철회 후 자동 재검토

전체 goal은 진행 중이다. 이 변경은 공통 fact 경로가 이미 사용한 근거의 보존·철회·자동 재검토를 연결한다. 일반 native 근거의 의미 admission, parser·정책·접근 권한 변경의 자동 전파와 파생 기억 재계산은 남아 있다. 유효한 두 근거의 우열을 알 수 없는 충돌에서 B를 선택하지 않는다.

## 구현

관측마다 실제 source receipt를 가리키는 `lbrain_fact_observation_sources`를 저장한다. 관측·참조 양쪽의 deferred FK와 기존 assertion/event FK 때문에 교정 이력이 남은 근거를 삭제할 수 없다. 참조 없는 관측·연결·receipt는 한 transaction에서 함께 삭제할 수 있다. GC 계획 후 새로운 교정이 참조를 추가하는 경합도 FK가 commit 시 보호한다. 기존 모든 GC CLI의 정책·백업·보존 기간을 검증했다는 뜻은 아니다.

receipt의 본문과 hash·proof·생성 시각, 관측은 변경할 수 없다. 본문 SHA-256을 DB에서 검증한다. project 소유권은 첫 연결 때 확정하고 이후 바꿀 수 없다. 철회는 `active→revoked` tombstone이며 재활성화를 허용하지 않는다. 유실·변조된 기존 근거가 있으면 schema 적용 전체를 중단한다. 없는 근거를 만들거나 이력을 삭제해 migration을 통과시키지 않는다.

내부 `FactSourceLifecycle.revoke`는 project·receipt hash와 idempotency key를 검사한다. receipt 철회, 내구성 source event와 project generation 변경은 같은 transaction에 들어간다. 원문·이전 판단·과거 current 값은 보존한다. public MCP의 교정 요청자에게 철회 권한을 추가하지 않는다.

새 조회는 worker보다 먼저 철회를 확인한다. 현재 값을 지지하는 다른 유효 근거가 있으면 값을 유지하고 현재 근거 목록에는 유효한 참조만 싣는다. 근거가 전부 철회된 필드는 확정값으로 제공하지 않는다. 같은 카드의 다른 유효 필드는 등록된 표시 template으로 재구성하고, 독립적인 정상 카드는 계속 제공한다. 목록·context·vector 후보는 LIMIT 전에 무효 카드의 자리를 제외한다. 후보 검색과 최종 조회 snapshot 사이의 철회도 fact view가 다시 검사한다. `source_evidence_gap_fields`, `source_evidence_partial_cards`로 빠진 범위를 알린다. 과거 시점 조회의 재구성은 추가하지 않는다.

한 필드의 검증 표시는 그 필드의 현재 유효한 proof hash와 대조한다. 마지막 교정이 다른 필드였어도 철회된 근거의 `field_verifications`를 남기지 않는다. 내부 복구용 `project_cards`는 canonical 계보를 유지하며, 공개 `read_view`에서만 무효 필드를 제외한다.

worker는 source event를 fact/policy별 내구성 target으로 나눠 기존 재검토 queue에 등록한다. cursor와 batch 한도로 많은 대상을 여러 실행에 걸쳐 처리한다. 영향 계획·queue 등록은 같은 transaction이며 source 읽기나 모델 호출은 이 잠금 안에서 수행하지 않는다. 남은 동일 값의 유효 근거가 있으면 `covered`, 설정이 없으면 다음 시각을 가진 `blocked`로 둔다. 나중에 정책이 등록되면 cursor가 지난 대상도 발견한다. 새 근거가 생기면 기존 coordinator가 독립 현재 검증을 수행하고 같은 재검토 작업을 해결한다. 옛 값을 대신 복원하지 않는다.

Git ref가 A→B→A로 실제 돌아오면 불변의 A 관측을 다시 현재 검증해 사용할 수 있다. 지연된 A의 도착이나 과거 성공 요청의 replay만으로 현재를 되돌릴 수는 없다.

## 실행 경계

서버에서 `NEURON_LBRAIN_PGVECTOR_DSN`과 검증된 private 설정을 제공한다. 아래는 source 계약의 예시이며 운영 실행·상주 서비스 설치 기록이 아니다.

```sh
uv run -m agent_knowledge.fact_source_lifecycle revoke \
  --project synthetic-correction --source-ref '<receipt-id>' \
  --expected-hash 'sha256:<receipt-content-digest>' --request-key '<stable-key>'
uv run -m agent_knowledge.fact_source_lifecycle worker \
  --project synthetic-correction --config '<private-config-file>' \
  --iterations 1000 --poll-seconds 1
uv run -m agent_knowledge.fact_source_lifecycle monitor \
  --project synthetic-correction --heartbeat-grace 120 --overdue-grace 120
```

worker와 별도 프로세스인 monitor는 설정 파일·source·모델을 읽지 않고 PG의 event/target/queue/heartbeat를 확인한다. 사용할 수 없으면 `attention_required=true`와 비정상 종료를 돌려준다. worker의 source 영향 transaction은 lock 1초·statement 5초, CLI의 DB 연결은 5초로 상한을 둔다. 기존 caller의 더 짧은 제한은 유지한다. 실제 semantic worker는 기존 coordinator의 더 긴 lease를 사용한다. 상주 supervisor 설치, 외부 통지 연결, 장기 운영 감시는 별도 남은 조건이다.

## 검증과 한계

작업 전용 Ubuntu PostgreSQL·Git·읽기 전용 source overlay에서 실행했다. 최초 재현은 receipt 삭제·본문 덮어쓰기·source 없는 관측·철회 후 조회의 **4건 실패**였다. source 및 fixture 수정 뒤 실제 PG의 `test_fact_source_lifecycle.py`, `test_fact_resolution.py`, `test_fact_writer_guard.py`, `test_correction_semantic.py`는 **71 passed, 1 warning in 66.29s**다. lifecycle 모듈의 21건은 전부 실제 PG에서 실행했다.

별도 CLI 프로세스를 실제로 종료한 뒤 monitor가 heartbeat 중단을 감지하고, source의 새 commit과 worker 재시작만으로 동일 task가 해결됨을 확인했다. GC 경합은 실제 PG의 lock 대기를 확인한 뒤 FK 거부를 검사했다. 직접 Git JSON 검사에서는 명시적 source adapter를 사용하며, 코어 일부 검사의 admission callback은 대역이다.

같은 source의 `test_semantic_correction_live.py`는 **3 passed, 1 warning in 28.49s**다. 실제 `perplexity/pplx-decider-flash-v1.2`의 OpenRouter client, Git, PostgreSQL, MCP HTTP와 source lifecycle CLI를 사용했다. 추가한 경로는 근거 철회→worker 종료→독립 monitor 감지→새 source commit→worker 재시작→동일 task의 모델 재판단·교정이다. 이 검사에서 model client 대역과 수동 task 실행·due 시각 변경을 사용하지 않았다. 기존 독립 평가 profile의 byte hash는 `sha256:9cae8dfc9d32b362e33240625b25b32f17ba5516817166a79b20e50b83767e59`다. 이 live 회귀 3건을 새 독립 모델 평가로 합산하지 않는다. 작업 전용 인증 파일은 제거 후 부재를 확인했다.

71건과 실제 모델 3건의 source bundle SHA-256은 `0c738f75b7ac28a22487c4cb2cd67de2c32a90b117c7d22067602fc94c708dd9`다. 이후 제품 코드 변경 없이 합성 벡터의 LIMIT 전 제외 검사만 보강한 최종 bundle `ff58a4c50abee7ed17a7f6b4033f794a3cb582b77e391157a712253824f4b7d1`에서 lifecycle **21 passed, 1 warning in 26.22s**를 확인했다. 실제 embedding provider 호출 증거로 계산하지 않는다. 검증 의존성 image digest는 `sha256:ee9b21c07899568b49342da9b884adb84847fd02c0f7583f0464d000fa155f6a`이며 최종 배포 image identity 증거는 아니다.

worker 전체 `uv run pytest -q`: **5305 passed, 697 skipped, 4 warnings in 55.17s**. 환경 선택형 skip을 실제 통과로 계산하지 않는다. Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`와 `git diff --check`는 통과했다. 실제 PG CI lane에도 lifecycle 모듈을 추가했다.

같은 source에서 기존 PG CI 25모듈·schema 적용·fact graph·source capability·native intake·관측 provenance를 실제 PG/Couch/Neo4j로 순차 실행한 회귀는 **818 passed, 1 skipped, 1 warning in 183.50s**다. `ATLAS_QRET_SYNTHETIC_SQL=1`과 `uv run --no-project pytest -q --tb=short`를 사용했다. skip 1건은 별도 graph 종료 제어가 필요한 검사이며 이전의 종료·복구 실증을 다시 수행한 것으로 세지 않는다.

운영 runtime·사용자 기억은 변경하지 않았다. 이 schema의 rollback에 FK/guard를 삭제하고 과거 writer를 다시 허용하는 방법을 사용하지 않는다. 문제가 생기면 관리 대상 쓰기와 lifecycle worker를 중지하고, 호환되는 읽기 경로와 이력 보존을 유지한다. 운영 적용 및 rollback 자체는 수행하지 않았다.

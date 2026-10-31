# 관측 시각과 현재 검증 시각의 분리 및 기한 재검토

전체 goal은 계속 진행 중이다. 이번 변경은 공통 fact 경로의 현재 검증 시각·기한·자동 재확인을 연결한다. 일반 native 근거 자체의 의미 admission, 정책 version 전환·권한 회수·파생 기억 전파, 실제 embedding, 상주 운영·장기 감시와 최종 복원 검증은 남아 있다. 우열 불명 충돌의 B 정책은 선택하지 않는다.

## 기존 문제와 처리

Git의 같은 고정본을 다시 읽으면 기존 불변 관측을 재사용한다. 이 관측의 `observed_at`에 검증 기한을 계속 더하면, 지금 실제로 재확인했어도 이미 만료된 것으로 표시된다. 미래 시각이 들어 있으면 반대로 검증 기한이 과도하게 늘어날 수 있다. 원래 관측 시각을 현재 시각으로 덮어쓰면 이력을 훼손한다.

`lbrain_fact_verifications`에 성공한 현재 검증마다 별도 영수증을 저장한다. 기존 관측·근거·과거 처리 결과는 수정하지 않는다. 검증 영수증은 해당 terminal resolution·assertion·fact에 연결하며 DB가 `checked_at`과 `expires_at`을 계산한다. 외부가 미래 시각을 INSERT에 넣어도 DB 시각으로 대체하고, 기록 후 변경·삭제는 거부한다. 현재 교정·동일 값의 새 독립 재확인만 기록하며 history-only 처리와 단순 terminal 요청 replay는 검증을 연장하지 않는다. authority·검증 영수증·다음 확인 예약·resolution은 같은 PG transaction에 들어간다.

원래 관측이 오래됐거나 미래 시각을 가졌어도 서버가 실제 현재성을 확인한 시각부터 정책의 `max_age_seconds`를 계산한다. 원래 관측 시각이 없으면 빈 값을 보존하며 그 자리에 서버 시각을 채워 넣지 않는다. 현재 source 검증을 통과하는 조건은 그대로 필요하다. 시각만으로 어느 값이 최신인지 판단하지 않는다. 이 부분의 과거·미래·누락 검사는 admission callback 대역을 쓴 PG 코어 검사이며, 일반 native source의 진위 검증을 완료했다는 뜻은 아니다.

## 자동 확인과 조회

등록된 기존 fact/policy만 `lbrain_fact_check_schedules`에 예약한다. 기한이 지나면 기존 `fact_source_lifecycle worker`가 bounded batch로 현재 source 재조회 작업을 공통 queue에 등록한다. source 확인과 모델 호출은 authority 잠금 밖의 기존 coordinator가 수행한다. 한 기한에 여러 dispatcher가 실행돼도 작업을 중복 등록하지 않는다.

같은 고정본이 그대로 유효하면 관측·assertion·현재 카드를 추가 생성하지 않고 새 검증 영수증과 기한만 남긴다. source 장애는 동일 queue 작업의 backoff/lease 복구를 따른다. 설정이 없거나 비활성이면 내구성 `blocked`로 보존하고 같은 정책이 돌아오면 다시 진행한다. 설정 version 자체의 안전한 전환 API는 이번 변경에 포함하지 않는다. 확인 도중 source가 바뀌어 history-only 결과로 끝나도 예약이 영구 종료되지 않도록 다음 generation을 예약한다.

기존 head에 검증 영수증이나 예약이 없으면 검증 시각을 소급해 만들지 않는다. 초기 작업으로 실제 재확인한다. 독립 monitor는 기한 초과·차단뿐 아니라 예약 자체가 누락된 기존 fact도 확인한다. 따라서 worker가 시작조차 못한 상태를 정상으로 오인하지 않는다.

`fact_views`와 처리 결과의 `current`에 `verification.checked_at`, `verification.expires_at`, `verification_state`를 제공한다. 기한 경과는 `recheck_due`이며 값이 잘못됐다는 증거로 취급하지 않는다. `context`의 해당 항목과 section에는 `recheck_due_fields`를 표시한다. 근거 철회에 따른 `invalidated`와 구분한다. 시간 경과만으로 target revision을 바꾸지 않으며, 실제 새 검증·source·현재 값 변경은 hash와 generation에 반영한다. 다른 필드까지 카드 전체를 검증 완료로 올리지 않는다.

유효한 대체 근거가 있으면 그 근거의 실제 검증 영수증을 선택한다. 철회된 근거를 검증 표시로 남기지 않는다. 과거 처리 결과의 검증 시각과 지금의 검증 상태를 구분해 재시도 결과에도 제공한다.

v1 `registered_json` adapter는 기존 관측 window와 요청 계약을 유지한다. 관측 시각이 빠진 v1 입력을 이 변경으로 통과시키지 않는다. 새 서버 검증 시각은 공통 resolver가 실제 현재성을 재확인한 경로에서만 사용한다.

## 검증

작업 전용 Ubuntu PostgreSQL/Git에서 core·lifecycle·writer guard·semantic 회귀를 실행해 **86 passed, 1 warning in 95.92s**를 확인했다. 이 중 새 검증·기한 모듈은 parameter를 포함해 15건이다. 같은 고정본 재검증·terminal replay·미래/과거/누락 시각·transaction rollback·변조 거부·동시 dispatcher·설정 차단/회복·기존 head 초기 확인·history-only 후 예약 복구를 검사한다.

실제 CLI 프로세스에서는 새 요청이나 due 시각 수정 없이 기한을 기다려 같은 고정본을 재확인했다. worker를 종료하고 기한이 지난 뒤 독립 monitor가 중단/기한 초과를 감지함을 확인했다. 중단 중 source를 새 commit으로 변경한 뒤 worker 재시작만으로 새 값을 자동 교정했다.

실제 OpenRouter `perplexity/pplx-decider-flash-v1.2`, Git, PG, MCP HTTP와 CLI의 연동은 **4 passed, 1 warning in 45.28s**다. 기존 세 경로에 실제 모델의 기한 재검토·worker 중단/감지/복구 경로를 추가했다. 기존 검증 profile `sha256:9cae8dfc9d32b362e33240625b25b32f17ba5516817166a79b20e50b83767e59`를 그대로 검증했다. 모델·질문·source capability 선택 코드는 변경하지 않았으며 이 회귀를 새 독립 모델 평가로 계산하지 않는다. 일시 API 인증 파일은 제거하고 부재를 확인했다.

86건의 source bundle은 `fa285be33fadc67da7288f3568206a032f195acbf99a84d8558edb71e97b7c44`, v1 strict 관측 시각 계약을 보존한 실제 모델 4건의 bundle은 `592895bae43aff748fa2aa837953fb451b7a3b9f792db5ebf79ec45364b67db8`다. 이후 monitor의 예약 누락 감지까지 포함한 최종 bundle은 `bd3ff16cf84374b32e129693904571a0e38445a59e36e0ec730d845d2c827fae`다. 읽기 전용 source overlay와 검증 의존성 image를 사용했으며 최종 배포 image identity 검증은 남아 있다.

worker 전체 `uv run pytest -q`: **5305 passed, 713 skipped, 4 warnings in 54.80s**. 환경 선택형 skip을 실증으로 계산하지 않는다. Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`는 **BUILD SUCCESSFUL**, test task는 UP-TO-DATE다. 실제 PG CI에도 새 검증 모듈을 추가했다.

최종 bundle에서 기존 PG CI 25모듈·schema·fact graph·capability·native intake/provenance와 새 검증 기한 모듈의 실제 PG/Couch/Neo4j 회귀는 **833 passed, 1 skipped, 1 warning in 214.39s**다. `ATLAS_QRET_SYNTHETIC_SQL=1`과 `uv run --no-project pytest -q --tb=short`를 사용했다. skip 1건은 별도 graph 종료 제어가 필요한 검사이며 통과로 세지 않는다. 같은 DB의 PG 검사는 프로세스별로 순차 실행했다.

검증은 합성 source와 격리 runtime에서 수행했다. 운영 활성화·사용자 기억 변경·운영 GC를 수행하지 않았다. rollback 시 검증/근거 이력을 삭제하지 않고 관리 대상 쓰기·자동 worker를 중지하거나 호환되는 읽기·검증 경로를 유지한다. 단일 PG clock을 기준으로 하며 서버 clock 자체가 바뀌는 장애와 전체 native/provider 시각 경로의 검증은 별도 남은 조건이다.

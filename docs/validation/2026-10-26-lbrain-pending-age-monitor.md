# 장기 미해결 교정의 독립 감시

전체 goal은 진행 중이다. 이 변경은 worker가 정상적으로 재시도해도 오래 풀리지 않은 작업을 monitor에서 감추지 않도록 한다. 상주 supervisor·외부 통지·장기 운영 검증을 완료했다는 뜻은 아니다. B 자동 숨김은 계속 미결정이며 값을 임의로 선택하지 않는다.

## 오류와 처리

기존 감시는 worker heartbeat 중단, 다음 실행 시각의 초과와 lease 만료를 검사했다. 정상 worker가 충돌을 재확인하고 다음 실행을 예약하면, 하루가 지나도 `healthy`로 표시할 수 있었다. 실제 PG에서 미해결 등록 시각을 25시간 전으로 둔 검사로 **1 failed in 0.45s**를 재현했다. source bundle은 `aa8d5358a2104e293f70db0494bd3ac4629d9543193e751be004d158893f2930`다. 이 재현은 긴 대기 시간을 DB fixture로 구성한 검사이며 25시간 연속 운영 증거가 아니다.

이제 최초 등록 시각부터 현재 DB 시각까지를 별도로 확인한다. `created_at`을 재시도·heartbeat·worker 재시작으로 초기화하지 않는다. `reviews.aged`는 임계값을 넘긴 미해결 작업 수, `oldest_unfinished_seconds`는 가장 오래된 작업의 경과 시간이다. 기본 임계값은 86400초이며 lifecycle monitor의 `--pending-age-seconds`로 0.1~86400초 사이를 명시할 수 있다. 작업이 정상 완료되면 미해결 집계에서 제외한다. 경고만 추가하며 source·현재 값·판단 이력을 변경하지 않는다.

worker가 살아 있어도 `aged>0`이면 `attention_required=true`다. worker 장애를 뜻하는 `stalled`와 구분한다. 다른 project의 작업은 집계에 섞지 않는다. 잘못된 임계값과 저장소 장애를 정상으로 응답하지 않는다. 이 출력의 외부 통지 전달·수신 확인은 별도 남은 작업이다.

## 검증

수정 bundle `2a0ff21159369c1b2d24d72d422480e4dec0de5fa21c14efddd30ca8efd41545`에서 실제 Ubuntu PG의 scheduler/competition/lifecycle/verification **65 passed, 1 warning in 79.45s**를 확인했다. 이는 앞선 과거 요청 완료 검사의 65건과 다른 모듈 묶음이다.

재시도와 새 queue 인스턴스 이후의 경고 유지, project 격리, 완료 후 경고 해제, 잘못된 임계값을 검사했다. 실제 Git·PG·별도 CLI worker의 두 원천 충돌 동안 독립 monitor subprocess를 실행해 정상 heartbeat와 장기 미해결 경고를 동시에 확인했다. 이 CLI 검사는 짧은 임계값을 실제로 기다렸으며 DB 시각이나 due 시각을 바꾸지 않았다. 이후 실제 파일 합의로 모든 요청이 완료되면 `aged=0`이 된다. 해당 CLI 경로는 직접 JSON adapter이며 새 모델 실호출로 계산하지 않는다.

`cd worker && uv run pytest -q`: **5305 passed, 728 skipped, 4 warnings in 55.91s**다. 선택형 skip을 실제 PG 통과로 세지 않는다. 새 scheduler 검사를 실제 PG CI 공통 교정 step에 추가했다. 모델 선택 입력·질문·coordinator identity는 이전 실호출 source와 같다. 기존 v3 평가와 실제 모델 연동 결과는 [독립 source 검사](2026-10-26-lbrain-competing-source-revalidation.md)에 유지한다.

운영 서비스·운영 DB·사용자 기억·외부 알림 설정은 변경하지 않았다. 경고 기준 변경은 fact authority와 이력의 rollback을 요구하지 않는다. 감시와 worker가 동시에 중단되는 운영 장애의 탐지·복구까지 입증한 것은 아니다.

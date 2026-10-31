# 현재 사실 reader의 근거 유효성 차단

파생 기억 재검토를 설계하면서 `PgFactReader`를 확인했다. 이 reader는 실제 PostgreSQL의 fact head를 읽지만 기존에는 head가 `disputed`인지, 마지막 근거가 철회됐는지를 검사하지 않았다. 실제 PostgreSQL의 합성 검사에서 두 경우 모두 이전 `ready`를 `canonical_state` 관찰로 반환했다. 수정 전 두 검사는 모두 기대한 차단이 발생하지 않아 실패했다.

reader는 이제 해당 project·범위·필드와 활성 현재 카드뿐 아니라 fact 상태가 `current`이고 head assertion이 존재하며, 현재 값에 대한 유효한 source receipt가 적어도 하나 남아 있는지도 같은 SQL 조회에서 확인한다. 철회 처리를 위한 별도 worker가 실행되기 전이라도 마지막 source가 철회됐다면 차단한다. 서로 다른 독립 지지 중 하나만 철회되고 유효한 다른 지지가 남은 경우는 유지한다. 모델 판단 도중 마지막 지지가 철회되면 후속 현재성 검사에서 결과를 버린다.

이 차단은 불확실한 값을 검증된 `canonical_state` 근거로 발급하지 않는 규칙이다. 상충 사실을 공개 조회에서 숨길지에 관한 미결정 B 정책을 정하는 것이 아니다. 확인 기한이 지난 사실의 전반적인 읽기 정책이나 모든 외부 원천의 검증도 이 변경만으로 완결되지 않는다.

실제 PostgreSQL·Neo4j의 capability·철회·공통 resolution·기한·graph 묶음은 **73 passed, 1 skipped in 99.68s**다. skip은 별도 graph 종료·복구 제어 검사다. 새 코드와 질문·모델에 결속된 값 선택 독립 v10은 정상 **19/20**, 잘못된 허용 **0건**, 의미 거부 13건, 조건문 code 차단 3건이다. 정상 1건은 보류됐다. plan hash는 `sha256:c3a28b19141810c63c55808b7c3d7b7e1ae7229b51e2ace8ed8915b66b5fa637`, artifact hash는 `sha256:1778f31f4124f7104556f057ea71559bca987fd3f1e2e814fb7fb8f37148492b`, reported cost는 0.0003152 USD다. 실제 resolved model은 `perplexity/pplx-decider-flash-v1.2-20261016`이며 threshold 0.8과 fallback 없음은 유지했다.

기존 값 선택 v9 profile은 reader 코드 identity가 달라 이 코드에서 사용할 수 없다. 대상 판단은 별도 client·질문과 독립 v4 profile을 유지한다. 두 profile을 적용한 Ubuntu 실제 모델·PG·Couch·Neo4j 및 capability 묶음은 **27 passed, 1 skipped in 119.61s**이며 skip은 graph 종료·복구 제어가 필요한 검사다. 독립 평가 bundle과 `lib/`가 동일한 최종 bundle hash는 `sha256:3c1712532946e400c623d67df6b832e1fb67d20bb6425db52ffa43672d670476`이다. 로컬 전체 worker는 **5392 passed, 824 skipped in 57.47s**, Java `gradle test`는 앞선 실행에서 BUILD SUCCESSFUL과 test UP-TO-DATE였다. 작업 전용 OpenRouter 임시 인증 파일은 검사 뒤 삭제하고 부재를 확인했다.

`PgFactReader`를 안전하게 읽는 것만으로 파생 관계의 자동 재검토가 구현되는 것은 아니다. 별도 합성 PG 진단에서 명시적인 `derived_from` edge가 있는 파생 카드도 원래 사실이 바뀐 뒤 그대로 현재 조회에 남고 재검토가 예약되지 않았다. 이 진단은 결함의 재현이며 통과 증거가 아니다. [파생 사실 재검토 계약](../specs/2026-07-05-lbrain-knowledge-object-substrate/derived-fact-revalidation.md)에 남은 요구를 기록한다.

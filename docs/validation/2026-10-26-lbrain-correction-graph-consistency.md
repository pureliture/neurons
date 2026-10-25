# 공통 사실 교정과 실제 graph의 지연·장애·재생 검사

전체 goal은 계속 진행 중이다. [공통 resolution 부분 검증](2026-10-26-lbrain-common-fact-resolution.md)에 실제 Graphiti/Neo4j 검사를 추가했다. 외부 embedding provider와 의미 판단 모델의 검증을 대신하지 않는다.

## 실제로 발견하고 수정한 누락

Git 근거를 독립 검증해 PG의 현재 값을 교정한 뒤, graph에는 옛 episode만 있고 successor embedding은 `pending`인 구간을 만들었다. 실제 Graphiti/Neo4j 조회와 기존 fallback에서는 최신 값이 빈 결과로 누락됐다. `test_current_fact_remains_queryable_before_new_graph_or_embedding_projection`의 수정 전 실패 기록을 보존했다.

공통 fact head에 연결된 관련 현재 카드만 PG의 lexical index로 보완 회수한다. 전체 프로젝트 카드를 Python에 읽어 후보로 삼지 않는다. 서버의 project·권한·currentness 검증과 원본 hash 재검증을 유지한다. query tokenizer는 기존 공용 tokenizer를 사용한다. source·판단·교정 authority를 검색 유사도로 바꾸지 않는다.

새 embedding이 없어도 관련된 현재 fact는 조회할 수 있다. 응답은 `pg_fact_fallback`, `fact_fallback_used`, 실제 graph 상태 및 embedding 검색 불가를 구분한다. graph 반영 뒤에는 실제 graph 조회가 같은 현재 값으로 수렴한다. 정확한 문자열뿐 아니라 조사·대화형 표현이 있는 한국어 질의도 검사했다. 임의 자연어의 의미 검색을 완전히 해결했다는 의미는 아니다.

## 만료된 graph 작업의 완료 차단

기존 graph outbox 완료/실패 저장 조건에는 lease 만료 확인이 없었다. `lease_generation`을 schema에 가산하고 claim마다 증가시킨다. 완료/실패 저장은 worker ID·generation·처리 상태·현재 시각의 유효 lease가 모두 일치해야 한다. 같은 worker ID가 재시작하거나 다시 claim해도 이전 실행의 완료 기록을 받아들이지 않는다.

실제 PG와 Neo4j에서 lease를 실제 시간으로 만료시킨 뒤, 같은 worker ID의 새 claim과 옛 generation의 완료/실패를 검사했다. 최신 card projection을 먼저, 과거 card의 demotion을 나중에 보내고, 실제 projection을 중복 전달한 뒤 가장 오래된 current episode도 다시 재생했다. PG의 현재 head만 조회되는 것을 확인했다. graph는 근거/검색용 파생 상태이며 오래된 graph 본문이 PG 현재 사실을 덮어쓰지 않는다.

## 실제 Neo4j 종료와 복구

작업 전용 Neo4j만 외부 검증기가 종료했다. 테스트 컨테이너에는 Docker socket이나 운영 제어 권한을 주지 않았다. graph 장애 중 실제 Git source를 새 commit으로 바꾸고 교정 도구의 공통 작업자로 PG의 현재 값을 갱신했다. 실제 graph projection 실패가 retryable outbox로 남고, embedding 없이도 PG의 새 현재 값을 반환했다.

같은 Neo4j container/volume을 재시작하고 Cypher readiness를 확인했다. 시스템 시각이나 작업 deadline을 조작하지 않고 실제 retry backoff 이후 projection을 처리했다. 모든 해당 작업이 완료되고 실제 graph 경로가 같은 현재 값으로 수렴했다. 종료·재시작 전 task label을 확인했고 실패 시 재시작하는 복구 동작을 포함했다. 운영 graph는 사용하지 않았다.

## 증거

- 작업 전용 Ubuntu PostgreSQL·Neo4j와 실제 Graphiti driver/adapter를 사용했다. Neo4j는 저장소 compose와 같은 `neo4j:5.26-community`이며 image/repository digest는 `sha256:adaa077a3ab023015143b8f73f7076a7d053f12985f5b06f4c6a128c3a2f7240`이다.
- 실제 PG/Git/Graphiti/Neo4j/공통 교정/HTTP CLI/worker와 기존 outbox 회귀를 함께 실행: **125 passed, 1 skipped, 1 warning in 47.36s**. skip은 외부 종료 제어가 필요한 검사 하나이며 별도로 실행했다.
- 실제 Neo4j 종료·복구 제어 검사: **1 passed, 2 deselected, 1 warning in 52.65s**. 종료와 재시작 후의 readiness를 별도 제어 결과로 확인했다.
- worker 전체 `uv run pytest -q`: **5244 passed, 649 skipped, 4 warnings in 54.52s**. 환경 선택형 skip은 실제 검증으로 세지 않는다.
- source bundle SHA-256: `25afce9611267bc7b5fb10f4d993b24b3e4d490c27c0383ea6cf71403be780f2`.
- `git diff --check`: 통과.

검증용 의존성 image에 source를 읽기 전용 주입한 실행이다. 최종 배포 image identity의 증거로 승격하지 않는다. graph 검색의 embedder/reranker는 명시적 결정적 대역이고 외부 LLM 호출은 금지했다. 실제 embedding provider·의미 판단 성능은 남아 있다.

## 남은 범위와 적용 경계

- session chunk의 관측/사실 provenance와 공통 resolution 연결이 필요하다. 관련 fact로 식별하지 못한 과거 세션 문장이 현재 상태로 노출되지 않는지 별도 검사해야 한다. 모든 세션을 일괄 숨기는 것으로 대체하지 않는다.
- 자연어 의미 검색, 모델의 정상 적용률/오교정 평가, 모든 writer와 native intake, 철회·권한·parser 변경·파생물·retention/GC 경합은 남아 있다.
- 이번 검사는 bounded 실제 장애/복구다. 장기 운영의 감시·회복, 백업 재구축과 최종 보존/정리까지 완료했다는 의미는 아니다.
- 신규 generation schema를 적용할 때 이전 graph worker를 먼저 중지·drain하고 새 consumer로 전환해야 한다. 이전 consumer의 SQL에는 generation fence가 없으므로 혼합 실행을 허용하지 않는다. rollback 시에도 이전 worker가 해당 queue를 소비하게 두지 않는다.
- 공통 fact를 해석하지 못하는 이전 reader/writer로 단순 downgrade하지 않는다. 관리 대상 쓰기와 읽기를 차단하거나 호환되는 검증 버전을 유지하면서 기록은 보존한다. 운영 활성화는 아직 수행하지 않았다.

# LBrain 승인 카드 PG 단일 저장소 전환 — PR #271 수정 계약

상태: 구현 중. 운영 전환이나 운영 데이터 변경을 승인하는 문서가 아니다.
상위 계약: `requirements.md` R1.1/R2.4/R3.5, `design.md` §2.1.

## 목표와 경계

- 카드의 생성(candidate/needs_review), 검토 목록, 승인·거절, 교체·stale, 승인 기록, 현재/과거 조회의 **유일한 정본은 PostgreSQL**이다. PostgreSQL `memory_cards`와 PG의 전용 감사 기록을 사용한다.
- 새 카드 승인·감사 기록·이전 카드의 currentness/validity 변경·embedding/graph outbox는 **하나의 PG 트랜잭션**으로 처리한다. 실패하면 전부 롤백하고 승인 성공을 반환하지 않는다. 요청 재시도는 동일 결정 ID와 카드 상태를 확인하여 동일 결과로 끝나야 한다.
- Neo4j는 파생 조회색인이다. Graphiti가 카드를 찾아도 최종 승인/현재성/해시 검증은 PG가 맡는다. Neo4j 쓰기 실패는 PG 승인을 되돌리지 않지만 outbox를 `completed`로 표시하지 않고, 지연/실패로 관찰한다.
- 기존 SQLite `llm_brain_memory_cards`, `llm_brain_feedback_records`, 카드 review queue·approval 경로는 PG 경로에 **참여하지 않는다**. SQLite의 다른 ledger·세션 기능을 이 변경에서 삭제하지 않는다. 이전 SQLite 카드가 있다면 검증된 별도 이관 단계와 명시적 컷오버가 필요하며 운영 데이터를 임의 삭제하지 않는다.
- 대상은 LLM-brain steward 카드 lane이며, 별도 레거시 `repository.py`/`curation.py`/ArtifactPreference의 `memory_cards` lane과 세션 chunk는 이 PR의 카드 권위 전환에서 제외한다. 단, 이들 경로가 같은 승인 카드의 생성/조회에 섞여 들어오는지 호출 경계 테스트로 증명한다. `autopilot_loop.py`는 동일 steward lane이므로 PG 미주입 시 승인 성공 금지 또는 PG 경로로 명시적으로 배선한다.

## 경로별 요구사항

| 경로 | PG 동작 | 제거할 SQLite 의존 |
|---|---|---|
| 후보 생성/검토 | 권한 disabled, candidate 상태를 PG에 저장하고 PG 목록에서만 검토 | `upsert_llm_brain_memory_card`, `list_llm_brain_review_queue` |
| 사람 승인/자동 승인 | 승인자·결정ID·시각을 PG 감사 기록에 남기고 카드 active/accepted 전환, outbox enqueue | SQLite 카드+feedback 선커밋/후 PG 쓰기 |
| 거절/stale | PG에서 상태 및 감사 기록을 갱신; 현재 조회에서 즉시 제외 | SQLite 단독 상태 변경 |
| 교체 | PG 단일 트랜잭션에서 이전 카드의 currentness를 superseded로 만들고 새 카드 accepted/active 저장 | SQLite 단독 이전 카드 demote |
| 조회 | 목록/Graph-first/PG fallback 모두 PG 승인·현재성·시간 범위 필터 | SQLite 카드가 조회의 숨은 SoT가 되는 경로 |

## 실패와 재시도

1. PG가 없으면 후보/승인/교체가 성공으로 위장하지 않는다. SQLite fallback·자동 이중 쓰기 금지.
2. 같은 결정 ID 재호출은 이미 저장된 기록과 카드 hash를 읽어 동일 결과를 반환한다. 상충하는 결정 ID/서로 다른 카드 hash는 거부한다.
3. 카드 상태와 감사 기록 사이에 부분 커밋 없음. 이전 카드가 없는 교체, 재차 교체, 해시 변경, 동시 승인 경합을 검증한다.
4. outbox 작업 완료는 Graphiti가 `inserted` 또는 같은 episode의 `duplicate`를 확정한 뒤에만 가능하다. `failed`·`skipped_disabled`·연결 실패 시 completed 금지. 연결은 필수로 확인한다.
5. Graphiti 쓰기가 성공했으나 PG ack가 실패하면 중복 재시도 가능하다. 동일 episode ID/해시의 멱등 upsert를 검증한다.
6. 운영 로그/에러에 DSN, 후보 원문, 개인정보, 시크릿을 넣지 않는다.

## 검증 게이트

- 오프라인: 후보·검토·승인·거절·교체·stale·조회 경로별 PG-only 테스트, 오류·중복·동시 요청·재시도·graph skip 테스트.
- 실제 DB: 운영 자격증명이 아닌 격리 PG17+pgvector와 격리 Neo4j에서 승인 → PG readback → outbox claim/ack → graph 저장 → project-scoped MCP list/query. 실패 주입으로 PG 원자성, graph ack/idempotency 검증.
- 외부 CI에서 동일 테스트가 실제 실행되는지 확인하고 PR #271의 설명/커밋/검사 상태를 다시 읽어 병합 판정한다.
- **제외**: 운영 배포, 운영 DB 쓰기/스키마 변경, 사용자 대신 카드 승인, SQLite 파일/운영 이력 삭제, 자동 머지. 각각 별도의 승인 경계다.

## 구현 및 남은 게이트

- PG steward envelope와 결정 감사 기록, 후보 검토 목록, 승인/거절/교체/stale 전이, 일반 조회의 PG 배선을 로컬 작업 트리에 구현했다. 승인 전 후보는 embedding/graph outbox에 보내지 않는다. 기존 `PgVectorStore.upsert_card`의 accepted overwrite 방어는 유지하고, 별도의 잠금 기반 전이로 승인 기록과 outbox를 한 트랜잭션에 묶었다.
- 로컬 임시 PG17+pgvector와 loopback-only Neo4j 5.26.30에서 합성 카드의 승인 → PG·감사 readback → 실제 Graphiti 저장 → Neo4j readback → Graph-first 조회를 실행했다. 외부 LLM 호출은 테스트에서 차단했다. 이 증거는 운영 DB/사용자 카드 검증이 아니다.
- 로컬 전체 worker 테스트: 격리 PG17·Neo4j 설정에서 4,619 passed, 164 skipped, 실패 0. 실제 Graphiti/Neo4j 카드 E2E는 공개 MCP `brain.resolve` list/query, 교차 프로젝트·승인 전·stale 비노출, 그래프 skip 실패 → 재시도 후 저장을 검사한다. 자동 승인 경로는 PG 미주입 시 쓰기 전에 차단된다. 자동 승인 CLI에는 PG 연결 설정이 아직 없어 현재 비정상 종료/무변경만 허용한다. 이는 운영 자동 승인 활성화가 아니라 안전한 차단 상태다.
- PR #271에는 아직 이 로컬 변경을 게시하지 않았다. 원격 PR 본문은 과거 SQLite→PG 이중 쓰기 설명이며 현재 로컬 구현을 반영하지 않는다. 새 CI 서비스 작업의 실제 실행, 독립 재검토, PR 설명과 변경 사항의 일치 및 보호 검사 상태를 확인하기 전에는 병합 준비 완료로 판정하지 않는다.

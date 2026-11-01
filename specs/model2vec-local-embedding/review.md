# Model2Vec 설계 심사 및 승인 기록

## 현재 상태

- 정본 위치: `specs/model2vec-local-embedding/` (`requirements.md`, `design.md`, `milestones.md`, `review.md`).
- 원본 설계 초안 `design-v2.md` SHA-256: `fa9b4574cdc4811305f88eeeb13247608553eb2e15bcfa8c2416b05ceb2640cb`.
- 설계 명세 독립 검토: **조건부 PASS**. 4차 전체 리뷰의 서비스 PASS 및 PG/Graphiti 수정 뒤 5차 범위 한정 재검토 3/3 PASS. 설계 기준 가정과 격리 테스트·사용자 승인·정본 등재 조건이 붙는다.
- 격리 환경 구현 범위: M1–M3 격리 환경 구현 지시 수용. 운영 설치, 실데이터 변경, 운영 배포, 유료 호출 승인은 포함하지 않는다.

## 제1차 독립 심사 결과 (2026-11-01)

| 분야 | 판정 | 필수 보완 | v2 반영 |
|---|---|---|---|
| PostgreSQL/데이터 | REQUEST_CHANGES | 3072/256 분리, 기존 3072 보존, source+input hash, profile별 queue, fencing/CAS, backfill 재조정 | D2,D4 |
| Graphiti/Neo4j | REQUEST_CHANGES | stock 기존 property를 자동 대체하지 않음; 768·3072 혼재, 신규 256 property, 모든 writer/search/dedupe, group/time/authority 보존 | D3,D4,D6 |
| API/운영 | FAIL | 실제 단건·배열 클라이언트, 길이·timeout·큐, 사설 접근·인증, CI 선택과 비차단 구분, Mac 재시작 | D1,D5,D6 |

부모의 직접 재확인: 운영 graph DB 집계 Entity 22016 중 3072=21930, 768=69, null=17; RELATES_TO 47083 중 3072=47006, 768=64, null=13. 벡터 인덱스 0, FULLTEXT ONLINE 4. 모델 파일 해시 로컬 시험 캐시에서 직접 확인하여 v2 D0에 명시. 기존 큐의 profile 미포함과 실제 단건·배열 클라이언트 코드도 직접 확인했다. 768 혼합의 원인과 영향은 미확인이다.

## 제2차 독립 심사 (완료: 3/3 REQUEST_CHANGES)

- 데이터/원본·큐·CAS: REQUEST_CHANGES — 세션 projector가 동기 임베딩·기존 컬럼을 쓰는 문제, 삭제/FK·락 순서·재생성, 구 writer의 동시 실행, 지원되는 과거 범위, 기존 유료 작업 fence가 미확정.
- Graphiti 쓰기·검색·중복판정·기존 graph 보존: REQUEST_CHANGES — stock 함수의 직접 Cypher를 단순 driver 교체로 바꿀 수 없고, 단건/벌크 writer, 락 선행, backfill 중 dedupe coverage가 미확정.
- 서비스/API/네트워크·실행·CI: REQUEST_CHANGES — SDK 기본 base64 요청, 긴 입력의 결정적 chunking, 실제 CI selector·정답셋/보안/운영 연결이 미확정.
- 조치: 설계 v2에 base64, 8192-byte 단일 chunk 처리·weighted 합산, 모델/토크나이저/설정 해시, 기존 session ID 매핑, 삭제/advisory lock/fencing, 구 writer 중지, 과거 검색 coverage, fork injection/graph writer pause 및 lock-before-read, CI 및 품질 fixture 계약을 명시했다.

## 제3차 독립 심사 (완료: 3/3 REQUEST_CHANGES)

- 검토 문서 당시 SHA-256 `4aa6e950cc5fb22ef416c1b81bcead4bcdff22840c4b51df4feac4b13cb93d61`.
- PG: representation receipt v2 고정, legacy identity 충돌, identity/target lock 순서 및 canonical input/dead-letter 계약 추가 필요.
- Graphiti: group concurrent dedupe fencing, graph commit↔PG enqueue crash 사이 durable 재발견·ack 및 전 경로 실패 전파 필요.
- API/운영: 정확한 UTF-8 분할과 hash, input/errors envelope, SDK wire compatibility, CI/평가자·Mac node 신원 경계 명확화 필요.
- 조치: 대상 commit `pg_representation.py` v2 validator가 Gemini/default ID만 허용하고 PG schema에 `source_hash` 열이 없음을 확인하여 v2 문서에 명세했다.

## 제4차 독립 심사 (완료: 서비스 PASS / PG·Graphiti REQUEST_CHANGES)

- 검토 문서 SHA-256 `09cac507ff733209b48d52c95569d27656c360613ce9b5104b63ae1eb87f27ab`.
- 서비스/API: PASS (설계 수준).
- PG: REQUEST_CHANGES — receipt v3 digest 입력에 digest 결과 자체를 넣어 정의한 오류. `provenance_digest`를 입력에서 제외하고 명시적인 source field 목록만 직렬화하도록 수정.
- Graphiti: REQUEST_CHANGES — 삭제 뒤 element의 dirty property가 사라지는 문제, 다중 transaction projection 작업의 전체 완료 및 중단 복구 경계 누락. 삭제와 같은 graph tx에 독립 tombstone 생성, source outbox+PG operation journal과 Neo4j step receipt를 사용하고 중간 crash/재시작·낡은 epoch 처리 규칙을 명시.

## 제5차 범위 한정 재검토 (완료: 3/3 PASS)

- 수정 설계 `design-v2.md` SHA-256 `fa9b4574cdc4811305f88eeeb13247608553eb2e15bcfa8c2416b05ceb2640cb`.
- PG: PASS. receipt v3 digest 결과를 입력에서 제외해 자기참조 오류를 제거했고 이전 validator/구 receipt 보존과 모순이 없다는 범위 한정 검토 통과.
- Graphiti: PASS. 삭제별 tombstone, 다단계 PG operation journal 및 Neo4j step receipt 제안이 직전 두 결함을 명세상 해결했다는 범위 한정 검토 통과.
- 서비스/API: PASS. 직전 서비스 설계 PASS 이후 PG/graph 수정으로 인한 명백한 API 모순이 없음을 확인.

## 금지 범위

별도 승인 없는 운영 리소스/네트워크/DB/Neo4j 변경, 유료 API 호출, 모델 제공자 키 변경, 코드 push/merge/배포는 금지한다.

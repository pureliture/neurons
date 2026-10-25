# LBrain 교정 전체 목표의 달성 추적

목표는 합의한 교정 설계 전체의 구현과 Ubuntu 실제 runtime 검증이다. 부분 구현·합성 단위 검사·개별 canary는 해당 범위의 증거이며 전체 완료를 대신하지 않는다. [합의 결정과 36개 시나리오](../../validation/2026-10-25-lbrain-correction-reconciliation-audit.md)를 유지한다.

## 공통 완료 조건

| ID | 필수 결과 | 현재 증거와 남은 범위 |
| --- | --- | --- |
| G01 | 복수 사실별 변경안·근거 제출, exact target 또는 설명으로 기존 target 검색 | v2 batch·기존 값/불일치/제안/근거와 정확한 title/summary 검색 구현. 직접 검증 값과 제안 값이 달라도 결과를 구분한다. 일반 의미 검색은 남음 |
| G02 | 코드 검증과 실제 판단 모델 연결, 별도 평가에서 오교정과 정상 교정 모두 검사 | typed Decisions 연결 구현. 독립 명제 질문의 조정 12건·독립 확인 16건을 실행. 독립 세부 분류 13/16, 후보 정책 정상 허용 1/4·잘못된 허용 0/12로 적용 기준 미통과. 실제 교정 작업자 연결 필요 |
| G03 | 같은 관측의 교정·수집·재수집이 합류 | 공통 fact/관측/해석/판단 코어와 Git JSON 교정·수집·관리 대상 autopilot 합류 구현. native provider의 일반 admission 연결은 남음 |
| G04 | 과거 지연 입력의 역전 방지와 실제 후속 변경의 반영 | 실제 Git+PG에서 지연된 ancestor의 history 연결, 코어에서 새로운 현재 관측의 rollback 확인. 일반 세션 전체 경로는 남음 |
| G05 | 동일 사실의 단일 current, 복수 필드·중복 카드·동시 writer 처리 | 공통 head·다른 필드의 lineage 재검증·연결된 중복 카드의 현재 표시·동시 key 처리 구현. 연결된 authority의 기존 steward 우회 거부. 아직 식별하지 않은 신규 중복과 모든 일반 writer는 남음 |
| G06 | 근거 내용·버전·관측 시각과 모든 교정 이력 보존 | Git redacted source receipt·관측·assertion·resolution/event와 canonical 이력 보존. retention 참조 보호와 GC 경합 검사 필요 |
| G07 | 예약·장애·설정 차단·저장 직후 종료·만료 후 복구와 독립 감지 | 공통 coordinator를 내구성 queue/worker에 연결. source 복구와 commit 참조 복구 검사. 전체 운영 자동 실행·독립 감시와 의미 경로 증거는 남음 |
| G08 | context/list/query와 graph·embedding 지연·역순 작업에서도 올바른 조회 | 최종 fact view·generation·cursor와 실제 query를 연결. 실제 Neo4j에서 projection 전 누락을 수정하고 종료/복구·역순/중복·만료 generation을 확인. 실제 embedding provider·미식별 session chunk·장기 수렴은 남음 |
| G09 | 철회·parser 정정·권한 변경과 파생 기억 영향 처리 | 영향 관계와 재검토 연결 필요 |
| G10 | 실제 작업 근거로 전체 흐름 검증, artifact identity·백업 복원·정리 | 기존 좁은 실증에 Git+PG+실제 CLI/HTTP 검사를 추가. 최종 배포 identity·LLM 실제 전체 작업·보존/정리는 남음 |

## 시나리오별 완료 추적

상태 `미검증`은 해당 시나리오 전체를 실제 연결 경로에서 입증하지 못했다는 뜻이다. 이미 있는 단위 검사나 부분 실증을 삭제하거나 무효로 취급하지 않는다. 통과로 변경하려면 구현 경로·검사 이름·해당 source/image의 실제 receipt를 함께 명시해야 한다.

| 사례 | 범위 | 전체 연결 검증 |
| --- | --- | --- |
| C01 | 교정 뒤 같은 관측 수집 | Git 데이터와 관리 대상 autopilot 합류 부분 확인. 일반 native 세션 연결은 남음 |
| C02 | 수집 뒤 같은 관측 교정 | Git 동일 관측에서 두 번째 authority 변경 없이 근거 연결 확인. 일반 경로는 남음 |
| C03 | 같은 관측의 다른 miner 해석 | PG 코어에서 해석 version별 보존·현행 해석 검증 확인. 실제 miner 변경 연결은 남음 |
| C04 | provider/import/chunk가 다른 재수집 | 미검증 |
| C05 | 같은 값을 지지하는 독립 관측 | PG 코어에서 복수 관측을 보존하고 중복 authority를 만들지 않음. 일반 source 연결은 남음 |
| C06 | 최근 수집된 옛 snapshot | 실제 Git ancestor를 지연 수집해 history로 연결하고 현재 값 유지. 일반 세션 연결은 남음 |
| C07 | 한 세션의 여러 실제 상태 변화 | 미검증 |
| C08 | 실제 후속 제거·rollback | 새 현재 관측의 rollback을 PG 코어에서 확인. 실제 후속 source/운영 흐름은 남음 |
| C09 | 과거 원인을 몰라도 검증된 현재 값 교정 | 직접 검증 Git 데이터로 현재 값 교정 확인. 의미 근거 경로는 남음 |
| C10 | clock skew·관측 시각 누락 | 미검증 |
| C11 | 같은 revision의 상충 동시 교정 | 미검증 |
| C12 | 다른 key의 동일 사실·동일 값 | 실제 PG 동시 key에서 단일 successor 확인. 일반 admission 경로는 남음 |
| C13 | 같은 카드의 다른 필드 동시 교정 | 같은 원래 revision의 두 필드를 순차 교정해 보존 확인. 다른 필드의 실제 동시 실행은 남음 |
| C14 | 다른 카드 ID에 중복된 사실 | 연결된 두 카드의 현재 값 표시와 다른 문장 보존 확인. 미식별 중복·query/graph 경로는 남음 |
| C15 | 유사 문장의 다른 환경·branch | 미검증 |
| C16 | 여러 사실 중 한 사실만 교정 | 정확한 표시 구간 patch와 v2 batch 구현. 다른 사실 보존·모호한 구간 거부 확인. 의미 기반 span 결정은 남음 |
| C17 | summary·typed payload·표시문 일관성 | 초기 adapter에서 수정. summary와 typed payload의 동시 갱신 및 전체 freshness 비승격을 실제 PG에서 확인. 일반 복수 사실 경로는 미검증 |
| C18 | 오래된 cursor·target lineage | fact generation에 cursor 결속, 같은 값의 stale target 합류와 변경하지 않은 필드의 lineage 확인. 일반 의미 target의 전체 연결은 남음 |
| C19 | 서버 수집 전의 근거 제출 | 미검증 |
| C20 | 오래되거나 불완전한 근거 | 초기 source adapter에서 부분 확인. 일반 경로 미검증 |
| C21 | 우열을 판단할 수 없는 상충 근거 | B 정책 미결정. 증거 확보 전 임의 선택하지 않음 |
| C22 | 자기 재인용을 독립 근거로 오인 | 미검증 |
| C23 | 테스트·구현을 운영 배포로 과대 해석 | 최초 모델 표본 기록. 실제 교정 gate 미검증 |
| C24 | evidence 안의 명령·위조·권한 위반 | 초기 hash·정책 검사 있음. 일반 의미 경로 미검증 |
| C25 | source 철회·삭제·권한 회수 | 교정 시 거부 확인. 기존 교정 영향 재평가 미검증 |
| C26 | parser·miner·redaction·정책 변경 | 미검증 |
| C27 | evidence 저장 뒤 authority 실패 | 초기 adapter와 공통 PG 코어의 canonical/resolution 동시 rollback 확인. 일반 source 보존/GC 경합은 남음 |
| C28 | authority 저장 뒤 응답·완료 기록 유실 | 공통 coordinator에서도 commit receipt를 같은 transaction에 기록하고 source 소실 뒤 복구 확인. 전체 의미 경로는 남음 |
| C29 | PG 반영 뒤 graph·embedding 장애 | 실제 Neo4j 지연/종료와 embedding 미반영 중 현재 fact 조회 확인. 실제 embedding provider 장애와 일반 세션 경로는 남음 |
| C30 | graph 작업 역순·중복·lease 만료 | 실제 PG/Neo4j에서 역순·중복·옛 current 재생과 만료/동일 worker 재claim generation 거부 확인. 일반 재구축/다른 source는 남음 |
| C31 | projection 장기 실패와 복구 | 실제 Neo4j 종료·실패 예약·재시작·backoff 후 수렴 확인. 장기 운영/감시·복원 검사는 남음 |
| C32 | A→B 후 A의 오래된 재시도 | PG 코어에서 A의 불변 결과와 이후 현재 B를 구분하고 재변경하지 않음. 범용 의미 경로는 남음 |
| C33 | 복구·재구축·backfill 재생 | 미검증 |
| C34 | 교정 전 사실에 의존한 파생물 | 미검증 |
| C35 | 외부 cache·기존 prompt와 새 조회의 경계 | cache 무효화, generation/cursor, 같은 fact의 옛 표현 질의를 현재 head로 연결하는 실제 PG/Neo4j 검사 확인. 미식별 세션·외부 cache·기존 prompt 자체는 남음 |
| C36 | 대상 부재·모호함·검색 실패·잘림 | exact target과 정확한 title/summary 검색에서 부재·복수 거부 구현. 일반 의미 검색과 잘림 검증은 남음 |

## 완료 판정 규칙

각 요구는 source 검사·단위 검사·실제 PG·실제 graph·실제 모델·정규 수집 경로를 구분한다. 필요한 범위보다 약한 증거는 미완료로 남긴다. G01~G10과 C01~C36의 필수 결과, 미결정 정책, 실제 실행·보존·정리를 모두 확인하기 전 goal을 완료 처리하지 않는다. production 활성화 권한이나 임의 신규 기억 생성으로 범위를 확대하지 않는다.

공통 코어와 Git 데이터 경로의 추가 부분 증거는 [2026-10-26 검증 기록](../../validation/2026-10-26-lbrain-common-fact-resolution.md)을 따른다. C01~C36의 전체 연결 완료 상태는 이 부분 검증으로 자동 승격하지 않는다.

query 지연 누락 수정과 실제 graph의 추가 증거는 [graph 정합성 검사](../../validation/2026-10-26-lbrain-correction-graph-consistency.md)를 따른다.

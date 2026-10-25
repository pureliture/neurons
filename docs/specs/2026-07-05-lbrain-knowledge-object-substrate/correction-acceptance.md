# LBrain 교정 전체 목표의 달성 추적

목표는 합의한 교정 설계 전체의 구현과 Ubuntu 실제 runtime 검증이다. 부분 구현·합성 단위 검사·개별 canary는 해당 범위의 증거이며 전체 완료를 대신하지 않는다. [합의 결정과 36개 시나리오](../../validation/2026-10-25-lbrain-correction-reconciliation-audit.md)를 유지한다.

## 공통 완료 조건

| ID | 필수 결과 | 현재 증거와 남은 범위 |
| --- | --- | --- |
| G01 | 복수 사실별 변경안·근거 제출, exact target 또는 설명으로 기존 target 검색 | 초기 exact target API만 구현. batch·설명 검색·요청값 비교 미구현 |
| G02 | 코드 검증과 실제 판단 모델 연결, 별도 평가에서 오교정과 정상 교정 모두 검사 | typed Decisions 연결 구현. 독립 명제 질문의 조정 12건·독립 확인 16건을 실행. 독립 세부 분류 13/16, 후보 정책 정상 허용 1/4·잘못된 허용 0/12로 적용 기준 미통과. 실제 교정 작업자 연결 필요 |
| G03 | 같은 관측의 교정·수집·재수집이 합류 | native Codex 관측 anchor와 해석별 evidence version을 분리해 ledger에 보존. 공통 사실 resolution·정규 교정 연결은 미구현 |
| G04 | 과거 지연 입력의 역전 방지와 실제 후속 변경의 반영 | immutable Git blob·현재 branch/head·실제 commit ancestry 검증 adapter 구현 및 실제 로컬 Git 검사. 공통 resolution·Ubuntu 전체 경로 연결 필요 |
| G05 | 동일 사실의 단일 current, 복수 필드·중복 카드·동시 writer 처리 | 카드별 PG authority 원자성은 구현. 사실 범위 공통 resolution·일반 승인 우회 방지 필요 |
| G06 | 근거 내용·버전·관측 시각과 모든 교정 이력 보존 | 기존 immutable source store 재사용 가능. correction 참조 보호와 GC 경합 검사 필요 |
| G07 | 예약·장애·설정 차단·저장 직후 종료·만료 후 복구와 독립 감지 | 구조화된 별도 프로세스 실증 통과. 범용 교정 작업자·정규 intake에 연결 필요 |
| G08 | context/list/query와 graph·embedding 지연·역순 작업에서도 올바른 조회 | 초기 context/list 확인. 일반 query·최소 commit revision·projection 수렴 필요 |
| G09 | 철회·parser 정정·권한 변경과 파생 기억 영향 처리 | 영향 관계와 재검토 연결 필요 |
| G10 | 실제 작업 근거로 전체 흐름 검증, artifact identity·백업 복원·정리 | 초기 canary와 좁은 재검토 실증만 통과. 정규 전체 흐름 증거 필요 |

## 시나리오별 완료 추적

상태 `미검증`은 해당 시나리오 전체를 실제 연결 경로에서 입증하지 못했다는 뜻이다. 이미 있는 단위 검사나 부분 실증을 삭제하거나 무효로 취급하지 않는다. 통과로 변경하려면 구현 경로·검사 이름·해당 source/image의 실제 receipt를 함께 명시해야 한다.

| 사례 | 범위 | 전체 연결 검증 |
| --- | --- | --- |
| C01 | 교정 뒤 같은 관측 수집 | 미검증 |
| C02 | 수집 뒤 같은 관측 교정 | 미검증 |
| C03 | 같은 관측의 다른 miner 해석 | 미검증 |
| C04 | provider/import/chunk가 다른 재수집 | 미검증 |
| C05 | 같은 값을 지지하는 독립 관측 | 미검증 |
| C06 | 최근 수집된 옛 snapshot | 미검증 |
| C07 | 한 세션의 여러 실제 상태 변화 | 미검증 |
| C08 | 실제 후속 제거·rollback | 미검증 |
| C09 | 과거 원인을 몰라도 검증된 현재 값 교정 | 미검증 |
| C10 | clock skew·관측 시각 누락 | 미검증 |
| C11 | 같은 revision의 상충 동시 교정 | 미검증 |
| C12 | 다른 key의 동일 사실·동일 값 | 미검증 |
| C13 | 같은 카드의 다른 필드 동시 교정 | 미검증 |
| C14 | 다른 카드 ID에 중복된 사실 | 미검증 |
| C15 | 유사 문장의 다른 환경·branch | 미검증 |
| C16 | 여러 사실 중 한 사실만 교정 | 초기 adapter의 정확한 표시 구간 patch 구현. 다른 사실 보존 및 모호한 표시 구간 거부 검사. 일반 batch·의미 기반 span 결정은 미구현 |
| C17 | summary·typed payload·표시문 일관성 | 초기 adapter에서 수정. summary와 typed payload의 동시 갱신 및 전체 freshness 비승격을 실제 PG에서 확인. 일반 복수 사실 경로는 미검증 |
| C18 | 오래된 cursor·target lineage | 미검증 |
| C19 | 서버 수집 전의 근거 제출 | 미검증 |
| C20 | 오래되거나 불완전한 근거 | 초기 source adapter에서 부분 확인. 일반 경로 미검증 |
| C21 | 우열을 판단할 수 없는 상충 근거 | B 정책 미결정. 증거 확보 전 임의 선택하지 않음 |
| C22 | 자기 재인용을 독립 근거로 오인 | 미검증 |
| C23 | 테스트·구현을 운영 배포로 과대 해석 | 최초 모델 표본 기록. 실제 교정 gate 미검증 |
| C24 | evidence 안의 명령·위조·권한 위반 | 초기 hash·정책 검사 있음. 일반 의미 경로 미검증 |
| C25 | source 철회·삭제·권한 회수 | 교정 시 거부 확인. 기존 교정 영향 재평가 미검증 |
| C26 | parser·miner·redaction·정책 변경 | 미검증 |
| C27 | evidence 저장 뒤 authority 실패 | 초기 adapter rollback 확인. 일반 경로 미검증 |
| C28 | authority 저장 뒤 응답·완료 기록 유실 | 초기 adapter와 재검토 결정 참조 복구 실증. 일반 경로 연결 필요 |
| C29 | PG 반영 뒤 graph·embedding 장애 | 미검증 |
| C30 | graph 작업 역순·중복·lease 만료 | 미검증 |
| C31 | projection 장기 실패와 복구 | 미검증 |
| C32 | A→B 후 A의 오래된 재시도 | 미검증 |
| C33 | 복구·재구축·backfill 재생 | 미검증 |
| C34 | 교정 전 사실에 의존한 파생물 | 미검증 |
| C35 | 외부 cache·기존 prompt와 새 조회의 경계 | 초기 service cache 무효화 확인. 전체 consumer 미검증 |
| C36 | 대상 부재·모호함·검색 실패·잘림 | exact target 부재만 확인. 설명 검색 미구현 |

## 완료 판정 규칙

각 요구는 source 검사·단위 검사·실제 PG·실제 graph·실제 모델·정규 수집 경로를 구분한다. 필요한 범위보다 약한 증거는 미완료로 남긴다. G01~G10과 C01~C36의 필수 결과, 미결정 정책, 실제 실행·보존·정리를 모두 확인하기 전 goal을 완료 처리하지 않는다. production 활성화 권한이나 임의 신규 기억 생성으로 범위를 확대하지 않는다.

# LBrain 교정 전체 목표의 달성 추적

목표는 합의한 교정 설계 전체의 구현과 Ubuntu 실제 runtime 검증이다. 부분 구현·합성 단위 검사·개별 canary는 해당 범위의 증거이며 전체 완료를 대신하지 않는다. [합의 결정과 36개 시나리오](../../validation/2026-10-25-lbrain-correction-reconciliation-audit.md)를 유지한다.

## 공통 완료 조건

| ID | 필수 결과 | 현재 증거와 남은 범위 |
| --- | --- | --- |
| G01 | 복수 사실별 변경안·근거 제출, exact target 또는 설명으로 기존 target 검색 | v2 batch·기존 값/불일치/제안/근거와 정확한 title/summary 검색 구현. 직접 검증 값과 제안 값이 달라도 결과를 구분한다. 일반 의미 검색은 남음 |
| G02 | 코드 검증과 실제 판단 모델 연결, 별도 평가에서 오교정과 정상 교정 모두 검사 | Git 값 선택을 실제 worker/MCP HTTP에 연결. 관찰 참조 모듈을 포함한 새 코드 identity의 독립 v5 40건에서 정상 20/20·잘못된 허용 0/20, threshold 0.8 유지. redaction으로 중단된 v4는 제외하고 앞선 원시 모델 오허용·v3 정상 보류도 유지. 실제 모델의 충돌·합의 자동 처리 확인. 일반 native·운영 근거의 의미 admission은 남음 |
| G03 | 같은 관측의 교정·수집·재수집이 합류 | 공통 fact 코어·Git·관리 대상 autopilot과 native source→intake 연결에 더해, 서버 발급 관찰 참조를 수집 전 교정과 지연 native 세션이 같은 관찰/assertion에 합류하는 경로를 검증. 5개 provider 형식·실제 Couch/PG/HTTP/CLI에서 추가 authority 없는 합류 확인. 임의 native 응답의 일반 의미 admission은 남음 |
| G04 | 과거 지연 입력의 역전 방지와 실제 후속 변경의 반영 | 실제 Git+PG에서 지연 ancestor와 실제 ref A→B→A를 구분하고 불변 관측의 현재 재검증 확인. 일반 세션 전체 경로는 남음 |
| G05 | 동일 사실의 단일 current, 복수 필드·중복 카드·동시 writer 처리 | 공통 head·필드 lineage·알려진 alias의 현재 표시·동시 key 처리 구현. 무관한 Git commit으로 상대 근거를 제거해 잘못 승인하던 문제를 실제 재현/수정하고 두 source의 자동 합의를 확인. 알려진 대상의 직접 DML·중복 current·최초 queue 대기·옛 snapshot 우회 차단. 미식별 의미 중복과 모든 writer 자동 합류는 남음 |
| G06 | 근거 내용·버전·관측 시각과 모든 교정 이력 보존 | receipt/관측 불변성과 필수 FK, 참조 추가와 GC의 실제 잠금 경합 확인. 나중 결정에 함께 사용한 다른 source의 원래 보류 근거도 FK로 보존. 모든 source·모델 산출물·retention/GC CLI의 전체 보존 검증은 남음 |
| G07 | 예약·장애·설정 차단·저장 직후 종료·만료 후 복구와 독립 감지 | 실제 모델의 응답 유실·철회·기한 경과→CLI worker 종료→독립 감지→재시작 후 자동 해결 확인. 서버 검증 영수증·기한 예약·예약 누락 감지와, 살아 있는 worker의 재시도에도 장기 미해결 경고 유지·완료 후 해제를 실제 PG/CLI에서 확인. 상주 supervisor·외부 통지·장기 운영 자동 실행/감시는 남음 |
| G08 | context/list/query와 graph·embedding 지연·역순 작업에서도 올바른 조회 | 최종 fact view·generation·cursor와 실제 query를 연결. 실제 Neo4j에서 projection 전 누락을 수정하고 종료/복구·역순/중복·만료 generation을 확인. 실제 embedding provider·미식별 session chunk·장기 수렴은 남음 |
| G09 | 철회·parser 정정·권한 변경과 파생 기억 영향 처리 | 사용한 receipt의 철회→내구성 영향 target→자동 재검토, 즉시 조회 제외와 독립 유효 근거 유지 구현. parser·정책·접근 권한 자동 전파와 파생 기억 재계산은 남음 |
| G10 | 실제 작업 근거로 전체 흐름 검증, artifact identity·백업 복원·정리 | 기존 좁은 실증에 Git+PG+실제 CLI/HTTP 검사를 추가. 최종 배포 identity·LLM 실제 전체 작업·보존/정리는 남음 |

## 시나리오별 완료 추적

상태 `미검증`은 해당 시나리오 전체를 실제 연결 경로에서 입증하지 못했다는 뜻이다. 이미 있는 단위 검사나 부분 실증을 삭제하거나 무효로 취급하지 않는다. 통과로 변경하려면 구현 경로·검사 이름·해당 source/image의 실제 receipt를 함께 명시해야 한다.

| 사례 | 범위 | 전체 연결 검증 |
| --- | --- | --- |
| C01 | 교정 뒤 같은 관측 수집 | 서버의 독립 Git 관찰 참조로 교정한 뒤 5개 provider 형식의 지연 native 기록을 같은 관찰/assertion과 원래 resolution에 연결. 추가 authority 0건 확인. 일반 native 세계 상태 admission은 남음 |
| C02 | 수집 뒤 같은 관측 교정 | 서버 관찰 참조의 native 기록이 먼저 도착하면 기존 공통 reader·resolution으로 교정하고 후속 직접 교정은 근거만 합류. 실제 PG/Couch에서 assertion 1건·교정 1건 확인. 일반 native 세계 상태 admission은 남음 |
| C03 | 같은 관측의 다른 miner 해석 | PG 코어의 해석 version별 보존과 실제 Couch/PG에서 extractor 변경 시 같은 벡터에 별도 source 연결·새 처리 키 확인. 기존 fact 영향 재검토는 남음 |
| C04 | provider/import/chunk가 다른 재수집 | 같은 native call의 위치가 달라도 같은 관측과 두 alias를 보존하는 검사 추가. 5개 provider의 native 구조와 실제 Couch 왕복 확인. provider가 다른 동등 관측은 증명 없이 병합하지 않으며 일반 import 간 명시적 alias 연결은 남음 |
| C05 | 같은 값을 지지하는 독립 관측 | 실제 Git/PG·모델에서 독립 source가 같은 값을 지지하게 되면 자동 해결, matching pending support·후속 참조 보존·추가 authority 1건을 확인. 일반 source 연결은 남음 |
| C06 | 최근 수집된 옛 snapshot | 실제 Git ancestor를 지연 수집해 history로 연결하고 현재 값 유지. 일반 세션 연결은 남음 |
| C07 | 한 세션의 여러 실제 상태 변화 | native 추출에서 A→B→A 세 호출의 별도 관측·원래 응답·순서 보존 확인. 일반 세션의 각 관측이 fact resolution까지 연결되는 검증은 남음 |
| C08 | 실제 후속 제거·rollback | 실제 Git ref A→B→A와 같은 불변 관측의 현재 재검증, 오래된 terminal 요청 무변경 확인. 일반 source/운영 흐름은 남음 |
| C09 | 과거 원인을 몰라도 검증된 현재 값 교정 | 직접 Git 데이터와 파일 내용의 실제 의미 모델 경로에서 현재 값 교정 확인. 일반 native/운영 근거 경로는 남음 |
| C10 | clock skew·관측 시각 누락 | PG 코어에서 원래 과거/미래/누락 시각 보존과 DB 검증 시각·기한 분리, replay 비연장 확인. 실제 Git/모델에서 같은 고정본의 기한 자동 재확인·중단/감지/복구 확인. 전체 native/provider와 PG 서버 clock 변화 검증은 남음 |
| C11 | 같은 revision의 상충 동시 교정 | 실제 Git/PG의 두 worker를 snapshot 읽기 뒤 동시에 실행해, 다른 제안은 실제 단일 값으로 합류하고 두 독립 파일의 실제 반대 값은 동일 카드에서 충돌 보류함을 확인. 일반 native 경로는 남음 |
| C12 | 다른 key의 동일 사실·동일 값 | 실제 PG 동시 key에서 단일 successor 확인. 일반 admission 경로는 남음 |
| C13 | 같은 카드의 다른 필드 동시 교정 | 동일 원래 revision에서 실제로 다른 PG backend connection 두 개를 동시 실행해 최종 단일 successor에 두 변경 보존 확인. 코어 admission callback은 대역이며 일반 native 경로는 남음 |
| C14 | 다른 카드 ID에 중복된 사실 | 알려진 두 title의 정책 등록과 공통 head 연결, 실제 CLI/모델 합의 뒤 두 카드의 동일 값 표시 확인. 미식별 중복·일반 query/graph 경로는 남음 |
| C15 | 유사 문장의 다른 환경·branch | 고정 branch의 실제 Git reader가 checkout/dirty 변경을 혼동하지 않고 후속 commit을 재검증함을 확인. 자동 교정의 동적 HEAD 거부와 source context/resource mismatch 선차단 구현. 일반 target 의미 식별·모든 source의 환경/branch 검증은 남음 |
| C16 | 여러 사실 중 한 사실만 교정 | 정확한 표시 구간 patch와 v2 batch 구현. 다른 사실 보존·모호한 구간 거부 확인. 의미 기반 span 결정은 남음 |
| C17 | summary·typed payload·표시문 일관성 | 초기 adapter에서 수정. summary와 typed payload의 동시 갱신 및 전체 freshness 비승격을 실제 PG에서 확인. 일반 복수 사실 경로는 미검증 |
| C18 | 오래된 cursor·target lineage | fact generation에 cursor 결속, 같은 값의 stale target 합류와 변경하지 않은 필드의 lineage 확인. 일반 의미 target의 전체 연결은 남음 |
| C19 | 서버 수집 전의 근거 제출 | 실제 MCP HTTP `brain_source_observe`로 독립 source 관찰을 먼저 불변 저장하고 returned evidence로 교정한 뒤, 지연 Couch 기록의 자동 발견·active source 대기·별도 CLI 재시작 합류 확인. 임의 client 본문을 검증 없이 새 사실로 밀어넣는 경로는 없음. 일반 source reader 연결은 남음 |
| C20 | 오래되거나 불완전한 근거 | native 원래 응답의 byte 한도·잘림·redaction 구분, 불완전한 응답의 성공 요약 거부와 기본 complete 읽기의 차단을 검사. 원천 전체 freshness와 일반 의미 admission은 남음 |
| C21 | 우열을 판단할 수 없는 상충 근거 | B 정책 미결정. 증거 확보 전 임의 선택하지 않음 |
| C22 | 자기 재인용을 독립 근거로 오인 | assistant 문장/옛 요약의 native 승격과 임의 세계 상태 capability 발급을 거부. 서버 참조의 반복·서로 다른 native call도 이미 검증된 관찰에만 연결하며 assertion 1건과 추가 교정 0건을 확인. 일반 native 의미 admission·전체 lineage 처리는 남음 |
| C23 | 테스트·구현을 운영 배포로 과대 해석 | 원시 모델 실패는 보존. repository 관측을 graph/HTTP/test/query 결과로 승격하지 못하는 code gate와 새 실제 모델 평가 확인. 모든 runtime 근거 reader의 교정 연결은 남음 |
| C24 | evidence 안의 명령·위조·권한 위반 | 기존 hash·정책 검사에 native call/result 충돌·범위/본문/coverage 변조·중복 JSON key·symlink/FIFO/크기 거부를 추가. 실제 모델의 일반 native 지시문/locator/권한 경로는 남음 |
| C25 | source 철회·삭제·권한 회수 | 실제 PG receipt 철회 직후 조회 제외, 다른 필드·독립 근거 보존과 실제 CLI 종료/monitor/자동 재개 확인. 일반 source 권한 이벤트·파생 기억 전파는 남음 |
| C26 | parser·miner·redaction·정책 변경 | 미검증 |
| C27 | evidence 저장 뒤 authority 실패 | 초기 adapter와 공통 PG 코어의 canonical/resolution 동시 rollback 확인. 일반 source 보존/GC 경합은 남음 |
| C28 | authority 저장 뒤 응답·완료 기록 유실 | 실제 모델/Git/PG의 원자적 commit/복구에 더해 native 연결·queue 복구 참조도 같은 transaction에 보존. 완료 기록 유실 뒤 Git과 Couch catalog 없이 중복 successor 없는 복구 확인. 일반 runtime·운영 process 전체 연결은 남음 |
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

native 관측의 source 보존과 실제 Couch/PG intake 증거는 [관측 출처 검사](../../validation/2026-10-26-lbrain-native-observation-provenance.md)를 따른다. 이 부분 검증도 일반 native-to-fact 교정의 완료를 대신하지 않는다.

선택 모델의 새 독립 평가와 미통과 원인은 [판단 v3 평가](../../validation/2026-10-26-lbrain-judgment-v3-evaluation.md)를 따른다. 실호출 성공과 의미 판단 기준의 통과를 구분한다.

배타적 관계 분류와 별도 독립 평가의 미통과 결과는 [관계 판단 v4 평가](../../validation/2026-10-26-lbrain-relation-v4-evaluation.md)에 기록한다.

단일 지지 정책의 잘못된 허용과 source capability 연결의 필요성은 [단일 지지 평가](../../validation/2026-10-26-lbrain-relation-support-evaluation.md)에 기록한다.

관리 대상의 일반 writer 우회 차단과 남은 식별 범위는 [DB writer 보호 검사](../../validation/2026-10-26-lbrain-fact-writer-guard.md)에 기록한다.

Git 내용의 의미 기반 값 선택·새 독립 실제 모델 평가·실제 worker/MCP HTTP 증거는 [의미 admission 검사](../../validation/2026-10-26-lbrain-semantic-admission.md)를 따른다. 이 결과는 전체 source 종류와 모든 달성 기준의 완료를 뜻하지 않는다.

기존 교정 근거의 FK 보존·철회 전파·조회 범위와 자동 복구의 부분 증거는 [근거 lifecycle 검사](../../validation/2026-10-26-lbrain-source-lifecycle.md)를 따른다.

원래 관측 시각·서버 검증 시각의 분리와 실제 기한 자동 재검토는 [검증 기한 검사](../../validation/2026-10-26-lbrain-fact-verification-expiry.md)를 따른다.

무관한 commit과 실제 rollback에서 독립 근거를 누락하던 오류·자동 합의·새 독립 모델 평가의 증거는 [독립 source 재검토 검사](../../validation/2026-10-26-lbrain-competing-source-revalidation.md)를 따른다.

동일 카드의 복수 원천 정책·실제 동시 요청·초기 예약과 migration의 증거는 [동시 원천 검사](../../validation/2026-10-26-lbrain-concurrent-sources.md)를 따른다.

원래 native tool 응답의 보존·한도·역할·관측 identity와 실제 Couch/PG/CLI 증거는 [native receipt 검사](../../validation/2026-10-27-lbrain-native-tool-receipts.md)를 따른다.

서버 수집 전의 독립 관찰 참조, 지연 native source의 동일 관찰 합류와 수집 전 장애 감시는 [서버 관찰 참조 검사](../../validation/2026-10-27-lbrain-source-observation-join.md)를 따른다. 이 범위를 일반 native 세계 상태 admission이나 전체 goal 완료로 승격하지 않는다.

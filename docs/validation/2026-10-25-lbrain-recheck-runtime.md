# LBrain 자동 재검토의 격리 runtime 실증

## 판정

**구조화된 테스트 근거에 대한 자동 재검토·장애 복구·지연 수집 후 교정을 Ubuntu 격리 환경에서 확인했다.** PostgreSQL과 실제 별도 프로세스를 사용했다. 모델 호출, 범용 자연어 판단, 운영 수집 연결 또는 충돌 처리 B 전체의 완료 판정은 아니다. B는 계속 미결정이다.

초기 source는 `f04bfdba7fcafc168094c16acdcb61b2df690285`, 유효성 캐시 보완판은 `78e971e9975e24eb14164eb14ab2acd8929c3410`이다. 사용자 memory를 변경하지 않았고 main 반영·운영 활성화는 수행하지 않았다.

## 구현한 범위

`recheck_scheduler.py`는 작업·다음 예약·실행 소유권·시도 이력·판단 cache·heartbeat를 PostgreSQL에 보존한다. due 작업은 `FOR UPDATE SKIP LOCKED`로 하나의 작업자가 가져가며, 만료된 실행은 세대를 올려 회수한다. 이전 실행은 lease token·세대·DB 시각 검사를 통과하지 못하면 판단 저장·완료 처리·authority 트랜잭션에 들어갈 수 없다.

설정 누락은 `blocked`와 다음 예약을 함께 남긴다. 원인이 없어지면 추가 요청 없이 재시도한다. 독립 감시는 heartbeat·예약 초과·만료 lease·DB 조회 실패를 확인한다. 외부 알림 전송은 구현하거나 시험하지 않았다.

`eval/recheck_runtime_probe.py`는 기존 native Codex parser, durable ingress, tool-evidence ledger, JSON source 정책, canonical 교정 트랜잭션을 연결한 **실험용 연결기**다. 실제 pytest를 실행하고 그 결과로 synthetic Codex JSONL을 만든다. 설치된 provider hook의 실제 사용자 세션을 수집한 시험은 아니다. parser 결과를 근거 저장소에 연결하는 부분도 운영 intake에 설치하지 않았다.

근거 hash·정책·검증 상태가 같으면 저장된 판단을 재사용한다. 보완판에서는 내용이 같아도 만료·재관측·철회로 유효성이 달라지는 경우를 매번 코드로 확인해 cache 입력에 포함한다. authority 반영 시에는 기존 adapter가 source·CAS를 다시 검증한다. 의미 판단 모델을 매 polling마다 호출하는 구조로 시험하지 않았다.

## 실제 결과

| 시험 | 확인한 결과 |
| --- | --- |
| 별도 프로세스의 자동 실행 | 한 번 등록한 예약을 추가 요청 없이 처리했다. 같은 근거에서 반복 재검토해도 최초 판단 1건을 재사용했고 교정은 0건이었다. |
| 원천 읽기 실패 | `handler_connectionerror`와 다음 예약이 남았다. 원인을 제거하자 자동으로 재개했다. |
| 설정 누락 | `blocked`와 다음 예약을 보존하고 독립 감시가 주의 상태를 기록했다. 설정 복구 후 자동 재개했다. |
| 반영 전 프로세스 강제 종료 | 실제 프로세스가 exit 91로 종료됐다. supervisor 재시작과 lease 회수 후 저장된 판단을 재사용했다. |
| 작업자만 중단 | 별도 감시가 heartbeat 정지와 stalled 상태를 감지했다. 작업자 재시작 후 정상 상태로 돌아왔다. |
| DB 중단·재시작 | 감시는 storage unavailable을 기록했다. 고정 접속점과 영속 volume을 사용한 재시험에서 작업·판단을 보존하고 자동 복구했다. |
| 실행했지만 아직 수집하지 않은 근거 | 실제 pytest exit 0 뒤 수집을 지연했다. 기억은 바뀌지 않았다. |
| 지연된 근거의 수집 | 기존 parser·ingress와 실험 연결기를 통해 들어온 근거를 작업자가 발견했다. 교정 요청을 다시 제출하지 않았다. |
| 필수 근거의 만료 | 새 근거가 들어와도 다른 필수 근거가 오래됐으면 `source_stale`로 보류하고 교정하지 않았다. 실제 재검증 근거를 등록하자 진행했다. |
| 교정 commit 직후 작업 완료 기록 전 종료 | canonical 저장 직후 exit 91을 주입했다. 재시작 후 같은 결정으로 재생해 `correct_fact` 1건과 current 1건을 유지했다. |
| 동일 MCP 조회 | 실제 HTTP MCP `tools/list`와 `brain.resolve(mode=context)`를 호출했다. `missing → pass`를 확인했다. graph 경로는 unavailable이므로 Neo4j 검증으로 해석하지 않는다. |
| 같은 bytes의 만료·재관측 | 보완판의 별도 Ubuntu 프로세스에서 conflict → stale → 같은 bytes 재관측 후 conflict를 확인했다. 그 뒤 실제 테스트 수집·commit 직후 종료·재시작을 거쳐 결정 1건으로 완료했다. |

첫 runtime의 저장된 이력은 총 27,453회다. 이 중 conflict 대기 27,427회, 원천 오류 대기 3회, stale 대기 10회, 설정 blocked 9회, lease 만료 3회, resolved 1회였다. 중간 대기 시간에도 1초 시험 주기로 실행된 결과이며, 이 횟수는 사례의 다양성이나 모델 정확도를 뜻하지 않는다. 운영 주기·backoff·비용 정책은 별도로 정해야 한다.

시험 도중 Docker 자동 할당 포트가 DB 재시작 시 바뀌어 재접속하지 못한 실험 설정 문제가 있었다. 데이터는 보존됐고 독립 감시는 장애를 감지했다. 고정 포트로 수정한 뒤 중단·복구 시험을 다시 통과했다. 초기 Mac archive의 AppleDouble 메타데이터도 제거해 이미지를 다시 만들고 source manifest와 대조했다. 실패한 초기 실행을 통과 증거로 세지 않는다.

## source·설치 artifact 결속

- 두 source의 package content manifest: `sha256:69e881ec82d230fbb417ca6ce0cbd4968d0d95ecc1843bf7dc8d68b68e250d7f`. 보완 변경은 eval script에 있어 package 내용 hash는 같다.
- 보완판 runtime image: `sha256:6a72a637d50a43ab1b2c0f1e356e138bcd68e3399facf19695d7359ad719b4a7`.
- 보완판 eval script: `sha256:f13364a4a8e6c72502c62d8eb3338bcc6f1c1bf5a68677581bd3d8f39f73a9ec`.
- 설치된 package 파일 270개와 build source를 대조해 불일치 0개였다. SQL schema도 포함한다. 실제 실행은 설치된 package를 사용했으며 runtime `PYTHONPATH` 우회는 없었다.

## 자동 검사

- `cd worker && uv run pytest -q tests/test_recheck_scheduler.py`: 별도 격리 PostgreSQL에서 **14 passed in 84.39s**. opt-in DB 검사가 실제 실행된 결과다.
- `cd worker && uv run pytest -q tests/test_recheck_runtime_probe.py`: **1 passed**. 실제 source catalog와 freshness validator로 만료·같은 bytes 재관측·철회의 cache 무효화를 확인했다.
- 보완 후 `cd worker && uv run pytest -q`: **5202 passed, 620 skipped, 4 warnings in 49.96s**. 환경 선택형 skip을 runtime 증거로 세지 않는다.
- 같은 core 변경에 대한 Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`: **BUILD SUCCESSFUL**. 이후 변경은 Python eval/test뿐이다.
- `git diff --check`: 통과.

Docker 이미지의 Python·pip 실행은 해당 이미지의 명시적 runtime/build 방식이다. Mac의 Python 실행과 검사에는 `uv`를 사용했다.

## 보존·정리

실험·preflight·유효성 보완 시험의 세 DB에서 canonical current를 기존 `stale_mark`/`stale_commit` 경로로 제외했다. 각 DB의 교정 결정 1건과 재검토 이력은 보존했다. 첫 시험은 실제 MCP 재조회에서도 current 항목 0개였다. 세 DB 모두 non-synthetic 카드 0개였다.

각 DB dump를 별도의 임시 DB에 실제 복원해 current 0개·교정 결정 1개·재검토 작업 1개·non-synthetic 0개를 확인했다. 근거·시도·감시·수집 상태와 HTTP receipt는 private storage에 보존했다. 실험 소유 container와 volume을 제거하고 임시 bearer 설정 및 터널을 정리했다. 기존 운영 서비스는 변경하지 않았다.

## 남은 gate

1. `perplexity/pplx-decider-flash-v1.2` 실제 호출·의미 판단 평가. 현재 실증의 모델 호출은 **0회**다. 기존 설정에서 사용할 수 있는 OpenRouter 키를 찾지 못해 인증 위치에 대한 사용자 응답을 기다린다. 키 값을 문서에 기록하지 않는다.
2. 정규 session intake/miner와 재검토 등록·변경 발견의 연결. 실험 연결기의 성공만으로 운영 연결을 완료 처리하지 않는다.
3. 충돌 B를 선택할 경우 확정값을 어떻게 제외하고 반환할지에 대한 context/list/query 공통 읽기 정책. 이번 실증의 pending 중 MCP에는 기존 값이 남으며, conflict masking은 구현하지 않았다.
4. 같은 관측의 교정·지연 세션 합류, 역순 입력, 실제 후속 변경, graph·embedding 지연과 역순 projection, 근거 retention 참조 보호 등 [설계 감사](2026-10-25-lbrain-correction-reconciliation-audit.md)의 미검증 항목.
5. 운영 재시도 주기·감시의 운영 연결·외부 알림·감시까지 함께 중단되는 전체 host 장애. 현재 시험의 독립 감시는 같은 host의 별도 프로세스다.

따라서 자동 재검토의 저장·실행·복구와 구조화된 근거의 해소 경로는 입증했으나, 이 결과만으로 충돌 B를 선택하거나 전체 교정 기능을 완료 처리하지 않는다.

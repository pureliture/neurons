# 공통 fact resolution 구현과 Ubuntu 부분 검증

전체 교정 goal은 진행 중이다. 이 문서는 Git의 명시적 데이터와 기존 MemoryCard를 연결한 범위의 증거다. 자연어 의미 판단·전체 정규 수집·실제 graph 수렴까지 완료했다는 의미가 아니다.

## 저장과 처리

`FactScope`는 project·repository·branch·environment·subject·predicate로 사실을 구분한다. 관측 ID, source revision, extractor/정책 version, 그 관측이 지지하는 값은 분리해 보존한다. `lbrain_fact_scopes`에는 단일 현재 값과 generation을, `lbrain_fact_members`에는 카드별 필드의 연결을, observations/assertions/resolutions/events에는 근거와 판단 이력을 저장한다. 명시적으로 허용한 Git source의 redacted 원문은 별도 receipt에 불변 저장한다.

현재 값 변경은 기존 `correct_fact` authority transaction을 사용한다. successor·supersession·audit·embedding/graph outbox와 fact head·판단 결과·재검토 완료 참조가 함께 저장된다. 이미 공통 fact에 연결된 카드의 기존 steward authority 변경은 resolution transaction을 통과해야 한다. 아직 식별하지 않은 신규 중복 후보까지 모두 차단하는 규칙은 남아 있다.

도착 시각은 승자 결정에 사용하지 않는다. 실제 source의 현재 branch/head를 다시 확인하고 commit ancestry를 비교한다. 같은 값이면 근거만 연결하고, 오래된 관측이면 이력으로 남긴다. 새 관측으로 확인한 실제 rollback은 반영할 수 있다. 다른 값의 근거가 동시에 현재 유효하면 `disputed`로 보존한다. 현재 유효한 반대 근거가 남아 있는 동안 같은 값을 다시 제출해도 충돌을 해소하지 않는다. 이 상태의 제품 읽기 정책 B는 아직 선택하지 않았다.

같은 카드의 다른 필드가 먼저 바뀌었으면 후속 lineage의 모든 revision에서 지정 필드가 그대로였는지 확인한다. 확인될 때만 현재 카드로 이어가 두 변경을 모두 보존한다. 지정 필드까지 바뀐 stale 요청은 거부한다. 이미 같은 사실을 교정한 뒤 원래 revision으로 도착한 요청은 source가 현재 값과 일치함을 독립 확인한 경우에만 근거를 연결한다.

## 도구와 source 연결

`brain_object_correct`의 v2 입력은 batch이며 각 항목에 기존 값·불일치 설명·제안 값·근거 revision/hash를 받는다. exact target 또는 설명을 지정한다. 첫 설명 adapter는 정확한 title/summary 대응만 지원하며 의미 검색으로 확대 해석하지 않는다. 대상이 없거나 복수이면 새 accepted 기억을 만들지 않는다. 실제 적용 값은 호출자의 제안보다 서버가 직접 확인한 source 값을 따른다. legacy 단건 입력도 유지한다.

`--fact-correction-policy-file`로 서버가 허용한 Git repository/branch, JSON 필드, fact 범위와 표시 구간을 설정한다. 현재 adapter는 직접 대응하는 JSON 문자열만 처리한다. 자연어 문서나 코드의 의미를 JSON adapter로 인증하지 않는다. 선택한 OpenRouter 모델의 적용 평가는 여전히 미통과이며 자동 fallback은 없다.

교정과 관리 대상 autopilot 후보는 같은 `PgRecheckQueue`와 `FactResolutionService`를 사용한다. 관리 대상 후보는 별도 current 기억으로 승인하지 않고 원래 후보를 보존한 채 기존 사실의 재검토로 연결한다. source가 일시적으로 없거나 설정이 차단되어도 다음 예약은 남는다. worker/monitor 진입점은 `uv run -m agent_knowledge.fact_correction_coordinator`다. 실제 worker subprocess의 source 차단·종료·독립 monitor 감지·재시작·동일 작업 자동 완료도 확인했다. 운영 daemon 설치나 전체 native provider 수집 연결의 증거는 아니다.

## 조회 일관성

공개 resolver의 최종 직렬화에서 현재 fact 값을 적용한다. 검색과 graph의 원본 hash 검증은 먼저 유지한다. MVCC snapshot에서 canonical revision·fact head·project generation을 함께 읽고, 검색 이후 authority가 바뀌면 명시적 오류를 반환한다. 동일 사실이 여러 카드에 연결되어도 현재 값은 일치하고 다른 문장은 보존한다. 과거 canonical 기록의 원문은 덮어쓰지 않는다.

응답의 `fact_generation`과 요청의 `min_fact_generation`을 통해 최소 반영 지점을 확인할 수 있다. cursor도 generation에 결속하므로 교정 전 cursor를 그대로 이어가며 새 현재 값을 누락하지 않는다. 판단 당시 적용 값과 이후의 현재 값은 별도로 반환한다. 관련 필드의 상충 근거가 남아 있으면 context에도 `disputed_fields`를 표시하며 B의 자동 숨김 정책을 임의로 활성화하지 않는다. 과거 시점 재구성은 이 변경의 범위가 아니다. list/context와 실제 HTTP 재조회는 검사했으며 실제 Neo4j·embedding 역순/장애·장기 복구는 남아 있다.

## 검사와 증거의 한계

Ubuntu의 작업 전용 PostgreSQL과 작업 전용 컨테이너에서 실제 Git, source admission, canonical transaction, queue, MCP CLI subprocess와 실제 HTTP를 실행했다. source bundle은 SHA-256으로 결속했다. 검증 컨테이너는 이전 검증 이미지의 의존성을 기반으로 새 source를 읽기 전용 주입했으므로 최종 배포 이미지의 build identity 증거로 사용하지 않는다. 운영 서비스·운영 DB는 변경하지 않았다.

`worker/tests/test_fact_resolution.py`의 코어 관측 callback은 명시적 대역이다. 같은 파일의 coordinator 검사는 실제 Git adapter와 PostgreSQL을 사용한다. HTTP 검사는 실제 `neuron-knowledge mcp-http --surface correction` subprocess와 인증·도구 발견·교정·동일 서버 재조회를 실행한다. schema 없는 초기 DB나 private source를 시험 대상으로 사용하지 않았다.

검사 중 발견한 오래된 대역의 SQL 지원 누락과 재검토 due 시각 전 호출은 수정했으며 제품 검사를 skip/xfail하지 않았다. 환경 선택형 skip을 실제 통과로 세지 않는다.

- Ubuntu 검증 컨테이너의 `uv run --no-project pytest -q`로 `test_fact_resolution.py`, `test_evidence_correction.py`, `test_recheck_scheduler.py`, `test_correction_sources.py`를 실행: **90 passed, 1 warning in 38.10s**.
- 이 중 실제 HTTP CLI subprocess와 worker/monitor subprocess는 source 복구 뒤 수동 `run_once`, 새 요청 또는 시각 조작 없이 같은 queue 작업의 완료를 확인했다. 독립 monitor는 미완료 작업과 오래된 heartbeat를 감지했다.
- source bundle SHA-256: `8e6b5520d1029fafbd003b6bd0ce0b495122856c24a906d85d674a2cd9eb2207`.
- 검증용 의존성 이미지: `sha256:ee9b21c07899568b49342da9b884adb84847fd02c0f7583f0464d000fa155f6a`. 이 digest는 주입한 source의 최종 배포 이미지 digest가 아니다.
- worker `uv run pytest -q`: **5244 passed, 646 skipped, 4 warnings in 54.25s**. 실제 PG 실행과 별도이며 opt-in skip을 실증에 포함하지 않는다.
- Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`: **BUILD SUCCESSFUL**.
- `git diff --check`: 통과.

Mac의 Python 실행과 Ubuntu 검증 실행에는 `uv`를 사용했다. 기존 검증 이미지에 `uv`가 없어 image의 명시적 build 방식인 `pip`로 `uv`를 bootstrap했고, host Python/package 설정은 변경하지 않았다. 종료된 검증 컨테이너는 제거했으며 task 전용 PostgreSQL·증거·검증 이미지는 남은 goal 검증을 위해 유지한다.

## 다음 필수 범위

- native provider 관측/해석 ID와 일반 source admission, 모든 승인 writer의 공통 사실 식별을 연결한다.
- 설명 의미 검색과 의미 검증 모델의 독립 평가·출시 profile을 구현하고 입증한다.
- source 철회·권한 변경·parser 변경·파생물의 자동 영향 재검토와 retention/GC 경합을 처리한다.
- 실제 Neo4j/embedding에서 query·projection 역순·장기 장애·복구를 확인한다.
- 독립 감시와 자동 재검토를 운영 형태로 검증한 뒤 미결정 B 정책을 판단한다.
- 최종 source/image/MCP identity, 전체 실제 작업 흐름, 백업 복원·정리까지 확인한다.

이 항목들이 남아 있는 동안 전체 goal을 완료 처리하지 않는다.

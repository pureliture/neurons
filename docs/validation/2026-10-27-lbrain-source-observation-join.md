# 서버 관찰 참조와 지연 세션 기록의 합류

전체 goal은 계속 진행 중이다. 이 단계는 등록된 Git source를 서버가 독립적으로 읽은 관찰을 교정 요청과 이후 세션 수집이 공유하게 한다. 임의의 native 출력 전체를 세계 상태의 증명으로 인정하는 기능은 아니다. 일반 HTTP·test 실행 reader, 의미 target 검색, 모든 writer·정책 변경 전파·retention·실제 embedding·최종 전체 작업 검증은 별도로 남아 있다. B 자동 숨김은 미결정이다.

## 수집 전 관찰

교정 전용 MCP surface의 `brain_source_observe`는 project·repository·policy_id·idempotency_key를 받는다. 서버가 등록한 reader로 원문과 현재 revision을 확인하고 기존 source receipt와 불변 VerifiedObservation을 저장한다. 실제 읽기가 끝난 뒤 원천이 바뀌지 않았는지 재확인한다. 기존 기억을 승인하거나 값을 교정하지 않고 `brain_source_observation.v1`과 `observation_ref`, revision·content hash를 반환한다. 원문 본문은 반환하지 않는다.

반환된 `evidence`를 기존 `brain_object_correct` v2 change에 전달할 수 있다. 서버는 참조가 같은 project·policy version·fact·revision·본문에 속하는지 확인하고, 기존 공통 resolution에서 값을 독립 검증한다. LLM의 제안 값 자체를 신뢰하지 않는다. 참조가 없는 기존 v2 입력의 직렬화와 요청 hash는 유지한다. 같은 읽기 key의 재시도는 원래 참조와 시각을 반환하며 새 관찰처럼 갱신하지 않는다.

이 도구는 설정된 coordinator가 있는 correction/stdio surface에서만 광고한다. correction HTTP Bearer 경계와 기존 public/admin 분리를 유지한다. source 관찰도 불변 자료를 저장하므로 일반 public 읽기 도구로 추가하지 않는다.

## 지연된 native 기록

기존 native receipt를 담은 Couch source의 change feed를 별도 dispatcher가 확인한다. native 원래 응답의 완전한 JSON 안에 있는 서버 발급 관찰 참조만 후보로 받는다. MCP text/structured JSON wrapper를 처리하되 assistant 문장이나 JSON 앞뒤 prose에서 참조를 추측하지 않는다. 발급된 응답 전체의 hash와 project가 일치해야 한다.

발견한 참조를 기존 PostgreSQL 재검토 queue에 등록하고 cursor 이동을 같은 transaction에 기록한다. native call·receipt hash·서버 관찰 참조가 같으면 재수집과 동시 dispatcher가 중복 작업을 만들지 않는다. active source가 아직 게시되지 않았다면 작업을 보류하고 다음 예약을 유지한다. source 활성화 후 같은 작업이 자동 재검토된다.

교정이 먼저 끝났다면 worker는 원래 terminal resolution에 native provenance를 연결한다. 새 값 판정이나 추가 authority write를 실행하지 않는다. 세션이 먼저 도착했다면 기존 공통 reader·판정·resolution으로 기존 기억을 교정한다. 나중 교정은 같은 관찰과 근거에 합류한다. 그 사이 실제 값이 바뀌었다면 과거 결정과 현재 값을 구분해 응답하고 과거 기록 때문에 현재 값을 되돌리지 않는다. 후속 조회 token도 응답 시점의 project generation을 반영한다.

native 출력에 유효한 참조를 복사하더라도 이미 독립 검증된 관찰의 참조만 추가된다. 독립 지지 근거의 수나 HTTP/test/운영 상태 capability는 늘지 않는다. 동일한 서버 관찰을 서로 다른 native call이 참조할 수 있지만 assertion은 같은 관찰로 합류한다.

## 보존·복구·감시

서버 읽기, 작업과 읽기의 연결, native 연결에 project·fact·policy·receipt binding을 검사하는 DB 제약을 둔다. 읽기와 native 연결의 덮어쓰기를 막고, 참조 중인 원래 receipt·관찰·resolution을 FK로 보존한다. native 원래 응답은 기존 redacted source receipt에 보존하며 일반 recall/embedding body에 복사하지 않는다.

native 연결과 authority 결과, queue의 복구 참조는 같은 transaction에 기록한다. 저장 뒤 응답이 유실되어도 이 참조로 복구하며 source나 Couch를 다시 읽거나 successor를 중복 생성하지 않는다.

기존 `fact_source_lifecycle worker`의 선택적 `--native-source-policy-file`로 자동 발견을 연결한다. 기존 Couch source 설정과 extractor/redaction 정책을 사용한다. queue 생성 전 수집 장애도 cursor의 마지막 성공·시도·고정 실패 code로 감시한다. `monitor`에 같은 정책을 지정하면 필요한 source가 한 번도 시작되지 않은 경우도 감지한다. 저장된 오래된 key의 상태를 새 source의 정상 상태로 오인하지 않는다. 원격 예외의 credential·URL 본문은 결과에 노출하지 않는다. 운영 상주 설정은 이 단계에서 변경하지 않는다.

## 검증 범위

합성 provider-native 자료와 작업 전용 Ubuntu PostgreSQL·CouchDB·Neo4j를 사용한다. 사용자의 raw private transcript를 읽지 않는다. 실제 MCP HTTP·CLI subprocess·Git·DB 동작과 실제 OpenRouter 판단을 구분해 기록한다. provider 자체의 live SDK 호출 또는 실제 embedding 검증으로 확대하지 않는다.

Ubuntu source bundle `24542f8f31c91f1921fc79cf207cd4885f2801b9cd79ecbc8bd30111c7bc0693`의 교정·source lifecycle·경쟁 원천·기한·동시성·native receipt/intake·capability·Neo4j 회귀 11모듈은 **166 passed in 175.24s**다. skip 없이 실제 PostgreSQL·CouchDB·Neo4j를 사용했다. 참조 발급 전 실제 Git 변경 거부, provider 5개 형식의 늦은 합류, 위조/다른 project 거부, 중복 call의 assertion 1건, cursor 경쟁, 비활성 source 대기와 별도 CLI 재시작, commit 뒤 원천 부재 복구, DB binding·retention과 현재 조회 token을 포함한다.

새 평가기와 사전검사를 포함한 release bundle은 `68df166365f29f02d3924d4bda9bf75dcd518ac03d03dbca654a1c32ef494375`다. 앞선 bundle과 `lib/` 전체가 동일함을 Ubuntu에서 `diff -qr`로 확인했다. 로컬 전체 `uv run pytest -q`는 **5343 passed, 772 skipped in 55.76s**다. 환경 미설정·선택형 skip은 실제 연동 통과로 계산하지 않는다. Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`는 **BUILD SUCCESSFUL**, test는 UP-TO-DATE다. Ruff F 검사와 `git diff --check`도 통과했다.

새 독립 v5 평가의 실제 모델은 `perplexity/pplx-decider-flash-v1.2-20261016`이며 정상 선택 **20/20**, 잘못된 허용 **0/20**, 의미 거부 12건·code gate 거부 8건이다. threshold는 0.8을 유지하고 fallback을 사용하지 않았다. 64개 typed 응답의 reported cost는 **0.00025007 USD**다. 20개 정상 표본의 정답 위치를 섞었고 앞선 v1~v4 자료를 모두 제외했다. plan hash는 `sha256:14597ce2c516b76214999714d44e65f8cf5b165f8c9b0106a4aef8da9366861c`, private artifact hash는 `sha256:05a5478b67ab06d898385e22eec692b28cc3fa1256cd9b6295c549bfed614154`다. 관찰 참조 모듈도 code identity에 결속하며 기존 v3 profile은 이 새 코드에서 유효하지 않다.

개발 중 v4 평가는 합성 source의 `authorization` 문자열이 redaction되어 원문 독립 읽기 조건을 만족하지 못하고 중단됐다. 통과 또는 완결된 평가로 세지 않았다. 모든 입력의 redaction 적격성을 모델 호출 전에 검사하고, 읽기 전 실패도 결과 row로 보존하도록 보완했다. 같은 자료를 수정해 합격시킨 것이 아니라 v4 전체를 제외한 별도 v5 자료로 평가했다. 원시 모델의 기존 잘못된 허용 1/20과 v3 정상 1건 보류 기록도 유지한다.

release bundle의 `tests/test_semantic_correction_live.py tests/test_semantic_eval_plan.py`는 실제 Ubuntu에서 **10 passed in 87.30s**다. 7건은 실제 OpenRouter·Git·PG의 교정/응답 유실·source 철회·검증 기한·경쟁 원천·동시성 및 새 MCP 관찰→교정→지연 Couch 수집→source 활성화 대기→CLI 재시작 합류를 검증한다. 나머지 3건은 평가 전 입력 검사·과거 표본 제외·읽기 실패 row 보존 검사다. 호출 client 대역을 사용하지 않았다. 작업 전용 임시 인증 파일은 제거하고 부재를 확인했으며 원본 인증과 운영 설정은 수정하지 않았다.

운영 전환에는 이 코드 identity에 맞는 검증된 private profile과 등록 policy 설정이 필요하다. rollback은 새 쓰기/수집을 중지한 뒤 호환 consumer를 유지하면서 관찰·native 연결·원천 receipt·resolution을 보존한다. 이력 삭제나 DB guard 해제로 이전 consumer를 동작시키는 rollback은 지원하지 않는다. 운영 활성화·merge·운영 GC를 실행하지 않았다.

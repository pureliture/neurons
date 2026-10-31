# Native 도구 응답의 원천 보존과 읽기 계약

전체 goal은 진행 중이다. 이 변경은 일반 native 근거를 검증할 때 필요한 원래 도구 응답을 기존 Couch source에 보존하고 읽는 경로를 구현한다. native 응답을 현재 세계의 사실로 인증하거나, 교정 전에 제출한 관측과 나중 세션을 자동으로 같은 assertion에 합류시키는 단계는 아직 남아 있다. B 자동 숨김은 계속 미결정이다.

## 저장과 구분

기존 tool evidence lane은 high-signal 결과의 요약을 저장하며 file read/search 등을 제외한다. 그 요약에서 원래 tool 입력·출력 전체를 복원할 수는 없다. 새 `--native-tool-receipts` 모드는 원래 native call ID로 짝이 확인된 호출·응답을 별도 receipt로 보존한다. 기존 기본 모드와 public CLI 계약은 유지한다.

`native_tool_receipt.v1`은 provider·session·call 식별자, tool 이름, 제한된 입력과 응답, 전체 redacted 본문의 hash, 원래 보고된 시각, 잘림과 redaction 여부를 담는다. 시각이 없거나 잘못됐으면 현재 시각을 지어내지 않는다. 응답 범위는 `provider_tool_result`다. 예를 들어 shell이 `HTTP 200`이라는 문자열을 출력한 사실은 실제 HTTP 요청을 검증했다는 뜻이 아니다. 이 receipt로 `HTTP_RESPONSE` 등 독립 reader의 capability를 발급하지 않는다.

Codex·Claude·Gemini·Grok·Antigravity의 명시적 native 호출/응답 구조를 처리한다. assistant 문장 안의 JSON, 다른 역할의 tool result, 호출 ID가 없는 자료, 짝이 없는 호출, 같은 ID의 다른 입력·응답을 임의로 복원하지 않는다. Gemini의 실행 중 상태는 완성된 응답으로 처리하지 않는다. Antigravity는 명시적 call ID 연결이 있을 때만 처리하며 다음 행이라는 이유로 결과를 연결하지 않는다. 지원하지 않는 native 형식과 Hermes fixture를 자동 대체하지 않는다.

같은 native call의 재수집은 같은 관측 ID를 유지한다. chunk 위치와 파일 안의 순번은 관측 identity에서 제외하고 alias 위치에 남긴다. 반대로 같은 출력이라도 다른 call ID와 provider면 독립 관측이다. 한 세션의 A→B→A 세 호출을 하나의 A로 합치지 않는다. 동일 세대 안에서 같은 call ID가 다른 원래 본문을 갖는다면 원천 게시를 거부한다.

## 기존 source 구조에 연결

새 저장소를 만들지 않는다. 기존 `ToolEvidenceSummaryRecord`의 선택적 불변 JSON을 `tool_evidence_bundle.native_tool_receipts`로 보존한다. native 본문은 일반 recall/embedding body에 복사하지 않는다. 기존 요약은 유지하되 불완전한 native 입력·응답에서 성공 요약을 만들지 않는다. file read 같은 별도 요약 대상이 아닌 항목은 원래 응답을 읽을 수 있다는 참조만 표시한다.

입력은 최대 2048 bytes, 응답은 최대 4096 bytes이며 초과하면 전체 redacted 본문의 hash와 `*_complete=false`를 함께 남긴다. native metadata는 bundle마다 65536 bytes로 제한하며 기존 body 한도와 별도로 나눈다. 같은 receipt를 같은 bundle의 여러 표시 위치에서 참조할 수 있다. source 파일은 최대 256 MiB, 행은 2 MiB, record는 100000개로 제한하고 regular file·symlink 거부·읽기 전후 fd 상태를 검사한다. FIFO도 기다리지 않고 거부한다. 중복 JSON key와 비정상 수치는 허용하지 않는다.

receipt hash와 관측 참조를 bundle coverage에 포함한다. 그래서 요약 body가 같아도 원래 응답이 달라지면 source revision이 달라진다. 기존 불변 snapshot·active predecessor CAS·coverage·projection provenance를 재사용한다. 이전 원문을 덮어쓰지 않는다. 같은 embedding 표현은 재사용할 수 있으나 source의 새 버전 연결은 생략하지 않는다.

내부 `CouchObservationCatalog.read_tool_result`는 project/provider·현재 pinned source revision·extractor/redaction version·native receipt와 observation ref·coverage를 검사한다. 기본값은 완전한 입력·응답만 읽는다. 불완전한 본문을 조사하려면 명시적으로 `require_complete=False`를 선택하며 불완전 표시를 유지한다. 옛 native anchor만 있는 요약을 원래 응답으로 대신 반환하지 않는다. 현재 source 확인은 해당 source snapshot의 확인이며 최신 세계 상태의 인증과 구분한다.

## 실행 경계

서버의 기존 Couch 환경과 승인 manifest를 사용한다. 다음은 command surface 예시이며 운영 실행 기록이 아니다.

```sh
uv run -m agent_knowledge.couchdb_source.migration_cli \
  --provider codex --tool-evidence --native-tool-receipts \
  --source-root 'codex=<approved-server-source-root>' \
  --approval '<private-approval-file>'
```

이 옵션은 `--tool-evidence`와 함께 사용하며 기존 corrective dry-run 방식과 섞지 않는다. 원천 파일이 바뀌거나 일부 쌍이 불완전하면 기존 full-generation 게시의 오류·보류 경로를 사용한다. 이번 변경에서 상주 worker나 운영 source 설정은 바꾸지 않았다. Mac `dendrite`의 locator-only capture·thin shipper 책임도 변경하지 않았다.

## 실제 검증

잘린 응답 앞쪽에 `3 passed`, 보존 한도 뒤에 `1 failed`가 있는 입력에서 성공 요약이 나오는 문제를 새 검사로 재현했다. **1 failed in 0.09s**였으며, 불완전한 응답은 일반 native 관측으로 보존하고 성공으로 요약하지 않도록 수정했다. 이는 개발 중 합성 재현이며 운영에서 발생한 사고라는 주장이 아니다.

source bundle `50de36fd862cdb5cd10ec91f246d5da538b5ba726ceb43f13f201f72b97842b4`에서 native/source/migration·기존 공통 교정·schema·graph·capability 묶음은 Ubuntu 실제 PG·Couch·Neo4j에서 **272 passed, 1 skipped in 148.49s**다. skip은 별도 graph 종료 제어 opt-in 검사다. FIFO 거부 추가 전 source의 결과로 구분한다.

FIFO 거부와 bundle 한도 상수 연결을 추가한 최종 bundle `4b789cdf0ba9c508cb48b2e115dc441c2afc803c96c15045a0cd8315dbade26c`의 native/source/migration 6개 모듈은 실제 Ubuntu Couch·PG에서 **133 passed in 17.81s**다. 5개 provider의 합성 native 구조를 실제 Couch에서 저장/조회했고, 별도 Codex CLI 프로세스의 추출·승인된 작업 전용 source 게시·catalog 재조회도 확인했다. 같은 요약의 다른 응답이 PG representation/벡터를 재사용해도 최신 native body와 source provenance가 구분됨을 확인했다.

검증 준비 과정에서 source 추출 완료 전에 다른 container가 mount 경로를 생성해 한 실행이 준비 단계에서 실패했다. 완료된 source 추출을 확인한 후 순차 실행해 위 결과를 얻었다. 실패 준비 로그는 별도로 보존했으며 테스트 통과로 계산하지 않는다.

이 source의 전체 worker `uv run pytest -q`는 **5338 passed, 743 skipped in 54.74s**다. Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`는 **BUILD SUCCESSFUL**, test task는 UP-TO-DATE였다. `git diff --check`와 새 모듈의 Ruff F 검사는 통과했다. 선택형 skip을 실제 연동 통과로 세지 않는다.

검사는 합성 native 형식 자료를 사용한다. 실제 Couch HTTP·PG·기존 source 생성과 별도 CLI 프로세스는 실행했으며 사용자의 raw private transcript는 읽지 않았다. 벡터 재사용 검사는 synthetic embedder를 사용하므로 실제 embedding provider 증거가 아니다. 이번에 새 OpenRouter 호출을 수행하거나 native 의미 판단의 독립 평가를 통과한 것으로 주장하지 않는다. 기존 Git 판단 profile의 코드 identity는 바꾸지 않았다.

최종 source에서 네트워크 없는 container로 기존 Git v3 profile의 byte hash·코드/질문/모델 identity와 평가 결과를 재검산해 유효함을 확인했다. 정상 19/20·잘못된 허용 0/20은 이전 독립 평가의 결과이며 이번 native 의미 판단 표본이 아니다. CI의 실제 PG job에는 기존 compose와 같은 CouchDB 3.4 service와 위 native 6개 모듈을 추가해 Couch/PG 검사가 환경 미설정 skip으로 끝나지 않도록 했다.

원천 보존/읽기 다음에는 native 근거의 범위·현재성·주장 지지를 검증하고, 수집 전 교정의 근거와 이후 native 관측을 검증된 alias로 합류시켜야 한다. 의미 target 검색·모든 writer·정책/권한 변경 전파·모든 GC retention·실제 embedding·최종 작업/복원 검증도 계속 남아 있다. 운영 활성화·merge·운영 GC는 수행하지 않았다.

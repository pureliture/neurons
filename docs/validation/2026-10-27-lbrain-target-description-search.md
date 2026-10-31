# 설명으로 기존 교정 대상 식별

전체 goal은 진행 중이다. 이 단계는 서버에 이미 등록된 fact/source 정책 범위에서 정확한 ID나 제목을 모르는 요청이 설명으로 기존 기억을 찾도록 한다. 임의의 사실에 대한 source 정책 생성, 미식별 중복의 전체 병합, 일반 native 세계 상태 admission은 완료하지 않았다. B 충돌 자동 숨김 정책도 미결정이다.

## 처리 경계

`fact_correction_config.v1`의 선택적 `target_search`에 검증 profile 경로·hash와 후보 수 한도를 설정한다. 기존 설정과 Python의 기본 2개 반환값은 호환된다. 내장 CLI·MCP·lifecycle·intake는 이 선택적 설정을 전달한다. profile이 없거나 코드·질문·모델 identity가 다르면 자동 대상 판단을 허용하지 않는다.

정확한 ID 또는 제목/summary 일치는 기존 경로를 유지한다. 설명이 다르면 등록 정책에 속한 유일한 기존 카드를 queue의 예약 대상으로만 사용한다. 이 예약은 대상 확인이나 변경 승인이 아니다. 실제 선택 전에는 값을 쓰지 않는다. 예약 시 과거 조회의 old_value가 현재와 다르다는 이유만으로 요청을 버리지 않으며, 원래 요청과 source 검증 결과를 구분한다.

PG의 해당 project active/current 집합을 직접 읽는다. 기본 32개·최대 64개를 넘거나 지원하지 않는 legacy 기록이 있으면 검색 불완전으로 차단한다. Neo4j나 embedding에 아직 투영되지 않았다는 이유로 PG의 기존 대상을 놓치지 않는다. 알려진 alias는 head에 묶으면서 원래 표현을 보존한다. 후보 ID와 현재 content hash를 포함한 전체 inventory를 판단 입력에 결속한다.

서버가 등록 policy/fact scope에서 읽은 project·repository·branch·환경·주체·속성 식별자가 다른 후보는 코드가 제외한다. 제외 사유와 원래 inventory binding도 저장하며 적용 전 다시 계산한다. 입력 LLM이 이 식별자를 직접 제공하거나 바꾸지 않는다. 범위가 불명확한 후보는 제외하지 않고 의미 판단에 남긴다. 같은 정책에 속한다는 사실만으로 자연어 설명과 동일한 대상으로 확정하지 않는다.

선택 모델은 `perplexity/pplx-decider-flash-v1.2`다. 별도 질문 `target_identity.v4`는 현재 값의 참·거짓이 아니라 같은 주체·속성·범위인지 판단한다. 미구현·중지·비활성이라는 값도 그 속성을 묻는 질문과 같은 대상일 수 있다. 구현 여부와 가동 여부·설정·횟수는 서로 다른 속성이다. 원천에서 최신 값을 선택하는 기존 모델 질문과 client를 분리한다. 모델이 제안한 값을 authority에 직접 쓰지 않는다.

같은 사실 확률 0.8 이상인 후보가 정확히 하나이고 나머지도 확실히 다를 때만 선택한다. 불확실·복수 후보는 예약된 자동 재검토를 유지한다. 동일한 입력은 저장한 판단을 재사용한다. 완전한 검색에서 대상이 없으면 신규 기억을 만들지 않고 영속적인 거부 결과를 저장한다. 이 부재는 등록된 정책의 허용 범위에 한정된다.

모델 호출은 authority lock 밖에서 실행한다. 반영 직전에는 queue 소유권과 authority lock 안에서 profile과 전체 inventory를 다시 확인한다. 후보 추가·변경·정책 변경은 이전 판단으로 쓰지 못하게 한다. 선택한 대상은 기존 source fact/원래 예약 대상과도 일치해야 한다. 다른 항목의 source로 고치는 것을 차단한다.

## 보존과 복구

성공과 대상 부재 모두 `lbrain_fact_target_decisions`에 사용한 snapshot·판단 hash·profile·결과를 불변으로 보존한다. task/request/input hash, cached judgment와 terminal fact resolution을 FK와 지연 제약으로 연결한다. 사용 중인 판단의 덮어쓰기·삭제도 막는다. 대상 판단의 전체 inventory는 별도 256 KiB 한도 안에 보존하며 일반 판단의 기존 한도는 유지한다.

대상 부재의 완료 결과와 queue 복구 참조를 같은 transaction에 저장한다. commit 뒤 응답이 유실되어도 source나 profile을 다시 읽거나 새 successor를 만들지 않고 원래 결과로 복구한다. 정상 교정도 기존 공통 resolution의 복구 계약을 따른다.

Git 내용 reader는 파일 전체를 읽는 32 KiB 한도를 사용한다. 기존 8 KiB 뒤의 내용을 잘라 판단하지 않는다. 더 큰 파일·redaction 변형·모델 wire 한도 초과는 차단한다. 임의로 발췌한 부분을 전체 source의 증거로 승격하지 않는다.

## 검증 기록

실제 모델의 개발 자료와 독립 평가를 구분한다. 초기 개발 8건의 두 질문은 각각 잘못된 대상 3건을 선택했고, 후속 질문은 전부 보류하거나 정상 2/3만 선택했다. 이 자료로 profile을 만들지 않았다.

첫 독립 40건은 정상 14/20·잘못된 선택 0건으로 실패했다. 두 번째 독립 40건은 정상 15/20·잘못된 선택 1건으로 실패했다. archive reader와 writer의 출력 폴더를 같은 사실로 오인한 결과도 보존한다. 질문만 조정한 개발 재검사와 코드의 등록 범위 제외를 추가한 개발 재검사도 독립 평가로 재사용하지 않는다. 이전 표본은 hash와 case ID로 모두 제외한다. development 실행 자체도 profile loader가 거부한다.

독립 profile은 정상 최소 20건·거부 최소 20건, 정상 선택률 80% 이상, 잘못된 선택 0건을 요구한다. 거부 표본 중 최소 12건에는 실제 모델의 의미 판단이 있어야 한다. 등록 범위의 코드 거부만으로 모델 검증을 대신하지 않는다. fallback이나 threshold 완화는 사용하지 않는다.

새 독립 v3 40건은 정상 선택 **18/20**, 잘못된 선택 **0건**으로 통과했다. 거부 20건 중 실제 의미 판단을 포함한 것은 13건이다. 총 37개 실제 typed 응답과 15개 등록 범위 code 제외가 있으며 reported cost는 **0.00018856 USD**다. 실제 resolved model은 `perplexity/pplx-decider-flash-v1.2-20261016`이다. plan hash는 `sha256:4951ec8f5a646e8adb913ee41e3c68ff8d0077acf17e35cdf84e97da337a6b63`, private artifact hash는 `sha256:f30e05c8ea5a055841878af0350d39233a45eaad82dd46d68777dd17b8df3a76`다. 정상 2건의 보류도 남겨 두며 완벽한 식별로 보고하지 않는다.

coordinator와 source reader 변경으로 기존 source v5 profile은 유효하지 않다. 별도 새 독립 source v6 40건은 정상 **20/20**, 잘못된 허용 **0/20**, 의미 거부 12건으로 통과했다. reported cost는 **0.00024749 USD**, plan hash는 `sha256:82c46ba829f3d753a6d44ec08b6c9e67e53bf80a4f4de46d432875407f43f76a`, private artifact hash는 `sha256:aba7463ea8e6e93e0a10ffcf4ce5b09075e84c00cce7212fc815d34e0bc727f3`다.

Ubuntu의 실제 PG·CouchDB·Neo4j 13개 모듈은 **222 passed in 187.21s**, skip 0이다. source bundle은 `dab2e972aa380d479761f6e2a0344aa7ac00977f6417b88c7ac26384a6aeb698`다. source/대상 판정 client의 응답 대역을 사용하는 경계 검사와 실제 모델 실증은 구분한다. 후보 변경·완전성·기존 경로·불변 근거·응답 유실·지연 native 합류 등을 포함한다.

실제 연결 검사 초기에 HTTP 교정 1건은 새 표시 template과 seed 카드의 기존 표시 형식이 달라 기존 렌더 범위 guard가 차단했다. seed부터 같은 의미의 template로 등록하도록 합성 fixture를 고친 뒤 실제 source+대상 모델과 HTTP/CLI/PG 교정은 성공했다. guard를 해제하거나 수정 범위를 넓히지 않았다. 다른 속성의 요청 1건은 실제 모델이 `target_identity_uncertain`으로 보류했으며 이 결과를 대상 부재로 바꾸지 않는다. 별도 검사로 보류·재검토 예약·저장된 판단 재사용·authority 무변경을 확인한다. 명백히 무관한 대상의 부재 검사도 별도로 둔다. 실패했던 실행 로그와 수치는 private evidence에 보존한다.

최종 bundle은 `fd3630ce50972af18c93e9eb3a57e16a263362f9e84d52c87a305301db3b06a5`이며 독립 대상 평가 bundle과 `lib/` 전체가 동일함을 Ubuntu에서 확인했다. 실제 모델/HTTP/CLI 11건, 대상·응답 계약과 평가기 경계 46건, 실제 graph 3건을 포함해 **60 passed, 1 skipped in 117.59s**다. skip 1건은 별도 task graph 종료/복구 제어가 필요한 검사다. 이전 graph 장애 검증을 이 실행에서 다시 했다고 계산하지 않는다. 겹치는 앞선 222건과 합산하지 않는다.

새 실제 모델 검사는 MCP HTTP의 서버 관찰→설명 대상 식별→source 값 선택→기존 카드 교정, 불확실 요청의 값 유지·자동 재검토 예약·동일 판단 재사용, 무관한 대상의 완전한 부재와 신규 생성 금지, 8 KiB 뒤 전체 source의 최종 값 보존을 포함한다. 기존 실제 모델의 응답 유실·철회·기한·경쟁·동시성·지연 세션 CLI 재시작 합류도 다시 통과했다. source와 세션은 합성 자료이며 실제 사용자 전체 작업이나 임의 provider SDK·embedding의 실증은 아니다.

최종 로컬 `cd worker && uv run pytest -q`는 **5375 passed, 787 skipped in 56.46s**다. skip을 실제 연동 통과로 세지 않는다. `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`는 **BUILD SUCCESSFUL**, test UP-TO-DATE다. 변경 모듈의 `uv tool run ruff check --select F ...`와 `git diff --check`도 통과했다. CI의 실제 PostgreSQL 필수 step에 대상 검색/응답 계약 모듈을 추가했다. 작업 전용 OpenRouter 임시 인증 파일을 삭제하고 부재를 확인했으며 원본 인증은 변경하지 않았다.

구현 커밋 `29b3a7aacfa36e0c2be8678b40e54ba536d04b4e`의 CI 8개가 모두 성공했다. 실제 job logs는 전체 worker **5375 passed, 787 skipped (110.53s)**, 기존 PG 계약 **782 passed (102.74s)**, schema **8 passed (0.52s)**, 공통 교정/새 대상·응답 계약 **159 passed (75.13s)**, native Couch/PG **162 passed (32.98s)**다. CodeRabbit의 Draft skip은 코드 리뷰 성공으로 세지 않는다.

운영 활성화·merge·운영 GC는 실행하지 않았다. rollback 시 새 대상 검색 설정을 제거하고 기존 exact 경로를 사용할 수 있지만, 저장된 판단·receipt·resolution과 보존 제약은 유지해야 한다.

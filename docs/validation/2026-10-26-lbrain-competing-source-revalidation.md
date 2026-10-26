# 독립 원천의 현재성·충돌·합의 재검토

전체 goal은 진행 중이다. 이 변경은 Git 기반 공통 fact 경로에서 독립 원천을 누락하거나, 확인 불가를 반증으로 취급해 잘못 교정하는 문제를 해결한다. 일반 native 근거 admission, 의미 기반 target 검색·미식별 중복, 정책/parser/권한 변경과 파생 기억 전파, 전체 retention/GC·실제 embedding·최종 운영/복원 검증은 계속 남아 있다. 충돌에서 B를 선택하지 않는다.

## 실제 오류 재현

같은 fact에 대해 두 정책과 두 JSON 파일을 등록했다. 첫 파일은 `ready`, 다른 파일은 `failed`를 지지한다. 초기 처리는 `disputed/ready`로 보류됐다. 두 파일을 바꾸지 않고 `notes.txt`만 commit한 뒤 두 번째 정책의 자동 재검토를 실행했다.

기존 코드는 첫 근거의 commit이 HEAD와 다르다는 이유로 현재 근거에서 제외했다. 그 결과 **`disputed/ready→current/failed`, 새 authority 결정 1건**이 발생했다. source bundle `dc255653d42970f905a86e4573ec49b2ccd64a18168e7df7bc52bf3fab870048`에서 실제 Git·PG 검사 **1 failed in 2.73s**로 확인했다. CI 성공이 이 조합의 정합성을 입증하지 못했다.

후속 검사에서는 실제 파일 내용의 rollback 뒤 과거 근거가 다시 유효해졌는데도 충돌을 발견하지 못하는 문제와, 알려진 다른 카드가 공통 현재 값을 표시하지 못하는 문제도 재현했다. 중간 bundle `f7fb7a37d1daa7bdc116b502d3933581e54e735006716b8adbb64727a15f61c2`에서 **3 passed, 2 failed**였다. 이 실패를 새 통과 결과로 덮어쓰지 않는다.

## 처리 원칙과 구현

commit이 달라졌어도 등록한 파일의 내용·blob·branch·경로·redacted hash가 그대로면 기존 근거는 여전히 현재 내용과 일치한다. 반면 상대 파일이 바뀌었다는 사실만으로 그 파일의 새 값이 무엇인지 판단하지 않는다.

현재성 callback은 `True`(현재 확인), `False`(확인된 대체·소멸), `None`(확인 불가)을 구분한다. 확인 불가는 반증으로 사용하지 않는다. 같은 source와 같은 해석의 새 현재 본문이 확인되면 이전의 다른 본문 assertion을 history로 전환한다. 원래 관측·근거 본문·처리 이력은 유지한다. 단순 도착 순서나 관측 시각으로 값을 선택하지 않는다.

현재 입력과 값이 다른 assertion만 보지 않고, 동일 fact의 다른 등록 정책 전체를 확인한다. 이전에 같은 값을 지지하던 source도 새 본문으로 바뀌었을 수 있기 때문이다. 현재 파일과 일치하는 기존 판단이 없거나 설정/source/profile을 확인할 수 없으면 해당 정책의 내구성 확인 일정을 앞당긴다. 실제 새 값의 JSON 해석 또는 모델 선택은 기존 queue worker에서 수행한다. authority 잠금 안에서 새 모델 판단을 호출하지 않는다.

다른 source를 기다리는 동안 이번 입력의 검증된 assertion은 pending으로 보존한다. 그래서 서로의 새 판단을 기다리는 작업이 교착되지 않는다. 우열을 알 수 없는 실제 충돌은 보류하고, 두 source가 같은 값을 지지하게 되면 자동 재검토가 현재 값을 반영한다. 이미 알려진 충돌은 다른 source를 잠시 읽을 수 없다는 이유로 지우지 않는다.

고정된 과거 snapshot으로 검증을 마친 입력은 `retain_history`로 처리 완료한다. 이 처리와 현재 source의 확인 예약을 구분해, 최신 합의 이후에도 오래된 직접 요청이 대기열에 계속 남는 경로를 막는다. 과거 입력의 완료는 현재 값 승인이나 검증 기한 연장이 아니다. CLI 검사는 현재 값뿐 아니라 등록된 모든 재검토 작업의 `resolved`까지 기다린다.

이번 입력은 판단이 읽은 HEAD를 유지한다. 적용 전후에 그 HEAD를 다시 확인하므로 다른 파일의 commit이 적용 도중 끼어든 경우도 새 snapshot에서 재시도한다. 같은 값의 재확인이나 pending 이력 변경 도중 현재성이 사라져도 검증 시각 연장·이력 변경을 함께 rollback한다. 과거 근거를 유지하기 위한 내용 일치 확인과 이번 입력의 적용 frame을 구분한다.

서버가 동일 fact로 등록한 기존 카드도 공통 head에 연결한다. 다른 title의 알려진 alias가 원래 raw 값을 계속 표시하던 문제를 수정했다. 새 accepted 기억을 생성하는 동작은 추가하지 않았다. 새 값이 채택될 때 다른 현재 근거의 pending 판단도 같은 값을 지지하면 support로 연결한다. 실제로 사용한 근거 assertion을 결과와 `lbrain_fact_corroborations`에 보존하며, 원래 보류 기록을 정리하더라도 나중 결정이 참조한 근거는 FK가 보호한다. 결정이 남아 있는 참조의 삭제·변경을 거부한다.

`context`는 확인 대기 필드를 `pending_fields`, 충돌 필드를 `disputed_fields`로 표시한다. 기한 경과와 source 철회를 같은 상태로 합치지 않는다. 원래 카드·근거·과거 처리 결과를 변경 경위 설명문으로 다시 쓰지 않는다.

## 검증

독립 두 파일의 무관 commit·제3값 변경·확인 불가·실제 rollback, 별도 CLI 프로세스의 자동 합의 해결, 같은 값 재확인 도중 source 변경과 후속 결정의 근거 retention을 검사했다. 코어의 일부 admission callback은 대역이며 coordinator/CLI 검사는 실제 Git과 PG다.

최종 실제 PG·공통 resolution·source lifecycle·검증 기한·writer guard·semantic·schema 묶음은 **101 passed, 1 warning in 112.53s**다. source bundle은 `2b1974df34c9b9ba8f5fe6d1d3f3d09687009aa3aaac4fd7375159b9decef3c2`다. 최초 보류 기록을 제거하는 GC 시도에서도 나중 결정이 참조한 근거 assertion의 삭제가 실제 FK 오류로 중단됨을 확인했다. 모든 GC CLI의 전체 정책을 검증했다는 뜻은 아니다.

과거 입력의 완료 우선순위와 대기열 전체 완료 검사를 추가한 bundle `b3eb457eddd9489c601783d1a089e202b6229f03fdf8f5994ef3a825d720bf56`에서는 실제 PG의 competition/resolution/lifecycle/verification **65 passed, 1 warning in 99.69s**다. 이 후속 검사는 앞선 101건의 재실행 전체와 구분한다.

coordinator 코드가 변경돼 기존 모델 profile은 재사용하지 않았다. 질문·선택 모델·threshold 0.8을 유지한 새 독립 40건에서 **정상 19/20, 잘못된 허용 0/20**, 의미 거부 12건·code gate 거부 8건, 실제 typed 응답 64건을 확인했다. 정상 `h3-14`는 millisecond/second 주장에 동시 지지가 나와 보류됐다. 이 1건을 수정하거나 같은 평가를 재합격시키지 않았다. 기존 v2 40건은 개발 제외 목록에 넣었고 v3도 다음 변경부터 새 독립 자료로 재사용하지 않는다.

새 plan hash는 `sha256:2e84b149ddd064898f0c5629ac040e2aeaf08c3c8c4896dbc32654bc045beba8`, 검증된 profile byte hash는 `sha256:5b9e6ec1d480acc51a961475a15019cb8b0db5e9e646434957f8a98afb830fb2`다. 실제 resolved model은 `perplexity/pplx-decider-flash-v1.2-20261016`, 보고 비용은 USD 0.00025023이다. 이는 제한된 Git 내용 평가의 표본 결과이며 범용 오류율 보장이 아니다. 중간에 코드가 바뀌어 사용하지 않은 최초 freeze plan은 실제 호출 결과로 계산하지 않는다.

새 profile의 실제 OpenRouter·Git·PG·MCP HTTP·CLI 연동은 **5 passed, 1 warning in 61.01s**다. model client 대역 없이 두 원천의 충돌을 유지하고 실제 합의 후 worker가 자동 교정했다. 수동 task 실행·due 시각 변경으로 자동 실행을 대신하지 않았다. 이 5건을 새 독립 평가 40건과 합산하지 않는다. 모델 평가 bundle은 `0e50b857a0758fa0418ab18c5661e1bb1be44cc5cd8e2546fbb9474289f4e1c1`이며, 최종 연동은 위 `2b1974df34c9` bundle에서 실행했다. 의존성 image·읽기 전용 source overlay는 최종 배포 image identity 증거와 다르다.

대기열 전체 완료 검사를 추가한 `b3eb457eddd9` bundle에서도 실제 모델 연동 **5 passed, 1 warning in 62.26s**를 확인했다. 원래 직접 요청과 자동 확인 요청이 모두 완료돼야 검사가 끝난다. 이 재실행을 독립 평가 표본의 추가 건수로 세지 않는다.

기존 PG CI 25모듈·schema·fact graph·capability·native intake/provenance와 새 competition/verification의 실제 PG·Couch·Neo4j 회귀는 **840 passed, 1 skipped in 231.14s**다. bundle은 `2b1974df34c9`이며 마지막 과거 입력 우선순위 변경보다 앞선 결과다. skip은 별도 graph 종료 제어 검사다.

마지막 과거 입력 우선순위 변경 후 worker 전체 `uv run pytest -q`: **5305 passed, 721 skipped, 4 warnings in 55.52s**. Java `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`는 **BUILD SUCCESSFUL**, test task는 UP-TO-DATE였다. `git diff --check`는 통과했다. 환경 선택형 skip을 실제 통과로 계산하지 않는다.

운영 활성화·사용자 기억 변경·merge·운영 GC는 수행하지 않았다. 일시 API 인증 파일은 제거하고 부재를 확인했다. 새 profile의 기존 설정/대기 작업 전환은 별도 정책 lifecycle 작업으로 남아 있다. 사고 시 관리 대상 writer/worker를 중지하거나 호환 경로를 유지하며 fact·근거·판단·참조 이력을 보존한다.

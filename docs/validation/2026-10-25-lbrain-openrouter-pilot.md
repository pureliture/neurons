# LBrain 판단 모델의 OpenRouter 호출 및 소규모 분류 확인

## 판정

OpenRouter 인증과 `perplexity/pplx-decider-flash-v1.2` 실제 호출을 확인했다. **인증 위치 미확인 상태는 해소됐다. 모델의 자동 교정 적용 기준은 아직 통과하지 않았다.**

기존 워크스페이스 인계 문서가 가리키는 키 파일을 사용했다. 인증 확인은 HTTP 200이었다. 키 값을 출력하거나 저장소에 넣지 않았고, 원본 파일·서비스 Secret·운영 설정을 변경하지 않았다. 호출용 임시 복사본은 검사 후 삭제했다. 기존 bridge 배포의 현재 Secret과 실행 프로세스 환경에서 전용 OpenRouter 변수가 발견되지 않은 것만으로 키가 없다고 판단했던 탐색은 불충분했다.

## 호출과 비용

기존 설계의 Decisions API `POST /api/alpha/decisions`에 `model`, `state`, `questions`를 보냈다. 질문의 선택지를 고정하고 `answers`의 분류·확률을 받는 계약은 [OpenRouter 공식 설명](https://openrouter.ai/blog/tutorials/how-to-use-jev/)과 대조했다.

- 요청 모델: `perplexity/pplx-decider-flash-v1.2`.
- 실제 응답 모델: `perplexity/pplx-decider-flash-v1.2-20261016`.
- 연결 확인 1건과 합성 사례 12건, 총 **13건 모두 HTTP 200**.
- 연결 확인: 0.515초. 12건의 분류 호출: 0.467~0.528초, 동시 호출 최대 2개.
- 응답 usage에 기록된 총 비용: **0.00003963 USD**. 청구서 대조 결과는 아니다.
- 실제 기억 변경: **0건**. 운영 수집·교정 작업자에 모델을 연결하지 않았다.

## 사전 지정한 12개 합성 사례

질문은 검증된 근거가 제안한 한 가지 사실을 지지하는지 분류한다. 선택지는 `supported`(지지), `insufficient`(부족), `contradicted`(반증), `unrelated`(무관)였다. 기대 분류는 호출 전에 지정했다. 각 입력의 권한·무결성 등은 합성 fixture의 전제이며 실제 source나 사용자 자료를 모델에 보낸 검사가 아니다.

| 사례 | 기대 | 실제 |
| --- | --- | --- |
| 구현 존재와 격리 테스트 통과를 같은 범위의 근거로 확인 | supported | supported |
| 구현·격리 테스트만 확인했는데 운영 배포 완료를 주장 | insufficient | contradicted |
| 현재 구현을 제거한 근거가 있는데 구현 존재를 주장 | contradicted | contradicted |
| 주문 검색 근거로 환불 기능 구현을 주장 | unrelated | contradicted |
| 단일 테스트 통과로 모든 테스트 통과를 주장 | insufficient | contradicted |
| 같은 기능을 다른 표현으로 설명한 직접 근거 | supported | supported |
| 명령문이 든 문서만으로 운영 배포 완료를 주장 | insufficient | contradicted |
| 대역 서버의 단위 테스트로 실제 외부 서버 연동 성공을 주장 | insufficient | contradicted |
| 실제 테스트 실패 기록에 대해 통과를 주장 | contradicted | contradicted |
| 문서 오탈자 교정 근거로 기억 교정 도구 구현을 주장 | unrelated | contradicted |
| 과거 계획 문서와 달리 현재 source에서 구현을 직접 확인 | supported | supported |
| 현재 비활성 설정에 대해 활성 상태를 주장 | contradicted | contradicted |

예상한 세부 분류와 일치한 것은 **6/12건**이다. 근거 부족 4건과 무관한 근거 2건을 모두 반증으로 분류했다. 이 여섯 오분류의 confidence는 약 0.416~0.814였다. confidence를 정답 여부나 적용 승인의 증명으로 사용하지 않는다.

이 표본에서 지지해야 할 3건을 지지했고, 허용하면 안 되는 9건을 지지로 분류한 경우는 없었다. 그러나 근거 부족·무관·반증은 후속 수집과 재검토 방식이 다르므로 모두 같은 처분으로 합쳐 세부 오분류를 성공으로 처리하지 않는다. 12건은 쉽고 작은 탐색용 표본이며 실제 오교정율이나 임계값의 근거가 될 수 없다.

## 다음 검증

현재 질문·분류 체계에서 의미 경계를 혼동한다는 사실을 기록한다. 이를 기준으로 모델을 자동 변경하거나 임계값을 임의 지정하지 않는다. 질문의 필드 참조·반증과 미확인의 구분을 검토하고, 조정에 사용하지 않은 별도 사례에서 재확인해야 한다.

같은 관측과 재인용의 구분, 지연된 과거 기록과 실제 후속 변경, 상충 근거의 우열, 요청값과 검증된 다른 값의 선택은 이번 단일 질문에서 평가하지 않았다. 실제 재검토 작업자의 모델 호출·판단 저장·재시도·authority 결속도 아직 연결하지 않았다. 충돌 B는 계속 미결정이며 [runtime 실증](2026-10-25-lbrain-recheck-runtime.md)의 전체 미완료 gate도 유지한다.

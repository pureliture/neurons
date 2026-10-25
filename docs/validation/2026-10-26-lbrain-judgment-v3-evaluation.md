# 교정 판단 v3의 조정과 새 독립 평가

선택 모델은 `perplexity/pplx-decider-flash-v1.2`를 유지했고 OpenRouter Decisions를 실제 호출했다. 자동 fallback과 canonical write는 없다. **이번 후보 정책도 미통과이며 활성화하지 않았다.**

## 평가 절차

v2의 calibration 12건과 holdout 16건은 모두 development 28건으로 전환했다. 과거 holdout을 새 독립 평가로 다시 세지 않는다. 질문을 v3으로 바꿔 미확인과 실제 반대 관찰, 제한된 검사 주장과 무제한 보장을 구분했다. 관련성·지지·반증의 세 독립 `noul` 질문은 유지했다.

새 독립 사례 48건은 호출 전에 별도로 작성했다. 정상 16건과 허용하면 안 되는 32건이며, 기존 자료의 ID·근거 문장·주장을 재사용하지 않는다. 후보 평가 기준은 응답 완료 100%, 잘못된 허용 0건, 정상 허용 80% 이상으로 사전 지정했다. 이 숫자는 실험 후보 평가 기준이며 운영 오교정율을 보장하는 배포 승인 기준이 아니다.

development 실호출 28건을 보존하고, 그 응답만 사용해 하나의 공통 threshold를 조정했다. 최초 제한된 탐색에서는 기준을 만족하지 못해 profile을 생성하지 않았다. development 응답을 재사용한 두 번째 탐색에서는 잘못된 허용 0건과 정상 허용 6/7을 만족했다. 이 재사용은 추가 모델 호출이 아니며 독립 증거도 아니다. 정상 허용 기준을 넘는 후보 중 support·relevance 문턱이 더 높고 contradiction 문턱이 더 낮은 정책을 선택했다. 사례별 예외는 없다.

선택된 후보는 relevance 0.90 이상, support 0.80 이상, contradiction 0.30 이하다. 질문·connector·evaluator·새 holdout fixture의 hash와 실제 resolved model을 profile에 고정한 뒤 새 48건을 호출했다. 독립 결과를 보고 같은 자료에 맞춰 threshold를 바꾸지 않았다.

## 실제 결과와 한계

| 구분 | 결과 |
| --- | --- |
| development 실호출 | 28/28 응답 완료 |
| 조정 후보의 정상 허용 | 6/7 |
| 조정 후보의 잘못된 허용 | 0/21 |
| 새 독립 실호출 | 48/48 응답 완료 |
| 독립 정상 허용 | **7/16** |
| 독립 정상 미허용 | **9/16** |
| 독립 잘못된 허용 | **0/32** |
| 독립 후보 기준 | **미통과** |
| 보고된 실제 비용 | development $0.00025051 + holdout $0.00043653 |
| authority write / 운영 활성화 | 0 / 없음 |

현재 branch의 `missing` 확인, 운영 설정의 비활성 확인, 403 거부 결과와 새 accepted 미생성 등 정상적인 부정형 주장에서도 지지 확률과 반증 확률이 동시에 높게 나오는 사례가 있었다. lease/복구 등의 정상 사례는 지지나 관련성에서 보류되기도 했다. 확률은 사례를 정확히 이해했다는 보장이 아니다. 개별 점수를 무시하거나 해당 사례에 예외를 넣어 통과로 처리하지 않는다.

새 holdout fixture SHA-256은 `c7a01a2f1f9b415cfa70d893b8109071a7749b8319d3f4ee0f85b58fdd291a6e`다. 실제 key와 provider 원문 응답은 public repo에 넣지 않는다. private 보고서는 입력·질문·정책·source hash·실제 모델 identity와 비용을 보존한다.

다음 비교에서는 지지/반증의 독립 질문 외에 배타적인 관계 분류를 평가한다. [OpenRouter의 해당 모델 계열 예시](https://openrouter.ai/~perplexity/pplx-decider-flash-latest)는 `noul`과 `choice`의 typed 응답을 지원하는 계약을 설명한다. 이 문서는 모델의 우리 사례 성능을 입증하지 않으며 실제 평가는 별도로 필요하다. 이미 확인한 48건은 다음 조정에서 development로 쓸 수 있지만 다시 독립 확인 자료로 부르지 않는다. 선택 모델을 자동으로 변경하지 않는다.

단위 계약 검사 `uv run pytest -q tests/test_correction_judgment.py tests/test_correction_judgment_v3_eval.py`는 **24 passed**다. 전부 보류·부정확/미완료 development의 profile 생성 거부와 질문/connector 변경 시 holdout 호출 전 차단을 확인했다. 단위 계약 통과를 의미 판단 성능 통과로 세지 않는다.

worker 전체 `uv run pytest -q`는 **5265 passed, 655 skipped, 4 warnings in 54.69s**다. `git diff --check`도 통과했다. 환경 선택형 skip과 source 계약 검사는 모델의 실제 의미 성능으로 계산하지 않는다.

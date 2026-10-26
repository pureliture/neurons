# 배타적 관계 판단 v4의 실제 평가

모델은 합의한 `perplexity/pplx-decider-flash-v1.2`를 유지한다. 독립적인 지지/반증 확률 대신 `supported`, `contradicted`, `insufficient`, `unrelated` 중 하나의 배타적 관계를 묻는 `choice`를 비교했다. **독립 평가의 후보 기준은 아직 미통과이며 실제 교정 authority에 활성화하지 않았다.**

## 구현과 고정

`CorrectionRelationClient`는 source 범위와 근거를 별도 코드가 admission한 뒤 사용하는 typed 연결이다. source binding hash, 정책 version, 질문과 실제 요청을 fingerprint에 결속한다. 확률 전체의 존재·유한성·범위·합, 선택한 항목과 최대 확률의 일치, 지정 모델 identity를 검사한다. 잘못된 JSON·중복 필드·provider 오류·quota·timeout·model 치환은 typed 실패다. 원문이나 key를 결과에 복사하지 않고 자동 fallback도 하지 않는다.

새 질문은 부정형 주장에 같은 부정 상태를 관찰했다면 지지임을 명시했다. 예를 들어 기능이 없다는 주장과 실제 부재 확인은 서로 반대가 아니다. 질문이 이를 설명한다는 사실만으로 모델 성능을 인정하지 않는다.

이미 결과를 확인한 v2 자료 28건과 v3 자료 48건은 모두 development 76건이다. 초기 탐색과 제품 typed client 양쪽에서 실제 호출했고 각각 응답 76건을 확보했다. 정상 23건 중 19건을 후보 허용했고 부적절한 53건의 잘못된 허용은 0건이었다. 세부 관계 label 일치는 54/76이며, 허용되지 않은 label을 실제 반증으로 확정하지 않는다.

v4 후보는 기존 평가기의 세 문턱을 사용해 relevance 0.95, support 0.80, contradiction 0.075로 고정했다. 질문·connector·transport·fitter·evaluator·새 holdout fixture의 hash와 실제 모델 revision을 profile에 저장했다. 그 뒤 development와 문장/ID가 다른 새 40건을 실제 호출했다. 기준은 사전 지정한 정상 허용 80% 이상, 잘못된 허용 0건, 전 응답 완료다.

## 새 독립 결과

| 항목 | 결과 |
| --- | --- |
| 응답 완료 | 40/40 |
| 정상 후보 허용 | **12/16 (75%)** |
| 정상 후보 미허용 | **4/16** |
| 잘못된 후보 허용 | **0/24** |
| 후보 평가 기준 | **미통과** |
| 실제 보고 비용 | typed client development $0.00028825 + holdout $0.00015197 |
| authority write / 운영 활성화 | 0 / 없음 |

미허용된 정상 네 건도 최우선 관계 label은 모두 `supported`였다. 두 건은 support가 0.83 이상인데 contradiction 확률 0.08~0.09가 조정 자료에서 정한 0.075 문턱을 넘었다. 나머지 두 건은 support가 약 0.73과 0.69였다. 결과를 보고 해당 사례의 문턱을 변경하거나 이번 독립 평가를 통과로 다시 표시하지 않는다.

배타적 choice에서는 모든 확률의 합이 1이다. 다음 설계 검토에서는 이 관계에 독립 질문용 relevance/contradiction 문턱을 중복 적용하는 것이 과적합을 만드는지 검토한다. v4의 새 40건은 이후 조정에 사용할 수 있지만 다시 독립 자료로 세지 않는다. 정책을 바꾼다면 별도의 새 자료로 재검증해야 한다.

client·profile identity 및 입력 자료 구분 검사 `uv run pytest -q tests/test_correction_relation_v4_eval.py tests/test_correction_relation_pilot.py tests/test_correction_judgment.py tests/test_correction_judgment_v3_eval.py`는 **39 passed**다. 이것은 protocol/평가 절차 검사이며 실제 의미 판단 기준 통과를 대신하지 않는다. 실제 교정 worker와 공통 queue에 이 의미 경로를 연결하는 작업도 남아 있다.

worker 전체 `uv run pytest -q`는 **5280 passed, 655 skipped, 4 warnings in 55.10s**다. `git diff --check`도 통과했다. 선택형 skip은 실제 연동 증거로 세지 않는다.

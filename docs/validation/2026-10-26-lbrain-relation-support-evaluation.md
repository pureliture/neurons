# 배타적 관계의 단일 지지 정책 평가

배타적 choice 확률에 독립 질문용 relevance/contradiction 문턱을 중복 적용하지 않는 `RelationSupportPolicy`를 구현했다. 모델은 `perplexity/pplx-decider-flash-v1.2`를 유지했다. **새 독립 평가에서 잘못된 허용 한 건이 발생했으므로 아직 미통과이며 authority에 활성화하지 않았다.**

## 정책과 평가 분리

정책은 전체 choice 분포를 다시 검사한 뒤 최우선 관계가 `supported`이고 그 확률이 지정 문턱 이상인 경우에만 후보 허용한다. 근거 binding hash, 입력 hash, 정책, 질문 version과 고정 resolved model이 모두 일치해야 한다. 다른 source나 다른 작업에서 가져온 점수는 사용할 수 없다. 이 코드 자체가 profile을 승인하거나 source의 현재성을 검증하는 것은 아니다.

이미 확인한 이전 76건과 v4 독립 40건은 모두 development 116건으로 전환했다. 같은 질문의 실제 응답을 재사용했으며 추가 모델 호출이나 새 독립 증거로 세지 않는다. 정상 허용 80% 이상·잘못된 허용 0건을 만족하는 가장 높은 단일 문턱은 0.80이었다. 이때 정상 33/39 허용, 잘못된 허용 0/77이었다.

질문·client·transport·정책·평가기·새 fixture hash와 모델 revision을 고정한 뒤, 기존 자료와 별도로 작성한 새 40건을 실제 호출했다. 원래 기준인 전 응답 완료, 정상 허용 80% 이상, 잘못된 허용 0건을 유지했다.

## 새 독립 결과

| 항목 | 결과 |
| --- | --- |
| 실제 응답 | 40/40 |
| 정상 후보 허용 | **18/20 (90%)** |
| 정상 후보 미허용 | **2/20** |
| 잘못된 후보 허용 | **1/20** |
| 후보 기준 | **미통과** |
| 이번 새 호출 비용 | $0.00015145 |
| authority write / 운영 활성화 | 0 / 없음 |

잘못 허용한 `sp-23`은 PG에서 `changed`를 읽었고 graph worker와 graph readback은 실행하지 않은 자료다. 주장은 graph의 현재 저장 값이 `changed`라는 것이었다. 모델은 `supported`에 약 0.849를 부여했다. 이 결과를 통과로 처리하거나 해당 사례만 예외로 두지 않는다.

이 오류는 source가 실제로 관측한 대상과 권위 범위를 코드로 먼저 검증해야 한다는 기존 하이브리드 설계의 필요성을 보여준다. PG 관측은 canonical 상태를, 직접 graph 관측은 graph 저장 상태를 각각 입증한다. 공개 query의 fallback 응답이나 코드 구현만으로 graph 저장을 입증하지 않는다. 실제 source adapter의 관측 capability와 target의 요구를 대조해야 하며, 자연어에 `PG`/`graph`라는 단어가 있는지 검사하는 규칙으로 대체하지 않는다.

현재 이 결정적 capability 연결과 실제 교정 경로의 하이브리드 검증은 남아 있다. 모델 원시 성능의 미통과와 source 검사 이후의 전체 하이브리드 성능을 구분하고, 이후 새 정책은 새 자료에서 확인해야 한다. source adapter나 evaluator의 합성 단위 검사를 실제 연결 검증으로 세지 않는다.

정책·typed 응답·기존 평가기 관련 검사 `uv run pytest -q tests/test_correction_relation_policy.py tests/test_correction_relation_pilot.py tests/test_correction_relation_v4_eval.py`는 **23 passed**다. 부적절한 cached 점수와 전부 보류·불완전 응답의 profile 생성을 거부한다. 단위 검사는 실제 모델의 잘못된 허용을 해소한 증거가 아니다.

## 실제 reader의 capability 연결 부분 검증

후속 구현의 `EvidenceRequirement`는 source가 입증해야 할 종류, context hash, resource hash를 지정한다. 서버의 실제 `PgFactReader`는 기존 fact head에서 읽은 canonical 상태만 발급하고, `Neo4jFactReader`는 같은 대상의 직접 graph episode를 읽은 경우에만 graph 상태를 발급한다. RPC가 보낸 capability 주장은 이 경로의 입력이 아니다. 종류·환경/branch를 포함한 context·대상 resource가 다르면 모델을 호출하기 전에 거부한다. 이름/본문의 단어 검사로 backend를 추정하지 않는다.

`CapabilityCheckedRelation`은 source를 모델 호출 전후에 다시 읽고, source가 바뀌었거나 모델 응답의 입력 결속이 다르면 높은 점수라도 반환하지 않는다. 이 내부 경로는 authority를 쓰지 않는다. 현재 구현 reader는 PG와 Neo4j이며 다른 source 종류의 실제 adapter 연결은 남아 있다.

Ubuntu의 작업 전용 PG·Graphiti/Neo4j에서 실제 canonical 값 변경, pending graph, graph projection과 직접 readback을 실행했다. PG 값을 graph 증거로 쓰려는 시도는 모델 호출 전에 거부됐다. graph가 아직 반영되지 않았으면 unavailable이고, 실제 projection 후에만 직접 graph 근거를 읽었다. adapter가 빈 결과를 반환한 경우 이를 실제 부재나 현재 값으로 인증하지 않는다.

- `test_correction_capabilities.py`, `test_correction_relation_policy.py`, 기존 실제 graph 검사(`-k not real_graph_outage`) → **16 passed, 1 deselected, 1 warning in 7.88s**.
- source bundle SHA-256: `a4be8c5d1734d023b2fff05ede1331e1a23b80eec8fba7a9d686fbcf5582c6b9`. 과거 보조 파일이 섞이지 않도록 새 디렉터리에 풀어 read-only 주입했다.
- 모델 응답은 실패 사례의 높은 점수를 재현한 명시적 대역이며 embedding/reranker도 대역이다. 이 결과를 실제 OpenRouter+교정 authority의 전체 하이브리드 성능 증거로 세지 않는다.
- 기존 의존성 검증 image의 source 주입 방식이며 최종 배포 image identity와 다르다. 운영 DB/서비스는 변경하지 않았다.

일반 source admission, 검증된 하이브리드 profile, common coordinator/authority 연결과 새 독립 전체 검증은 여전히 남아 있다. raw 모델 평가의 잘못된 허용 한 건을 이 부분 검증으로 삭제하거나 통과로 변경하지 않는다.

후속 source capability 변경까지 포함한 worker 전체 `uv run pytest -q`는 **5292 passed, 656 skipped, 4 warnings in 54.54s**다. `git diff --check`도 통과했다. 선택형 skip과 단위 통과를 실제 모델의 의미 성능으로 계산하지 않는다.

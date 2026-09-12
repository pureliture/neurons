# Spec Draft — LBrain Memory Recall 수정 (5버그)

> 상태: **APPROVED** (사용자 승인, 2026-09-12) | 실행 계약: agentic-execution
> 근거: `~/.gemini/antigravity/brain/c044acd3-09db-4d3d-aa6d-54e36c7839a2/lbrain_code_analysis.md` (2026-09-12 코드 레벨 분석)
> 범위 결정: 사용자 — 5개 버그 전부 포함 (Minor 수정안도 이번에 정의)

## 1. 목표

`worker/lib/agent_knowledge/`의 memory recall 경로에서 확인된 5개 결함을 수정하여
`brain_memory_search`가 ledger의 실제 accepted 카드와 graph 사실을 정확한 시간 범위·
일관된 토크나이저 기준으로 반환하게 한다.

## 2. 사용자 결정 기록

| 결정 | 내용 |
|------|------|
| 범위 | 5개 전부 (Critical 1 + Major 1 + Minor 3) |
| `_terms()` 통합 위치 | 공용 모듈 신설: `worker/lib/agent_knowledge/terms.py` |
| stale GraphFact 정책 | null 완전 제외 — `valid_at`/`invalid_at`이 null이면 현재 시점 조회에서 제외 (보수적) |

## 3. 수정 항목 (분석 문서의 사실 기반)

### Fix 1 (Critical) — ledger 시그니처 불일치

- 사실: `brain_read_model.py:31`이 `list_llm_brain_memory_cards(project, accepted_only, limit)`를
  호출하지만 `ledger.py:596`의 실제 시그니처는 `(self, *, project: str)`뿐 → `TypeError` 전파,
  MCP 응답이 `results: []`로 귀결. 이것이 `memory_status.count = 0`의 근본 원인.
- 수정: `ledger.py`의 `list_llm_brain_memory_cards`에
  `accepted_only: bool = False`, `limit: int | None = None` 파라미터 추가.
  `accepted_only=True`일 때 분석 문서의 SQL 조건
  (`lifecycle_state IN ('accepted','human_accepted','auto_accepted')` AND
  `approval_state IN ('approved','auto_accepted')`)을 적용하고 `limit > 0`일 때 LIMIT 절 추가.
- 완료 조건(관찰 가능): 실제 accepted 카드가 존재하는 ledger에서
  `read_model.list_accepted_cards`가 예외 없이 카드 목록을 반환하고,
  `brain_memory_search` 응답의 카드 수가 0보다 크다.

### Fix 2 (Major) — `brain_memory_search` 날짜 파라미터 부재

- 사실: `context.py:171`의 `brain_memory_search`는 `query, project, card_types, limit`만 받는다.
  같은 파일의 `brain_objects_query`(context.py:218)는 `date_from/date_to/as_of`를 지원한다.
  날짜 필터가 없어 graphiti가 의미 유사도만으로 recall하여 오래된 GraphFact가 혼입된다.
- 수정: `brain_memory_search` 시그니처에 `date_from: str = ""`, `date_to: str = ""`(ISO 8601) 추가.
  값이 있으면 `search_context`로 전달하고, `graphiti_adapter`가 해당 파라미터를 받아
  episode/graphfact 조회 시 시간 창 필터로 적용한다 (`date_from`은 "이 시각 이후" 필터 —
  `reference_time`만으로는 불충분하므로 adapter 측 후보 필터링에 반영).
- 완료 조건: `date_from`/`date_to`를 지정한 쿼리에서 지정 기간 밖 에피소드/사실이 결과에 포함되지 않는다.

### Fix 3 (Minor) — `retrieve_episodes` ×5 fan-out 축소

- 사실: `graphiti_adapter.py:512`의 `last_n=max(bounded * 5, bounded)` — limit=8이면 40개 후보를
  풀로 가져오고, `_matches` 게이트만 유일한 필터. 약한 terms면 대부분 통과.
- 수정 (이번에 정의): fan-out 배율을 설정 상수로 추출하고 기본값을 1.0으로 축소
  (`last_n = bounded`). `_matches` 게이트는 유지하되, 게이트 통과 실패로 결과가 부족할 때만
  (bounded 미달 시) 1회 재조회로 후보 풀을 확장하는 형태로 제한한다.
- 완료 조건: limit=8 쿼리 시 graphiti 후보 조회 수가 8을 초과하지 않는다 (부족 시 1회 확장 제외).

### Fix 4 (Minor) — `_terms()` 이중 구현 통합

- 사실: `context.py:1776`은 `re.split(r"[^a-zA-Z0-9_가-힣]+")` + 길이≥3,
  `graphiti_adapter.py:1364`는 `str.split()`(공백만) + 길이≥3. 영어 복합어·구두점 혼재 쿼리에서
  필터 결과가 달라진다.
- 수정 (이번에 정의, 사용자 결정): 공용 모듈 `worker/lib/agent_knowledge/terms.py`를 신설하고
  한글 정규식 포함 버전(SoT)을 이식. `context.py`와 `graphiti_adapter.py` 모두 이 모듈을
  import하며 로컬 구현은 제거한다.
- 완료 조건: 두 호출처의 `_terms()`(또는 동일 기능)가 동일 모듈을 사용하며,
  구두점 혼재 영어 쿼리에서 두 필터가 동일 토큰 집합을 생성한다.

### Fix 5 (Minor) — stale GraphFact valid_to null 허용 제거

- 사실: `graphiti_adapter.py:483-488`의 `SearchFilters`에서 `valid_at`/`invalid_at` 모두
  `is_null` OR 조건을 허용 → `valid_to=""`인 2026-06-24 이전 사실들이
  `currentness: current`로 계속 반환.
- 수정 (이번에 정의, 사용자 결정: null 완전 제외): 현재 시점 조회에서는
  `valid_at`이 null이거나 `invalid_at`이 null인 사실을 모두 제외 —
  `is_null` OR 브랜치를 제거하고 엄격한 날짜 비교만 남긴다.
- 완료 조건: `valid_to`가 비어 있는 과거 사실이 `currentness: current`로 반환되지 않는다.

## 4. 경계

- 테스트/설치는 repo 규칙을 따른다: worker 검증은 `cd worker && uv run pytest -q`.
- Public repo에 운영값·raw ledger·raw transcript·raw `dataset_id`/`document_id`를 노출하지 않는다.
- External CLI 실행은 응답 포맷 유지 같은 scope 밖 인터페이스 호환성을 깨지 않는다
  (`brain_memory_search`에 추가되는 파라미터는 기본값 빈 문자열 — 기존 호출자 호환 유지).
- Liquibase/migration이 필요하면 DDL 변경이므로 별도 승인 대상 (본 spec 범위 밖).

## 5. 검증 계획 (관찰 가능 완료 조건)

1. `cd worker && uv run pytest -q` — 전체 green.
2. Fix 1: accepted 카드 fixture가 있는 ledger에서 `list_accepted_cards`가 카드를 반환
   (`accepted_only`/`limit` 전달 시 TypeError 부재 확인).
3. Fix 2: 과거 기간 지정 조회에서 기간 밖 사실 제외 확인.
4. Fix 3: fan-out 후보 수 상한 어설션.
5. Fix 4: 신설 `terms.py` 단위 테스트 + 두 호출처가 공용 모듈 사용 확인.
6. Fix 5: null valid_at/invalid_at 사실이 current 조회에서 제외되는 테스트.

---

**이 spec draft는 해당 문서의 승인을 대기 중입니다.** 승인 시 이 문서만 approved로 표기되며,
다른 문서의 승인이나 구현 착수는 열리지 않습니다.

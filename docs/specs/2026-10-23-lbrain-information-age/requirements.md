# LBrain 정보 나이(Age) 요구사항

## 문서 정보

| 항목 | 값 |
| --- | --- |
| 상태 | Draft — 기능 정의 (사용자 검토 대기) |
| 작성일 | 2026-10-23 |
| 범위 | 기능, 행동, 응답 계약, 판정 규칙 |
| 비범위 | 스키마·코드·배포·마이그레이션 구현 방식 |
| 관련 문서 | `docs/specs/2026-07-05-lbrain-knowledge-object-substrate/design.md`, `docs/contracts/brain-steward-mcp.md`, `milestones.md` (M5b/M5c/M7) |

이 문서는 **"LBrain이 정보의 나이를 어떻게 정의하고, 조회 결과에서 어떻게 알려야 하는가"**를 기능 관점에서 확정하기 위한 요구사항이다. 구현 방법(테이블, 함수, 투영 워커, 쿼리)은 이 문서의 범위가 아니다.

---

## 1. 한 문장 정의

> LBrain은 모든 지식 항목에 대해 **"언제 관측되었고, 언제 기록되었고, 언제부터 언제까지 권위가 있었으며, 지금 얼마나 지났는지"**를 함께 반환해야 한다. 사용자는 그 정보만으로 **최신 정보인지, 과거 정보인지, 판단할 수 없을 정도로 시간이 불명확한지**를 구분할 수 있어야 한다.

---

## 2. 문제 정의

### 2.1 사용자 문제

Neurons 프로젝트에는 파일, 문서, 세션, 카드, 런타임 사실이 매우 많다. 그중 상당수는 더 이상 최신이 아니다. 하지만 사용자는 매 조회마다 전체 저장소를 다시 분석해 "이게 최신인가"를 판정할 수 없다.

그래서 LBrain이 회수 결과를 돌려줄 때 **정보의 나이와 유효성**을 함께 알려줘야 한다.

### 2.2 현재 관측된 실패

- 조회 결과에 `observed_at`, `valid_from`, `valid_to`, `age` 같은 시간 정보가 없다.
- 카드의 `created_at`은 **적재 시각**이라, 오래된 세션을 나중에 적재하면 최신처럼 보인다.
- `valid_to`가 사실상 사용되지 않아, 폐기된 항목도 만료되지 않는다.
- 그래프 조회 경로는 카드의 **투영 시각**을 관측 시각으로 사용한다. 그래서 오래된 카드도 "방금 생성됨"으로 보인다.
- stale/superseded 전환이 그래프 조회 결과에 반영되지 않아, PG에서 폐기해도 그래프는 계속 current로 답할 수 있다.
- task 카드는 참조 대상이 사라져도 `in_progress`로 남는다.

대표 사례:

- "Qdrant가 아직 쓰이나?"라는 질문에 LBrain이 **폐기 이전 준비 카드**를 current로 반환하고, 삭제된 파일을 대상으로 한 task를 `in_progress`로 반환한다.
- 반면 실제 상태는 "런타임 폐기 완료, 코드 표면 일부 비활성 잔존"이다. 이 차이를 LBrain 응답만으로 판정할 수 없다.

### 2.3 왜 나이만으로는 부족한가

나이가 많다고 해서 항상 틀린 것은 아니다. 오래된 아키텍처 결정이 지금도 유효할 수 있다. 반대로 방금 적재된 문서가 이미 폐기된 내용일 수 있다.

따라서 **나이(age)** 는 "얼마나 오래되었는가"만 말하고, **현재성(currentness)** 과 **유효기간(validity)** 은 별도로 표현해야 한다.

---

## 3. 목표와 비목표

### 3.1 목표

1. 모든 회수 결과가 시간 메타데이터를 포함한다.
2. 사용자가 **관측 나이**, **적재 지연**, **권위 유효 구간**을 구분할 수 있다.
3. 기본 조회는 현재 유효한 정보를 우선하되, 과거 정보가 필요할 때는 명시적으로 조회할 수 있다.
4. `as_of`(기준 시각) 조회로 "그 시점에는 무엇이 유효했는가"를 답할 수 있다.
5. 시간 정보가 없거나 이상하면 **"모른다"**고 말한다. 현재 시각으로 추정하지 않는다.
6. 그래프 조회와 PG fallback이 같은 시간 의미를 반환한다.
7. 나이는 **판정의 근거**로만 쓰고, 나이만으로 자동 stale/supersede하지 않는다.

### 3.2 비목표

- 정보의 진위를 자동으로 판정하는 기능 (나이 ≠ 정답)
- stale/supersede/retire를 자동으로 확정하는 기능 (별도 correction 요구사항)
- 코드 심볼의 폐기 여부를 자동 분류하는 기능 (별도 인벤토리 요구사항)
- 보존 기간(retention)과 삭제 정책
- 임베딩 모델·본문 압축·검색 품질 변경
- 시계열 대시보드나 통계 제품
- Qdrant 제거 (테스트 표본으로 유지)

---

## 4. 용어와 시간 축

### 4.1 네 개의 시간

LBrain은 하나의 "시간"으로 모든 것을 설명하지 않는다. 최소 네 개의 시간 축을 구분한다.

| 용어 | 식별자 | 의미 | 예 |
| --- | --- | --- | --- |
| 관측 시각 | `observed_at` | 사실이나 사건이 **현실에서 관측된** 시각 | 세션 시작/종료, 문서 커밋 시각, 런타임 점검 시각 |
| 기록 시각 | `recorded_at` | LBrain이 그 사실을 **처음 적재/물질화한** 시각 | backfill로 어제 적재한 3개월 전 세션 |
| 권위 시작 | `valid_from` | 그 사실이 **권위 있는 현재 정보로 인정된** 시각 | 카드 승인 시각, 결정 확정 시각 |
| 권위 종료 | `valid_to` | 그 사실이 **더 이상 현재 권위가 아닌** 시각 | supersede/stale/retire 확정 시각 |

추가 파생 개념:

| 용어 | 의미 |
| --- | --- |
| `age` | 기준 시각(`as_of`)과 `observed_at`의 차이. **정보의 나이** |
| `ingestion_lag` | `recorded_at - observed_at`. **적재 지연** |
| `authority_age` | `as_of - valid_from`. **권위가 시작된 뒤 얼마나 지났는가** |
| `freshness` | 소스가 마지막으로 검증된 상태와 다음 검증 예정 시각 |
| `currentness` | 현재 권위 상태 (`current`, `stale`, `superseded`, `conflicted`, `unknown`, `retired`) |
| `time_confidence` | 시간 값의 신뢰도 (`high`, `medium`, `low`, `anomalous`, `unknown`) |
| `as_of` | 조회의 기준 시각. 기본값은 현재 |

### 4.2 age / freshness / currentness는 서로 다르다

| 개념 | 질문 | 예 |
| --- | --- | --- |
| `age` | "이 정보는 얼마나 오래되었나?" | 92일 전에 관측됨 |
| `freshness` | "소스를 마지막으로 검증한 게 언제인가?" | 30일 전 검증, 다음 검증 7일 후 |
| `currentness` | "지금 권위 있는 정보인가?" | `current` / `superseded` / `stale` |
| `validity` | "언제부터 언제까지 유효했나?" | `valid_from` ~ `valid_to` |

**규칙:** 나이가 많다는 이유만으로 `currentness`를 바꾸지 않는다. `freshness`가 오래되었다는 이유만으로도 바꾸지 않는다. 상태 전환은 별도의 권위 결정으로만 일어난다.

### 4.3 age는 증거이지 판정이 아니다

LBrain은 "이 정보는 오래되었으니 틀렸다"고 말하지 않는다. 대신 다음을 함께 반환한다.

- 얼마나 오래되었는가
- 현재 권위 상태는 무엇인가
- 유효기간이 언제 끝났는가/끝나지 않았는가
- 마지막 검증은 언제였는가
- 어떤 근거로 그렇게 판정했는가

사용자(또는 상위 에이전트)가 이 정보를 조합해 최신/과거를 판단한다.

---

## 5. 행위자와 사용 시나리오

### 5.1 행위자

| 행위자 | 역할 |
| --- | --- |
| 질의 에이전트 | LBrain을 조회해 작업에 필요한 정보를 얻는다. 나이를 보고 최신/과거를 판단한다. |
| 사람 운영자 | 오래된 정보가 여전히 유효한지, 폐기되었는지 검토한다. |
| LBrain steward | 권위 상태를 승인/폐기/교체한다. |
| 수집·투영 시스템 | 세션·문서·런타임 사실을 적재하고 시간 메타데이터를 보존한다. |
| 하위 소비자 | MCP 도구, object pack, 문서 생성기가 나이 정보를 사용한다. |

### 5.2 핵심 시나리오

**S1. 폐기 여부 확인**

> "Qdrant 아직 쓰고 있어?"

기대: 폐기 결정 카드가 반환된다. 카드에는 `observed_at`(결정 시점), `valid_from`, `valid_to`(있는 경우), `currentness=superseded|retired`, `superseded_by`, `age`가 포함된다. 폐기 이전 준비 카드는 current로 반환되지 않고, historical로 명시된다.

**S2. 문서 최신성 확인**

> "이 설계 문서 최신이야?"

기대: 문서의 `observed_at`(커밋 시각), `recorded_at`(적재 시각), `age`, `freshness.checked_at`, `source_hash` 검증 여부, `gaps`가 반환된다.

**S3. 과거 시점 조회**

> "2026년 8월 기준으로는 어떤 구조였어?"

기대: `as_of=2026-08-01` 조회에서 그 시점에 `valid_from <= as_of < valid_to`였던 항목이 반환된다. 현재 폐기된 항목도 그 시점에는 current로 표시된다. 응답에 `temporal_view=as_of`가 명시된다.

**S4. 오래되었지만 유효한 결정**

> "3개월 전 결정인데 아직 따라야 해?"

기대: `age=old`, `currentness=current`, `valid_to=null`, `freshness=unchecked`가 함께 반환된다. LBrain은 "오래됨"과 "폐기됨"을 혼동하지 않는다. `recommended_action=use_with_verification`이 제안된다.

**S5. 오래된 세션을 나중에 적재**

> "방금 적재된 세션인데 내용은 1년 전이야."

기대: `observed_at`은 1년 전, `recorded_at`은 방금, `age`는 1년, `ingestion_lag`는 1년으로 표시된다. "방금"이라고 표시하지 않는다.

**S6. 시간 정보 누락**

> "이 항목은 언제 것인지 모르겠어."

기대: `age=unknown`, `time_confidence=unknown`, `reason=observed_at_missing`이 반환된다. 현재 시각으로 추정하지 않는다.

**S7. 미래 시각/시계 오차**

> "관측 시각이 현재보다 미래로 찍혀 있어."

기대: 허용 오차(기본 ±5분)를 넘으면 `time_confidence=anomalous`, `temporal_relation=future`로 표시하고, 최신 항목처럼 최상위 정렬하지 않는다.

**S8. 현재와 과거가 충돌**

> "최신 카드와 예전 카드가 서로 다른 말을 해."

기대: 둘 다 반환하되, current와 historical을 명확히 구분하고 `temporal_conflict=true`, `recommended_action=review_conflict`를 표시한다. 조용히 하나를 고르지 않는다.

---

## 6. 기능 요구사항

식별자는 `FR-AGE-NNN` 형식을 사용한다. "반드시"는 필수 요구사항이다.

### 6.1 시간 메타데이터 노출

- **FR-AGE-001 (필수)** 모든 지식 항목(카드, 세션 청크, 문서, 그래프 사실, object pack 항목)은 다음 시간 메타데이터를 포함해야 한다.
  - `observed_at`
  - `recorded_at`
  - `valid_from`
  - `valid_to`
  - `superseded_by` (해당 시)
  - `age`
  - `currentness`
  - `freshness`
  - `time_confidence`
- **FR-AGE-002 (필수)** 시간 메타데이터는 항목 단위로 제공되어야 한다. 목록 전체의 요약만 제공해서는 안 된다.
- **FR-AGE-003 (필수)** 시간 값이 없는 경우 필드를 생략하지 말고 `null` 또는 `unknown`으로 명시해야 한다. 침묵은 "최신"으로 해석될 수 있으므로 금지한다.
- **FR-AGE-004 (필수)** 모든 시간 값은 UTC ISO-8601로 제공하고, 필요 시 사용자 로컬 시간을 함께 표시할 수 있어야 한다.

### 6.2 나이 계산

- **FR-AGE-010 (필수)** `age`는 `as_of - observed_at`으로 계산한다. 기본 `as_of`는 조회 시각이다.
- **FR-AGE-011 (필수)** `observed_at`이 없으면 다음 순서로 대체하되, 사용한 시계를 `age.clock`에 명시해야 한다.
  1. `reference_time` (소스가 제공한 사건 기준 시각)
  2. `recorded_at` (적재 시각) — 이 경우 `time_confidence=low`
  3. 없음 — `age=unknown`
- **FR-AGE-012 (필수)** `recorded_at`을 `observed_at`의 정상값처럼 사용해서는 안 된다. 적재 시각밖에 없으면 "나이를 모른다"에 준하는 낮은 신뢰도로 표시한다.
- **FR-AGE-013 (필수)** `age`는 음수가 될 수 있다. 음수는 미래 시각이며 `temporal_relation=future`로 분류한다.
- **FR-AGE-014 (필수)** `age`는 초 단위 정확값과 사람이 읽는 버킷을 함께 제공한다.
  - 기본 버킷: `fresh` (≤24시간), `recent` (≤7일), `aging` (≤30일), `old` (≤180일), `historical` (>180일), `unknown`
  - 버킷 경계는 정책으로 변경 가능해야 한다.
- **FR-AGE-015 (필수)** 나이 버킷은 **나이만** 나타낸다. `currentness`나 `freshness`를 대신하지 않는다.
- **FR-AGE-016 (필수)** `age`는 조회 시점에 계산되어야 한다. 저장된 나이 값을 그대로 반환해 조회 시점과 어긋나게 해서는 안 된다.

### 6.3 기본 조회와 `as_of` 조회

- **FR-AGE-020 (필수)** 기본 조회(`as_of=now`)는 `currentness=current`이고 `valid_to`가 없거나 미래인 항목을 **현재 레인**으로 반환한다.
- **FR-AGE-021 (필수)** `stale`, `superseded`, `retired` 항목과 `valid_to`가 과거인 항목은 **과거 레인**으로 분류한다.
- **FR-AGE-022 (필수)** 기본 조회에서 현재 레인이 비어 있으면 과거 레인을 `fallback_reason=no_current_match`와 함께 반환할 수 있다. 이때도 과거 항목을 current로 표시해서는 안 된다.
- **FR-AGE-023 (필수)** 사용자가 `include_historical=true`를 지정하면 과거 레인을 포함할 수 있다.
- **FR-AGE-024 (필수)** `as_of` 조회는 `valid_from <= as_of`이고 (`valid_to`가 없거나 `as_of < valid_to`)인 항목을 그 시점의 현재 레인으로 반환해야 한다.
- **FR-AGE-025 (필수)** `as_of` 조회 결과에는 `temporal_view=as_of`와 기준 시각이 명시되어야 한다.
- **FR-AGE-026 (필수)** `as_of`가 현재보다 미래인 경우, 허용 오차(기본 5분)를 넘으면 `invalid_as_of_future`로 거부해야 한다. 테스트/평가 목적의 명시적 우회는 별도 플래그로만 허용한다.
- **FR-AGE-027 (필수)** `as_of`는 모든 읽기 표면(`brain.resolve`, 검색, object pack)에서 동일한 의미로 동작해야 한다.
- **FR-AGE-028 (필수)** 현재 항목과 과거 항목이 동시에 관련되면, 기본 조회에서도 **충돌 사실을 숨기지 않는다.** 최소한 `temporal_conflict=true`와 대체/폐기 관계를 표시해야 한다.

### 6.4 현재성·수명주기와의 분리

- **FR-AGE-030 (필수)** `observed_at`과 `valid_from`은 다른 값이어야 한다. 승인/확정은 `valid_from`을 설정하며, `observed_at`을 덮어쓰지 않는다.
- **FR-AGE-031 (필수)** `supersede`, `stale`, `retire` 전환은 대상 항목의 `valid_to`를 닫아야 한다. 단, `valid_to`가 없는 "계속 유효" 상태와 "아직 모름"을 구분해야 한다.
- **FR-AGE-032 (필수)** `superseded`는 대체 항목이 있는 상태, `stale`은 대체 없이 신뢰가 철회된 상태, `retired`는 의도적으로 더 이상 적용되지 않는 상태로 구분한다.
- **FR-AGE-033 (필수)** `retired`는 `stale`/`superseded`와 구분되어야 한다. 폐기된 기능/백엔드처럼 "대체가 있든 없든 더 이상 현재가 아닌" 상태를 표현할 수 있어야 한다.
- **FR-AGE-034 (필수)** task 항목은 `observed_at`(마지막 상태 관측), `valid_to`(종료/폐기), `next_check_after`(다음 확인 예정)를 가져야 한다.
- **FR-AGE-035 (필수)** task의 참조 대상이 사라졌다는 사실은 **나이만으로 추론하지 않는다.** 별도의 검증 결과(`source_missing` 등)로 표시하고, `in_progress`를 자동으로 `obsolete`로 바꾸지 않는다. 상태 전환은 correction/authority 절차를 따른다.
- **FR-AGE-036 (필수)** `currentness`가 `unknown`인 항목은 `current`로 간주해서 정렬하거나 표시해서는 안 된다.

### 6.5 신선도(freshness)와 검증

- **FR-AGE-040 (필수)** `freshness`는 `age`와 독립적으로 제공되어야 하며 다음을 포함한다.
  - `state`: `verified_current`, `verified_changed`, `unchecked`, `check_due`, `unverifiable`, `unknown`
  - `checked_at`
  - `next_check_after`
  - `gaps[]`
- **FR-AGE-041 (필수)** 신선도 검증 이력이 없으면 `state=unchecked`와 `gap=no_recent_freshness_check`를 반환해야 한다. 검증된 것처럼 표시해서는 안 된다.
- **FR-AGE-042 (필수)** 문서/코드의 신선도 검증은 소스 해시·커밋·경로 존재 여부 같은 결정론적 근거를 사용해야 한다.
- **FR-AGE-043 (필수)** 런타임 사실의 신선도 검증은 마지막 런타임 점검 시각을 사용해야 한다.
- **FR-AGE-044 (필수)** `next_check_after`가 지난 항목은 `check_due`로 표시하되, 그것만으로 `currentness`를 바꾸지 않는다.
- **FR-AGE-045 (필수)** `freshness`가 `verified_changed`이면 `gap=source_changed`를 포함하고, `recommended_action=verify_or_supersede`를 제안해야 한다.

### 6.6 정렬·필터·페이지네이션

- **FR-AGE-050 (필수)** 일반 조회는 관련도(relevance)를 우선하되, 현재 레인과 과거 레인을 섞어 정렬해서는 안 된다.
- **FR-AGE-051 (필수)** 시간 중심 조회(`temporal_work_recall` 등)는 `observed_at` 내림차순을 기본으로 하되, `currentness`가 current인 항목을 우선한다.
- **FR-AGE-052 (필수)** `observed_at`이 같으면 안정적인 2차 정렬 키를 사용해야 한다.
- **FR-AGE-053 (필수)** `age` 버킷, `currentness`, `time_confidence`로 필터링할 수 있어야 한다.
- **FR-AGE-054 (필수)** `max_age`, `min_age` 같은 나이 범위 필터를 지원해야 한다.
- **FR-AGE-055 (필수)** 페이지네이션은 `observed_at` 정렬에서 stable해야 한다. 같은 `as_of`로 다음 페이지를 요청했을 때 항목이 중복/누락되지 않아야 한다.
- **FR-AGE-056 (필수)** 정렬이나 필터 때문에 `current` 항목이 `historical` 항목보다 항상 위에 온다고 가정해서는 안 된다. 응답의 lane 표시가 정렬보다 우선한다.

### 6.7 일관성과 커버리지

- **FR-AGE-060 (필수)** 동일 항목에 대해 그래프 조회 경로와 PG fallback이 반환하는 시간 의미가 일치해야 한다. 허용 오차(기본 1초)를 넘으면 `temporal_conflict=true`로 표시하고 PG authority를 우선한다.
- **FR-AGE-061 (필수)** 그래프 투영 지연이 있더라도 `age`는 소스의 `observed_at`으로 계산되어야 한다. 투영 시각을 관측 시각으로 사용해서는 안 된다.
- **FR-AGE-062 (필수)** 응답에는 현재 `temporal_projection_lag`(조회 경로의 투영 지연)를 포함해, 사용자가 "지금 보는 그래프가 얼마나 뒤처졌는지" 알 수 있어야 한다.
- **FR-AGE-063 (필수)** backfill/import로 들어온 오래된 세션은 원래 `observed_at`을 보존해야 한다. 적재 시각으로 재설정해서는 안 된다.
- **FR-AGE-064 (필수)** 시간 메타데이터가 누락된 항목의 비율을 커버리지 지표로 제공해야 한다. 누락을 조용히 현재 시각으로 대체해서는 안 된다.
- **FR-AGE-065 (필수)** 그래프와 PG의 `currentness`/`valid_to`가 다르면 PG authority를 기준으로 응답하고, `temporal_conflict`와 `authority_source=postgres`를 표시해야 한다.
- **FR-AGE-066 (필수)** 폐기/교체 전환이 발생하면 다음 조회에서 그 사실이 반영되어야 한다. "반영 예정" 상태를 응답에 표시할 수 있어야 한다.

### 6.8 누락·이상 시간 처리

- **FR-AGE-070 (필수)** `observed_at` 누락 시 `age=unknown`, `time_confidence=unknown`, `age.reason=observed_at_missing`을 반환한다.
- **FR-AGE-071 (필수)** `observed_at`이 `recorded_at`보다 미래인 경우 `time_confidence=anomalous`, `age.reason=observed_after_recorded`를 반환한다.
- **FR-AGE-072 (필수)** `valid_from > valid_to`인 경우 `time_confidence=anomalous`, `age.reason=invalid_validity_window`를 반환한다.
- **FR-AGE-073 (필수)** 타임존이 없는 입력은 UTC로 해석하되 `time_confidence=medium`으로 낮춘다.
- **FR-AGE-074 (필수)** 서로 다른 소스의 시계 오차는 허용 오차(기본 ±5분) 내에서만 동일 시점으로 취급한다.
- **FR-AGE-075 (필수)** 시간 이상 항목은 최신 항목처럼 최상위로 정렬하지 않는다.

### 6.9 개인정보·보안

- **FR-AGE-080 (필수)** 시간 메타데이터에는 raw session ID, 문서 본문, 경로, 자격 증명, 해시 원문을 포함해서는 안 된다.
- **FR-AGE-081 (필수)** 시간 정보는 활동 패턴을 드러낼 수 있으므로 프로젝트/권한 범위를 넘어 노출해서는 안 된다.
- **FR-AGE-082 (필수)** 삭제되었거나 접근 권한이 없는 항목의 존재를 시간 정보로 추론할 수 없어야 한다.
- **FR-AGE-083 (필수)** 응답의 시간 값은 공개 안전(public-safe) 형식이어야 하며, 내부 식별자는 노출하지 않는다.

### 6.10 응답 표현

- **FR-AGE-090 (필수)** 각 항목은 기계 판독 가능한 `age.seconds`와 사람이 읽는 `age.label`을 함께 제공해야 한다.
- **FR-AGE-091 (필수)** 각 항목은 `currentness`, `lifecycle_status`, `freshness.state`를 명시해야 한다.
- **FR-AGE-092 (필수)** "오래되었지만 현재 유효"와 "최근이지만 폐기됨"이 한눈에 구분되어야 한다.
- **FR-AGE-093 (필수)** 시간 정보가 불명확하면 "unknown"이라고 명시하고, 가능한 이유를 `reason_codes`로 제공해야 한다.
- **FR-AGE-094 (필수)** 과도한 정밀도를 피한다. 사람이 읽는 표현은 버킷+대략값(예: "약 3개월 전")을 사용하고, 정확값은 기계 필드로 제공한다.
- **FR-AGE-095 (필수)** 응답에는 항목별 한 줄 시간 요약이 포함되어야 한다. 예: "관측 92일 전 · 현재 유효 · 신선도 미검증".
- **FR-AGE-096 (필수)** `recommended_action`은 나이/현재성/신선도를 종합해 제안되어야 하며, 최소 다음 값을 지원한다.
  - `use`
  - `use_with_verification`
  - `prefer_replacement`
  - `treat_as_historical`
  - `review_conflict`
  - `ask_user`
  - `do_not_use`

---

## 7. 판정 규칙

### 7.1 응답 라벨 결정표

| 나이 | currentness | valid_to | freshness | 표시 | 권장 행동 |
| --- | --- | --- | --- | --- | --- |
| young | current | 없음/미래 | verified_current | 최신·검증됨 | `use` |
| old | current | 없음/미래 | unchecked | 오래되었지만 현재 유효 | `use_with_verification` |
| any | current | 없음/미래 | verified_changed | 현재 권위이나 소스 변경됨 | `verify_or_supersede` |
| any | superseded | 과거 | any | 폐기됨·대체 있음 | `prefer_replacement` |
| any | stale | 과거 | any | 신뢰 철회됨 | `treat_as_historical` |
| any | retired | 과거/없음 | any | 의도적으로 폐기됨 | `prefer_replacement` 또는 `treat_as_historical` |
| young | superseded | 과거 | any | 최근이지만 이미 폐기됨 | `prefer_replacement` |
| old | current | 없음/미래 | check_due | 오래됨·재검증 필요 | `use_with_verification` |
| unknown | current | 없음/미래 | any | 현재 권위이나 나이 불명 | `ask_user` |
| unknown | unknown | any | any | 시간/권위 불명 | `ask_user` |
| future | any | any | any | 시간 이상 | `ask_user` / `do_not_use` |
| any | conflicted | any | any | 현재 항목 간 충돌 | `review_conflict` |

### 7.2 기본 조회 레인

| 레인 | 조건 | 기본 포함 |
| --- | --- | --- |
| current | `currentness=current` + 유효기간 내 | 예 |
| historical | `stale`/`superseded`/`retired` 또는 `valid_to` 경과 | 아니오 (현재 결과 없음 또는 명시 요청 시) |
| unknown | `currentness=unknown` 또는 시간 불명 | 아니오 (명시 요청 시) |
| conflict | 동시에 유효한 상충 항목 | 예 (숨기지 않음) |

### 7.3 `as_of` 판정

어떤 항목이 시점 `T`에 유효한 현재 정보이려면:

1. `valid_from <= T`
2. `valid_to`가 없거나 `T < valid_to`
3. `currentness`가 `current` (또는 `T` 시점 기준으로 재구성한 상태가 current)

`as_of` 조회는 현재 상태를 과거에 투영하는 것이 아니라, **그 시점의 유효 구간**으로 판정해야 한다. 현재 `superseded`인 항목도 `T`가 `valid_to` 이전이면 그 시점에는 current로 반환된다.

---

## 8. 응답 계약 (개념 모델)

특정 전송 형식을 강제하지 않지만, 의미는 다음과 같아야 한다.

```yaml
temporal:
  observed_at: "2026-07-04T09:43:47Z"
  recorded_at: "2026-10-12T05:24:58Z"
  valid_from: "2026-07-04T09:43:47Z"
  valid_to: null
  superseded_by: []
  as_of: "2026-10-23T12:00:00Z"
  age:
    seconds: 10368000
    label: "old"
    clock: "observed_at"
    confidence: "high"
    reason_codes: []
  ingestion_lag_seconds: 8640000
  currentness: "current"
  lifecycle_status: "accepted"
  freshness:
    state: "unchecked"
    checked_at: null
    next_check_after: null
    gaps: ["no_recent_freshness_check"]
  temporal_relation: "past"   # past | present | future | unknown
  temporal_conflict: false
  projection_lag_seconds: 128845
  recommended_action: "use_with_verification"
  summary: "관측 120일 전 · 현재 유효 · 신선도 미검증"
```

카드/문서/청크/그래프 사실 모두 같은 `temporal` 블록을 가져야 한다. 항목 유형별로 추가 필드가 있을 수 있지만, 위 의미는 공통이다.

---

## 9. 수용 기준

### AC-AGE-001 — 오래된 세션의 나이

**Given** 1년 전에 발생한 세션을 오늘 backfill했다.
**When** LBrain이 그 세션을 반환한다.
**Then** `observed_at`은 1년 전, `recorded_at`은 오늘, `age.label=historical`, `ingestion_lag`는 약 1년이어야 한다. "방금"으로 표시되면 실패다.

### AC-AGE-002 — 폐기된 항목의 기본 조회

**Given** 카드 A가 `current`에서 `superseded`로 전환되고 `valid_to`가 설정되었다.
**When** 기본 조회를 실행한다.
**Then** A는 current 레인에 나오지 않아야 한다. `include_historical=true` 또는 `as_of`가 `valid_to` 이전이면 historical/current-at-that-time으로 반환되어야 한다.

### AC-AGE-003 — 시간 정보 누락

**Given** `observed_at`이 없는 항목이 있다.
**When** LBrain이 그 항목을 반환한다.
**Then** `age=unknown`, `time_confidence=unknown`, `reason_codes`에 `observed_at_missing`이 포함되어야 한다. 현재 시각으로 추정하면 실패다.

### AC-AGE-004 — 그래프/PG 불일치

**Given** 그래프 경로와 PG가 같은 항목에 대해 다른 `valid_from`을 반환한다.
**When** LBrain이 응답을 조립한다.
**Then** PG authority를 기준으로 하고 `temporal_conflict=true`, `authority_source=postgres`를 표시해야 한다.

### AC-AGE-005 — 현재와 과거의 충돌

**Given** 현재 카드와 과거 카드가 서로 다른 결론을 말한다.
**When** 기본 조회를 실행한다.
**Then** 현재 카드를 우선하되 과거 카드의 존재와 폐기 관계를 숨기지 않아야 한다. `temporal_conflict=true`와 `superseded_by`가 표시되어야 한다.

### AC-AGE-006 — `as_of` 조회

**Given** 카드 B가 2026-08-01에 유효했고 2026-09-01에 폐기되었다.
**When** `as_of=2026-08-15`로 조회한다.
**Then** B는 current-at-that-time으로 반환되고 `temporal_view=as_of`가 표시되어야 한다. `as_of=2026-10-01`에서는 historical로 반환되어야 한다.

### AC-AGE-007 — 미래 시각

**Given** `observed_at`이 현재보다 1시간 미래다.
**When** LBrain이 그 항목을 반환한다.
**Then** `time_confidence=anomalous`, `temporal_relation=future`로 표시되고 최신 항목처럼 최상위 정렬되지 않아야 한다.

### AC-AGE-008 — 오래되었지만 유효한 결정

**Given** 6개월 전 결정이 아직 `current`이고 `valid_to`가 없다.
**When** 조회한다.
**Then** `age.label=old`이지만 `currentness=current`로 표시되어야 한다. 나이만으로 `stale`로 바꾸면 실패다.

### AC-AGE-009 — 최근이지만 폐기된 항목

**Given** 1시간 전에 적재된 항목이 이미 `superseded`다.
**When** 조회한다.
**Then** `age.label=fresh`이지만 `currentness=superseded`로 표시되어야 한다. 최신으로 표시되면 실패다.

### AC-AGE-010 — 신선도 미검증

**Given** 카드에 `freshness.checked_at`이 없다.
**When** 조회한다.
**Then** `freshness.state=unchecked`이고 `gaps`에 `no_recent_freshness_check`가 있어야 한다. "검증됨"으로 표시되면 실패다.

### AC-AGE-011 — Qdrant 사례

**Given** Qdrant 런타임 폐기 결정과, 폐기 이전 "Qdrant readiness" 카드가 함께 존재한다.
**When** "Qdrant 아직 쓰이나?"를 조회한다.
**Then** 폐기 결정이 current/historical 관계와 함께 반환되고, readiness 카드는 current로 반환되지 않아야 한다. 응답만 보고 "런타임 폐기, 코드 표면 일부 잔존"을 판단할 수 있어야 한다.

### AC-AGE-012 — task 상태

**Given** task가 `in_progress`이고 마지막 상태 관측이 60일 전이다.
**When** 조회한다.
**Then** `age.label=old`이고 `freshness.state=check_due`여야 한다. `in_progress`를 자동으로 `obsolete`로 바꾸면 실패다 (그 전환은 correction 절차).

### AC-AGE-013 — 페이지네이션

**Given** 동일한 `as_of`로 나이 정렬된 결과가 100개 있다.
**When** 10개씩 페이지를 넘긴다.
**Then** 중복/누락 없이 모든 항목을 볼 수 있어야 한다.

### AC-AGE-014 — 개인정보

**Given** 시간 메타데이터가 포함된 응답을 생성한다.
**When** 원본 세션 ID, 본문, 경로, 자격 증명이 응답에 포함될 수 있는 상황이다.
**Then** 시간 필드만으로 그러한 값이 노출되어서는 안 된다.

### AC-AGE-015 — 투영 지연

**Given** 그래프 투영이 1시간 지연되었다.
**When** LBrain이 응답한다.
**Then** `projection_lag_seconds`가 표시되고, `age`는 투영 시각이 아니라 `observed_at`으로 계산되어야 한다.

---

## 10. 비기능 요구사항

| 항목 | 요구 |
| --- | --- |
| 결정성 | 같은 입력과 같은 `as_of`에 대해 같은 `age`, `label`, `currentness`, `recommended_action`을 반환해야 한다. |
| 호환성 | 기존 응답 필드는 유지하고 시간 블록은 추가적으로 제공한다. 기존 소비자가 깨지지 않아야 한다. |
| 단일 조회 | 나이 계산 때문에 사용자에게 추가 왕복 조회를 요구해서는 안 된다. |
| 설명 가능성 | 모든 라벨과 `recommended_action`은 `reason_codes`로 설명 가능해야 한다. |
| 관측성 | `observed_at` 커버리지, `unknown` 비율, 시간 이상 비율, 투영 지연을 지표로 제공해야 한다. |
| 시간대 | 저장/비교는 UTC, 표시는 로컬 시간대를 허용한다. DST 경계에서도 동일한 instant를 가리켜야 한다. |
| 시계 오차 | 소스 간 허용 오차는 기본 ±5분이며, 정책으로 조정 가능해야 한다. |
| 보안 | 시간 메타데이터는 public-safe 해야 하며 권한 범위를 넘지 않아야 한다. |
| 언어 | 기계 식별자는 영문, 사람이 읽는 요약은 한국어/영어를 모두 지원할 수 있어야 한다. |
| 하위 호환 | 과거 데이터에 시간이 없어도 조회 자체는 실패하지 않고 `unknown`으로 반환되어야 한다. |

---

## 11. 기존 데이터 커버리지와 이행 원칙

- 기존 항목을 일괄 `historical`로 낙인찍지 않는다. 시간이 없으면 `unknown`으로 시작한다.
- 소스에 `observed_at`이 있으면 backfill로 복원한다. 없으면 `unknown`을 유지하고 커버리지 지표에 남긴다.
- `recorded_at`만 있는 항목은 `time_confidence=low`로 표시한다.
- 기존 `current` 카드도 `valid_to`가 없으면 "계속 유효"로 취급하되, `freshness`는 `unchecked`로 표시한다.
- 시간 메타데이터가 수정되면 다음 조회에서 반영되어야 하며, 이전 값으로 캐시된 나이를 반환해서는 안 된다.
- 과거 조회(`as_of`)는 이행 후에도 계속 가능해야 한다.
- Qdrant는 제거하지 않고, "런타임 폐기 + 코드 잔존" 표본으로 회귀 검증에 사용한다.

---

## 12. 열린 질문

| # | 질문 | 제안 |
| --- | --- | --- |
| Q1 | 나이 버킷 경계(24시간/7일/30일/180일)가 적절한가? | 기본값으로 두고 정책 설정 가능하게 한다. |
| Q2 | 기본 조회에서 과거 레인을 어느 정도 포함할 것인가? | 현재 결과가 없을 때만 fallback, 그 외에는 명시 요청 시 포함. |
| Q3 | `as_of` 미래 허용 오차는? | ±5분, 테스트용 우회 플래그 별도. |
| Q4 | "정보 나이"의 기준 시계는 항상 `observed_at`인가? | 원칙적으로 yes. 유형별 예외(런타임 사실은 마지막 점검 시각)는 문서화한다. |
| Q5 | `retired`를 `currentness`에 추가할 것인가, `lifecycle_status`로만 표현할 것인가? | 별도 상태로 구분하는 것을 권장. `stale`/`superseded`와 의미가 다르다. |
| Q6 | 오래되었지만 current인 항목을 정렬에서 감점할 것인가? | 자동 감점은 하지 않는다. `freshness` gap과 `recommended_action`으로 표현한다. |
| Q7 | 여러 세션을 요약한 파생 항목의 나이는? | 가장 최근 `observed_at`과 가장 오래된 `observed_at`을 함께 표시하고, 단일 `age`는 최신 기준으로 계산한다. |
| Q8 | 과거 항목의 보존 기간은? | 이 문서 범위 밖. 별도 retention 정책에서 다룬다. |
| Q9 | 프로젝트 간 시간 비교 기준은? | UTC instant를 사용하고, 표시만 로컬 시간대를 허용한다. |
| Q10 | `valid_to`가 없는 것과 "영원히 유효"를 구분할 것인가? | `valid_to=null`은 "선언된 만료 없음"일 뿐 "영원히 유효"를 뜻하지 않는다고 정의한다. |

---

## 13. 범위 밖

- 조회 결과를 실제 분석 결과로 교정하는 authority/correction 루프
- `brain_correct`류의 쓰기 도구와 권한 모델
- Neo4j/Graphiti 투영 구현과 스키마
- Qdrant 제거
- 임베딩 모델/차원/본문 압축 변경
- 자동 사실 검증(LLM judge 등) 정책
- 보존/삭제 정책
- UI 대시보드

---

## 14. 현재 상태(As-Is)와 기능 격차

이 절은 구현 지시가 아니라, 요구사항이 해결해야 할 **현재의 기능적 격차**를 기록한다.

| # | 현재 동작 | 사용자 영향 |
| --- | --- | --- |
| G1 | 조회 결과에 나이/유효기간 필드가 없다. | 최신/과거를 응답만으로 판단할 수 없다. |
| G2 | 카드의 `created_at`이 적재 시각이라 오래된 세션도 최신처럼 보인다. | backfill된 과거 정보를 현재로 오인한다. |
| G3 | `valid_to`가 사실상 설정되지 않는다. | 폐기된 항목도 만료되지 않는다. |
| G4 | 그래프 카드 투영이 관측 시각 대신 투영 시각을 기록한다. | 오래된 카드가 "방금 생성됨"으로 보인다. |
| G5 | stale/superseded 전환이 그래프 조회에 반영되지 않는다. | PG에서 폐기해도 그래프는 current로 답할 수 있다. |
| G6 | 그래프 투영 지연이 수십 시간에 달할 수 있다. | 교정/폐기가 즉시 조회에 반영되지 않는다. |
| G7 | task의 참조 대상이 사라져도 `in_progress`로 남는다. | 존재하지 않는 파일을 대상으로 작업을 계속한다. |
| G8 | `as_of` 개념이 일부 경로에만 존재하고 응답에 일관되게 노출되지 않는다. | "그때는 어땠는가"를 안정적으로 조회할 수 없다. |
| G9 | 과거/현재 충돌 시 하나만 반환하거나 관계를 숨긴다. | 폐기된 결정을 현재로 오인할 수 있다. |

---

## 15. 참고

- `docs/specs/2026-07-05-lbrain-knowledge-object-substrate/design.md` — KnowledgeObject envelope, `observed_at`/`valid_from`/`valid_to`, freshness, temporal_work_recall
- `docs/contracts/brain-steward-mcp.md` — 승인 시 `valid_from`, stale/supersede 시 `valid_to` 종료 규칙
- `milestones.md` — M5b(entity/relation/temporal/provenance), M5c(temporal correctness gate), M7(PG authority → graph projection)
- `docs/specs/2026-06-24-qdrant-mirror-cutover/milestones.md` — currentness reconcile 이력

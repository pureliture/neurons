# Grok Build Transcript Provider — Neurons Requirements

## 승인 대상

- Source of truth: `specs/grok-transcript-provider/requirements.md`
- Preview companion: `requirements.html` (필요 시)
- Companion (client): `dendrite` repo `specs/grok-transcript-provider/requirements.md`
- 승인 상태: **approved** (사용자 사전승인 2026-07-09; design.md 동시 승인 → agentic-execution)

## 배경

`neurons`는 ingest 이후 transcript 파싱, CouchDB source lane, session-memory, historical
import를 소유한다. `dendrite`가 `provider=grok`로 `updates.jsonl` locator·
`conversation_chunk`를 내도, server 측 allowlist·native parser·migration lane이 없으면
**재파싱·bulk import·session-memory 품질 경로가 fail-closed**된다(`unsupported provider`,
`UNKNOWN_PROVIDER`).

이 문서는 **neurons 측 요구사항만** 정의한다. Mac capture·spool·drain은 dendrite companion
requirements를 따른다.

## 구현 전 필수 리서치 (정확성 게이트)

구현·스펙 상세(design) 작성 **전에** dendrite와 **동일한 리서치 세트**를 수행하고, parser가
다룰 **실제 `updates.jsonl` 레코드 형태**를 샘플링한다. 추측으로 필드 경로를 고정하지 않는다.

| 항목 | 최소 출처 |
| --- | --- |
| Grok Build 버전 | 로컬 `grok --version` + dendrite 리서치 산출물과 일치 여부 |
| 세션 SoT | `$(GROK_HOME)/docs/user-guide/17-sessions.md` — `updates.jsonl`이 authoritative |
| ACP update 타입 | 실측 `updates.jsonl`에서 `params.update.sessionUpdate` 값 빈도·대표 레코드 키 목록(비밀·본문 전체 커밋 금지) |
| 청크 조립 규칙 | `user_message_chunk`, `agent_message_chunk`, `turn_completed` 등 완결 턴 조립 |
| tool 신호 | `tool_call` / `tool_call_update` → Codex식 high-signal evidence 후보 매핑 근거 |
| 부가 메타 | `summary.json`의 `current_model_id`, timestamps (migrate/backfill 라벨용) |
| upstream 변경 | 로컬 번들 docs와 공식 문서 불일치 시 design에 diff 명시 |

### Discovery 스냅샷 (grilling 시점, non-authoritative until re-verified)

로컬 실측 예(Grok **0.2.93**, 세션 4개, 약 340 lines; 본문 미커밋):

| `sessionUpdate` | 취급 정책 (본 requirements) |
| --- | --- |
| `user_message_chunk` | 청크 조립 → user turn |
| `agent_message_chunk` | 청크 조립 → assistant turn |
| `turn_completed` | 턴 경계(조립 완료 신호) |
| `tool_call` / `tool_call_update` | turn 본문 제외; Codex식 high-signal evidence 레인 |
| `agent_thought_chunk` | turn 제외 (침묵 drop) |
| `hook_execution` | 침묵 drop |
| 기타/미지 타입 | 침묵 drop |

레코드 envelope 예: top-level `method` (`session/update` 또는 `_x.ai/session/update`),
`params.sessionId`, `params.update.sessionUpdate`. design에서 파서 입력 경로를 고정한다.

리서치 결과로 **parser가 생성해야 할 최소 산출물**을 고정한다: `TranscriptSession`,
`TranscriptTurn` 목록, 그리고 Codex 패리티 high-signal tool evidence 요약(기존 codex
extractor 표면과 동급 계약).

## 질문-답변 흐름

### Q: Java ingress만 고치면 되나?

아니다. `IngestJobValidator`는 `provider` non-blank만 요구한다. 그러나 **worker/session-memory/
CouchDB import**는 `transcript_parsers`·`PROVIDER_LANES` allowlist에 묶여 있다. worker 변경이
핵심이다.

### Q: dendrite가 이미 redacted body를 ship하면 parser 없이 되지 않나?

enqueue·ledger 적재만으로는 일부 가능할 수 있으나, **historical import, backfill, locator 기반
rebuild, coverage/retirement lane**은 native parser·lane이 필요하다. 정식 완료 기준에 parser
포함을 **필수**로 둔다.

### Q: 기존 디스크 세션 migrate는 누가 열거하고 누가 ingest하나?

- **열거·spool·drain**: dendrite `transcript-migrate --provider grok`
- **source store / session-memory rebuild**: neurons `import_historical_source` 등 **grok lane**

양쪽 companion 시나리오가 end-to-end로 맞물려야 한다.

### Q: provider id는?

`grok`. `session_id_hash` seed는 `grok:<sessionId>` (dendrite와 동일 규칙 — design에서
단일 SoT).

### Q: `agent_thought_chunk`는 turn에 넣나? (grilling)

**아니오.** Codex/Claude Code native parser 패리티: turn 본문은 user/assistant **메시지
텍스트만**. thought/reasoning 스트림은 turn에 포함하지 않는다(침묵 drop).

### Q: `hook_execution`·미지 `sessionUpdate`는? (grilling)

**침묵 skip.** 본문·별도 메타 승격·필수 `parser_warnings` 없음. Codex/Claude가 non-message
record를 조용히 넘기는 것과 동일 원칙.

### Q: tool 이벤트는 Antigravity처럼 전량 `TranscriptToolEvent`인가? (grilling)

**아니오.** **Codex 스타일**: turn parser는 tool을 본문에 넣지 않고, **별도 high-signal
evidence 추출 레인**에서 고신호만 남긴다. draft 초기 FR-N3a “전 tool → TranscriptToolEvent
필수”는 **축소**한다.

### Q: tool evidence는 이번 완료 조건에 포함인가? (grilling)

**예.** user/assistant parser + lane/import + redaction과 함께, Grok용 high-signal evidence
추출·테스트까지 green이어야 neurons 측 done이다.

## 기능 요구사항

### FR-N1 — Provider 정규화

- `canonicalize_provider()`가 `grok`/`Grok`/공백 변형을 **`grok`** 로 정규화한다.
- 기존 provider 정규화 회귀 테스트를 유지한다.

### FR-N2 — Transcript parser allowlist

- `parse_transcript_source()`가 **`grok`** 를 지원한다.
- SoT 경로 정책: locator가 가리키는 **`updates.jsonl`** 에 대해 native parser를 호출한다.
- 미지원 형식·빈 턴 결과는 fail-closed한다(추측 파싱 금지). Codex/Claude와 같이
  유효 user/assistant turn이 없으면 `source_parse_failed`(또는 동등)로 실패한다.

### FR-N3 — Native parser (`updates.jsonl`) — turn 레인

- native grok parser가 ACP JSONL 스트림을 읽어 `ParsedTranscript`를 반환한다.
- **Turn 최소 추출 (Codex/Claude 패리티):**
  - `user_message_chunk` / `agent_message_chunk` 청크 조립으로 user/assistant **완결 턴**
  - `turn_completed` 등 경계 신호는 조립에 사용 가능; 그 자체는 turn 본문이 아님
  - session id·started_at/ended_at (가능한 범위; `summary.json` 보강은 design)
- **Turn에 넣지 않음 (침묵 drop):**
  - `agent_thought_chunk`
  - `hook_execution`
  - 미매핑/`sessionUpdate` 미지 타입
  - tool 관련 이벤트(아래 FR-N3a)
- parser 버전 문자열을 노출한다(예: `grok-updates-jsonl-parser.v1` — design에서 고정).

### FR-N3a — Tool evidence 레인 (Codex 패리티, 완료 필수)

- turn parser는 `tool_call` / `tool_call_update`를 turn 본문·필수 전량 `TranscriptToolEvent`
  타임라인으로 승격하지 않는다.
- **별도** high-signal tool evidence 추출 경로를 둔다. 계약 목표는 기존 Codex
  `extract_codex_tool_evidence` / `ToolEvidenceSummaryRecord` 계열과 **동급**:
  durable·고신호 요약만, redacted, session identity와 연결.
- Grok 필드 → evidence category/outcome 매핑 세부는 design + 리서치 게이트에서 고정한다.
- 이 레인은 **본 requirements 완료 조건에 포함**한다(후속 분리 금지).

### FR-N4 — CouchDB historical import lane

- `PROVIDER_LANES`에 **`grok`** entry를 추가한다.
- `parser=parse_transcript_source`.
- `live_allowed` 초기값·smoke 후 전환 조건은 design에 명시한다(기본 자세: smoke 전
  보수적, 기존 lane 패턴을 따른다).
- 미등록 provider는 `ImportStatus.UNKNOWN_PROVIDER`를 유지한다.

### FR-N5 — 기존 세션 server-side ingest (migration)

- dendrite migrate+drain으로 들어온 `provider=grok` 문서를 기존 ingest worker 경로가
  **거부하지 않는다**(unsupported provider 금지).
- operator가 server에서 locator 파일 경로를 직접 import하는 경우(테스트·복구):
  `import_historical_source(SourceLocator(provider="grok", source_path=...))`가
  `IMPORTED` 또는 명시적 fail status를 반환한다(침묵 성공 금지).
- **이미 neurons에 적재된 동일 session** re-import 시 멱등·중복 정책은 기존 document
  idempotency 규칙을 따른다(design에서 grok 예시 1건).

### FR-N6 — Migration CLI 정합 (해당 시)

- `worker` 측 `MIGRATION_PROVIDERS`(또는 동등 enum)에 `grok`를 추가한다면,
  dendrite `transcript-migrate --provider grok`와 **이름·의미가 일치**해야 한다.
- neurons-only bulk enumerate를 새로 만들지 않는다(YAGNI). 클라이언트 열거 + server import가
  기본 경로이다.

### FR-N7 — Redaction / denylist

- private path 패턴에 **`~/.grok`** (및 `GROK_HOME` 하위)를 redaction/denylist에 반영한다.
- public artifact·로그에 raw `updates.jsonl` 본문·API key·auth 파일 내용이 새지 않도록
  기존 redaction 회귀를 유지한다.

### FR-N8 — Live cutover / shadow (정책)

- `LIVE_CUTOVER_PROVIDERS`에 `grok`를 넣는 것은 **본 requirements 완료 조건에 포함하지 않는다**
  (별도 승인). parser smoke·import 증거 후 follow-up.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Fail-closed | unsupported provider·parse 실패·턴 없음 시 coverage 조작·침묵 skip 금지 |
| Content parity | turn 정책은 Codex/Claude와 같이 user/assistant 메시지 본문 중심 |
| Tool parity | tool은 Codex식 high-signal evidence; Antigravity 전량 tool timeline 필수 아님 |
| Public/private | public repo에 raw transcript·live secret·host topology 미커밋 |
| Tests | parser fixture(비밀 없는 샘플), evidence 추출, import round-trip, provider identity, codex/hermes 구분 |
| Compatibility | 기존 provider parser·import 회귀 green |
| Separation | `test_server_boundary`, leak-scan 정책 준수 |

## 인터페이스 계약 (dendrite와의 경계)

neurons가 기대하는 입력(dendrite 보장):

| 입력 | 요구 |
| --- | --- |
| `provider` | `grok` |
| locator 파일 | 세션 디렉터리 내 `updates.jsonl` |
| live ingress | `conversation_chunk` + source 메타에 `provider=grok` |
| session identity | `session_id_hash` = f(`grok`, sessionId) — dendrite와 동일 |

neurons가 보장하는 출력:

| 출력 | 요구 |
| --- | --- |
| `ParsedTranscript` | turns ≥1 (유효 세션; 없으면 fail-closed) |
| tool evidence | high-signal 요약 0..N (Codex 동급 계약; 전 tool timeline 아님) |
| historical import | `IMPORTED` 또는 명시적 status |
| provider 구분 | `grok` 세션이 `codex`/`hermes`와 다른 `session_id_hash` 공간 |

## 범위 제외

- dendrite hook 설치·spool·drain 구현
- Grok OAuth/API 키 관리
- Graphiti LLM connector에 xAI 직접 추가(별 트랙)
- `LIVE_CUTOVER_PROVIDERS` 기본값 변경(별도 승인)
- Hermes ingest identity 변경
- Antigravity 수준의 전량 `TranscriptToolEvent` 타임라인 필수화
- `agent_thought_chunk` / `hook_execution`을 recall 본문 또는 필수 메타로 승격
- `chat_history.jsonl`을 대안 SoT로 사용(YAGNI; authoritative는 `updates.jsonl`)

## 사용자 시나리오

1. Maintainer가 리서치 게이트 후 비밀 제거된 `updates.jsonl` fixture를 tests에 추가한다.
2. CI에서 `parse_transcript_source("grok", ...)`가 user/assistant turn만 조립하고,
   thought/hook/미지 타입을 침묵 drop 하는 단위 테스트가 pass한다.
3. CI에서 Grok high-signal tool evidence 추출 테스트가 pass한다(Codex 동급 계약).
4. Operator가 dendrite migrate+drain으로 기존 Grok 세션을 적재한 뒤, neurons에서
   session-memory/CouchDB import status가 `IMPORTED`(또는 문서화된 skip)임을 확인한다.
5. 동일 sessionId re-import가 중복을 오염시키지 않음을 확인한다.

## 검증 완료 기준 (neurons)

- [ ] 리서치 게이트: `sessionUpdate` 타입 목록·turn/evidence 정책이 design에 반영
- [ ] `cd worker && uv run pytest -q` green (grok turn parser + tool evidence + 기존 import 회귀)
- [ ] `PROVIDER_LANES["grok"]` 존재·import_historical_source round-trip 테스트 pass
- [ ] `canonicalize_provider("Grok") == "grok"` 및 ingest identity stored 테스트 pass
- [ ] turn 패리티: fixture에 thought/hook이 있어도 turns는 user/assistant 메시지만
- [ ] tool evidence 패리티: high-signal 추출 경로 테스트 pass (전량 tool timeline 필수 아님)
- [ ] redaction/denylist 회귀(해당 테스트 스위트) pass
- [ ] (통합) dendrite dry-run/migrate 산출 세션 ≥1건에 대해 server import 또는 live drain
      적재 후 provider=`grok` 메타 확인 — endpoint/환경은 operator; 불가 시 fixture import로
      대체하고 gap을 보고서에 명시

## End-to-end 완료 (양 레포)

정식 “Grok Build → brain” 완료는 **dendrite + neurons 검증 체크리스트 모두** 충족으로 정의한다.

## 미결정 항목

- 없음 (제품 분기 grilling 기준). 구현 세부(클래스명, evidence category 매핑 테이블,
  `live_allowed` 전환 절차, parser_version 문자열)는 **design.md**에서 확정한다.

## Grilling 결정 요약

| # | 결정 | 선택 |
| --- | --- | --- |
| 1 | `agent_thought_chunk` | turn 제외 (Codex/Claude 패리티) |
| 2 | `hook_execution` / 미지 타입 | 침묵 skip |
| 3 | tool 레인 | Codex식 high-signal evidence (Antigravity 전량 event 아님) |
| 4 | tool evidence 완료 포함 | 이번 done 조건에 포함 |

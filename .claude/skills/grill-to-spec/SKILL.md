---
name: grill-to-spec
description: Grill-to-spec skill for Claude Code, with requirements.md as source and requirements.html as generated preview companion.
---

# Grill to Spec

## Overview

**"Agree before you build"** — 코드 작성 전 사용자와 요구사항에 먼저 합의하고,
합의된 요구사항을 바탕으로 상세 spec을 작성하는 2-phase 설계 스킬.

- **Phase 1**: Requirements Discovery — 끊임없는 질문(grilling)으로 요구사항 도출
- **Phase 2**: Spec Creation — 승인된 요구사항을 바탕으로 상세 설계 작성

핵심 차별점은 relentless grilling session이다. 사용자가 방어적이어도 같은 지점에서
2회 이상 회귀할 때까지 질문을 멈추지 않는다.

## Activation Modes

### Mode 1: Explicit Call

사용자가 다음 키워드 중 하나를 발화하면 즉시 Phase 1을 시작한다.

- `"grill-to-spec"`, `"grill-me"`, `"explore"`
- `"요구사항 정리"`, `"grill-to-spec로"`, `"스킬 써줘"`

확인 질문 없이 Requirements Discovery로 진입한다.

### Mode 2: Implicit Suggestion

다음 키워드나 의도가 감지되면 스킬 사용을 제안하고, 사용자가 확인한 뒤 시작한다.

- `"요구사항으로 정리해줘"`, `"설계 전에 먼저 정리하자"`
- `"우리가 뭘 만들고 있는지 정리해보자"`
- `"이거 scope부터 정의하자"`, `"기능 목록 정리"`

제안 메시지:

```text
grill-to-spec 스킬로 진행할까요? (요구사항 -> 승인 -> spec 작성)
[Yes/No]
```

### Mode 3: Auto-Transition

일반 대화 중 모호한 요구가 3회 이상 반복되거나 사용자가 혼란을 표현하면 다음처럼
제안한다.

```text
대화 내용을 정리해서 grill-to-spec으로 진행할까요?
```

## Phase 1: Requirements Discovery

### HARD-GATE

```text
요구사항 Markdown source가 사용자 승인되기 전에는 Phase 2(spec 작성)를 시작하지 않는다.
No exceptions. Do NOT write implementation code or architecture during Phase 1.
```

### Source and Preview Contract

- `requirements.md` is the source of truth for Phase 1 approval.
- `requirements.html` is a generated companion / preview companion for human review.
- Approval must name the approved artifact: the user approves `requirements.md`, not the generated HTML.
- `requirements.html` may be regenerated at any time from the current source.
- `optimal-response` may be used only as a preview surface. It must not create or replace official artifacts.
- `human-doc-curator` registration requires separate approval. Do not register grill-to-spec outputs by default.
- No dedicated grill-to-spec hook is part of this contract.

### Process

1. **Grilling Session 시작**
   - One question at a time. 한 메시지에 하나의 질문만 한다.
   - 질문은 하나의 의문문이어야 한다. 한 문장 안에 여러 선택지나 후속 질문을 묶지 않는다.
   - 다음 질문에는 괄호 속 예시, slash-separated options, "A/B/C 중 하나" 같은 선택지 목록을 넣지 않는다.
   - 모든 결정 트리 분기가 해결될 때까지 계속한다.
   - 같은 지점에서 2회 이상 회귀할 때만 질문 전략을 조정한다.

2. **5 Whys 드릴다운**
   - 뿌리 원인(root cause)에 도달할 때까지 파고든다.
   - 이 답변이 또 다른 질문을 낳는지 확인한다.
   - 아직 명확하지 않은 가정과 예외 케이스를 확인한다.

3. **Running Summary**
   - 매 3-5턴 간격으로 현재 상태를 요약한다.
   - 사용자가 "지금까지 어디까지 됐지?"라고 물으면 같은 형식으로 답한다.

```text
=== 현재까지 확정된 것 ===
- [확정 항목 1]
- [확정 항목 2]

=== 아직 열린 분기 ===
- [미결정 항목 1]
- [미결정 항목 2]

=== 다음 질문 ===
- [예정 질문]
```

4. **Incremental source 업데이트**
   - 세션 중 퍼-체인지 워크스페이스에 `requirements.md`를 업데이트한다.
   - 사람이 보기 쉬운 화면이 필요하면 같은 디렉터리에 `requirements.html`을 generated companion으로 생성한다.
   - HTML preview는 검토 편의를 위한 화면이며, source of truth가 아니다.

5. **최종 요구사항 source 생성**
   - 모든 열린 분기가 닫히면 최종 `requirements.md`를 작성한다.
   - `requirements.html`은 최종 source 옆에 companion으로 생성하거나 갱신한다.

6. **사용자 승인 대기**
   - 사용자가 `requirements.md`를 승인하면 Phase 2로 진행한다.
   - 수정 요청이 있으면 Phase 1으로 회귀한다.

## Preview Presentation Protocol

### Rendering

요구사항 preview는 `requirements.md`를 기준으로 생성한다.

1. 퍼-체인지 워크스페이스에 `requirements.md`를 작성한다.
2. 같은 디렉터리에 `requirements.html`을 generated companion으로 만든다.
3. 공유 render server가 있으면 `scripts/render-server/start-server.sh --project-dir <repo-root>`를 실행한다. 이 스크립트는 `render-server/start-server.sh` adapter path로도 참조될 수 있다.
4. 반환 JSON의 `content_dir`에 `requirements.html` 사본을 push하고 `http://localhost:<port>` 링크를 안내한다.
5. 서버 시작 실패 시 `file://<workspace>/requirements.html`을 degraded preview로 안내할 수 있다.
6. terminal에는 전체 보기 링크를 한 줄로 출력한다.
7. preview 실패는 approval gate를 통과시키지 않는다. 사용자는 source Markdown을 기준으로 승인한다.

`open` 명령으로 브라우저를 자동으로 열지 않는다.

### Workspace Convention

```text
requirements.md      # Phase 1 source of truth
requirements.html    # generated companion / preview companion
design.md            # Phase 2 design spec, created only after requirements.md approval
```

기본 임시 경로는 target adapter가 정한다. Claude adapter는 `.claude/specs/<timestamp>-<topic>/`
같은 preview workspace를 사용할 수 있고, Codex adapter는 대응되는 runtime-safe preview
workspace를 선택할 수 있다. canonical skill body는 특정 live runtime 경로를 강제하지 않는다.

### Requirements Markdown Structure

```markdown
# {{topic}} Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Preview companion: `requirements.html`

## 질문-답변 흐름

### Q: {{question}}

{{answer}}

## 기능 요구사항

- {{functional_requirement}}

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| {{name}} | {{value}} |

## 사용자 시나리오

- {{scenario}}

## 미결정 항목

- {{open_question}}
```

### Terminal Output Example

```text
요구사항 source와 preview companion이 생성되었습니다.

Source: <workspace>/requirements.md
전체 보기: <preview-url-or-file-url>

확인 후 requirements.md 승인 또는 수정 요청을 해주세요.
- 승인 -> Phase 2 (Spec Creation)로 진행
- 수정 -> 이어서 질문/정정 계속
```

## Phase 2: Spec Creation

### Trigger

Phase 1의 `requirements.md`가 사용자에 의해 명시적으로 승인된 후에만 진행한다.

### Process

1. 승인된 `requirements.md` 분석
2. 아키텍처 설계
3. 에러 처리와 테스트 전략 정리
4. `design.md` 작성
5. 사용자 승인 대기

수정 요청이 있으면 Phase 2로 회귀한다. 승인되면 스킬을 종료하고, 구현 계획은 별도 흐름으로
진행한다.

### Design Spec Structure

```markdown
# {{topic}} Design Spec

## Overview

핵심 목표 1-2문장.

## Requirements Reference

- Phase 1 source: `requirements.md`
- Preview companion: `requirements.html`
- 핵심 기능 요구사항 요약

## Architecture

컴포넌트 다이어그램.

## Data Flow

시퀀스 또는 플로우.

## Component Details

각 컴포넌트: 입력, 출력, 의존성.

## Error Handling

예상 에러 시나리오와 대응.

## Testing Strategy

테스트 범위와 유형.

## Open Questions

미결정 기술적 사항.
```

## Output Artifacts

| Phase | 산출물 | 형식 | 목적 |
| --- | --- | --- | --- |
| Phase 1 | Requirements source | Markdown | 승인 대상, source of truth |
| Phase 1 | Requirements preview | HTML | generated companion, human preview |
| Phase 2 | Design spec | Markdown | 상세 기술, 구현 참조 |

## Promotion Rules

- 임시 요구사항을 공식 문서로 승격하려면 사용자가 별도로 승격 의사를 밝혀야 한다.
- HTML을 공식화하지 않는다. 승인된 결정을 source Markdown으로 승격한 뒤 companion HTML을 다시 생성한다.
- 장기 human view 관리가 필요하면 별도 승인 후 `human-doc-curator`에 등록한다.
- 단발성 요구사항 source라면 등록하지 않아도 된다.

## Key Principles

- 요구사항(What)과 spec(How)은 별개의 산출물이다.
- 각 phase 끝에는 반드시 사용자 승인 gate가 있다.
- YAGNI: 불확실한 미래 기능은 요구사항에서도 제외한다.
- One question at a time.
- "다음 질문"도 하나의 의문문만 포함한다.
- "다음 질문"에는 예시 선택지나 괄호 설명을 붙이지 않는다.
- 암묵적 합의를 가정하지 않는다.
- Running Summary로 공유된 맥락을 유지한다.
- Preview HTML은 source Markdown을 대체하지 않는다.

## Anti-Patterns

| 안티패턴 | 이유 |
| --- | --- |
| Phase 1에서 아키텍처 제안 | What/How 분리 위반 |
| HTML만 만들고 Markdown source를 생략 | 승인 대상과 source of truth가 사라짐 |
| `requirements.html`을 공식 문서로 취급 | generated companion 계약 위반 |
| 승인 gate 우회 | 요구사항 품질 보장 실패 |
| 한 메시지에 여러 질문 | 사용자 압도, 답변 품질 저하 |
| 한 질문에 여러 선택지/후속 질문을 묶음 | 실제로는 다중 질문이 되어 one-question gate가 깨짐 |
| 다음 질문에 괄호 예시를 붙임 | 사용자가 선택지를 검토하게 되어 discovery 질문이 복합 질문으로 변질됨 |
| optimal-response로 공식 산출물 대체 | preview surface 역할을 넘어섬 |
| human-doc-curator 자동 등록 | 별도 승인 계약 위반 |
| dedicated grill-to-spec hook 추가 | 승인된 contract 범위 밖 |

## Cross-References

- **Related**: `superpowers:brainstorming` — Phase 1 질문 기법
- **Related**: `grill-me` (mattpocock) — Phase 1 추궁 패턴
- **Related**: `OpenSpec (SDD)` — "Agree before you build" 철학

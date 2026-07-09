# Grok Build Transcript Provider — Neurons Design Spec

## Overview

`provider=grok`를 neurons worker의 first-class transcript provider로 붙인다. SoT는
`updates.jsonl`(ACP session update stream). Turn 정책과 tool evidence 정책은
**Codex/Claude Code 패리티**를 따른다: turn에는 user/assistant 메시지 텍스트만,
tool은 별도 high-signal evidence 레인, thought/hook/미지 타입은 침묵 drop.

## Requirements Reference

- Phase 1 source: `specs/grok-transcript-provider/requirements.md` (**approved**)
- Companion (client): dendrite `specs/grok-transcript-provider/requirements.md`
- 핵심 FR: FR-N1..N7 완료 포함; FR-N8 `LIVE_CUTOVER_PROVIDERS` 제외

## Approach (selected)

**Recommend: extend existing parser/lane surfaces (Approach A)**

| Approach | 요약 | Trade-off |
| --- | --- | --- |
| **A. Extend `transcript_parsers` + `PROVIDER_LANES` (선택)** | codex/claude와 동일 이음새에 grok branch 추가 | 최소 표면, 회귀 경로 명확 |
| B. 별도 grok package | 독립 모듈 | 디스패치 중복, allowlist 이중화 |
| C. Fixture-only (hermes-like) | native ACP 미지원 | historical rebuild 품질 부족; requirements 위반 |

Approach A를 채택한다.

## Architecture

```
updates.jsonl (fixture or server-reachable path)
        │
        ├─► parse_transcript_source("grok", ...)
        │         └─ _parse_grok_native_jsonl
        │              → ParsedTranscript(session, turns[], tool_events=[])
        │
        └─► extract_tool_evidence("grok", ...)
                  └─ extract_grok_tool_evidence
                       → list[ToolEvidenceSummaryRecord]  # Codex classifier 재사용

historical_import.PROVIDER_LANES["grok"]
        → parser=parse_transcript_source, live_allowed=True (tests green 후; hermes 패리티)
```

### Components

| Component | Responsibility | Depends on |
| --- | --- | --- |
| `transcript_parsers/` package | public facade + registries | providers/* |
| `transcript_parsers/providers/grok.py` | ACP JSONL turns + tool evidence | common + evidence |
| `parse_transcript_source` allowlist | accept `grok` + `.jsonl` native route | `_NATIVE_PARSERS` |
| `_TOOL_EVIDENCE_EXTRACTORS["grok"]` | evidence dispatch | `extract_grok_tool_evidence` |
| `PROVIDER_LANES["grok"]` | historical import | parse_transcript_source |
| redaction `PROVIDER_TRANSCRIPT_PATH_RE` | include `grok` home path segment | redaction.py |
| `MIGRATION_PROVIDERS` / roots | name parity with dendrite | migration_cli.py |

### Package layout (post M7)

```
session_memory/transcript_parsers/
  __init__.py          # parse_transcript_source, extract_tool_evidence, re-exports
  common.py            # ParsedTranscript, loaders, text helpers, version constants
  evidence.py          # shared high-signal classifier
  providers/
    claude.py | gemini.py | codex.py | antigravity.py | grok.py | fixture.py
```

Public import path remains `agent_knowledge.session_memory.transcript_parsers`.

## Data Flow

### Turn assembly

Input record envelope (verified shape):

```text
{ "timestamp": <unix_sec int>,
  "method": "session/update" | "_x.ai/session/update",
  "params": {
    "sessionId": "<uuid>",
    "update": { "sessionUpdate": "<type>", ... },
    "_meta": { ... }
  }
}
```

| `sessionUpdate` | Action |
| --- | --- |
| `user_message_chunk` | buffer user text from `update.content.text` |
| `agent_message_chunk` | buffer assistant text from `update.content.text` |
| `turn_completed` | flush current buffer as one turn (if non-empty) |
| role switch (user↔assistant) | flush previous buffer first |
| `agent_thought_chunk`, `hook_execution`, unknown | **silent skip** |
| `tool_call`, `tool_call_update` | **silent skip in turn parser** |

End of file: flush remaining buffer.

Fail-closed:

- no `sessionId` → `source_parse_failed: missing session_id`
- no user/assistant turns after parse → `source_parse_failed: missing transcript turns`
- empty/unreadable jsonl → existing load errors

Identity:

- `session_id_hash = sha256("grok:" + sessionId)` (hex with `sha256:` prefix via `_sha256`)
- `provider = "grok"` after `canonicalize_provider`

Parser version constant: `GROK_PARSER_VERSION = "grok-updates-jsonl-parser.v1"` (exposed via module constant; not required on every turn).

### Tool evidence (Codex-style)

1. Scan records for `tool_call` / `tool_call_update` by `toolCallId`.
2. Prefer latest update with `rawOutput` or `status` for output pairing.
3. Build raw_items:
   - `tool_name` ← `update._meta["x.ai/tool"].name` or title fallback
   - `command` ← `rawInput.command` / `rawOutput.command` when shell-like
   - `output` ← `rawOutput.output_for_prompt` or joined content text
   - `is_error` ← `rawOutput.exit_code not in (0, None)` or status in {error, failed}
4. Pass through existing `_build_evidence_records` / `_classify_tool_evidence`.
5. Add `run_terminal_command` to `_SHELL_TOOL_NAMES` (Grok execute tool).

Exploration tools (`read_file`, `grep`, `list_dir`, …) drop via existing classifier (Codex parity).

## Error Handling

| Scenario | Behavior |
| --- | --- |
| unsupported provider | existing `ValueError("unsupported provider: …")` |
| missing turns / session | `source_parse_failed: …` |
| evidence extract without session | same missing session_id error |
| unknown ACP types | silent skip (no warning required) |
| re-import same session | existing historical_import idempotency |

## Testing Strategy

| Area | Tests |
| --- | --- |
| Identity | `canonicalize_provider("Grok")=="grok"`; hash space ≠ codex/hermes |
| Turn parser | fixture with thought/hook/tool + user/assistant → turns only user/assistant |
| Chunk assembly | multi-chunk user/agent → single turns |
| Fail-closed | empty messages only → parse fails |
| Tool evidence | shell pytest-like command → test_result; read_file → dropped |
| Import | `PROVIDER_LANES` + `import_historical_source` IMPORTED |
| Redaction | path containing `/.grok/` redacted by provider path pattern |

Fixtures: **synthetic sanitized JSONL only** (no raw private session body in repo).

## TDD Strategy

Default red → green → refactor per milestone:

1. **M1** identity + allowlist + turn parser (+ fail-closed)
2. **M2** grok tool evidence extractor + shell name + dispatch
3. **M3** PROVIDER_LANES + import round-trip + redaction pattern + migration name align

## Milestones

- **M1** Turn parser + provider identity — done when grok parse tests green and unsupported regression intact
- **M2** Tool evidence — done when extract_tool_evidence("grok") tests green
- **M3** Lane/import/redaction/migration roots — done when import + redaction tests green
- **M4** Mid-review — codebase-architecture-manager + code-simplifier on changed surfaces; address only clear defects
- **M5** Full `cd worker && uv run pytest -q` green

## Open Questions (implementation, not product)

- None blocking. Optional: `live_allowed` starts `True` after unit evidence (hermes parity) rather than staged smoke flag; chosen for testability and requirements FR-N4 flexibility.

## Approval

- User pre-approved `requirements.md` and `design.md` (2026-07-09) and directed `/agentic-execution` with intermediate `@code_simplifier` / `@codebase_architecture_manager` reviews.

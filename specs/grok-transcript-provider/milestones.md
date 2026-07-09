# Milestones — grok-transcript-provider (neurons)

goal: approved `specs/grok-transcript-provider/design.md`
mode: normal
worktree: `.worktrees/grok-transcript-provider-requirements`
branch: `claude/grok-transcript-provider-requirements`

## Orchestration defaults

- code-changing milestones: TDD-first
- mid-flight reviews: `codebase-architecture-manager` (read-only) + `code-simplifier`
- SoT change: stop/regress to grill-to-spec (not in-loop)

## M1 Turn parser + identity

- status: done
- delegation_decision: single-executor
- expected_evidence_class: code-change
- tdd_status: red tests first then green
- evidence: `tests/test_grok_transcript_provider.py` identity + turn assembly + fail-closed green
- sot_change: false

## M2 Tool evidence

- status: done
- evidence: `test_extract_grok_tool_evidence_high_signal_only` green; shell → test_result, read_file dropped

## M3 Lane / import / redaction / migration align

- status: done
- evidence: PROVIDER_LANES + import round-trip + redaction path tests green; migration_cli roots/enumerate for grok

## M4 Architecture + simplify review

- status: done
- delegation_decision: review
- evidence:
  - architecture-manager: no merge-blocking findings; med notes on migration_cli cwd / module size (nice-to-have)
  - code-simplifier: `_parse_grok_native_jsonl` / `extract_grok_tool_evidence` clarity helpers
  - post-simplify: `uv run pytest -q tests/test_grok_transcript_provider.py` → 9 passed

## M5 Full worker pytest

- status: done
- evidence: `cd worker && uv run pytest -q` → **1811 passed, 9 skipped** (pre-simplifier suite; grok file rechecked after simplify)

## M6 Code-review remediation (no open deferrals)

- status: done
- evidence:
  - bug: grok project authority no longer becomes `updates.jsonl` (historical_import + migration `_grok_project_from_path`)
  - suggestion: tool meta name not overwritten by title; migration enumerate/cwd/project tests added
  - nit: exit_code coercion; `GROK_PARSER_VERSION` wired into packer metadata
  - `uv run pytest -q` → **1818 passed, 9 skipped**

## M7 Provider module split

- status: done
- evidence: `transcript_parsers` package with `common`/`evidence` + `providers/{claude,gemini,codex,antigravity,grok,fixture}`; public import path preserved; full pytest green

## Exit

- design scope implemented with objective test evidence
- architecture review + formal code review + full remediation
- provider-module split applied (M7)

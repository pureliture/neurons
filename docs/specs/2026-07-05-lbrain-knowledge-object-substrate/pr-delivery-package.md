# LBrain Knowledge Object Substrate PR Delivery Package

Status: `PR_READY_WITH_GAPS`

This package captures the stack PR plan for the LBrain Ontology-Style Knowledge Product Roadmap continuation branches. It is a delivery aid, not merge, CI, deploy, or runtime evidence.

## Gate

Do not create GitHub PRs from this package until one of the following is true:

- a linked issue number is supplied for the stack, or
- the user explicitly approves PR creation without a linked issue and accepts the repository workflow exception.

The active GitHub PR workflow expects a real closing reference such as `Closes #N`. Replace every `<ISSUE_NUMBER>` token before using any body preview below. Do not submit a preview with placeholder text.

Recommended issue strategy:

- Create one `type:task` issue per stack PR.
- Do not reuse a single closing issue across the whole stack unless premature issue closure after the first merged PR is acceptable.
- If a single tracking issue is preferred, use it as an epic/reference issue and create separate task issues for the PR closing references.
- Issue creation is also a GitHub mutation; do not create these issues without explicit user approval.

## Linked Issue Drafts

Use these drafts only after the issue creation gate is satisfied.

| PR | Issue title | Labels |
| --- | --- | --- |
| P1 | `task: validate LBrain production MCP activation gaps` | `type:task`, `area:lbrain` |
| P2 | `task: deliver LBrain living reference corpus store` | `type:task`, `area:lbrain` |
| P3 | `task: deliver LBrain object extraction pipeline previews` | `type:task`, `area:lbrain` |
| P4 | `task: deliver LBrain review authority promotion previews` | `type:task`, `area:lbrain` |
| P5 | `task: deliver LBrain golden query quality gates` | `type:task`, `area:lbrain` |
| P6 | `task: deliver LBrain session project work-unit rollup` | `type:task`, `area:lbrain` |
| P7 | `task: deliver LBrain artifact preference packs` | `type:task`, `area:lbrain` |
| P8 | `task: deliver LBrain runtime authority truth previews` | `type:task`, `area:lbrain` |
| P9 | `task: deliver LBrain agent context product packs` | `type:task`, `area:lbrain` |
| P10 | `task: record LBrain UI object browser defer decision` | `type:task`, `area:lbrain` |

Issue body template:

```markdown
## Goal

Deliver the corresponding phase from the LBrain Ontology-Style Knowledge Product Roadmap without design drift.

## Acceptance Criteria

- Phase branch is reviewed through a PR.
- PR body separates merge, CI, deploy, and live runtime evidence.
- Result is reported as PASS, PASS_WITH_GAPS, or FAIL.
- Production mutation remains denied unless a separate approved gate exists.
- No protected content, credential, topology, or raw external ID is exposed.
```

## Current Evidence

- `origin/main` includes PR #73 merge commit `c3f3e34`.
- No existing PR currently targets the `codex/p1` through `codex/p10` continuation branches.
- All continuation branches include PR #73 merge commit `c3f3e34`.
- All continuation branches have been rebased onto the latest P1 evidence commit `fe39cdd`.
- Current Codex `mcp__lbrain` namespace still does not expose `brain_objects_query`; P1 remains `PASS_WITH_GAPS`.
- Branch push is not merge, CI, deploy, or live runtime evidence.

## Stack Order

| Order | Title | Branch | Base |
| --- | --- | --- | --- |
| 1 | `docs(lbrain): record P1 production MCP activation gaps` | `codex/p1-production-mcp-activation-live` | `main` |
| 2 | `feat(lbrain): add living reference corpus store` | `codex/p2-living-reference-corpus-store` | `codex/p1-production-mcp-activation-live` |
| 3 | `feat(lbrain): add object extraction pipeline previews` | `codex/p3-processing-object-extraction-pipeline` | `codex/p2-living-reference-corpus-store` |
| 4 | `feat(lbrain): add review authority promotion previews` | `codex/p4-review-authority-promotion` | `codex/p3-processing-object-extraction-pipeline` |
| 5 | `feat(lbrain): add golden query quality gates` | `codex/p5-continuous-golden-query-quality` | `codex/p4-review-authority-promotion` |
| 6 | `feat(lbrain): add session project work-unit rollup` | `codex/p6-session-device-project-workunit-360` | `codex/p5-continuous-golden-query-quality` |
| 7 | `feat(lbrain): add artifact preference packs` | `codex/p7-preference-style-artifact-memory` | `codex/p6-session-device-project-workunit-360` |
| 8 | `feat(lbrain): add runtime authority truth previews` | `codex/p8-runtime-truth-security-deployment-authority` | `codex/p7-preference-style-artifact-memory` |
| 9 | `feat(lbrain): add agent context product packs` | `codex/p9-agent-context-productization` | `codex/p8-runtime-truth-security-deployment-authority` |
| 10 | `docs(lbrain): defer UI object browser surface` | `codex/p10-ui-object-browser-defer-decision` | `codex/p9-agent-context-productization` |

## Per-PR Preview

### 1. P1 Production MCP Activation

- branch: `codex/p1-production-mcp-activation-live`
- base: `main`
- head: `fe39cdd`
- commits:
  - `fe39cdd P1 최신 배포 재확인 gap을 기록`
  - `0b2cf1d P1 최신 main 배포 identity gap을 기록`
  - `c31bfc9 P1 Production MCP Activation 증거 갱신`
- diff: `2 files changed, 109 insertions(+), 25 deletions(-)`

Body preview:

```markdown
## What

- Records P1 production MCP activation evidence and remaining gaps.
- Separates deployed/configured HTTP MCP object-native proof from the current Codex session tool-registry gap.
- Keeps current-main image identity unproven instead of promoting branch or merge state to runtime proof.

## Why

P1 needs production read-path evidence without overstating deployment or current-session availability.

## Validation

- Validation report and roadmap evidence updated.
- No production mutation performed.

Closes #<ISSUE_NUMBER>
```

### 2. P2 Living Reference Corpus Store

- branch: `codex/p2-living-reference-corpus-store`
- base: `codex/p1-production-mcp-activation-live`
- head: `343bbb5`
- commits: `8`
- diff: `13 files changed, 1707 insertions(+), 22 deletions(-)`

Body preview:

```markdown
## What

- Adds ledger-backed reference corpus and document version storage.
- Connects corpus status, ingest planning, CLI, MCP schema, and local/test gate coverage.
- Records P2 as PASS_WITH_GAPS because private manifest ingest and production approval remain gaps.

## Why

The roadmap requires a living reference corpus store that keeps raw corpus material separate from accepted/current authority.

## Validation

- `cd worker && uv run pytest -q tests/test_reference_corpus.py tests/test_neuron_cli.py tests/test_neuron_mcp_stdio.py`
- `cd worker && uv run pytest -q`
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`

Closes #<ISSUE_NUMBER>
```

### 3. P3 Processing And Object Extraction Pipeline

- branch: `codex/p3-processing-object-extraction-pipeline`
- base: `codex/p2-living-reference-corpus-store`
- head: `dafad79`
- commits: `11`
- diff: `6 files changed, 2809 insertions(+), 3 deletions(-)`

Body preview:

```markdown
## What

- Adds deterministic local/test extraction previews for reference corpus, repo documents, runtime truth, preference/style, work units, sessions, PR/commit details, and graph/search projection joins.
- Adds evaluator suite coverage for deterministic fixtures and golden-query slices.
- Records P3 as PASS_WITH_GAPS because live graph/Qdrant projection join remains unproven.

## Why

The object substrate needs repeatable processing and extraction evidence before authority promotion or production claims.

## Validation

- `cd worker && uv run pytest -q tests/test_extraction_pipeline.py`
- `cd worker && uv run pytest -q`
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`

Closes #<ISSUE_NUMBER>
```

### 4. P4 Review Queue And Authority Promotion

- branch: `codex/p4-review-authority-promotion`
- base: `codex/p3-processing-object-extraction-pipeline`
- head: `ab8d1ba`
- commits: `5`
- diff: `10 files changed, 856 insertions(+), 9 deletions(-)`

Body preview:

```markdown
## What

- Adds local/test object authority decision audit, authority state overlay, object explain decision history, and production denial promotion plan.
- Keeps production/default authority writes denied unless the allowed local_test ledger scope is used.
- Records P4 as PASS_WITH_GAPS because approved production pilot and write gate remain gaps.

## Why

Review and authority promotion must separate proposal/local-test behavior from production authority mutation.

## Validation

- `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py`
- `cd worker && uv run pytest -q tests/test_object_packs.py tests/test_knowledge_objects.py tests/test_ledger_area_boundaries.py`
- `cd worker && uv run pytest -q`
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`

Closes #<ISSUE_NUMBER>
```

### 5. P5 Continuous Golden Query Quality Gates

- branch: `codex/p5-continuous-golden-query-quality`
- base: `codex/p4-review-authority-promotion`
- head: `336db2f`
- commits: `4`
- diff: `5 files changed, 380 insertions(+), 4 deletions(-)`

Body preview:

```markdown
## What

- Adds phase golden-query coverage and stricter quality-axis evaluation.
- Requires edge, freshness, gap fields, empty-lane disclosure, and runtime evidence or explicit runtime gaps.
- Keeps the release gate not green while future phase slices remain incomplete.

## Why

The roadmap needs continuous quality gates that make gaps visible instead of hiding weak object-pack answers.

## Validation

- `cd worker && uv run neuron-knowledge golden-query-eval --phase-coverage`
- `cd worker && uv run pytest -q tests/test_golden_query_eval.py tests/test_neuron_cli.py`
- `cd worker && uv run pytest -q`
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`

Closes #<ISSUE_NUMBER>
```

### 6. P6 Session, Device, Project, And Work-Unit 360

- branch: `codex/p6-session-device-project-workunit-360`
- base: `codex/p5-continuous-golden-query-quality`
- head: `bf1de38`
- commits: `3`
- diff: `5 files changed, 656 insertions(+), 4 deletions(-)`

Body preview:

```markdown
## What

- Adds session/project rollup preview with Device, Session, Repository, Branch, WorkUnit, Spec, PullRequest, Commit, and safe handoff pack objects.
- Separates same-device and all-device scopes.
- Records P6 as PASS_WITH_GAPS because live multi-device/project rollup remains unproven.

## Why

The roadmap needs temporal repo recall that groups sessions, branches, linked work metadata, and handoff context without raw transcript exposure.

## Validation

- `cd worker && uv run pytest -q tests/test_extraction_pipeline.py`
- `cd worker && uv run pytest -q tests/test_golden_query_eval.py`
- `cd worker && uv run pytest -q`
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`

Closes #<ISSUE_NUMBER>
```

### 7. P7 Preference, Style, And Artifact Memory

- branch: `codex/p7-preference-style-artifact-memory`
- base: `codex/p6-session-device-project-workunit-360`
- head: `5883d04`
- commits: `1`
- diff: `5 files changed, 579 insertions(+), 11 deletions(-)`

Body preview:

```markdown
## What

- Adds ArtifactPreferencePack, PersonalCodeStyleProfile, RepoStyleProfile, HtmlReviewProfile, and VisualizationProfile previews.
- Separates accepted preferences from inferred/proposal-only style and artifact memory.
- Adds no-UI HTML artifact preference checks and drift review suggestions.

## Why

Agents should start with accepted user and repo preferences without promoting inferred or legacy behavior to authority.

## Validation

- `cd worker && uv run pytest -q tests/test_extraction_pipeline.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py`
- `cd worker && uv run pytest -q`
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`

Closes #<ISSUE_NUMBER>
```

### 8. P8 Runtime Truth, Security, And Deployment Authority

- branch: `codex/p8-runtime-truth-security-deployment-authority`
- base: `codex/p7-preference-style-artifact-memory`
- head: `e65924e`
- commits: `1`
- diff: `6 files changed, 411 insertions(+), 8 deletions(-)`

Body preview:

```markdown
## What

- Adds runtime authority truth previews for PullRequest, Commit, CIStatus, DeploymentTarget, RuntimeSurface, RuntimeTruth, and LiveEvidenceGap.
- Separates merge, CI, deployment target, artifact identity, live evidence, and permission-sensitive actions.
- Denies runtime authority promotion without approved scope and redacts protected deploy authority values.

## Why

Runtime truth must not conflate merge, CI, deployment, and live evidence.

## Validation

- `cd worker && uv run pytest -q tests/test_extraction_pipeline.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py tests/test_object_packs.py`
- `cd worker && uv run pytest -q`
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`

Closes #<ISSUE_NUMBER>
```

### 9. P9 Agent Context Productization

- branch: `codex/p9-agent-context-productization`
- base: `codex/p8-runtime-truth-security-deployment-authority`
- head: `5468e45`
- commits: `1`
- diff: `5 files changed, 299 insertions(+), 18 deletions(-)`

Body preview:

```markdown
## What

- Adds agent_context_product_pack.v1 for Codex, Claude Code, Gemini, and Hermes consumers.
- Adds compact current authority, reference, style/preference, active work, guardrail, and verification sections.
- Makes degraded/stale state, missing promotion evidence, surface policy, and proposal-safe action hints visible.

## Why

Agent startup and review workflows need compact object-pack context without granting mutation authority.

## Validation

- `cd worker && uv run pytest -q tests/test_context_pack_builder.py tests/test_object_packs.py tests/test_golden_query_eval.py`
- `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_stdio_cli_serves_contextpack_for_codex_claude_code_and_hermes_agents`
- `cd worker && uv run pytest -q`
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`

Closes #<ISSUE_NUMBER>
```

### 10. P10 UI And Object Browser Surface

- branch: `codex/p10-ui-object-browser-defer-decision`
- base: `codex/p9-agent-context-productization`
- head: verify with `git rev-parse --short codex/p10-ui-object-browser-defer-decision` immediately before PR creation; this package edits the P10 branch and therefore cannot carry a stable self-referential head SHA.
- commits:
  - `c5389e9 P10 UI surface defer decision을 기록`
  - `b5a6f59 로드맵 PR stack delivery gap을 기록`
  - `746f15b PR delivery package를 추가`
  - `4ec66b2 PR delivery issue draft를 추가`
  - package refresh commit(s) after `4ec66b2` update this delivery metadata only
- diff: `2 files changed, 460 insertions(+), 2 deletions(-)`

Body preview:

```markdown
## What

- Records the P10 start-readiness review as PASS_WITH_GAPS and defers UI/object browser implementation.
- Adds the PR-ready branch stack and delivery gate for P1 through P10.
- Keeps UI explicitly non-prerequisite for MCP/read-path activation, authority writes, and production rollout.

## Why

P10 should not force premature object semantics while P1/P5/P8/P9 production gates remain open.

## Validation

- `git diff --check`
- docs sensitive-pattern scan

Closes #<ISSUE_NUMBER>
```

## Creation Commands

Only run after replacing `<ISSUE_NUMBER>` in each body preview and after the PR creation gate is satisfied.

```bash
gh pr create --base main --head codex/p1-production-mcp-activation-live --title "docs(lbrain): record P1 production MCP activation gaps" --body-file <p1-body.md>
gh pr create --base codex/p1-production-mcp-activation-live --head codex/p2-living-reference-corpus-store --title "feat(lbrain): add living reference corpus store" --body-file <p2-body.md>
gh pr create --base codex/p2-living-reference-corpus-store --head codex/p3-processing-object-extraction-pipeline --title "feat(lbrain): add object extraction pipeline previews" --body-file <p3-body.md>
gh pr create --base codex/p3-processing-object-extraction-pipeline --head codex/p4-review-authority-promotion --title "feat(lbrain): add review authority promotion previews" --body-file <p4-body.md>
gh pr create --base codex/p4-review-authority-promotion --head codex/p5-continuous-golden-query-quality --title "feat(lbrain): add golden query quality gates" --body-file <p5-body.md>
gh pr create --base codex/p5-continuous-golden-query-quality --head codex/p6-session-device-project-workunit-360 --title "feat(lbrain): add session project work-unit rollup" --body-file <p6-body.md>
gh pr create --base codex/p6-session-device-project-workunit-360 --head codex/p7-preference-style-artifact-memory --title "feat(lbrain): add artifact preference packs" --body-file <p7-body.md>
gh pr create --base codex/p7-preference-style-artifact-memory --head codex/p8-runtime-truth-security-deployment-authority --title "feat(lbrain): add runtime authority truth previews" --body-file <p8-body.md>
gh pr create --base codex/p8-runtime-truth-security-deployment-authority --head codex/p9-agent-context-productization --title "feat(lbrain): add agent context product packs" --body-file <p9-body.md>
gh pr create --base codex/p9-agent-context-productization --head codex/p10-ui-object-browser-defer-decision --title "docs(lbrain): defer UI object browser surface" --body-file <p10-body.md>
```

## Merge And Runtime Notes

- Merge stacked PRs from P1 to P10 in order.
- After every merge, rebase or retarget the next PR as needed.
- GitHub CI proves public repo validation only.
- Merge does not prove production deploy.
- Production runtime proof still requires the configured LBrain MCP object-native namespace and deployed artifact identity checks described in the roadmap.

# LBrain Knowledge Object Substrate Production Validation Design

## Overview

이 design은 1차 구현 결과를 production-readiness 관점에서 검증한다. 검증은 read-only first이며, production mutation은 수행하지 않는다. production에 아직 배포되지 않은 기능은 실패가 아니라 `not_validated` gap으로 분리한다.

## Validation Model

검증 단위는 `ValidationClaim`이다.

```yaml
claim_id: local.worker.full-regression
claim: worker tests pass for object substrate implementation
evidence_class: local_test
status: validated
evidence:
  - command: cd worker && uv run pytest -q
    result: pass
gaps: []
```

Status vocabulary:

- `validated`: 직접 증거가 있다.
- `denied_as_expected`: safety gate가 의도대로 막았다.
- `not_validated`: 증거가 없거나 production에 아직 없다.
- `blocked`: read-only 검증 자체를 할 수 없다.

Final status:

- `PASS`: 필수 local/runtime/denial gate가 모두 validated.
- `PASS_WITH_GAPS`: local and safety gates pass, live production inclusion remains unverified or not deployed.
- `NO_GO`: regression failure, unexpected mutation, or public-safe violation.
- `BLOCKED`: 필수 evidence source가 없어서 판정 불가.

## Execution Flow

1. Preflight
   - `git status --short --branch`
   - `git worktree list`
   - confirm current worktree is not `main`

2. Local regression gates
   - `cd worker && uv run pytest -q`
   - `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
   - `git diff --check`

3. CLI contract gates
   - `uv run neuron-knowledge golden-query-eval --baseline`
   - `uv run neuron-knowledge object-query --repository neurons --branch <branch> --query <doc cleanup query>`
   - `uv run neuron-knowledge okf-export --root okf`
   - `uv run neuron-knowledge corpus-ingest-plan --project neurons --storage-mode metadata_only --corpus-name palantir-ontology`
   - `uv run neuron-knowledge corpus-ingest --project neurons --target local_test`
   - `uv run neuron-knowledge corpus-ingest --project neurons --target production`

4. MCP contract gates
   - run focused JSON-RPC tests for object tools and restricted denial
   - verify `brain_objects_query`, `brain_object_proposal_create`, `brain_review_proposals`, and `brain_object_decision_commit`
   - verify live/readiness `brain_objects_query` route smokes include `authority_archive_separation`, `code_style_preference`, `temporal_work_recall`, and `deployment_runtime_truth`
   - expected: local/test proposal writes to local/test ledger; production/restricted decision denies

5. LBrain read-path gates
   - use available LBrain MCP read tools if configured
   - read current authority/context for `neurons`
   - classify whether current accepted memory knows the new substrate
   - do not promote, write, or mutate memory

6. Live production inclusion gate
   - if read-only deployment/version evidence is available, compare deployed source/image/version to current branch.
   - if unavailable or not deployed, mark `not_validated: not_deployed_to_production`.
   - do not run deployment, GitOps, image push, or Argo sync.

7. Report
   - write `validation-report.md` in this spec directory.
   - include command evidence, claim statuses, gaps, and final status.

## Stop Conditions

Stop and report `NO_GO` if:

- any regression gate fails
- production corpus ingest performs mutation
- restricted decision commit writes authority
- public-safe scan finds newly added sensitive output
- a command requires secret printing or raw transcript/body access

Stop and report `BLOCKED` if:

- local environment cannot run required tests
- LBrain read path is unavailable and no substitute evidence exists
- production read-only evidence cannot be accessed and the question requires live production inclusion

## Validation Report Shape

```markdown
# Production Validation Report

## Final Status

PASS_WITH_GAPS

## Validated

- local.worker.full-regression
- local.root.gradle
- local.mcp.object-tools

## Denied As Expected

- production.corpus-ingest
- restricted.object-decision-commit

## Not Validated

- live.production.object-tools-loaded: not_deployed_to_production

## Gaps

- production deployment/promotion not part of this goal
```

## Acceptance Mapping

- requirements local/package claims -> steps 2 and 3
- runtime/MCP claims -> step 4
- production write-denial -> steps 3 and 4
- live production inclusion -> steps 5 and 6
- report -> step 7

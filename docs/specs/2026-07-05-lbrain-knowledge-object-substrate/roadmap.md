# LBrain Ontology-Style Knowledge Product Roadmap

## Status

This roadmap is evidence-gated, not calendar-gated.

It does not assign percentage completion. The first formal denominator starts here: each phase is complete only when its gate evidence exists and the production/read-path state is honestly labeled.

Current state:

- Phase 1 substrate implementation: complete in local/test scope.
- Production validation follow-up: `PASS_WITH_GAPS`; local/safety gates passed, deployed HTTP MCP runtime and configured endpoint validated, current Codex session tool registry still missing object-native tools.
- P1 Production MCP Activation: `PASS_WITH_GAPS`; deployed/configured HTTP MCP exposes object-native tools, but the current Codex session's `mcp__lbrain` namespace still does not expose them.
- Product activation: not complete; configured agent read path refresh remains required.
- UI/object browser: not a prerequisite for product activation, but remains an open later product surface.

Roadmap lock state:

- This document is the planning SoT only for phase ordering and gate definitions.
- It is not proof that a phase is complete.
- A phase can move to `production_validated` only with live deployed/read-path evidence and a no-mutation report when mutation is out of scope.

## Product Goal

LBrain should become a working knowledge product for development work, not just a memory retrieval surface.

The target is an ontology-style product in the practical sense:

- typed objects for work knowledge
- typed relationships between those objects
- evidence and freshness on claims
- proposal/review/action lifecycle
- accepted/current authority separated from archive, reference, graph, search, and runtime evidence
- agent-facing context packs for Codex, Claude, Gemini, Hermes, and later tools

This is not a goal to clone Palantir. Palantir reference material is used to reduce the problem into needed mechanics: object, link, action, function, pipeline, governance, and application surface.

## Palantir Corpus Traceability

The current local Palantir reference corpus manifest is the evidence baseline for this roadmap.

Manifest state:

- corpus name: `palantir-ontology`
- source count: 65
- sources with URL: 39
- manual text sources without URL: 26
- source types: PDF 6, web page 33, text 26
- authority lane: `reference_only`
- review state: `unreviewed`
- ingest state: `normalized`

The local filesystem contains raw markdown, normalized markdown, metadata, and manifest files, so file count is higher than source count. Roadmap gates must use manifest source count, not raw filesystem file count. If a later corpus has more sources, it must enter P2 as a new manifest version with its own hash, source count, and freshness gaps.

Corpus-derived roadmap mapping:

| Palantir-derived mechanic | Roadmap phase |
| --- | --- |
| Object, link, action, function, application, governance model | P0, P3, P4, P8, P9 |
| Source-schema is not the domain object | P0, P3 |
| Data, logic, action, security as one decision model | P3, P4, P8 |
| Document extraction strategy, chunking, evaluation, deployment lifecycle | P2, P3, continuous P5 |
| Function evaluation, debugging, variance, model comparison | P3, continuous P5 |
| MCP as context plus tools/actions | P1, P4, P9 |
| Application surface and permission-scoped interaction | P8, P9, P10 |

## Non-Goals

- Do not treat search, graph, session archive, or raw corpus text as accepted/current authority.
- Do not require a UI before CLI/MCP quality is usable.
- Do not store raw private transcript, secret, token, private runtime evidence, raw dataset id, or raw document id in public repo artifacts.
- Do not collapse merge, CI, deploy, and live runtime evidence into one status.
- Do not claim production readiness from local tests.
- Do not assign roadmap percentage until every phase has accepted gate criteria and comparable product weight.

## Current Evidence Baseline

Completed local/test substrate gates:

- `KnowledgeObjectEnvelope`, `KnowledgeEdge`, `EvidenceRef`, `ReviewProposal`, and `AuthorityDecision` model exist.
- `authority_lane` and `verification_state` are separated.
- reference corpus ingest planning exists.
- documentation cleanup, runtime truth, preference/style, and agent context object packs have local/test coverage.
- MCP/CLI object surfaces exist locally.
- production proposal and restricted authority mutation paths deny by default.
- OKF export exists as a review/export companion, not canonical authority.
- golden query baseline exists and records current low-quality behavior.

Known production gaps:

- deployed/configured HTTP LBrain MCP exposes object-native tools, but the current Codex session's `mcp__lbrain` namespace is still stale and does not expose them.
- reference corpus store is not configured as a living LBrain corpus store.
- golden queries are baseline red, not production-quality green.
- accepted/current promotion workflow is not open for production object decisions.
- object extraction and processing pipeline is skeletal.
- session/device/project 360 is specified but not production-usable.

Known roadmap lock gaps from multi-agent review:

- P1 must prove the deployed/configured read path contains the exact commit, image, or artifact that includes object-native tools.
- P2 must close source rights, raw-body retention, deletion, return capability, and managed snapshot policy.
- P3 must include extraction strategy comparison, chunk preview, evaluation metrics, cost/speed, and debug trace gates.
- P4 must define how approved production authority promotion opens, audits, rolls back, and stays scoped.
- P5 must run continuously across P1-P9, not only as a late phase.
- P8/P9 must include object/property/action permission and application restriction gates.

## Continuous Quality Gate

P5 is both a named phase and a continuous gate. It starts at P1 and stays active through P9.

Each phase must define at least one golden query or evaluator slice that proves the new capability improves user-meaningful answers. A phase is not product-complete if it only exposes tools, schemas, or storage while the relevant answer still falls back to generic safety/current cards.

Minimum continuous checks:

- answer includes object, edge, evidence, freshness, gap, and recommended action when applicable
- accepted/current, reference-only, proposal/review, archive, derived projection, and runtime evidence lanes stay separated
- missing authority is stated as a gap instead of hidden
- production/runtime claims require live evidence
- raw/private evidence is redacted or denied
- query routing explains source lanes and stop reason

## Phase Gates

### P0. Local Object Substrate Foundation

State: complete for local/test scope.

Purpose:

Provide the shared object/edge/evidence/review vocabulary used by all later phases.

Done evidence:

- local worker regression passed
- root Gradle regression passed
- object model tests passed
- MCP/CLI object contract tests passed
- production mutation denial tests passed

Remaining boundary:

This phase does not prove production deployment or live LBrain quality.

### P1. Production MCP Activation

State: `PASS_WITH_GAPS` as of 2026-07-05.

Deployed HTTP MCP runtime activation and configured endpoint smoke passed. Current Codex session tool-registry activation remains a gap.

Purpose:

Make the new object-native read surfaces available on the configured/deployed LBrain MCP read path.

Required capabilities:

- deployed `tools/list` exposes object-native tools
- deployed object query can answer in public-safe shape
- deployed object explain and corpus status can return gaps without mutation
- configured MCP read path identifies the deployed artifact or source/image identity
- deployed schema exposes read, explain, corpus status, review proposal listing, and proposal-safe action tools
- proposal-safe actions either write only to approved local/test scope or return denied/no-mutation in production
- production write paths remain denied unless a later approved gate opens them

Gate evidence:

- read-only live MCP tool-list proof
- read-only live `brain_objects_query` smoke
- deployed artifact identity check that ties the configured read path to the exact commit, image, or build artifact
- live denial smoke for production proposal/decision calls when no production mutation gate is open
- no production ledger/corpus mutation during activation validation

Current evidence summary:

- live production Argo application tracks `main` and is `Synced/Healthy`
- deployed MCP image identity is tied to source commit `c216ff4`, which contains PR #64 merge commit `7a0b6a6`
- live HTTP MCP `tools/list` exposes `brain_objects_query`, `brain_object_explain`, `brain_corpus_status`, `brain_corpus_ingest_plan`, `brain_object_proposal_create`, `brain_object_decision_commit`, and `brain_review_proposals`
- live read-only `brain_objects_query` returns `brain_objects_query.v1` with `object_pack.v1`
- user-level Codex LBrain MCP config source includes object-native tools, and standalone smoke against the configured endpoint returns the same object-native tool list and read-only query shape
- production-scope `brain_object_proposal_create` returns denied/no-mutation
- `brain_object_decision_commit` returns denied/no-mutation
- no production ledger/corpus mutation was performed
- current Codex session's `mcp__lbrain` namespace does not expose `brain_objects_query` even though the configured endpoint smoke passes; this keeps P1 at `PASS_WITH_GAPS`, not `PASS`

Next gate:

- restart or refresh the Codex LBrain MCP tool registry until the configured `mcp__lbrain` namespace exposes object-native tools directly, then rerun read-only smoke through that configured path.

### P2. Living Reference Corpus Store

State: planned.

Purpose:

Turn external reference material, including the Palantir corpus, into managed LBrain reference objects without confusing them with accepted/current authority.

Required capabilities:

- `ReferenceCorpus`, `DocumentSource`, `DocumentSnapshot`, `DocumentVersion`, `DocumentChunk`, `ExtractionRun`, and `FreshnessCheck` persisted in an approved store
- corpus policy supports `external_object_store`, `managed_snapshot`, and `metadata_only`
- source URL, hash, freshness, source rights, storage mode, and missing-evidence gaps are tracked
- raw corpus body policy is explicit per corpus
- retention, deletion, redaction, and return-capability policy is explicit per storage mode
- managed snapshot requires approved source-rights and raw-body policy
- manual text without source URL remains usable as reference material but carries a freshness/source gap
- corpus objects stay `reference_only` until review/promotion

Gate evidence:

- Palantir corpus manifest loads with expected source count, URL count, manual-text gap count, and manifest hash
- corpus status reports source counts, hash state, freshness gaps, and extraction runs
- corpus status reports source-rights, retention, deletion, redaction, and raw-return policy
- repeated ingest is idempotent
- production ingest remains gated

### P3. Processing And Object Extraction Pipeline

State: planned.

Purpose:

Convert files, docs, sessions, PRs, commits, runtime evidence, code style, and artifact preferences into typed knowledge objects and edges.

Required capabilities:

- extractor registry for repo documents, reference documents, sessions, work units, PRs, commits, tests, runtime surfaces, style rules, and artifact preferences
- extraction strategy registry for chunking, mapping, summarization, style inference, runtime evidence mapping, and preference inference
- extraction run records with input hash, output object ids, evaluator result, and failure reason
- extraction run records quality metrics, cost estimate, speed, token budget, and debug trace availability
- chunk preview and public-safe output preview exist before managed snapshot or corpus-derived object promotion
- evaluator supports deterministic fixture checks, golden query checks, variance checks, and model/prompt comparison where LLM extraction is used
- freshness checks separated from authority decisions
- public-safe projection for every object returned through MCP

Gate evidence:

- fixture extraction creates deterministic objects and edges
- failed extraction reports gaps instead of inventing authority
- extraction strategy comparison exists for Palantir corpus document mapping and repo document cleanup mapping
- chunk preview proves evidence can be inspected without raw/private leakage
- evaluator report ties extractor output to at least one golden query slice
- graph/search projection can join to objects but cannot become canonical authority

### P4. Review Queue And Authority Promotion

State: planned.

Purpose:

Create a closed lifecycle from candidate object to accepted/current, stale, superseded, retired, rejected, or archive-only authority.

Required capabilities:

- proposal creation for current/stale/supersede/retire/reject decisions
- review queue listing with evidence and confidence
- restricted authority decision gate
- read-after-write proof for local/test and approved production flows
- audit record for who/what promoted an object and from which evidence
- approved production promotion plan with explicit object classes, allowed actions, reviewer role, rollback path, and maximum blast radius
- authority demotion path for stale, superseded, retired, rejected, and archive-only states
- object decision history that can explain why a fact is current, stale, superseded, or retired
- separate permission scopes for human approval, agent proposal, production write, and read-only query

Gate evidence:

- proposal write never changes accepted/current authority
- restricted decision is denied by default in production
- approved local/test decision updates authority lane correctly
- approved production pilot updates only scoped object classes and proves read-after-write
- rollback or supersession can reverse or demote an accepted/current decision without deleting audit history
- audit trail cites proposal id, evidence refs, approver identity hash, before/after lanes, and decision reason
- stale/superseded/retired state is visible in object queries

### P5. Continuous Golden Query Quality Gates

State: planned; continuous from P1 onward.

Purpose:

Make LBrain quality measurable by user-meaningful questions, not by tool availability.

Required golden query families:

- temporal repo recall
- documentation cleanup
- stale/archive discovery
- code change impact analysis
- PR merge and deploy truth
- current SoT versus stale archive separation
- reference corpus freshness/source authority
- corpus-to-design concept extraction
- code style drift
- HTML/visualization review preference

Required evaluator families:

- extractor fixture quality
- function/evaluator debug trace
- LLM extraction variance
- model or prompt comparison when applicable
- corpus freshness/source-authority checks
- runtime truth evidence checks

Gate evidence:

- every phase has a phase-specific golden query slice
- each query returns object, edge, evidence, freshness, gap, and recommended action
- empty authority lanes are stated explicitly
- runtime claims require runtime evidence
- query routing does not fall back to generic safety/current cards for domain-specific questions
- failed queries are reported as product gaps, not hidden as successful tool calls

### P6. Session, Device, Project, And Work-Unit 360

State: planned.

Purpose:

Support questions across one PC, many PCs, one project, one branch, one session, PRs, specs, commits, and handoff contexts.

Required capabilities:

- hashed device/session/project identity
- bidirectional edges between `Device`, `Session`, `WorkUnit`, `Repository`, `Branch`, `Spec`, `PullRequest`, and `Commit`
- per-device answers and all-device project rollups
- temporal recall such as "어제 이 repo에서 뭐 했어?"
- handoff pack generated from current work objects and gaps

Gate evidence:

- one-device and all-device fixture queries produce different but compatible answers
- raw host/path is not exposed
- project rollup can cite sessions, specs, PRs, and commits without raw transcript body

### P7. Preference, Style, And Artifact Memory

State: planned.

Purpose:

Let AI tools start with the user's accepted preferences instead of rediscovering them each session.

Required capabilities:

- `PersonalCodeStyleProfile`
- `RepoStyleProfile`
- `HtmlReviewProfile`
- `VisualizationProfile`
- `ArtifactPreferencePack`
- inferred versus accepted preference separation
- evidence refs to examples without raw source/body storage
- diff/artifact review suggestions for style or preference drift

Gate evidence:

- inferred rule enters review/proposal state first
- accepted preference appears in agent context pack
- old code inertia is not automatically promoted into style authority
- HTML review artifact can be checked against accepted preference without requiring UI

### P8. Runtime Truth, Security, And Deployment Authority

State: planned.

Purpose:

Answer operational truth questions with merge, CI, deploy authority, and live runtime evidence separated.

Required capabilities:

- `PullRequest`, `Commit`, `CIStatus`, `DeploymentTarget`, `RuntimeSurface`, `RuntimeTruth`, and `LiveEvidenceGap`
- public repo CI separated from private deploy authority
- live runtime check represented as evidence, not assumption
- deployed artifact identity joined to source commit where available
- object/property/action permission model for runtime and authority claims
- application or agent-surface restrictions for who may read, propose, approve, or execute actions
- audit trail for permission-sensitive object reads and authority-changing actions

Gate evidence:

- merged PR does not imply deployed
- missing live evidence returns `runtime_evidence_unverified`
- private deploy authority is referenced without leaking private values
- permission denial is public-safe and does not leak protected object values
- action permission test proves an agent cannot promote authority without approved scope

### P9. Agent Context Productization

State: planned.

Purpose:

Make LBrain useful at agent startup and during review, not only through explicit search.

Required capabilities:

- compact context packs for Codex, Claude, Gemini, Hermes
- current authority, reference objects, style/preference, active work, guardrails, and required verification in one pack
- consumer-specific shaping from the same authority substrate
- degraded mode that states missing lanes and stale evidence
- application-surface policy that says which object types and actions each consumer can see or request
- proposal-safe action hints that distinguish "can suggest" from "can execute"

Gate evidence:

- each consumer can request a compact pack
- pack cites authority lane and gaps
- stale/no-recent-source state is visible instead of hidden
- consumer pack omits object properties and actions outside its allowed surface
- pack tells the agent which missing evidence must be gathered before promotion

### P10. UI And Object Browser Surface

State: planned; deferred and open.

Purpose:

Provide human inspection and review workflows after object contracts, authority lifecycle, and corpus store are stable.

Position:

UI is not required to activate CLI/MCP product quality. It remains open for later productization.

Possible surfaces:

- review queue UI
- corpus management UI
- object graph browser
- HTML dashboard for inspection
- work-unit and project 360 view

Entry criteria:

- P1 production MCP activation is complete
- P2 corpus status is usable
- P4 review lifecycle has stable object contracts
- P5 golden query quality shows object answers are useful enough to inspect visually

## Recommended Execution Order

P5 runs continuously across the sequence below. It is listed as a phase because the full golden-query suite becomes a release gate, but phase-specific evaluator slices must run during P1-P9.

1. P1 Production MCP Activation
2. P2 Living Reference Corpus Store
3. P3 Processing And Object Extraction Pipeline
4. P4 Review Queue And Authority Promotion
5. P5 Continuous Golden Query Quality Gates
6. P6 Session, Device, Project, And Work-Unit 360
7. P7 Preference, Style, And Artifact Memory
8. P8 Runtime Truth, Security, And Deployment Authority
9. P9 Agent Context Productization
10. P10 UI And Object Browser Surface

P5 must not be declared green until data, processing, authority, runtime, preference, and context lanes can support production-quality answers.

## Decision Rules

- If a feature can only work by treating candidate/search/archive data as current authority, do not ship it.
- If a corpus has no source URL or freshness check, return the gap instead of implying currentness.
- If a production path would write authority, require a separate approved production gate.
- If a query asks about deployment, require live evidence or return runtime gap.
- If an answer depends on raw private content, return a redacted evidence ref or deny the answer.
- If a UI would force premature object semantics, defer UI and fix the object contract first.
- If an action would change authority, require scoped permission, human approval, audit, and rollback or supersession path.
- If an object property or action is outside a consumer's allowed surface, omit or deny it instead of returning a partial secret.
- If a phase cannot improve its golden query slice, keep the phase in `planned` or `in_progress` even if code and tools exist.

## Progress Accounting

Use phase states, not percentages.

Allowed states:

- `not_started`
- `planned`
- `in_progress`
- `blocked`
- `local_validated`
- `production_validated`
- `complete`

Current accounting:

| Phase | State | Notes |
| --- | --- | --- |
| P0 Local Object Substrate Foundation | `complete` | complete for local/test scope |
| P1 Production MCP Activation | `in_progress` | `PASS_WITH_GAPS`; deployed/configured endpoint validated, current Codex session tool registry gap remains |
| P2 Living Reference Corpus Store | `planned` | corpus store not configured |
| P3 Processing And Object Extraction Pipeline | `planned` | skeletal extraction only |
| P4 Review Queue And Authority Promotion | `planned` | production authority write closed |
| P5 Continuous Golden Query Quality Gates | `planned` | baseline red exists; runs across P1-P9 |
| P6 Session, Device, Project, And Work-Unit 360 | `planned` | object types specified, productized/live flow missing |
| P7 Preference, Style, And Artifact Memory | `planned` | local profile seeds exist, productized workflow incomplete |
| P8 Runtime Truth, Security, And Deployment Authority | `planned` | local pack exists, live evidence and governance workflow incomplete |
| P9 Agent Context Productization | `planned` | local pack exists, production context and consumer policy not proven |
| P10 UI And Object Browser Surface | `planned` | deferred, open, non-prerequisite |

## Next Design Targets

The next `grill-to-spec` / `agentic-execution` loop should close the remaining P1 configured-agent read-path gap, then move to P2.

Recommended goal:

```text
Refresh the configured Codex LBrain MCP tool registry so object-native tools are available through the agent read path, without production authority mutation.
```

Expected outputs:

- activation `requirements.md`
- activation `design.md`
- deployment/read-path validation plan
- live MCP tool-list smoke
- read-only object query smoke
- deployed artifact identity check against exact commit, image, or build artifact
- production proposal/decision denial smoke
- phase-specific golden query slice result
- explicit no-mutation report

After P1, start P2 for the living reference corpus store.

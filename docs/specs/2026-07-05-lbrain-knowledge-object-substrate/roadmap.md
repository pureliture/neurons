# LBrain Ontology-Style Knowledge Product Roadmap

## Status

이 roadmap은 calendar가 아니라 evidence gate를 기준으로 진행합니다.

이 문서는 percentage completion을 부여하지 않습니다. 첫 formal denominator는 여기서 시작합니다: 각 phase는 gate evidence가 있고 production/read-path 상태가 정직하게 label될 때만 완료됩니다.

Current state:

- Phase 1 substrate implementation: local/test scope에서는 완료되었습니다.
- Production validation follow-up: `PASS_WITH_GAPS` overall; P1 MCP activation and P2 reference-corpus store gates are now `PASS` / `production_validated`, P3 deployed source-to-candidate review-loop evidence is validated with a remaining projection-join gap, and P4 deployed production authority gate policy/no-mutation, single-object bounded execution, rollback/demotion, and approval-board-to-production integration evidence are validated. Local/safety gates, deployed/configured HTTP MCP smoke, post-#115 source/image identity, production deploy-button rollout, object authority schema ensure, source/review/readiness MCP tool exposure, production denied/no-mutation smokes, six-route `brain_objects_query` object-pack activation proof, bounded production Palantir reference-corpus ingest evidence, deployed P2-corpus-to-candidate-review-loop evidence, P4 deployed proposal/decision gate policy plus denial/no-mutation readiness proof, P4 one-shot synthetic `RepoDocument` bounded reject execution, P4 fresh synthetic accepted-current rollback-to-archive execution, and P4 approval-board production promotion all passed. PR #95 was merged on 2026-07-07 with merge commit `32f4fec`; PR #97, PR #103, PR #105, PR #107, PR #109, PR #111, PR #113, PR #115, PR #119, PR #121, PR #122, PR #123, PR #124, PR #125, PR #126, and PR #128 are source/docs/evidence-gate follow-ups. Jenkins build #23 produced MCP HTTP image tag `sha-910a9cf24a70` from source `910a9cf24a70`, neurons-ops PR #15 updated production desired state at merge commit `cf3e7d1`, and Jenkins production deploy button #12 synced `neurons-oci-production` to that desired state. This is P1/P2 gate proof plus partial P3/P4 live proof, not product-wide production readiness: P3 live graph/Qdrant projection join, P4 full supersession/replacement-current pilot, and P6/P7/P8/P9 runtime evidence remain gaps.
- Post-deploy capture handoff: source-side CLI/MCP는 이제 기존 fail-closed runtime evidence normalizer/evaluator의 operator-friendly alias로 sanitized `post_deploy_capture` / `normalize_post_deploy_capture` 입력을 허용합니다. P8 product evidence checks는 alias packet/report metadata를 기록하고, post-deploy capture가 production readiness를 주장하거나 network-use provenance를 건너뛰거나 production mutation을 보고하면 fail closed로 처리합니다. 이는 ops runner handoff를 개선하지만 그 자체로 deploy, image identity, rollout, 또는 live runtime proof는 아닙니다.
- P1 Production MCP Activation: `PASS` for the activation gate / `production_validated`. Deployed HTTP MCP now exposes baseline object-native tools plus `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, `brain_approval_board_decide`, and `brain_source_to_candidate_runtime_readiness`; deployed and configured read paths return public-safe `brain_objects_query.v1` / `object_pack.v1`; production proposal/source-to-candidate/approval-board write paths deny or run no-mutation previews without authority writes; Jenkins #19, neurons-ops PR #12, and Jenkins production deploy #9 tie the P1 activation checkpoint to source `773ed7a1a1cd`; approved `object-authority-schema-ensure` executed against the server-backed ledger and postcheck six-route smokes report `authority_state_overlay_status=available`. Product-wide status remains `PASS_WITH_GAPS` because P3 projection join, P4 full supersession/replacement-current pilot, and P6/P7/P8/P9 runtime evidence remain gaps.
- P2 Living Reference Corpus Store: `PASS` / `production_validated`; local/test corpus policy, configured local/test store, first-class reference object rows, CLI/MCP status, idempotence, unscoped production-denial evidence, bounded production corpus ingest readiness evaluator, deployed `corpus-ingest` schema support, production deploy of source `9bdd780c2756`, live Palantir manifest count gate, bounded production ingest evidence, read-after-write corpus status, redaction postcheck, and repeated-ingest idempotence proof all passed. This is reference-corpus readiness only; it does not promote reference material to accepted/current authority and does not prove P3/P4 extraction/review/authority workflow readiness.
- P3 Processing And Object Extraction Pipeline: `PASS_WITH_GAPS` / `local_validated`; local/test reference corpus extraction preview는 deterministic objects, edges, public-safe chunk preview, strategy comparison, evaluator evidence, blocked-extraction gaps를 생성합니다. local_test `source-to-candidate-graph` CLI 및 `brain_source_to_candidate_graph` MCP tool은 configured reference corpus store를 candidate graph review pack으로 연결합니다. candidate graph review pack은 candidate objects/edges/evidence/confidence/supported edit actions를 surface하고 reviewer edit fixture는 authority mutation 없이 candidate object/edge/evidence state만 바꾸며 add/remove edge/evidence와 edge-ref sync를 검증합니다. `source-to-candidate-runtime-readiness` CLI 및 `brain_source_to_candidate_runtime_readiness` MCP tool은 post-deploy sanitized evidence packet을 PASS/PASS_WITH_GAPS/FAIL로 판정하고, `projection_join` packet field와 `live.source_to_candidate.projection_join` claim으로 graph/Qdrant projection join schema, runtime evidence class, non-empty edge count, no production mutation, redacted postcheck를 검증할 수 있습니다. Deployed runtime now reads the production P2 Palantir reference corpus store into a `candidate_graph_review` pack and validates a live `source_to_candidate_review_loop_evidence.v1` packet through candidate-review edit, local_test approval-board decision, read-after-write, and redaction postcheck. Live graph/Qdrant projection join remains unproven.
- P4 Review Queue And Authority Promotion: `PASS_WITH_GAPS` / `approval_board_production_validated`; local/test decision commit은 authority state/audit history를 기록하고, object queries는 local/test stale, superseded, retired, archive-only, rejected states를 surface하며, object explain은 local/test decision history를 반환합니다. local/test rollback decision은 accepted/current decision을 audit 삭제 없이 archive-only로 demote하고 `rollback_of_decision_id`를 decision/state/explain view에 보존합니다. `candidate-review-edit` / `approval-board-decide` CLI 및 `brain_candidate_review_edit` / `brain_approval_board_decide` MCP tools가 candidate edit에서 local_test approval-board preview까지 연결합니다. candidate review edit은 `target_scope`와 `mutation_mode=no_mutation`을 반환하고, production target 이름이 들어와도 pack preview만 바꾸며 authority/production mutation은 수행하지 않습니다. Runtime readiness는 이제 supplied review-loop evidence가 production ledger/corpus/runtime mutation, non-local authority scope, rejected edits, or raw private evidence를 보고하면 FAIL로 판정합니다. source-to-candidate activation preview는 sanitized `approval_board_runtime` evidence가 local_test authority write/read-after-write와 no-production-mutation을 증명할 때만 `approval_board_runtime_integration_unproven` gap을 제거합니다. Deployed HTTP MCP image `sha-910a9cf24a70` exposes `production_gate` schema for `brain_approval_board_decide`, keeps object-authority production writes default-disabled on the long-running HTTP service, requires the runtime flag `--allow-object-authority-production-writes` plus per-call `production_gate`, and live approval-board denial smoke reports `production_mutation_performed=false`, `proposal_write_performed=false`, `authority_write_performed=false`, `authoritative_memory_changed=false`, and `decision_count=0`. Bounded one-shot `mcp-stdio` operator executions opened the production write flag only for each process lifetime: prior smokes validated one synthetic `RepoDocument` reject and accepted-current rollback-to-archive execution, and the post-#128 deployed approval-board smoke promoted one synthetic `RepoDocument` through `brain_approval_board_decide` with `production_gate`, `proposal_write_target=production_ledger`, `authority_write_scope=production_ledger`, `decision_count=1`, and read-after-write authority lane `accepted_current`. Read-after-write, decision history, targeted queue statuses, redacted provenance, and `live.production.object_authority_bounded_execution` all validated. Full supersession/replacement-current production pilot remains a gap.
- P5 Continuous Golden Query Quality Gates: `PASS_WITH_GAPS` / `in_progress`; phase coverage report는 P1-P10 golden query families를 나열하고, source-to-authority quality gate는 source_to_candidate_graph, candidate_review_edit, approval_board_local_test, authority_read_after_write, production_decision_denial path를 검증합니다. candidate_review_edit path는 이제 object update뿐 아니라 add/remove evidence+edge, edge/evidence count, `target_scope=production`, `mutation_mode=no_mutation`, no rejected edits까지 gate evidence로 반환합니다. activation progress report는 P2-P9 scope, P2/P3/P4 minimum review-loop checkpoint, next phase P5, remaining P5-P9 gaps를 한 JSON gate로 반환합니다. `product_surface_checks`는 `brain_objects_query`, object-native MCP tool registry surface, runtime readiness tool, local_test/default production-denial policy를 함께 검증합니다. Branch-local `neuron-knowledge object-query` CLI는 MCP `brain_objects_query`와 같은 read-side route contract를 사용해 default authority/archive, style/preference, HTML/visualization preference, temporal work recall, code change impact, deploy/runtime gap routes를 반환하고, 각 returned object pack은 FR6 `route_trace`로 selected source lanes, confidence, stop reason, and missing evidence를 명시합니다. `code_change_impact` route는 파일 변경 질문을 `RepoFile`, `VerificationCommand`, `RuntimeSurface`, `McpTool` 및 `validated_by`/`requires_live_evidence` edges로 반환하고 `live_runtime_impact_unverified`, `source_freshness_unverified`, `production_mutation_forbidden` gaps를 유지합니다. `html_visualization_preference` route는 HTML review artifact 기준/선호 질문을 P7 artifact preference memory로 라우팅하고, accepted preference가 없으면 `accepted_html_preference_missing` 및 `visualization_preference_missing` gaps를 반환합니다. runtime readiness는 live `source_to_candidate.review_loop` claim으로 P3/P4 source→candidate→review→approval local_test loop smoke를 검증하고, live `object_authority_gate_policy` claim으로 production proposal/decision schema의 `production_gate`, runtime opt-in flag, per-call gate requirement를 확인하고, live `object_authority_bounded_execution` claim으로 sanitized `production_authority_execution` packet의 proposal/decision gate hash, single-object scope, read-after-write, rollback/supersession, postcheck, and protected-output false guards를 검증합니다. `live.evidence.provenance` claim은 evaluator 자체 `network_used=false`와 evidence 수집 경로의 `evidence_collection_network_used`를 분리하고, collection mode, mutation scope, redaction guard를 검증합니다. live agent context `tool_hints`는 suggest-only/no-execute/no-production-mutation safe targets와 approval-board scope blocker를 노출해야 합니다. `source-to-candidate-runtime-readiness --collect-shadow-evidence` and MCP `collect_shadow_evidence=true` now build a public-safe `source_to_candidate_runtime_evidence.v1` collector packet from branch-local read-only route smokes, a local_test source→candidate→review→approval shadow smoke, a local_test P6 session/project/work-unit rollup smoke, a local_test P7 preference/artifact memory smoke, a local_test P8 permission-sensitive audit denial/no-mutation smoke, and a local_test P9 agent-context startup/read-path smoke; the packet validates route-smoke/review-loop/session-rollup/preference-artifact/permission-audit/startup-read-path shape but keeps `collector_packet_not_live_evidence`, `network_used=false`, and `production_mutation_performed=false`. `product_evidence_checks`는 P2/P6/P7/P8/P9 evidence를 fail-closed로 검증하고, P2 evidence summary에는 `reference_corpus_production_ingest_readiness.v1`, P8 evidence summary에는 runtime evidence collection plan, `source_to_candidate_runtime_evidence_packet_template.v1` packet template, `source_to_candidate_runtime_shadow_collection_request.v1` route-smoke request, `source_to_candidate_runtime_shadow_collection_registration.v1` branch-local registration artifact, collector packet metadata, and post-deploy capture alias packet/report metadata를 포함하되 P2 `p2_production_corpus_ingest_evidence_unverified`, P6 `p6_live_multi_device_rollup_unproven`, P7 `p7_accepted_preference_context_pack_live_unproven`, P7 `p7_html_artifact_review_live_unproven`, P8 `p8_runtime_evidence_collection_plan_not_live_evidence`, `p8_runtime_evidence_packet_template_not_live_evidence`, `p8_runtime_evidence_collector_not_live_evidence`, route별 `p8_shadow_route_smoke_collection_pending:<route>`, route별 `p8_shadow_collection_run_pending:<route>`, P9 `p9_runtime_evidence_unverified`, P9 `p9_production_consumer_context_pack_live_unproven`, and P9 `p9_consumer_action_surface_runtime_policy_unproven` gaps를 명시적으로 유지합니다. post-deploy capture alias metadata는 `post_deploy_read_only_smoke`, `network_used=true`, `production_ready=false`, `production_mutation_performed=false`, and `PASS_WITH_GAPS`를 gate로 검증하며 live proof를 대신하지 않습니다. runtime readiness는 injected route smoke의 `object_pack_route_not_implemented`를 expected deployed identity 포함 여부와 분리해 판정합니다: expected commit identity가 없으면 route-specific not-validated gap이고, expected commit identity가 있는데 route가 fallback이면 FAIL입니다. report는 `local_quality_gate=green`, `production_approval_gate=preapproved`, `production_mutation_execution=not_performed_by_local_gate`를 분리해서 반환합니다. release quality gate는 명시적으로 `not_green` 상태로 유지합니다.
- P6 Session, Device, Project, And Work-Unit 360: `PASS_WITH_GAPS` / `local_validated`; 현재 증거는 local/test only입니다. local/test session project rollup preview는 Device/Session/Repository/Branch/WorkUnit/Spec/PullRequest/Commit objects를 생성하고, same-device와 all-device fixture rollup을 분리하며, safe handoff pack과 resume context를 반환합니다. runtime readiness는 P6 handoff의 visible/all-device session count 및 Session/WorkUnit ref count가 preview/resume evidence와 불일치하면 fail-closed로 처리합니다. local CLI/MCP `brain_objects_query` temporal work recall route는 "어제 이 repo에서 뭐 했어?"류 질의를 `WorkUnit` object pack으로 반환하고, runtime readiness는 live `temporal_work_recall` route smoke를 요구합니다. live multi-device runtime evidence는 아직 증명되지 않았습니다.
- P7 Preference, Style, And Artifact Memory: `PASS_WITH_GAPS` / `local_validated`; 현재 증거는 local/test only입니다. local/test artifact preference pack은 accepted/proposal lanes, profile objects, no-UI HTML artifact check, branch-local HTML/visualization preference object-query route, branch-local collector P7 preference/artifact memory packet shape, and post-deploy `html_visualization_preference` route-smoke contract를 검증하지만, live agent context pack 및 production authority promotion은 아직 gap입니다.
- P8 Runtime Truth, Security, And Deployment Authority: `PASS_WITH_GAPS` / `local_validated`; local/test runtime authority policy, artifact identity join, private authority redaction, preapproved-scope permission check, no-write local gate checks, local CLI/MCP deployment-runtime route with explicit `runtime_evidence_unverified` gap, branch-local sanitized bounded-execution evidence packet validation, and branch-local read-only collector packet generation for route smokes plus local_test review-loop, P6 session/project/work-unit rollup, P7 preference/artifact memory, P8 permission-sensitive audit denial/no-mutation, and P9 startup/read-path evidence pass. source-to-candidate activation preview can remove `production_authority_write_denied` only from sanitized `production_authority_write` evidence that proves a bounded single-object proposal/decision write, 64-hex approval ref hash, read-after-write, rollback/supersession, postcheck, and no protected output return; the preview itself still reports `production_mutation_performed=false`. Bounded production authority evidence now also requires the rollback/supersession path to include `demote_prior_object_to_accepted_non_current_or_archive_only`, full SHA-256 approval ref hash, and postcheck false guards for raw private evidence, secret, host topology, and raw external ids. Runtime readiness now fails closed if a `post_deploy_read_only_smoke` provenance claims bounded production mutation scope. Deployed/live production runtime authority execution evidence is present for single-object reject, rollback/demotion, and approval-board promotion smokes; live permission audit and startup/read-path evidence remain gaps.
- P8 production-readiness interpretation guard: post-deploy live-mode provenance with `network_used=false` is not live evidence and cannot set `production_ready=true`; it remains `PASS_WITH_GAPS` with `live_evidence_provenance_network_not_used_for_live_mode`.
- P9 Agent Context Productization: `PASS_WITH_GAPS` / `local_validated`; local/test consumer compact packs, degraded/stale disclosure, reference object lane, surface policy, proposal-safe action hints, `brain_objects_query` read-path hint, route-aware CLI read controls, object-native review/readiness `tool_hints`, and branch-local collector P9 startup/read-path packet shape pass. Runtime readiness report now checks whether live agent context contains required `tool_hints`, required product sections, product schema/consumer/degraded disclosure contract, mutation-disabled policy, safe suggest-only/no-production-mutation action-surface fields, allowlisted tool-specific safe targets, startup-loaded context, read-only `brain_objects_query`, runtime enforcement, and public-safe postcheck, but production startup/read path and runtime enforcement remain live gaps.
- Product activation: 완료되지 않았습니다; deployed source-to-candidate review-loop proof, P4 production authority gate policy/no-mutation proof, P4 single-object bounded reject proof, P4 rollback/demotion execution proof, and P4 approval-board promotion runtime integration proof는 추가되었지만 P3 live graph/Qdrant projection join, P4 full supersession/replacement-current pilot, and P6/P7/P8/P9 runtime packets이 여전히 필요합니다.
- UI/object browser: full UI는 product activation prerequisite가 아니지만, minimal candidate object/edge/evidence edit surface는 P3/P4 product workflow의 prerequisite입니다. Branch-local HTML/visualization preference route는 P7 read-path evidence이며 P10 object browser launch evidence가 아닙니다.

Roadmap lock state:

- This document is the planning SoT only for phase ordering and gate definitions.
- It is not proof that a phase is complete.
- A phase can move to `production_validated` only with live deployed/read-path evidence plus either a no-mutation report when mutation is out of scope, or bounded mutation evidence with postcheck/rollback criteria when mutation is explicitly in scope.

## Product Goal

LBrain should become a working knowledge product for development work, not just a memory retrieval surface.

The target is an ontology-style product in the practical sense:

- typed objects for work knowledge
- typed relationships between those objects
- evidence and freshness on claims
- AI-assisted candidate graph extraction from source material
- human correction of object, edge, and evidence candidates
- proposal/review/action lifecycle
- accepted/current authority separated from archive, reference, graph, search, and runtime evidence
- agent-facing context packs for Codex, Claude, Gemini, Hermes, and later tools

This is not a goal to clone Palantir. Palantir reference material is used to reduce the problem into needed mechanics: object, link, action, function, pipeline, governance, and application surface.

## Product Operating Model

The product direction is:

```text
source material
→ AI extraction
→ candidate KnowledgeObjects, edges, and evidence refs
→ human review and correction
→ approval board decision
→ accepted/current authority only after approval
```

Automatic extraction should help create and maintain the graph, but it must not directly rewrite production authority.

User-facing correction is part of the core workflow, not a cosmetic UI layer. A reviewer must be able to:

- rename or rewrite object claims
- add, remove, merge, or split objects
- add, remove, or correct typed edges
- attach, replace, or reject evidence refs
- promote, reject, hold, stale-mark, supersede, retire, or request more evidence

Authority rule:

```text
AI output = draft/candidate graph
human-approved decision = accepted/current authority
```

Therefore P3 and P4 must include a minimal editable review surface even if the full P10 object browser remains deferred.

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

- deployed/configured HTTP LBrain MCP exposes object-native tools plus source/review/readiness tools, and the current Codex session can call six required `brain_objects_query` routes with implemented object packs; P3 source-to-candidate review-loop evidence is live-validated, while remaining production gaps are P3 projection join plus broader P4/P6/P7/P8/P9 live evidence, not P1 activation.
- reference corpus store is configured and populated for the bounded Palantir reference corpus gate; this is reference-only corpus state, not accepted/current authority.
- golden queries are baseline red, not production-quality green.
- accepted/current promotion workflow is not open for production object decisions.
- object extraction and processing pipeline is wired through the deployed source-to-candidate review-loop preview, but live graph/Qdrant projection join proof is still missing.
- minimal editable object/edge/evidence review surface is live-proven for the deployed candidate review-loop smoke; full object-browser UX remains deferred.
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

State: `production_validated` for the activation gate as of the 2026-07-08 post-#115 production recheck.

Deployed HTTP MCP runtime activation and configured endpoint smoke passed for object-native availability. Post-#115 image/source identity, production deploy-button rollout, tools/list, approved schema repair, production denied/no-mutation smokes, and six-route `brain_objects_query` route activation are proven for the deployed/configured read path. The latest deployed runtime smoke exposes the source-to-candidate review/readiness tools, returns public-safe `brain_objects_query.v1` / `object_pack.v1` responses for all required routes, and keeps production authority mutation denied unless a later bounded gate opens it. This is P1 `PASS`; it is not product-wide production readiness because P3 projection join, P4 full supersession/replacement-current pilot, and P6/P7/P8/P9 live evidence remain open.

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

- historical P1 activation evidence records the Argo application tracking `main` and `Synced/Healthy`; after neurons-ops PR #12 and Jenkins production deploy button #9, the desired-state and live deployment image tag both pointed at the post-#115 MCP HTTP build `sha-773ed7a1a1cd`
- source repo `origin/main` contains PR #95 merge commit `32f4fec`, with source head `5c301c6` and review-follow-up commit `0c70111`
- source repo `origin/main` contains PR #97 merge commit `de62106`, a source/docs-only post-merge patch that updates this roadmap/report to post-#95 source-delivery status
- source repo `origin/main`은 PR #103 merge commit `30ad7d9`, PR #105 merge commit `d3a609e`, PR #107 merge commit `fa42134`, PR #109 merge commit `063f751`, PR #111 merge commit `d4f121b`, PR #113 merge commit `c85bbf6`, and PR #115 merge commit `773ed7a`를 포함합니다.
- 2026-07-08 KST post-#107 configured MCP read-only six-route smoke originally returned `object_pack_route_not_implemented` for `authority_archive_separation`, `code_style_preference`, `temporal_work_recall`, `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth`; this is now historical/superseded evidence.
- PR #111 source fix keeps object query read path fail-open when live authority overlay schema is unavailable, returning public-safe `authority_state_overlay_unavailable` instead of killing route responses.
- Jenkins build #18 produced MCP HTTP image tag `sha-d4f121bf32c4` from source `d4f121bf32c4`; this is historical post-#111 route-fix evidence.
- Jenkins build #19 produced MCP HTTP image tag `sha-773ed7a1a1cd` from source `773ed7a1a1cd`.
- neurons-ops PR #12 updated production desired state to image tag `sha-773ed7a1a1cd` and merged at `5ff673e`; Jenkins production deploy button #9 synced `neurons-oci-production` non-prune to that revision, and Argo `Synced/Healthy`, rollout, healthz, `tools/list=passed`, and `toolsCount=31` passed.
- deployed/current configured six-route smoke now returns `brain_objects_query.v1` / `object_pack.v1` for all required routes, with requested route matched, no `object_pack_route_not_implemented`, no sanitized internal error, and `production_mutation_performed=false`.
- cluster-internal tools/list proof reports 31 tools, including `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, `brain_approval_board_decide`, and `brain_source_to_candidate_runtime_readiness`; the earlier deploy-script `toolsList=gap` transport issue is superseded by Jenkins deploy button #9 `toolsList=passed`.
- live HTTP MCP `tools/list` exposes `brain_objects_query`, `brain_object_explain`, `brain_corpus_status`, `brain_corpus_ingest_plan`, `brain_object_proposal_create`, `brain_object_decision_commit`, and `brain_review_proposals`
- live read-only `brain_objects_query` returns `brain_objects_query.v1` in public-safe shape
- user-level Codex LBrain MCP config source includes object-native tools, and standalone smoke against the configured endpoint returns the same object-native tool list and read-only query shape
- latest standalone configured-endpoint smoke exposes the baseline object-native tools required at that checkpoint and denies production proposal/decision mutation with no authoritative memory change
- production-scope `brain_object_proposal_create` returns denied/no-mutation
- `brain_object_decision_commit` returns denied/no-mutation
- no production ledger/corpus mutation was performed
- current Codex-session live recheck after production mutation preapproval: review queue pre/post `count=0`; production-scope `brain_object_proposal_create` returned `permission=denied`, `proposal_write_performed=false`, `authority_write_performed=false`, and `authoritative_memory_changed=false`
- current Codex-session live recheck after production mutation preapproval: production-scope `brain_object_decision_commit` returned `permission=denied`, `authority_write_performed=false`, `authoritative_memory_changed=false`, and a promotion plan with `mutation_allowed=false`
- current Codex-session live tool discovery did not expose source-delivered `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, `brain_approval_board_decide`, or `brain_source_to_candidate_runtime_readiness`
- latest configured `mcp__lbrain.brain_objects_query` direct route smoke called the required six routes read-only: `authority_archive_separation`, `code_style_preference`, `temporal_work_recall`, `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth`
- latest configured route-smoke result: all six required routes returned public-safe `brain_objects_query.v1` envelopes with `object_pack.v1`, route matched, no fallback gap, no internal error, and no mutation.
- deployed MCP call smokes for `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, `brain_approval_board_decide`, and `brain_source_to_candidate_runtime_readiness` passed without production authority mutation: production source-to-candidate returned denied/no-mutation, candidate review edit ran `mutation_mode=no_mutation`, approval-board production decision returned denied/no-mutation, and runtime readiness with no evidence returned `PASS_WITH_GAPS` / `production_ready=false`.
- approved production `neuron-knowledge object-authority-schema-ensure` executed after deploy with `status=ensured`, `production_mutation_performed=true`, `server_backed_ledger=true`, and `protected_values_returned=false`; the postcheck six-route deployed HTTP MCP smoke reports `authority_state_overlay_status=available` for all six required routes.
- latest current Codex-session evidence keeps product-wide `production_ready=false` because P3 live graph/Qdrant projection join, P4 full supersession/replacement-current pilot, and P6/P7/P8/P9 runtime packets remain unproven.
- raw live evidence, host topology, private ledger details, and raw dataset/document ids remain outside this public repo

Next gate:

- collect sanitized live packets for the remaining P3 projection join, P4 full supersession/replacement-current pilot, P6 session/project/work-unit rollup, P7 preference/artifact memory, P8 permission-sensitive audit, and P9 startup/read-path evidence.
- run any production authority writes only through bounded approval-board, audit, rollback/supersession, redaction, and postcheck gates.
- keep local test success, source merge, CI image build, GitOps deploy, and live runtime evidence separated in every subsequent report.

### P2. Living Reference Corpus Store

State: `production_validated` / `PASS`.

Local/test reference corpus store gates pass, and the bounded production Palantir reference corpus ingest gate is now live-validated. This remains reference-only corpus state, not accepted/current authority.

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

Current evidence summary:

- sanitized reference corpus fixture maps to `ReferenceCorpus`, `DocumentSource`, `DocumentVersion`, `DocumentSnapshot`, `DocumentChunk`, `ExtractionRun`, and `FreshnessCheck` metadata without raw body return
- ingest plan reports manifest hash, hash verification state, source count, missing manual URL gap count, storage mode, raw body policy, and no writes planned
- `corpus-ingest-plan --manifest-file ...` loads an operator-supplied manifest read-only and reports source URL count, manual text count, source type distribution, and manifest hash
- sanitized full-count Palantir-shaped fixture proves the P2 count gate shape for 65 sources, 39 sources with URL, 26 manual text sources without URL, and PDF/Web/Text distribution 6/33/26 without raw body access
- `corpus-ingest-plan --expect-source-count ... --expect-source-url-count ... --expect-manual-text-without-url-count ... --expect-source-type-count ...` compares operator expected counts against the loaded manifest and returns `count_gate_status=pass` without writes
- expected-count mismatches return `count_gate_status=fail`, public-safe `count_gate_gaps`, CLI exit 1, and `writes_planned=false`
- MCP `brain_corpus_ingest_plan` schema and dispatch expose the same expected-count gate for read-only plan validation
- managed snapshot metadata carries raw-return denial, retention, redaction, deletion, and source-rights policy
- re-ingest produces stable corpus/source/snapshot/chunk/run ids for unchanged hashes
- content hash mismatch blocks extraction output instead of creating reference objects
- CLI and MCP `brain_corpus_status` report storage mode support and raw-body/source-rights policy even while the persistent reference corpus store is empty
- local/test ledger-backed `reference_corpus_bundles` store persists sanitized corpus metadata, keeps repeated ingest idempotent by corpus id, and returns read-after-write corpus status counts
- local/test ledger-backed first-class rows persist public-safe `DocumentSource`, `DocumentVersion`, `DocumentSnapshot`, `DocumentChunk`, `FreshnessCheck`, and `ExtractionRun` metadata separately from the aggregate bundle
- `brain_corpus_status` reports first-class store counts plus limited public-safe rows for document sources, versions, snapshots, chunks, freshness checks, and extraction runs
- CLI `corpus-ingest --target local_test --ledger ... --manifest-file ...` can load a sanitized manifest into the local/test store; production target remains denied before manifest/store write
- CLI `corpus-ingest --target local_test --manifest-file ...` and `corpus-status` can use configured `NEURON_REFERENCE_CORPUS_LEDGER` for a local/test store read-after-write path without printing the ledger path
- CLI production corpus ingest remains denied/no-mutation even when a local/test reference corpus ledger is configured
- CLI `corpus-ingest-readiness --evidence-file ...` validates sanitized `reference_corpus_production_ingest_evidence.v1` packets for operator approval, single-corpus scope, reference-only corpus lane, production corpus store write evidence, read-after-write corpus identity, rollback/deletion path, postcheck redaction, and provenance without running network or mutation in the evaluator
- missing production corpus ingest evidence returns `reference_corpus_production_ingest_readiness.v1` with `PASS_WITH_GAPS`, `production_mutation_performed=false`, `network_used=false`, and `production_corpus_ingest_evidence_unverified`
- sanitized bounded production corpus ingest evidence can report `PASS` and `production_mutation_performed=true` as evidence validation only; this means the supplied packet claims a bounded corpus write happened elsewhere, not that this local branch evaluation performed production mutation
- product evidence evaluation fails closed if P2 claims `PASS` without `live_evidence_provided=true`, or claims evidence collection without supplied live evidence; this prevents a local summary from being promoted as production corpus readiness
- MCP `brain_corpus_status` reads the local/test ledger-backed corpus store through `KnowledgeSearchService.core_brain()`
- ledger area boundary manifest assigns reference corpus bundle and first-class object tables to the LBrain object/native-memory area and the boundary guard passes
- P2 production-ingest readiness focused evidence: `cd worker && uv run pytest -q tests/test_reference_corpus.py::test_reference_corpus_production_ingest_readiness_without_evidence_preserves_gap tests/test_reference_corpus.py::test_reference_corpus_production_ingest_readiness_accepts_bounded_evidence_packet tests/test_reference_corpus.py::test_reference_corpus_production_ingest_readiness_fails_on_raw_body_or_authority_write tests/test_neuron_cli.py::test_neuron_knowledge_corpus_ingest_readiness_accepts_bounded_evidence_file tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p2_to_p9_scope_visible tests/test_golden_query_eval.py::test_product_evidence_summary_fails_when_p2_claims_pass_without_live_evidence`
- P2 production-ingest readiness focused result: `6 passed, 1 warning`
- P2/P5 related evidence: `cd worker && uv run pytest -q tests/test_reference_corpus.py tests/test_neuron_cli.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py`
- P2/P5 related result: `80 passed, 1 warning`
- focused evidence: `cd worker && uv run pytest -q tests/test_reference_corpus.py tests/test_neuron_cli.py tests/test_neuron_mcp_stdio.py`
- focused result: `98 passed, 1 warning`
- ledger boundary evidence: `cd worker && uv run pytest -q tests/test_ledger_area_boundaries.py`
- ledger boundary result: `10 passed`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1657 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`
- PR #119 added the approved bounded production corpus ingest gate and merged at `905d04b`.
- PR #121 added support for the actual Palantir `corpus-index` manifest schema and merged at `9bdd780`.
- Jenkins `neurons/ci/mcp-http` build #22 produced image `localhost:5000/neurons/mcp-http:sha-9bdd780c2756` from source `9bdd780c2756`; digest `sha256:55af7af2ee9b8807ff2e5365872cb78baed751bf30f7862814a283297184ca79`.
- neurons-ops PR #14 updated production desired state to image `sha-9bdd780c2756` and merged at `3145811`; Jenkins production deploy button #11 synced that desired state and reported `RESULT=PASS`, `toolsList=passed`, and `toolsCount=31`.
- Live Argo postcheck reported `Synced` / `Healthy` at desired-state revision `314581173d5f2703891e0956b509340294fca99b`; live `neurons-mcp-http` deployment used image `localhost:5000/neurons/mcp-http:sha-9bdd780c2756`, `readyReplicas=1`, `updatedReplicas=1`, and restart count `0`.
- Deployed `corpus-ingest` and `corpus-ingest-plan` help exposed `--corpus-name`, `--manifest-file`, `--expect-source-count`, `--expect-source-url-count`, `--expect-manual-text-without-url-count`, and `--expect-source-type-count`.
- Live read-only `corpus-ingest-plan` loaded the Palantir manifest with canonical manifest hash `sha256:d92aa9172381959ba1a33488d8a4934965249539588accf62d62e72ac84a94b5`, corpus id `rc:77c53587880f81dd`, source count `65`, source URL count `39`, manual-text-without-URL count `26`, source type counts `PDF=6`, `TEXT=26`, `WEB_PAGE=33`, and `count_gate_status=pass`.
- Approved bounded production `corpus-ingest` wrote the reference corpus only: `production_mutation_performed=true`, `authority_write_performed=false`, `protected_values_returned=false`, `raw_body_returned=false`, `secret_returned=false`, `host_topology_returned=false`, and `raw_external_ids_returned=false`.
- Live read-after-write status validated corpus id `rc:77c53587880f81dd`, manifest hash `sha256:d92aa9172381959ba1a33488d8a4934965249539588accf62d62e72ac84a94b5`, source count `65`, storage mode `managed_snapshot`, first-class `DocumentSource` / `DocumentVersion` / `DocumentSnapshot` / `DocumentChunk` / `FreshnessCheck` counts `65`, `ExtractionRun` count `1`, and no status gaps.
- Deployed `corpus-ingest-readiness` replayed the sanitized evidence packet and returned `reference_corpus_production_ingest_readiness.v1`, `status=PASS`, `failed_claims=[]`, `gaps=[]`, `live_evidence_provided=true`, and `evidence_collection_network_used=true`.
- Repeating the approved production ingest with the same manifest preserved stable source, document source, version, snapshot, chunk, freshness check, and extraction run counts, proving the P2 repeated-ingest idempotence gate.

Remaining gaps:

- P2 no longer has a production corpus ingest gap for the bounded Palantir reference corpus gate.
- P2 does not prove P3 extraction quality, live graph/Qdrant projection joins, P4 authority promotion, P6-P9 runtime evidence, or bounded production authority execution.
- Reference corpus rows remain `reference_only`; accepted/current authority still requires the P4 approval-board lifecycle.
- Raw private corpus bodies, raw source paths, raw dataset/document ids, secrets, host topology, and raw live evidence remain outside this public roadmap.

### P3. Processing And Object Extraction Pipeline

State: local_validated / PASS_WITH_GAPS.

Purpose:

Convert files, docs, sessions, PRs, commits, runtime evidence, code style, and artifact preferences into editable candidate knowledge objects and edges.

Required capabilities:

- extractor registry for repo documents, reference documents, sessions, work units, PRs, commits, tests, runtime surfaces, style rules, and artifact preferences
- extraction strategy registry for chunking, mapping, summarization, style inference, runtime evidence mapping, and preference inference
- extraction run records with input hash, output object ids, evaluator result, and failure reason
- extraction run records quality metrics, cost estimate, speed, token budget, and debug trace availability
- extracted objects and edges are marked candidate/draft until reviewed
- extraction output includes proposed edits, merge/split suggestions, confidence, and evidence gaps
- chunk preview and public-safe output preview exist before managed snapshot or corpus-derived object promotion
- reviewer can edit object names, claims, edge types, and evidence refs before approval
- evaluator supports deterministic fixture checks, golden query checks, variance checks, and model/prompt comparison where LLM extraction is used
- freshness checks separated from authority decisions
- public-safe projection for every object returned through MCP

Gate evidence:

- fixture extraction creates deterministic objects and edges
- failed extraction reports gaps instead of inventing authority
- extraction strategy comparison exists for Palantir corpus document mapping and repo document cleanup mapping
- chunk preview proves evidence can be inspected without raw/private leakage
- candidate graph preview shows objects, edges, evidence refs, confidence, and edit actions
- reviewer edit fixture changes candidate object/edge/evidence without changing accepted/current authority
- evaluator report ties extractor output to at least one golden query slice
- graph/search projection can join to objects but cannot become canonical authority

Current evidence:

- historical stacked branch note: this phase originally stacked on the P2 reference corpus store branch; the integrated P2-P9 source delivery is now merged through PR #95, but deployed/runtime proof is still separate
- extractor registry report exists for implemented `reference_corpus_manifest` and planned-gap extractor entries
- deterministic reference corpus extraction preview creates `ReferenceCorpus` and `ReferenceDocument` objects plus `member_of_corpus` edges
- chunk preview omits raw body storage refs and keeps raw body return denied
- extraction run preview reports quality metrics, zero model calls, zero LLM token budget, speed class, and debug trace availability
- hash mismatch blocks extraction output and reports gaps without inventing authority
- documentation cleanup strategy comparison compares `document_authority_pack_v1` against `path_inventory_only_v1`
- documentation cleanup strategy comparison reports lane counts, evidence counts, recommended action counts, and evaluator evidence for the current-vs-archive golden query slice
- full repo-document extraction preview maps repo inventory into `RepoDocument` objects, `supersedes` / `requires_evidence` edges, evidence refs, recommended actions, and extraction-run metrics
- full repo-document extraction preview reports missing accepted-current document lanes as gaps instead of inventing authority
- runtime truth extraction preview separates PR merge evidence from deployment/runtime truth
- runtime truth extraction preview returns `runtime_evidence_unverified` without inferring deploy from merge when live evidence is missing
- runtime truth extraction preview creates a candidate `RuntimeTruth` object and `validated_by` edge only when sanitized live evidence is explicitly `runtime_verified`
- preference/style extraction preview maps preference and repo-style memory cards into `ArtifactPreference` and `StyleRule` objects
- preference/style extraction preview reports source evidence refs without raw body inference and rejects raw-session-body inference as a strategy gap
- work-unit extraction preview groups session, PR, commit, and test evidence into a candidate `WorkUnit` object
- work-unit extraction preview emits evidence refs and `supported_by_evidence` / `validated_by` edges without raw transcript body return
- session-detail extraction preview maps session metadata into `Session` objects with `part_of_work_unit` and `supported_by_evidence` edges
- session-detail extraction preview keeps raw body return denied, reports ignored raw session body as a gap, and hashes sanitized metadata only
- PR/commit detail extraction preview maps PR metadata, commits, and test runs into separate `PullRequest`, `Commit`, and `TestRun` objects
- PR/commit detail extraction preview emits `includes_commit` and `validated_by` edges, reports missing test refs as gaps, and rejects merge-only runtime truth inference
- graph/search projection join preview maps derived graph/search hits into `ProjectionHit` objects and `projection_join` edges
- graph/search projection join preview keeps projection objects and edges in `derived_projection`, reports unknown join targets as gaps, and rejects projection-as-authority strategy
- local_test `source-to-candidate-graph` CLI reads the configured reference corpus store and returns a `candidate_graph_review` pack without ledger, production, or authority mutation
- `brain_source_to_candidate_graph` MCP tool exposes the same local_test store-to-candidate graph preview and keeps production target denied/no-mutation
- production `source-to-candidate-graph` target is denied/no-mutation before ledger access
- candidate graph review pack exposes candidate objects, edges, evidence refs, confidence, approval-board reviewer actions, supported edit actions, and minimal editable object/edge/evidence fields
- candidate reviewer edit fixture changes candidate object fields, edge type, and evidence summary; adds/removes candidate evidence and edges; synchronizes object `edge_refs`; rejects direct `authority_lane` edits and non-candidate authority lanes; preserves the original extraction hash; and keeps `authority_write_performed=false`
- broader evaluator suite preview aggregates deterministic fixture checks, golden-query checks, strategy comparison checks, variance checks, and model/prompt comparison status
- broader evaluator suite preview reports stable deterministic outputs as pass, reports changed outputs as `variance_detected`, and marks model/prompt comparison `not_applicable_no_llm` while all current preview extractors use zero model calls
- candidate review focused evidence: `cd worker && uv run pytest -q tests/test_object_packs.py`
- candidate review focused result: `14 passed, 1 warning`
- store-to-candidate CLI focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_extractor_registry_reports_implemented_and_gap_extractors tests/test_neuron_cli.py::test_neuron_knowledge_help_lists_server_owned_commands tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_uses_configured_local_test_store tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_denies_production_without_mutation tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_does_not_create_missing_local_store`
- store-to-candidate CLI focused result: `5 passed, 1 warning`
- store-to-candidate MCP focused evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_tool_list_exposes_object_substrate_tools tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_graph_and_review_approval_preview_roundtrip tests/test_neuron_mcp_stdio.py::test_mcp_approval_board_preview_denies_production_without_mutation`
- store-to-candidate MCP focused result: `3 passed, 1 warning`
- store-to-candidate CLI smoke: local_test configured store returns `status=PASS_WITH_GAPS`, `candidate_graph_review`, `candidate_count=3`, `accepted_count=0`, and all quality gate checks `PASS`
- store-to-candidate production-denial smoke: production target returns `status=denied`, `mutation_performed=false`, `production_mutation_performed=false`, `network_used=false`, and does not create the requested ledger path
- candidate review adjacent evidence: `cd worker && uv run pytest -q tests/test_object_packs.py tests/test_extraction_pipeline.py tests/test_llm_brain_core_objects_subpackage.py`
- candidate review adjacent result: `49 passed, 1 warning`
- focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py`
- focused result: `19 passed, 1 warning`
- adjacent regression evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py tests/test_object_packs.py tests/test_reference_corpus.py tests/test_neuron_cli.py tests/test_neuron_mcp_stdio.py tests/test_preference_authority_model.py tests/test_repo_style_profile.py tests/test_llm_brain_core_package_depth.py tests/test_llm_brain_core_objects_subpackage.py tests/test_llm_brain_core_layering.py`
- adjacent regression result: `180 passed, 1 warning`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1607 passed, 9 skipped, 1 warning`
- Deployed runtime evidence: live image `localhost:5000/neurons/mcp-http:sha-9bdd780c2756` read production reference corpus `rc:77c53587880f81dd` from the configured corpus store and returned `source_to_candidate_graph_activation.v1`.
- Deployed source-to-candidate graph preview used strategy `stored_reference_corpus_to_candidate_graph_v1`, input source count `65`, reference object count `65`, extraction run count `1`, and returned `quality_gate.source_to_candidate_graph=PASS`, `quality_gate.candidate_review_surface=PASS`, `quality_gate.authority_no_mutation=PASS`, `production_mutation_performed=false`, and `ledger_mutation_performed=false`.
- Deployed candidate graph review pack returned `object_pack.v1`, route `candidate_graph_review`, candidate count `21`, object count `21`, edge count `20`, evidence count `20`, `minimal_edit_surface.supported=true`, and `raw_body_return_capability=denied`.
- Deployed candidate-review edit smoke ran with `target_scope=local_test`, `mutation_mode=no_mutation`, accepted edit count `1`, rejected edit count `0`, `candidate_state_changed=true`, `authority_write_performed=false`, and `production_mutation_performed=false`.
- Deployed approval-board local_test smoke promoted one candidate in pack-local authority state with `authority_write_scope=local_test`, decision count `1`, read-after-write accepted-current count `1`, and `production_mutation_performed=false`.
- Deployed `source-to-candidate-runtime-readiness` replayed the sanitized live evidence packet with `evidence_is_live=true`, `evidence_collection_network_used=true`, `failed_claims=[]`, and validated `live.source_to_candidate.review_loop`; overall readiness remains `PASS_WITH_GAPS` because `live.source_to_candidate.projection_join` returned `not_validated` with `live_graph_qdrant_projection_join_unproven`.
- Deployed graph projection status reported `llm_brain_graph_projection_status.v1`, `status=ok`, but selected/projected counts were `0`; this is read-only status evidence and does not satisfy the P3 projection join edge-count gate.

PASS_WITH_GAPS rationale:

- Local/test P3 gate evidence is present for deterministic extraction, failed extraction gaps, strategy comparison, chunk preview, configured-store-to-candidate graph CLI wiring, candidate review/edit surface, evaluator reports, derived projection join authority separation, and a runtime-readiness evidence contract for sanitized projection join proof.
- Deployed/runtime P3 review-loop evidence is now present for production reference corpus read, candidate graph review pack creation, candidate edit no-mutation, local_test approval-board decision, read-after-write, and public-safe postcheck.
- The remaining live graph/Qdrant projection join proof requires configured runtime evidence with non-empty edge count; absent evidence remains `live_graph_qdrant_projection_join_unproven` and is not proven by local fixture tests or by graph projection status alone.
- This phase did not perform or claim production authority, corpus, graph, search, or deployment mutation.

Remaining gaps:

- P3 is not production-complete; local/test reference corpus extraction preview, repo-document extraction preview, documentation cleanup strategy comparison, runtime truth extraction preview, preference/style extraction preview, work-unit extraction preview, session-detail extraction preview, PR/commit detail extraction preview, graph/search projection join preview, and broader evaluator suite preview slices are implemented
- evaluator coverage is still local/test only; it covers reference corpus, repo-document cleanup, documentation cleanup, PR merge/deploy truth, preference/style, temporal work recall, session detail extraction, PR commit/test provenance, graph/search projection join, deterministic variance, and no-LLM model/prompt applicability
- graph/search projection join is proven only for local/test fixture hits, not for a live graph/Qdrant projection surface
- deployed/runtime source-to-candidate review-loop is live-proven against the production reference corpus store, but the live graph/Qdrant projection join edge-count gate is still missing
- no production authority, graph, search, or deployment mutation has been performed or claimed during this P3 evidence collection

### P4. Review Queue And Authority Promotion

State: `PASS_WITH_GAPS` / `approval_board_production_validated`.

Purpose:

Create a closed lifecycle from candidate object to accepted/current, stale, superseded, retired, rejected, or archive-only authority.

Required capabilities:

- approval board for candidate KnowledgeObjects, edges, and evidence refs
- proposal creation for current/stale/supersede/retire/reject decisions
- review queue listing with evidence and confidence
- reviewer actions for promote, reject, hold, merge, split, stale-mark, supersede, retire, and request more evidence
- reviewer edits preserve original AI extraction output as audit evidence
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
- approval board can show candidate object, candidate edges, evidence refs, conflicts, gaps, and recommended action
- reviewer edit changes candidate state without mutating accepted/current authority
- approved local/test decision updates authority lane correctly
- approved production pilot updates only scoped object classes and proves read-after-write
- rollback or supersession can reverse or demote an accepted/current decision without deleting audit history
- audit trail cites proposal id, evidence refs, approver identity hash, before/after lanes, and decision reason
- stale/superseded/retired state is visible in object queries

Current evidence:

- object-native proposal creation writes only to the local/test review queue and reports `proposal_write_performed=true`, `authority_write_performed=false`, and `authoritative_memory_changed=false`
- `brain_review_proposals` can read local/test proposal metadata after write without exposing raw/private evidence
- default and production-scope `brain_object_decision_commit` remains denied/no-mutation
- `brain_object_decision_commit` with explicit `ledger_scope=local_test` writes an `AuthorityDecision`, updates local/test object authority state, marks the proposal accepted, invalidates the authority cache, and returns read-after-write evidence
- local/test authority decision audit records proposal id, evidence refs, approver identity hash, previous authority lane, new authority lane, and decision reason
- `brain_objects_query` overlays local/test object authority state onto returned objects and lane indexes after a decision commit
- local/test object queries now surface stale, superseded, retired, archive-only, and rejected states without deleting audit history or mutating production
- `brain_object_explain` returns local/test authority state plus decision history for object ids with committed decisions, while still reporting that the object body comes from ledger state only when no object store is configured
- local/test `rollback_decision` preserves prior accepted/current audit history, demotes the object to `archive_only`, marks the rollback proposal `rolled_back`, and exposes `rollback_of_decision_id` through authority state and object explain
- production-scope `brain_object_decision_commit` remains denied/no-mutation and returns `object_authority_promotion_plan.v1` with allowed object class, decision types, reviewer role, required gate evidence, rollback path, blast radius, and no-mutation report
- branch-local `brain_object_proposal_create` 및 `brain_object_decision_commit`은 explicit per-call `production_gate` schema를 노출하며, runtime flag `--allow-object-authority-production-writes`만으로는 production scope mutation이 열리지 않습니다
- branch-local MCP stdio/http service wiring은 object authority production write를 기본 비활성화 상태로 유지하며, `--allow-object-authority-production-writes`를 명시적으로 선택한 경우에만 writable ledger를 엽니다
- branch-local production-gate focused test writes a single `RepoDocument` proposal to the test ledger using `ledger_scope=production`, commits a bounded `reject_candidate` decision, records `authority_write_scope=production_ledger`, and verifies read-after-write authority state and review-queue status without touching live production
- historical deployed runtime evidence: HTTP MCP image `localhost:5000/neurons/mcp-http:sha-9bdd780c2756` ran without `--allow-object-authority-production-writes`; it exposed `brain_object_proposal_create`, `brain_object_decision_commit`, `brain_candidate_review_edit`, `brain_approval_board_decide`, and `brain_source_to_candidate_runtime_readiness`, and both proposal/decision schemas included all nine `production_gate` fields.
- deployed production authority gate policy smoke: live `brain_source_to_candidate_runtime_readiness` validated `live.production.object_authority_gate_policy` with `default_enabled=false`, `per_call_gate_required=true`, runtime flag name `--allow-object-authority-production-writes`, no missing gate schemas, and `production_mutation_performed=false`.
- deployed production denial smoke: live `brain_object_proposal_create` without `production_gate` returned `permission=denied`, reason `proposal_write_requires_local_test_ledger_or_later_production_gate`, `proposal_write_performed=false`, `authority_write_performed=false`, `authoritative_memory_changed=false`, and `production_mutation_performed=false`.
- deployed restricted decision denial smoke: live `brain_object_decision_commit` without `production_gate` returned `permission=denied`, reason `restricted_tool_requires_human_gate`, `proposal_write_performed=false`, `authority_write_performed=false`, `authoritative_memory_changed=false`, and `production_mutation_performed=false`.
- deployed review queue postcheck stayed stable at `count=0 -> 0`; this proves the live denial smoke did not enqueue a proposal or change authority state.
- earlier deployed read-only runtime readiness replay returned `PASS_WITH_GAPS`, `production_ready=false`, `failed_claims=[]`, validated `live.evidence.provenance` with `post_deploy_read_only_smoke`, `network_used=true`, `mutation_scope=none`, and correctly kept `live.production.object_authority_bounded_execution` at `not_validated` with `bounded_production_authority_execution_unverified`.
- deployed one-shot production write preflight: a separate `mcp-stdio` process with `--allow-object-authority-production-writes` still denied a production proposal without per-call `production_gate`, preserved review queue count `0 -> 0`, and reported `proposal_write_performed=false`, `authority_write_performed=false`, and `production_mutation_performed=false`.
- deployed bounded proposal write: a separate one-shot `mcp-stdio` process with `--allow-object-authority-production-writes` and a valid per-call `production_gate` wrote exactly one synthetic `ko:RepoDocument:*` proposal to `production_ledger`; it returned `proposal_write_performed=true`, `proposal_write_target=production_ledger`, `authority_write_performed=false`, `authoritative_memory_changed=false`, `production_mutation_performed=true`, a full `sha256:` gate hash, and review queue count `0 -> 1` with the targeted proposal in `needs_review`.
- deployed bounded decision write: a second one-shot `mcp-stdio` process reused the same gate hash, committed one `reject_candidate` decision for the same synthetic `RepoDocument`, returned `authority_write_scope=production_ledger`, `authority_write_performed=true`, `authoritative_memory_changed=true`, `production_mutation_performed=true`, and updated the targeted review-queue item to `rejected`.
- deployed read-after-write: `brain_object_explain` for the synthetic object returned authority state lane `rejected`, the expected decision id, and one decision-history entry; the only selected gap was `authority_state_from_ledger_only`, which is expected for a synthetic object with no object-store body.
- deployed bounded-execution readiness replay: a sanitized `redacted_operator_packet` with `mutation_scope=bounded_production_authority_execution` validated `live.production.object_authority_bounded_execution`, `live.production.object_authority_gate_policy`, and `live.evidence.provenance`; the bounded execution claim reported `postcheck_status=validated`, `read_after_write_status=validated`, `rollback_or_supersession_status=planned`, object count `1`, `production_mutation_performed=true`, and no bounded-execution gaps. Overall readiness remained `PASS_WITH_GAPS` because this P4-focused packet did not include P3 projection join, full source-to-candidate review-loop, P6 rollup, or later runtime packets.
- deployed rollback/demotion execution safety review: 5.5/high verifier returned `GO_WITH_GAPS` with the constraint that rollback must use a fresh synthetic `RepoDocument`, first prove an `accepted_current` decision, then rollback that exact decision id; rejected-object rollback was explicitly treated as no-go for accepted/current demotion proof.
- deployed rollback/demotion execution: a fresh synthetic `ko:RepoDocument:*` was created through a one-shot `mcp-stdio` process with `--allow-object-authority-production-writes` plus per-call `production_gate`; the accept proposal committed `accept_current` to `accepted_current`, then a second proposal committed `rollback_decision` to `archive_only` with `rollback_of_decision_id` pointing to the accepted/current decision.
- deployed rollback/demotion postcheck: review queue count changed `1 -> 3`; the accepted proposal status is `accepted`, the rollback proposal status is `rolled_back`, final authority lane is `archive_only`, and object explain decision history starts with `[rollback_decision, accept_current]`, proving audit history was preserved instead of deleted.
- deployed rollback/demotion readiness replay: corrected sanitized `redacted_operator_packet` provenance validated `live.production.object_authority_bounded_execution` and `live.evidence.provenance` with `mutation_scope=bounded_production_authority_execution`, `rollback_or_supersession_status=validated`, `postcheck_status=validated`, `failed_claims=[]`, and protected-output flags all false. Overall readiness remained `PASS_WITH_GAPS` because P3 projection join, full source-to-candidate review-loop packet, P6 rollup, P7 preference/artifact memory, and P9 startup/read-path evidence were not included in this P4-focused packet.
- post-#128 source and CI identity: PR #128 merged approval-board production-gate integration at merge commit `910a9cf`; Jenkins `mcp-http` CI build #23 produced image tag `sha-910a9cf24a70` from that source.
- post-#128 GitOps deploy identity: neurons-ops PR #15 merged production desired state commit `cf3e7d1`, changing production `mcp-http` to `localhost:5000/neurons/mcp-http:sha-910a9cf24a70`; Jenkins production deploy button #12 ran `MODE=SYNC_MAIN`, requested non-prune sync, and finished `SUCCESS`.
- post-#128 Argo/runtime identity: `neurons-oci-production` reports `Synced/Healthy` at revision `cf3e7d17b283da52901d6389f6e6150e55d8cb8c`; `neurons-mcp-http` reports ready `1/1`, image `sha-910a9cf24a70`, and restarts `0`.
- post-#128 live tool schema proof: cluster-internal deployed HTTP MCP `tools/list` returns `toolsCount=31`, includes `brain_approval_board_decide`, and exposes `production_gate` fields `approval_ref`, `approved`, `configured_deployed_mcp_identity_matches_source`, `max_objects`, `no_raw_private_evidence`, `project`, `read_after_write_smoke_plan`, `rollback_or_supersession_plan`, and `scope`.
- post-#128 live approval-board denial/no-mutation smoke: long-running HTTP MCP with a valid per-call `production_gate` but without the runtime opt-in flag returns `permission=denied`, `reason=production_approval_gate_invalid`, `production_mutation_performed=false`, `proposal_write_performed=false`, `authority_write_performed=false`, `authoritative_memory_changed=false`, `decision_count=0`, and `production_write_state=closed_without_valid_production_gate`.
- post-#128 live approval-board production promotion smoke: a bounded one-shot `mcp-stdio` process inside the deployed `sha-910a9cf24a70` image used `--allow-object-authority-production-writes` plus per-call `production_gate` to promote exactly one synthetic public-safe `RepoDocument`; it returned `permission=allowed`, `proposal_write_target=production_ledger`, `authority_write_scope=production_ledger`, `production_mutation_performed=true`, `proposal_write_performed=true`, `authority_write_performed=true`, `authoritative_memory_changed=true`, `decision_count=1`, full `sha256:` gate-hash shape, read-after-write object match, final authority lane `accepted_current`, lifecycle `current`, review state `accepted`, decision type `accept_current`, and decision history count `1`.
- ledger boundary manifest assigns `object_review_proposals`, `object_authority_decisions`, and `object_authority_states` to the native-memory/object area
- candidate graph approval-board preview shows editable candidate object state, related edges, evidence refs, confidence, gaps, recommended action, allowed reviewer actions, and supported edit actions
- reviewer edit fixture changes only candidate object/edge/evidence state, supports add/remove edge/evidence, synchronizes object `edge_refs`, rejects direct authority-lane edits and non-candidate authority lanes, preserves original extraction hash, and performs no authority write
- local_test approval-board decision preview promotes a candidate object to `accepted_current`, records an `AuthorityDecision`, rebuilds lane indexes, and marks the write scope as local_test only
- production-scope approval-board decision preview is denied with `production_approval_gate_required` and returns a no-mutation promotion plan
- `candidate-review-edit` CLI applies reviewer JSON edits to candidate packs without authority or production mutation and returns explicit `target_scope` plus `mutation_mode=no_mutation`
- `approval-board-decide` CLI connects edited candidate packs to local_test approval-board decisions and keeps production target denied/no-mutation
- `brain_candidate_review_edit` and `brain_approval_board_decide` MCP tools expose the same preview flow for agent surfaces; candidate review edit remains no-mutation even when the call target is named `production`, and production approval remains denied/no-mutation
- review/approval CLI focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_extractor_registry_reports_implemented_and_gap_extractors tests/test_neuron_cli.py::test_neuron_knowledge_help_lists_server_owned_commands tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_uses_configured_local_test_store tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_denies_production_without_mutation tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_does_not_create_missing_local_store tests/test_neuron_cli.py::test_neuron_knowledge_candidate_review_and_approval_board_cli_chain_local_test tests/test_neuron_cli.py::test_neuron_knowledge_approval_board_cli_denies_production_without_mutation`
- review/approval CLI focused result: `7 passed, 1 warning`
- review/approval MCP focused evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_tool_list_exposes_object_substrate_tools tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_graph_and_review_approval_preview_roundtrip tests/test_neuron_mcp_stdio.py::test_mcp_approval_board_preview_denies_production_without_mutation`
- review/approval MCP focused result: `3 passed, 1 warning`
- candidate review focused evidence: `cd worker && uv run pytest -q tests/test_object_packs.py`
- candidate review focused result: `14 passed, 1 warning`
- CLI regression evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py`
- CLI regression result: `33 passed, 1 warning`
- object explain history evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_brain_object_explain_includes_local_authority_decision_history`
- object explain history result: `1 passed, 1 warning`
- production-denial plan evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_object_decision_commit_is_restricted_denied_by_default`
- production-denial plan result: `1 passed, 1 warning`
- runtime readiness production safety evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_requires_proposal_and_decision_production_safety_smokes`
- runtime readiness production safety result: `1 passed, 1 warning`
- runtime readiness production gate policy evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_object_authority_gate_schema_is_missing tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_object_authority_gate_schema_is_partial tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_object_authority_runtime_opt_in_is_unsafe tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_reports_schema_and_runtime_gate_failures_together tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence`
- runtime readiness production gate policy result: `5 passed, 1 warning`
- runtime readiness bounded execution evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_reports_bounded_execution_gate_hash_mismatch tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_accepts_bounded_execution_evidence_from_local_production_gate_simulation`
- runtime readiness bounded execution result: `2 passed, 1 warning`
- production gate focused evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_tool_list_exposes_object_substrate_tools tests/test_neuron_mcp_stdio.py::test_mcp_object_proposal_create_local_test_and_production_denial tests/test_neuron_mcp_stdio.py::test_mcp_object_authority_production_gate_writes_single_object_with_postcheck tests/test_neuron_mcp_stdio.py::test_mcp_object_decision_commit_is_restricted_denied_by_default tests/test_neuron_mcp_stdio.py::test_mcp_object_authority_local_test_write_requires_test_service_gate`
- production gate focused result: `5 passed, 1 warning`
- runtime opt-in focused evidence: `cd worker && uv run pytest -q tests/test_brain_steward.py::test_mcp_cli_object_authority_production_write_flag_requires_explicit_runtime_opt_in tests/test_brain_steward.py::test_mcp_cli_review_commit_flag_enables_only_review_commit`
- runtime opt-in focused result: `2 passed, 1 warning`
- focused evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_object_decision_commit_local_test_updates_authority_state_with_audit`
- focused result: `1 passed, 1 warning`
- object-query visibility evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_overlays_local_authority_state`
- object-query visibility result: `5 passed, 1 warning`
- rollback/audit lifecycle focused evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_object_authority_rollback_preserves_audit_history tests/test_neuron_mcp_stdio.py::test_mcp_tool_list_exposes_object_substrate_tools tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_overlays_local_authority_state tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_requires_bounded_execution_demote_step tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_accepts_bounded_execution_evidence_from_local_production_gate_simulation`
- rollback/audit lifecycle focused result: `10 passed, 1 warning`
- rollback/audit lifecycle related evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py tests/test_source_to_candidate_runtime_readiness.py tests/test_extraction_pipeline.py tests/test_golden_query_eval.py`
- rollback/audit lifecycle related result: `196 passed, 1 warning`
- rollback/audit lifecycle worker full evidence: `cd worker && uv run pytest -q`
- rollback/audit lifecycle worker full result: `1676 passed, 9 skipped, 1 warning`
- rollback/audit lifecycle root evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- rollback/audit lifecycle root result: `BUILD SUCCESSFUL`
- activation-progress demotion-step visibility focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p2_to_p9_scope_visible tests/test_golden_query_eval.py::test_product_evidence_summary_marks_p8_runtime_unverified_as_gap_not_pass tests/test_golden_query_eval.py::test_product_evidence_summary_fails_when_p8_collection_plan_is_missing_or_mutating`
- activation-progress demotion-step visibility focused result: `3 passed, 1 warning`
- activation-progress demotion-step CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --activation-progress`
- activation-progress demotion-step CLI smoke result: `status=PASS_WITH_GAPS`, `goal_complete=false`, `production_mutation_performed=false`, P8 product evidence includes `runtime_authority_bounded_execution_required_demote_step=demote_prior_object_to_accepted_non_current_or_archive_only` and `runtime_authority_bounded_execution_demote_step_required=true`
- MCP regression evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py`
- MCP regression result: `80 passed, 1 warning`
- object/model/boundary regression evidence: `cd worker && uv run pytest -q tests/test_object_packs.py tests/test_knowledge_objects.py tests/test_ledger_area_boundaries.py`
- object/model/boundary regression result: `23 passed, 1 warning`
- ledger boundary evidence: `cd worker && uv run pytest -q tests/test_ledger_area_boundaries.py`
- ledger boundary result: `10 passed`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1607 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`

PASS_WITH_GAPS rationale:

- Local/test P4 gate evidence is present for proposal creation, review queue listing, default production denial/no-mutation, candidate approval-board decision preview, local/test authority decision commit, audit state, stale/superseded/retired/archive/rejected object-query visibility, object decision history explainability, and explicit production-gate semantics.
- Production authority promotion and bounded production ledger/corpus/runtime mutation are preapproved, and branch-local MCP proposal/decision code now has a bounded gate shape. Runtime readiness can validate a sanitized bounded execution evidence packet from a local/fake-ledger production-gate simulation.
- Deployed/live P4 safety evidence is now present for tool schemas, default-disabled runtime policy, per-call gate requirement, proposal/decision denied-no-mutation smokes, stable review queue postcheck, and read-only provenance. This proves the deployed runtime is closed by default; it is not a production approval record or bounded execution proof.
- Deployed/live P4 bounded execution evidence is now present for a single synthetic `RepoDocument` proposal/decision path, one-shot runtime opt-in, gate hash continuity, production ledger proposal write, production ledger `reject_candidate` decision write, read-after-write, redacted operator provenance, and protected-output postcheck.
- Deployed/live P4 rollback/demotion evidence is now present for a fresh synthetic `RepoDocument`: `accept_current` created an `accepted_current` state, `rollback_decision` demoted it to `archive_only`, `rollback_of_decision_id` preserved the accepted/current decision lineage, decision history retained both decisions, targeted review queue states were `accepted` and `rolled_back`, and readiness validated bounded execution plus provenance.

Remaining gaps:

- full production supersession/replacement-current pilot remains unproven; live rollback/demotion execution is validated, but replacement object/current successor semantics have not been piloted
- candidate approval-board-to-production authority integration remains unproven; the live bounded execution used direct proposal/decision tools against a synthetic object, not a reviewed candidate graph object from the approval board
- product-wide readiness remains `PASS_WITH_GAPS` until P3 graph/Qdrant projection join, complete source-to-candidate review-loop packet, P6 rollup, P7 preference/artifact memory, P8 permission audit/runtime authority, and P9 startup/read-path evidence are attached together

### P5. Continuous Golden Query Quality Gates

State: `PASS_WITH_GAPS` / `in_progress`; continuous from P1 onward.

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

Current local/test evidence:

- `golden-query-eval --phase-coverage` returns `knowledge_object_phase_golden_query_coverage.v1`
- phase coverage report lists P1-P10 golden query families, required quality axes, evaluator owner, result status, and explicit gaps
- current report status is `PASS_WITH_GAPS` and `release_quality_gate=not_green`; it does not claim production-quality answers
- `evaluate_object_pack_response(..., required_axes=...)` can now enforce the full object, edge, evidence, freshness, gap, and recommended-action axis set without changing legacy evaluator callers
- strict evaluator fails empty authority lanes unless the gap list explicitly states the empty lane
- strict evaluator fails deployment/runtime truth claims unless runtime evidence or an explicit runtime evidence gap is present
- P1-P4 are represented as PASS_WITH_GAPS slices with their production/live gaps preserved
- source-to-authority quality gate covers source-to-candidate graph, candidate review edit, local_test approval-board promotion, authority read-after-write, and production decision denial paths
- local_test store-to-candidate CLI/MCP proof covers configured reference corpus store read, candidate graph review pack creation, and production target denied/no-mutation
- source-to-authority quality report now includes `product_surface_checks` for `brain_objects_query`, `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, `brain_approval_board_decide`, and `brain_source_to_candidate_runtime_readiness` MCP registry/policy surface
- `brain_objects_query` local MCP read path now returns context-authority object packs for broad authority/archive queries, preference/style object packs for style queries, HTML/visualization preference packs or explicit missing-evidence gaps for artifact-review preference queries, temporal work recall `WorkUnit` packs for session/work-resume queries, code-change-impact packs for "이 파일 바꾸면 어떤 테스트/런타임 영향 있어?" queries, and runtime truth gap packs for merge/deploy queries instead of falling back to `object_pack_route_not_implemented`
- `neuron-knowledge object-query` branch-local CLI now shares the same `BrainReadService.brain_objects_query` route contract as MCP, supports explicit `--route`, repeated `--current-file`, repeated `--object-type`, `--response-mode`, and allowlisted `--consumer`, and remains read-only/no-network/no-production-mutation
- 2026-07-08 KST source-side matrix guard locks the six required object-query routes across branch-local CLI inferred route, CLI explicit `--route`, and MCP stdio explicit route paths: `authority_archive_separation`, `code_style_preference`, `temporal_work_recall`, `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth` all return `brain_objects_query.v1` / `object_pack.v1` with route traces and without `object_pack_route_not_implemented`; this began as source/contract evidence, post-#111/#11 live smokes supplied the first configured/deployed route activation proof, and post-#115/#12/#9 smokes now supersede it with schema overlay `available`
- FR6 route trace is now attached to branch-local `brain_objects_query` object packs with `route`, `route_source`, non-empty `selected_source_lanes`, route `confidence`, `stop_reason`, and gap-derived `missing_evidence`; this explains routing decisions without treating local tests as deployed runtime proof
- `code_change_impact` branch-local route returns `RepoFile`, `VerificationCommand`, `RuntimeSurface`, and `McpTool` objects, `validated_by`, `requires_live_evidence`, and `exposes_tool` edges, and explicit `live_runtime_impact_unverified`, `source_freshness_unverified`, and `production_mutation_forbidden` gaps; it does not claim deployed runtime readiness from local tests
- `html_visualization_preference` branch-local route returns accepted HTML/visualization artifact preferences when present and otherwise reports `accepted_html_preference_missing` plus `visualization_preference_missing`; explicit route, generic-artifact negative routing, unrelated-preference filtering, write-time private preference rejection, and route-pack public-safe rejection are locked by tests; it is P7 read-path evidence, not P10 UI/object browser evidence
- source-to-candidate runtime readiness CLI can evaluate sanitized post-deploy evidence for MCP read/review tools, `brain_objects_query` route smokes, source-to-candidate review-loop smokes, agent context `tool_hints`, required agent context sections, deployed identity, production-denial/safety smokes, object authority production gate policy, bounded production authority execution evidence, and evidence provenance without network or mutation; required post-deploy object-query route smokes now include `authority_archive_separation`, `code_style_preference`, `temporal_work_recall`, `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth`; post-#115/#12/#9 populated P1 route/tool/denial activation proof, and P4 single-object bounded reject, rollback/demotion, and post-#128 approval-board production promotion evidence are now attached, but the readiness surface remains `PASS_WITH_GAPS` until projection-join, full supersession/replacement-current pilot, agent context, and P6/P7/P8/P9 evidence are supplied; agent-context tool hints must remain suggest-only/no-execute/no-production-mutation with safe targets and approval-board scope blockers, and production safety claims include source-to-candidate review-loop smoke, source-to-candidate denial, approval-board denial, proposal-create, decision-commit, object authority gate policy, object authority bounded execution, and evidence provenance
- runtime readiness packet template now includes `source_to_candidate_review_loop` with `source_to_candidate_review_loop_evidence.v1`; supplied evidence must prove source-to-candidate graph pack creation, candidate review edit no-mutation, approval-board local_test decision, read-after-write object pack, and public-safe postcheck, while no supplied evidence remains `PASS_WITH_GAPS` with `live_source_to_candidate_review_loop_unverified`
- runtime readiness rejects review-loop evidence that reports production mutation, non-local authority scope, candidate review authority writes, rejected edits, incomplete read-after-write, or raw/private/secret/topology/raw external id return
- source-to-candidate activation preview now accepts sanitized runtime `projection_join` evidence and removes `live_projection_join_unproven` only when that evidence is `object_extraction_projection_join_preview.v1`, `runtime_projection_join`, `status=pass`, non-empty edge count, and no production mutation
- runtime readiness packet template now includes `projection_join`, and readiness reports include `live.source_to_candidate.projection_join`; the claim validates `object_extraction_projection_join_preview.v1`, `runtime_projection_join`, `status=pass`, non-empty edge count, no production mutation, and redacted postcheck, while missing evidence remains `live_graph_qdrant_projection_join_unproven`
- source-to-candidate activation preview now also accepts sanitized runtime `approval_board_runtime` and `production_authority_write` evidence; it removes `approval_board_runtime_integration_unproven` only for local_test approval-board write/read-after-write/no-production-mutation proof, removes `production_authority_write_denied` only for bounded single-object authority execution proof with 64-hex approval ref hash, rollback/supersession, postcheck, and protected-output false flags, and keeps preview-local `production_mutation_performed=false`
- runtime readiness bounded production authority execution now fails closed unless the rollback/supersession path includes `demote_prior_object_to_accepted_non_current_or_archive_only`, approval ref hash is full `sha256:` + 64 hex, and postcheck explicitly reports no raw private evidence, secret, host topology, or raw external id return
- runtime readiness partial live evidence now decomposes broad `not_validated` states into actionable gap ids for missing live MCP tools, agent-context tool hints, agent-context sections, object-query routes, and deployed identity mismatch so post-deploy follow-up can target the exact missing proof without reading raw/private evidence
- activation progress report returns `lbrain_product_activation_progress.v1` with `scope_phases=[P2..P9]`, `minimum_review_loop_checkpoint.status=PASS_WITH_GAPS`, `next_phase=P5`, `remaining_phases=[P5..P9]`, `local_quality_gate=green`, `release_quality_gate=not_green`, `goal_complete=false`, `production_ready=false`, `production_approval_gate=preapproved`, `production_mutation_execution=not_performed_by_local_gate`, and `production_mutation_performed=false`
- activation progress keeps P10/deferred future-surface sentinels visible in phase coverage but excludes `future_phase_golden_query_slices_planned` and `future_phase_slices_planned` from P2-P9 `goal_completion_blockers`
- activation progress `product_evidence_summary` now includes P2 reference-corpus production-ingest readiness evidence, P6 session/project/work-unit rollup evidence, P7 artifact preference memory evidence, P8 runtime authority evidence, and P9 agent context product evidence as sanitized local previews
- P2 evidence summary includes `reference_corpus_production_ingest_readiness.v1`; without supplied live evidence it remains `PASS_WITH_GAPS`, `live_evidence_provided=false`, `production_mutation_performed=false`, and `production_corpus_ingest_evidence_unverified`
- P6 evidence summary includes `object_extraction_session_project_rollup_preview.v1`, `object_count=8`, `edge_count=16`, `evidence_count=1`, and `session_project_handoff_pack.v1`
- P7 evidence summary includes `object_extraction_preference_style_preview.v1`, accepted artifact preference pack status `pass`, and source evidence refs without raw body
- P8 evidence summary keeps merge/deploy/runtime separated with `runtime_unverified_count=1`, `runtime_verified_count=0`, production promotion `permission=allowed`, `permission_reason=approved_scope_present`, `authority_write_performed=false`, a post-deploy evidence packet template, a shadow route-smoke request for `authority_archive_separation`, `code_style_preference`, `temporal_work_recall`, `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth`, and a branch-local collector packet that validates route-smoke plus local_test review-loop, P6 session/project/work-unit rollup, P7 preference/artifact memory, P8 permission-sensitive audit, and P9 startup/read-path packet shape without claiming live evidence
- P8 product evidence treats absent source/image identity as `p8_source_commit_matches_pr_head_unverified` gap but explicit `source_commit_matches_pr_head=false` as `p8_source_commit_mismatch_with_pr_head` hard failure, so image/source mismatch cannot be hidden behind `PASS_WITH_GAPS`
- P9 evidence summary includes `tool_hint_safe_target_count` and `unsafe_tool_hint_count`; product evidence fails closed if tool hints have missing/non-allowlisted safe targets, allow execution, allow production mutation, omit approval-board scope blockers, or omit sanitized runtime-readiness target/raw-private blockers
- activation progress `product_evidence_checks`는 P2/P6/P7/P8/P9 모두 `result=PASS_WITH_GAPS`를 반환하며, P2 `p2_production_corpus_ingest_evidence_unverified`, P6 `p6_live_multi_device_rollup_unproven`, P7 `p7_accepted_preference_context_pack_live_unproven`, P7 `p7_html_artifact_review_live_unproven`, P8 `p8_runtime_evidence_unverified`, `p8_runtime_verified_evidence_missing`, `p8_runtime_evidence_collection_plan_not_live_evidence`, `p8_runtime_evidence_packet_template_not_live_evidence`, `p8_runtime_evidence_collector_not_live_evidence`, route-specific `p8_shadow_route_smoke_collection_pending:<route>`, route-specific `p8_shadow_collection_run_pending:<route>`, P9 `p9_runtime_evidence_unverified`, P9 `p9_production_consumer_context_pack_live_unproven`, and P9 `p9_consumer_action_surface_runtime_policy_unproven` gaps를 보존합니다. required phase evidence가 없거나, P2가 supplied live evidence 없이 PASS를 주장하거나, P9 tool hints가 unsafe이거나, permission audit/startup-read-path collector evidence가 없거나 불완전하거나, validating evidence 없이 mutation을 주장하거나, runtime evidence collection/template/collector/shadow request/registration이 network/live/mutation behavior를 주장하면 fail closed로 처리합니다.
- runtime readiness route-smoke claim now separates current-session deployment lag from deployed-regression evidence: an injected `deployment_runtime_truth` smoke with `object_pack_route_not_implemented` and no expected-commit identity is `PASS_WITH_GAPS` with `brain_objects_query_route_unimplemented:deployment_runtime_truth` plus `shadow_route_smoke_not_implemented:deployment_runtime_truth`; the same fallback with expected-commit identity is `FAIL`
- the same route-smoke claim now exposes `route_fallback_interpretation`, using `gap_until_deployed_identity_matches_expected_commit` before expected-commit identity is proven and `fail_expected_deployed_identity` when an expected-commit deployment still falls back to `object_pack_route_not_implemented`
- P9 evidence summary includes `agent_context_product_pack.v1`, Codex tool hints for `brain_objects_query` plus object-native review/readiness tools, style/preference section evidence, active work section evidence, and `mutation_allowed=false`
- `candidate_graph_review` packs state empty authority lanes explicitly so P5 strict axis checks do not hide candidate-vs-authority separation
- `code_change_impact` packs state empty authority lanes, Korean `런타임` runtime claims, runtime evidence gaps, and freshness gaps explicitly so FR8 strict-axis checks do not hide local-vs-live separation
- P6-P9 remain represented with local/test evidence plus production/live gaps, and P10 remains planned
- activation progress focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green tests/test_golden_query_eval.py::test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p2_to_p9_scope_visible tests/test_golden_query_eval.py::test_product_evidence_summary_fails_closed_when_required_phase_evidence_is_missing tests/test_golden_query_eval.py::test_product_evidence_summary_marks_p8_runtime_unverified_as_gap_not_pass tests/test_golden_query_eval.py::test_product_evidence_summary_fails_when_p8_collection_plan_is_missing_or_mutating tests/test_golden_query_eval.py::test_product_evidence_summary_fails_when_p9_active_work_is_missing`
- activation progress focused result: `7 passed, 1 warning`
- activation progress adjacent evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py tests/test_neuron_cli.py tests/test_extraction_pipeline.py tests/test_context_pack_builder.py`
- activation progress adjacent result: `90 passed, 1 warning`
- activation progress CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --activation-progress`
- activation progress CLI smoke result: `status=PASS_WITH_GAPS`, `local_quality_gate=green`, `release_quality_gate=not_green`, `minimum_review_loop_checkpoint.status=PASS_WITH_GAPS`, `next_phase=P5`, `goal_complete=false`, `production_ready=false`, `product_evidence_status=PASS_WITH_GAPS`, `production_approval_gate=preapproved`, `production_mutation_execution=not_performed_by_local_gate`, `product_evidence_summary phases=P2/P6/P7/P8/P9`, P2/P6/P7/P8/P9 `result=PASS_WITH_GAPS`, P2 gap `p2_production_corpus_ingest_evidence_unverified`, P6 gap `p6_live_multi_device_rollup_unproven`, P7 gaps `p7_accepted_preference_context_pack_live_unproven` and `p7_html_artifact_review_live_unproven`, P8 gap `p8_runtime_evidence_unverified`, P8 collection plan `source_to_candidate_runtime_evidence_collection_plan.v1`, P8 packet template `source_to_candidate_runtime_evidence_packet_template.v1`, P8 collector packet `source_to_candidate_runtime_evidence.v1`, P8 required bounded-authority demotion step `demote_prior_object_to_accepted_non_current_or_archive_only`, P8 collector gap `p8_runtime_evidence_collector_not_live_evidence`, P8 shadow request `source_to_candidate_runtime_shadow_collection_request.v1`, P8 registration artifact `source_to_candidate_runtime_shadow_collection_registration.v1`, P9 gaps `p9_runtime_evidence_unverified`, `p9_production_consumer_context_pack_live_unproven`, and `p9_consumer_action_surface_runtime_policy_unproven`, pending routes include `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth`, route counts are 6, `network_used=false`, `mutation_allowed=false`, `readiness_claim=plan_only_not_runtime_evidence`, P9 `section_counts.active_work=1`, `production_mutation_performed=false`
- source-to-authority gate focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation`
- source-to-authority gate focused result: `1 passed, 1 warning`
- source-to-authority CLI evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_source_to_authority_gate`
- source-to-authority CLI result: `1 passed, 1 warning`
- source-to-authority CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --source-to-authority-gate`
- source-to-authority CLI smoke result: `status=PASS_WITH_GAPS`, `local_quality_gate=green`, `release_quality_gate=not_green`, `production_mutation_performed=false`, `authority_write_scope=local_test`
- runtime readiness CLI smoke: `cd worker && uv run neuron-knowledge source-to-candidate-runtime-readiness --expected-commit 789b95cd2c248ee89394dcb20917a8e13d89db89`
- runtime readiness CLI smoke result: `status=PASS_WITH_GAPS`, `live_evidence_provided=false`, `production_mutation_performed=false`, `network_used=false`, `evidence_collection_network_used=false`, live MCP read/review tools/object query route smokes/source-to-candidate review-loop/context tool hints/context product sections/deployed identity/production denial/safety/object authority gate policy/object authority bounded execution/evidence provenance claims `not_validated`, and top-level gaps now include actionable missing proof ids such as `live_mcp_tool_missing:<tool>`, `live_source_to_candidate_review_loop_unverified`, `live_agent_context_tool_hint_missing:<tool>`, `live_agent_context_section_missing:<section>`, `live_brain_objects_query_route_missing:<route>`, `bounded_production_authority_execution_unverified`, and `live_evidence_provenance_unverified`
- runtime readiness sanitized current-session shadow packet result after post-#107 recheck was `PASS_WITH_GAPS` with route-unimplemented gaps; post-#115/#12/#9 direct configured/deployed-path smokes supersede that route gap by returning implemented object packs for all six routes, exposing source/review/readiness tools in live HTTP MCP, and reporting authority overlay availability, while missing live agent context sections, projection-join evidence, P4 full supersession/replacement-current pilot, and P6/P7/P8/P9 runtime packets remain unverified
- runtime readiness sanitized execution packet smoke result: no-evidence CLI returns `PASS_WITH_GAPS` with `bounded_production_authority_execution_unverified` and `live_evidence_provenance_unverified`; sanitized evidence-file CLI returns `PASS`, `production_mutation_performed=true`, `live.production.object_authority_bounded_execution.status=validated`, and `live.evidence.provenance.status=validated`; this is local/sanitized evidence replay, not live production mutation by this session
- runtime readiness provenance mode/scope guard evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_read_only_provenance_claims_bounded_mutation_scope tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_evidence_provenance_hides_bounded_mutation_scope tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence`
- runtime readiness provenance mode/scope guard result: `3 passed, 1 warning`; a packet that labels collection as `post_deploy_read_only_smoke` but claims `bounded_production_authority_execution` now fails with `live_evidence_provenance_read_only_mode_mutation_scope_mismatch`, preventing read-only smoke evidence from being promoted to production-ready bounded-mutation proof
- runtime readiness provenance live/network guard evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_post_deploy_mode_without_network_does_not_claim_live_or_ready`
- runtime readiness provenance live/network guard result: `1 passed, 1 warning`; a packet that labels collection as post-deploy but reports `network_used=false` remains `PASS_WITH_GAPS`, `evidence_is_live=false`, and `production_ready=false`
- source-to-candidate activation preview focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_source_to_candidate_graph_activation_preview_resolves_approval_and_production_gaps_from_evidence tests/test_extraction_pipeline.py::test_source_to_candidate_graph_activation_preview_resolves_projection_join_gap_when_evidence_present tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_uses_configured_local_test_store`
- source-to-candidate activation preview focused result: `3 passed, 1 warning`
- projection-join runtime readiness contract evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_extraction_pipeline.py tests/test_golden_query_eval.py`
- projection-join runtime readiness contract result: `105 passed, 1 warning`; missing runtime evidence remains `live_graph_qdrant_projection_join_unproven`, sanitized `projection_join` evidence validates `live.source_to_candidate.projection_join`, and unsafe/incomplete projection evidence fails closed
- projection-join runtime readiness adjacent evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py tests/test_neuron_cli.py tests/test_extraction_pipeline.py tests/test_golden_query_eval.py`
- projection-join runtime readiness adjacent result: `240 passed, 1 warning`
- activation-progress smoke evidence: `cd worker && uv run neuron-knowledge golden-query-eval --activation-progress`
- activation-progress smoke result: `status=PASS_WITH_GAPS`, `goal_complete=false`, `production_mutation_performed=false`, `release_quality_gate=not_green`, `runtime_evidence_collection_plan_required_step_count=13`, `runtime_evidence_packet_template_required_field_count=15`, and `live_graph_qdrant_projection_join_unproven` remains in blockers
- P5/P7 route-smoke contract evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py tests/test_neuron_cli.py tests/test_golden_query_eval.py`
- P5/P7 route-smoke contract result: `230 passed, 1 warning`; required post-deploy `brain_objects_query` route smokes now include `code_change_impact` and `html_visualization_preference`, route counts are 6, and the configured/deployed read path now has post-#115/#12/#9 route activation proof; broader readiness remains `PASS_WITH_GAPS`
- P5/P7 route-smoke activation-progress result: `status=PASS_WITH_GAPS`, `goal_complete=false`, `production_mutation_performed=false`, P8 `shadow_route_smoke_pending_routes` and `shadow_collection_registration_routes` include `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth`; this contract/template evidence is complemented by post-#115/#12/#9 deployed route proof, but it is still not full production readiness
- P5 six-route source matrix focused evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_object_query_required_routes_never_fallback tests/test_neuron_cli.py::test_neuron_knowledge_object_query_explicit_required_routes_never_fallback tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_required_routes_never_fallback`
- P5 six-route source matrix focused result: `18 passed, 1 warning`; this prevents source/CLI/MCP regression to `object_pack_route_not_implemented`, and post-#115/#12/#9 configured/deployed smokes now close the route activation gap while leaving broader runtime evidence gaps open
- production-readiness interpretation guard: runtime readiness reports now expose top-level `evidence_is_live`, `production_ready`, and `production_readiness`; sanitized/local evidence can still prove contract shape with `status=PASS`, but remains `production_ready=false` unless the provenance is live
- production-readiness live/network interpretation guard: runtime readiness now also requires evidence-side `network_used=true` before a live-mode provenance can set `evidence_is_live=true`
- P9 tool-hint safe-target guard evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_agent_context_tool_hint_targets_production tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_agent_context_tool_hint_allows_execution_or_mutation tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_approval_board_hint_lacks_approved_scope_blocker tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_runtime_readiness_hint_omits_sanitized_target_policy tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence`
- P9 tool-hint safe-target guard result: `5 passed, 1 warning`; required tool hints now fail closed if a permission-sensitive action surface advertises non-allowlisted `safe_targets` such as `production`, even when it remains suggest-only and includes `approved_scope_required`
- post-change local related evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py tests/test_neuron_cli.py tests/test_golden_query_eval.py tests/test_source_to_candidate_runtime_readiness.py`
- post-change local related result: `144 passed, 1 warning`
- post-change worker full evidence: `cd worker && uv run pytest -q`
- post-change worker full result: `1679 passed, 9 skipped, 1 warning`
- post-change root evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- post-change root result: `BUILD SUCCESSFUL`
- runtime readiness focused evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_evaluates_sanitized_evidence_without_mutation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_without_evidence_preserves_live_gaps tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_default_route_returns_agent_context_objects tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_temporal_route_returns_current_work_objects tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_style_route_uses_preference_objects tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_deploy_route_returns_runtime_gap_pack`
- runtime readiness review-loop focused evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_without_live_evidence_preserves_gaps_and_no_mutation tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_review_loop_smoke_mutates_production tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_review_loop_smoke_returns_private_or_incomplete_evidence`
- runtime readiness review-loop focused result: `4 passed, 1 warning`
- runtime readiness focused result: `37 passed, 1 warning`
- runtime readiness related evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py tests/test_neuron_cli.py tests/test_golden_query_eval.py`
- runtime readiness related result: `185 passed, 1 warning`
- focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- focused result: `1 passed, 1 warning`
- strict-axis evaluator evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_eval_strict_axes_require_edge_freshness_and_gap_fields`
- strict-axis evaluator result: `1 passed, 1 warning`
- empty-lane disclosure evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_eval_strict_axes_require_empty_authority_lane_disclosure`
- empty-lane disclosure result: `1 passed, 1 warning`
- runtime evidence gate evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_eval_strict_axes_require_runtime_evidence_for_runtime_claims`
- runtime evidence gate result: `1 passed, 1 warning`
- Korean runtime claim gate evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_requires_p5_p7_routes_for_post_deploy_live_smokes tests/test_golden_query_eval.py::test_eval_strict_axes_detect_korean_runtime_claims tests/test_golden_query_eval.py::test_code_change_impact_pack_passes_strict_axes_with_runtime_gap tests/test_object_packs.py::test_code_change_impact_pack_links_file_to_tests_and_runtime_surface tests/test_neuron_cli.py::test_neuron_knowledge_object_query_infers_code_change_impact_route tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_code_change_impact_route_returns_impact_pack`
- Korean runtime claim gate result: `6 passed, 1 warning`
- CLI evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_phase_coverage`
- CLI result: `1 passed, 1 warning`
- adjacent regression evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py tests/test_object_packs.py`
- adjacent regression result: `67 passed, 1 warning`
- broader adjacent regression evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py tests/test_golden_query_eval.py tests/test_neuron_cli.py tests/test_object_packs.py tests/test_llm_brain_core_objects_subpackage.py`
- broader adjacent regression result: `101 passed, 1 warning`
- object query route focused evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_object_query_accepts_explicit_html_visualization_route tests/test_neuron_cli.py::test_neuron_knowledge_object_query_infers_html_visualization_preference_route tests/test_neuron_cli.py::test_neuron_knowledge_object_query_does_not_infer_html_route_for_generic_artifact_review tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_html_visualization_route_uses_artifact_preferences tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_html_visualization_route_can_be_explicit tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_html_visualization_route_filters_unrelated_preferences tests/test_neuron_mcp_stdio.py::test_mcp_html_preference_memory_card_rejects_private_preference_text_before_route tests/test_neuron_mcp_stdio.py::test_brain_objects_query_html_visualization_route_rejects_private_pack_text`
- object query route focused result: `8 passed, 1 warning`
- CLI object query route evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py`
- CLI object query route result: `38 passed, 1 warning`
- CLI object query route smoke: `cd worker && uv run neuron-knowledge object-query --repository pureliture/neurons --branch codex/knowledge-object-review-flow-roadmap --route deployment_runtime_truth --query '이 PR merge됐어? 배포도 됐어?' --response-mode compact --consumer codex`
- CLI object query route smoke result: `brain_objects_query.v1`, `object_pack.v1`, `route=deployment_runtime_truth`, `runtime_evidence_unverified`, no `object_pack_route_not_implemented`
- CLI code-change-impact route smoke: `cd worker && uv run neuron-knowledge object-query --repository pureliture/neurons --branch codex/knowledge-object-review-flow-roadmap --query '이 파일 바꾸면 어떤 테스트/런타임 영향 있어?' --current-file worker/lib/agent_knowledge/llm_brain_core/objects/runtime_readiness.py --response-mode compact --consumer codex`
- CLI code-change-impact route smoke result: `brain_objects_query.v1`, `object_pack.v1`, `route=code_change_impact`, object types include `RepoFile`, `VerificationCommand`, `RuntimeSurface`, and `McpTool`, gaps include `live_runtime_impact_unverified`, `source_freshness_unverified`, and `production_mutation_forbidden`, `route_trace.selected_source_lanes=[candidate, reference_only]`, `route_trace.stop_reason=missing_evidence_gap_returned`, no `object_pack_route_not_implemented`
- CLI HTML/visualization preference route smoke: `cd worker && uv run neuron-knowledge object-query --repository pureliture/neurons --branch codex/knowledge-object-review-flow-roadmap --query '내가 선호하는 HTML review artifact 기준으로 이 산출물을 평가해줘.' --response-mode compact --consumer codex`
- CLI HTML/visualization preference route smoke result: `brain_objects_query.v1`, `object_pack.v1`, `route=html_visualization_preference`, gaps `accepted_html_preference_missing` and `visualization_preference_missing`, `route_trace.stop_reason=missing_evidence_gap_returned`, no `object_pack_route_not_implemented`
- object/CLI/MCP adjacent evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py tests/test_object_packs.py tests/test_neuron_mcp_stdio.py`
- object/CLI/MCP adjacent result: `160 passed, 1 warning`
- CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --phase-coverage`
- CLI smoke result: `status=PASS_WITH_GAPS`, `release_quality_gate=not_green`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1635 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`

Remaining gaps:

- P5 is not production-green; this slice adds source-to-authority path, FR6 route-trace, and FR8 code-change-impact route gate evidence, not production-quality golden answer generation or deployed route proof
- P6-P9 production/live slices and P10 product surface are still intentionally reported as gaps where runtime evidence is missing
- release quality remains `not_green` until data, processing, authority, runtime, preference, and context lanes can support production-quality answers

### P6. Session, Device, Project, And Work-Unit 360

State: `PASS_WITH_GAPS` / local_validated.

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

Current local/test evidence:

- `run_session_project_rollup_preview` maps redacted session metadata into `Device`, `Session`, `Repository`, `Branch`, and `WorkUnit` objects
- local/test rollup emits `repository_has_branch`, `session_on_device`, `session_in_repository`, `session_on_branch`, and `part_of_work_unit` edges
- same-device scope and all-device scope produce different visible session counts while preserving all-device rollup counts
- optional Spec, PullRequest, and Commit metadata can be linked into the same WorkUnit rollup with bidirectional object edges
- local/test rollup emits `session_project_handoff_pack.v1` from current work objects, edge counts, explicit gaps, and a `session_project_resume_context.v1`
- resume context carries latest session ref, active branch, work unit refs, linked Spec/PullRequest/Commit refs, local/test gaps, and live gap disclosure without raw transcript/body return
- local MCP `brain_objects_query` routes temporal/session/work-resume questions such as "어제 이 repo에서 뭐 했어?" to `temporal_work_recall` and returns `WorkUnit` objects from the `current_work` object pack
- runtime readiness now requires a live `temporal_work_recall` `brain_objects_query` smoke before P6/P9 startup/read-path proof can be called complete
- runtime readiness now includes `live.session_project.rollup`, requiring a sanitized `session_project_rollup_runtime_evidence.v1` packet for all-device session/project/work-unit rollup, safe handoff/resume context, `temporal_work_recall` read-after-write, and public-safe postcheck before P6 live runtime proof can pass
- P6 runtime readiness now cross-checks handoff `visible_session_count`, `all_device_session_count`, `Session` ref count, and `WorkUnit` ref count against the preview/resume evidence so a partial handoff cannot pass as a complete project rollup
- missing P6 runtime packet evidence remains `PASS_WITH_GAPS` with `live_session_project_rollup_unverified` and `live_multi_device_rollup_unproven`; unsafe or incomplete supplied evidence fails closed
- local path sentinels and source bodies are not returned
- P5 phase coverage now marks P6 as `PASS_WITH_GAPS` with `live_multi_device_rollup_unproven`, not `handoff_pack_not_implemented`
- P5 product evidence checks now also mark P6 as `PASS_WITH_GAPS` with `p6_live_multi_device_rollup_unproven` until deployed/live multi-device rollup evidence is attached
- focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_session_project_rollup_preview_separates_same_device_and_all_devices`
- focused result: `1 passed, 1 warning`
- bidirectional linked metadata evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_session_project_rollup_preview_links_specs_prs_and_commits_bidirectionally`
- bidirectional linked metadata result: `1 passed, 1 warning`
- handoff pack evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_session_project_rollup_preview_builds_safe_handoff_pack`
- handoff pack result: `1 passed, 1 warning`
- temporal MCP object query evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_brain_objects_query_temporal_route_returns_current_work_objects`
- temporal MCP object query result: `1 passed, 1 warning`
- runtime readiness temporal route evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_requires_temporal_work_recall_live_smoke`
- runtime readiness temporal route result: `1 passed, 1 warning`
- runtime readiness P6 packet gate evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_evidence_packet_template_is_public_safe_and_not_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_without_live_evidence_preserves_gaps_and_no_mutation tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_session_project_rollup_runtime_is_unsafe_or_incomplete tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_session_project_rollup_handoff_counts_do_not_match_preview`
- runtime readiness P6 packet gate result: `5 passed, 1 warning`
- runtime/MCP P6 packet gate adjacent evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_evaluates_sanitized_evidence_without_mutation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_collection_plan tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_packet_template tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_accepts_bounded_execution_evidence_from_local_production_gate_simulation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_without_evidence_preserves_live_gaps`
- runtime/MCP P6 packet gate adjacent result: `43 passed, 1 warning`
- phase coverage evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- phase coverage result: `1 passed, 1 warning`
- source-to-authority strengthened review-edit gate evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation`
- source-to-authority strengthened review-edit gate result: `1 passed, 1 warning`
- adjacent regression evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py tests/test_neuron_cli.py tests/test_extraction_pipeline.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py`
- adjacent regression result: `234 passed, 1 warning`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1661 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`

Remaining gaps:

- local/test P6 gate evidence is present for same-device/all-device fixture rollups, safe handoff/resume context generation, and bidirectional linked metadata edges
- branch-local runtime readiness can now validate or reject a sanitized P6 live rollup evidence packet, but that packet has not been collected from deployed runtime
- PR/commit/test provenance is covered only by local/test metadata fixtures, not live repository history
- live multi-device/project rollup evidence is unproven

### P7. Preference, Style, And Artifact Memory

State: local_validated.

Result: PASS_WITH_GAPS.

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

Local validation evidence:

- artifact preference pack gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_preference_style_extraction_preview_builds_artifact_preference_pack_lanes`
- artifact preference pack result: `1 passed, 1 warning`
- HTML artifact review gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_preference_style_extraction_preview_checks_html_artifact_without_ui`
- HTML artifact review result: `1 passed, 1 warning`
- runtime readiness P7 packet gate evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_evidence_packet_template_is_public_safe_and_not_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_without_live_evidence_preserves_gaps_and_no_mutation tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_preference_artifact_memory_is_unsafe_or_incomplete`
- runtime readiness P7 packet gate result: `4 passed, 1 warning`
- runtime/MCP P7 packet gate adjacent evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_evaluates_sanitized_evidence_without_mutation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_collection_plan tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_packet_template tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_accepts_bounded_execution_evidence_from_local_production_gate_simulation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_without_evidence_preserves_live_gaps`
- runtime/MCP P7 packet gate adjacent result: `44 passed, 1 warning`
- phase coverage gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- phase coverage result: `1 passed, 1 warning`

Implemented local/test scope:

- `ArtifactPreferencePack`, `PersonalCodeStyleProfile`, `RepoStyleProfile`, `HtmlReviewProfile`, and `VisualizationProfile` preview objects
- accepted versus proposal lane separation for preferences and style claims
- accepted preference context pack lane with public-safe evidence refs
- inferred preference and legacy style inertia routed to review/proposal lane first
- HTML review artifact summary/metrics preference check that does not require UI rendering and does not return artifact body
- diff/artifact review suggestions for HTML, visualization, and repo style drift
- runtime readiness now includes `live.preference_artifact.memory`, requiring a sanitized `preference_artifact_memory_runtime_evidence.v1` packet for accepted/proposal preference lane separation, accepted context-pack presence, explicit `html_visualization_preference` route smoke, no-UI/no-raw-body artifact review check, and public-safe postcheck before P7 live runtime proof can pass
- missing P7 runtime packet evidence remains `PASS_WITH_GAPS` with `live_preference_artifact_memory_unverified` and `accepted_preference_context_pack_live_unproven`; unsafe or incomplete supplied evidence fails closed
- P5 product evidence checks now also mark P7 as `PASS_WITH_GAPS` with `p7_accepted_preference_context_pack_live_unproven` and `p7_html_artifact_review_live_unproven` until deployed/live consumer evidence is attached

Remaining gaps:

- branch-local runtime readiness can now validate or reject a sanitized P7 preference/artifact memory evidence packet, but that packet has not been collected from deployed runtime
- accepted preference context pack is not live-proven in a deployed agent read path
- production preference/style authority promotion remains closed until an approved write gate exists
- HTML artifact check is local/test summary/metrics validation only, not a live product consumer workflow
- `html_visualization_preference` route is now part of the required post-deploy route-smoke contract, but no configured/deployed MCP read-path proof has been collected for it yet

### P8. Runtime Truth, Security, And Deployment Authority

State: local_validated.

Result: PASS_WITH_GAPS.

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

Local validation evidence:

- missing live evidence gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_runtime_truth_extraction_preview_keeps_merge_and_deploy_separate_without_live_evidence`
- missing live evidence result: `1 passed, 1 warning`
- live evidence object gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_runtime_truth_extraction_preview_creates_runtime_verified_object_only_with_live_evidence`
- live evidence object result: `1 passed, 1 warning`
- runtime authority policy gate: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_runtime_truth_extraction_preview_denies_authority_promotion_without_leaking_private_deploy_values`
- runtime authority policy result: `1 passed, 1 warning`
- runtime readiness permission audit packet gate evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_evidence_packet_template_is_public_safe_and_not_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_without_live_evidence_preserves_gaps_and_no_mutation tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_permission_sensitive_audit_is_unsafe_or_incomplete`
- runtime readiness permission audit packet gate result: `4 passed, 1 warning`
- runtime/MCP permission audit packet gate adjacent evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_evaluates_sanitized_evidence_without_mutation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_collection_plan tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_packet_template tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_accepts_bounded_execution_evidence_from_local_production_gate_simulation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_without_evidence_preserves_live_gaps`
- runtime/MCP permission audit packet gate adjacent result: `45 passed, 1 warning`
- phase coverage gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- phase coverage result: `1 passed, 1 warning`
- runtime evidence collection plan gate: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_evidence_collection_plan_is_public_safe_and_read_only tests/test_source_to_candidate_runtime_readiness.py::test_neuron_knowledge_runtime_readiness_cli_outputs_evidence_collection_plan tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_collection_plan`
- runtime evidence collection plan result: `3 passed, 1 warning`
- runtime evidence packet template gate: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_evidence_packet_template_is_public_safe_and_not_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_neuron_knowledge_runtime_readiness_cli_outputs_evidence_packet_template tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_packet_template`
- runtime evidence packet template result: `3 passed, 1 warning`
- runtime evidence collector gate: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_collector_builds_shadow_evidence_packet_without_mutation tests/test_source_to_candidate_runtime_readiness.py::test_neuron_knowledge_runtime_readiness_cli_collects_shadow_evidence tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_collects_shadow_evidence tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p2_to_p9_scope_visible tests/test_golden_query_eval.py::test_product_evidence_summary_marks_p8_runtime_unverified_as_gap_not_pass`
- runtime evidence collector result: `5 passed, 1 warning`
- runtime evidence collector CLI smoke: `source-to-candidate-runtime-readiness --collect-shadow-evidence` emits `source_to_candidate_runtime_evidence.v1` with `source_to_candidate_review_loop_evidence.v1`, `session_project_rollup_runtime_evidence.v1`, `preference_artifact_memory_runtime_evidence.v1`, `permission_sensitive_runtime_audit_evidence.v1`, and `agent_context_startup_runtime_evidence.v1`; evaluating that packet returns `status=PASS_WITH_GAPS`, `failed_claims=[]`, `live.brain_objects_query.route_smokes.status=validated`, `live.source_to_candidate.review_loop.status=validated`, `live.session_project.rollup.status=validated`, `live.preference_artifact.memory.status=validated`, `live.production.permission_sensitive_audit.status=validated`, and `live.agent_context.startup_read_path.status=validated` for local_test shadow evidence, `production_mutation_performed=false`, and `evidence_collection_network_used=false`

Implemented local/test scope:

- `PullRequest`, `Commit`, `CIStatus`, `DeploymentTarget`, `RuntimeSurface`, `RuntimeTruth`, and `LiveEvidenceGap` preview objects
- merge, CI, deployment target, artifact identity, and live runtime evidence are represented as separate evidence surfaces
- missing live evidence returns `runtime_evidence_unverified` and does not create a runtime-verified truth object
- deployment target identity joins artifact digest to source commit when provided, but remains runtime-unverified without live evidence
- private deploy authority is represented by presence/digest fields only; protected connection values are not returned
- runtime authority promotion without approved scope returns denied/no-mutation and records a public-safe audit event
- runtime readiness validates sanitized `production_authority_execution` evidence packets for single-object production authority proposal/decision execution, gate-hash continuity, full SHA-256 approval ref hash, read-after-write, rollback/supersession path, postcheck, and raw-private/secret/topology/raw-external-id redaction guards
- missing bounded execution evidence returns `bounded_production_authority_execution_unverified` instead of silently passing P8 production authority execution
- runtime readiness now includes `live.production.permission_sensitive_audit`, requiring a sanitized `permission_sensitive_runtime_audit_evidence.v1` packet for production-scope proposal/decision denial audit events, hashed actor/request refs, no authority write, no protected value return, audit-store recording, and public-safe postcheck before P8 audit proof can pass
- missing P8 audit packet evidence remains `PASS_WITH_GAPS` with `permission_sensitive_audit_unverified`; unsafe or incomplete supplied evidence fails closed
- runtime readiness validates sanitized evidence provenance so a post-deploy evidence packet must disclose collection mode, evidence-side network usage, mutation scope, and public-safe redaction checks without returning raw private evidence, secret, host topology, or raw external ids; read-only provenance (`post_deploy_read_only_smoke`) cannot also claim bounded production authority mutation scope
- live-mode provenance with `network_used=false` is `not_validated` with `live_evidence_provenance_network_not_used_for_live_mode` rather than `evidence_is_live=true`, preventing local or incomplete post-deploy packets from being reported as production-ready
- `source-to-candidate-runtime-readiness --evidence-collection-plan` and `brain_source_to_candidate_runtime_readiness(evidence_collection_plan=true)` return a public-safe post-deploy read-only collection plan for the required MCP tools, `brain_objects_query` route smokes, deployed identity, production denied/no-mutation checks, authority gate policy, and evidence provenance schema
- `source-to-candidate-runtime-readiness --evidence-packet-template` and `brain_source_to_candidate_runtime_readiness(evidence_packet_template=true)` return a public-safe template for the sanitized `source_to_candidate_runtime_evidence.v1` packet that a post-deploy runner must fill; the template itself is marked `template_only_not_runtime_evidence`
- `source-to-candidate-runtime-readiness --collect-shadow-evidence` and `brain_source_to_candidate_runtime_readiness(collect_shadow_evidence=true)` generate a branch-local read-only collector packet from object-query route smokes, a local_test source-to-candidate review-loop smoke, a local_test P6 session/project/work-unit rollup smoke, a local_test P7 preference/artifact memory smoke, a local_test P8 permission audit smoke, and a local_test P9 startup/read-path smoke; the packet is evaluator-ready but marked `collector_packet_not_live_evidence`

Remaining gaps:

- no live rollout artifact identity proof is attached to this branch-local collector packet itself; post-#115 image/source identity is tracked separately in P1 evidence
- branch-local runtime readiness can now validate or reject a sanitized P8 permission-sensitive audit evidence packet, but that packet has not been collected from deployed runtime
- production permission-sensitive audit flow is not live-proven
- production authority promotion is preapproved, but only branch-local/sanitized bounded execution packet validation exists; no deployed/live runtime write gate execution evidence is attached yet
- the collection plan is a branch-local template, not collected live evidence; it must not be reported as production readiness until an actual sanitized evidence packet is collected from the deployed read path
- the evidence packet template is branch-local handoff metadata, not collected live evidence; it must not be reported as production readiness until a sanitized packet is populated from the deployed read path and validated by runtime readiness
- the collector packet is branch-local read-only evidence preparation, not deployed read-path proof; post-#115/#12/#9 closes the separate P1 six-route/source-review-readiness activation proof, but local_test review-loop, P6 session/project/work-unit rollup, P7 preference/artifact memory, P8 permission audit, and P9 startup/read-path fields still must not be reported as production readiness until populated from the configured/deployed MCP read path with source/image identity proof

### P9. Agent Context Productization

State: local_validated.

Result: PASS_WITH_GAPS.

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

Local validation evidence:

- compact consumer pack gate: `cd worker && uv run pytest -q tests/test_context_pack_builder.py::test_builder_adds_consumer_specific_compact_agent_context_pack_with_safe_action_hints`
- compact consumer pack result: `1 passed, 1 warning`
- runtime readiness product contract gate: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_live_agent_context_product_contract_is_incomplete tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_runtime_readiness_hint_omits_sanitized_target_policy`
- runtime readiness product contract result: `2 passed, 1 warning`
- runtime readiness startup/read-path packet gate evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_evidence_packet_template_is_public_safe_and_not_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_without_live_evidence_preserves_gaps_and_no_mutation tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_agent_context_startup_runtime_is_unsafe_or_incomplete`
- runtime readiness startup/read-path packet gate result: `4 passed, 1 warning`
- runtime/MCP startup/read-path packet gate adjacent evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_evaluates_sanitized_evidence_without_mutation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_collection_plan tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_packet_template tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_accepts_bounded_execution_evidence_from_local_production_gate_simulation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_without_evidence_preserves_live_gaps`
- runtime/MCP startup/read-path packet gate adjacent result: `46 passed, 1 warning`
- phase coverage gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_pass_with_gaps_not_green`
- phase coverage result: `1 passed, 1 warning`

Implemented local/test scope:

- `agent_context_product_pack.v1` is attached to the context authority block for `codex`, `claude-code`, `gemini`, and `hermes`
- compact sections cover current authority, reference objects, style/preference, active work, guardrails, and required verification
- activation progress product evidence now fails closed if the P9 active work section is empty
- activation progress product evidence now fails closed if P9 object-native tool hints are unsafe even when the hint count is high enough
- consumer surface policy is read-only, omits protected properties, and keeps mutation disabled
- degraded mode exposes graph/runtime evidence gaps instead of hiding them
- stale memory count is visible in freshness metadata
- proposal-safe action hints distinguish `suggest_allowed` from `execute_allowed` and list missing evidence before promotion
- object-native read/review `tool_hints` list `brain_objects_query`, `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, and `brain_approval_board_decide` as suggest-only local_test/read-only preview surfaces with production mutation disabled; runtime readiness rejects non-allowlisted safe targets such as `production` in these hints
- runtime readiness now includes `live.agent_context.startup_read_path`, requiring a sanitized `agent_context_startup_runtime_evidence.v1` packet for startup-loaded context product, required sections, read-only `brain_objects_query`, mutation-disabled runtime enforcement, raw-private context blocking, approval-scope enforcement, stale/degraded disclosure, and public-safe postcheck before P9 live runtime proof can pass
- missing P9 startup packet evidence remains `PASS_WITH_GAPS` with `live_agent_context_startup_unverified` and `production_startup_read_path_unproven`; unsafe or incomplete supplied evidence fails closed
- P5 product evidence checks now also mark P9 as `PASS_WITH_GAPS` with `p9_runtime_evidence_unverified`, `p9_production_consumer_context_pack_live_unproven`, and `p9_consumer_action_surface_runtime_policy_unproven` until deployed/live startup/read-path and runtime policy evidence is attached
- object-native readiness `tool_hints` list `brain_source_to_candidate_runtime_readiness` as a suggest-only sanitized-evidence evaluator with production mutation disabled
- runtime readiness report includes live agent context `tool_hints` and `product_sections` claims so post-deploy startup/read-path evidence can be checked without upgrading local tests into runtime proof
- runtime readiness fails unsafe live agent context `tool_hints` when a required object-native tool allows direct execution, allows production mutation, omits safe targets, advertises non-allowlisted safe targets, or when `brain_approval_board_decide` lacks the `approved_scope_required` blocker
- runtime readiness fails incomplete live agent context products when schema/consumer/degraded gap disclosure or `missing_evidence_before_promotion` is absent, and requires the runtime-readiness tool hint to target `sanitized_evidence_packet` while blocking `raw_private_runtime_evidence`
- runtime readiness surfaces `live.production.object_authority_bounded_execution` so agent context/product-readiness checks can distinguish preapproved authority mutation from actual bounded execution evidence
- runtime readiness surfaces `live.evidence.provenance` so agent context/product-readiness checks can distinguish evaluator-local no-network execution from external live evidence collection
- runtime readiness exposes a plan/template mode for agents and operators to collect the missing post-deploy evidence without performing production mutation or returning protected values

Remaining gaps:

- production agent startup/read path has not live-proven these compact packs
- consumer action surface policy is local/test only, not enforced by deployed runtime
- production authority-changing actions are preapproved, but still require bounded scope, audit, rollback/supersession, redaction, and postcheck evidence before any production claim

### P10. UI And Object Browser Surface

State: planned; deferred and open.

Decision: DEFER.

Result: PASS_WITH_GAPS for start-readiness review.

Purpose:

Provide human inspection and review workflows after object contracts, authority lifecycle, and corpus store are stable.

Position:

Full UI is not required to activate CLI/MCP product quality. It remains open for later productization.

However, a minimal review/edit surface is required before P3/P4 can be considered product-ready, because users must be able to correct AI-extracted objects, edges, and evidence before authority promotion.

Possible surfaces:

- review queue UI
- corpus management UI
- object graph browser
- HTML dashboard for inspection
- work-unit and project 360 view
- minimal candidate object/edge/evidence editor for P3/P4

Entry criteria:

- P1 production MCP activation is complete
- P2 corpus status is usable
- P4 review lifecycle has stable object contracts
- P5 golden query quality shows object answers are useful enough to inspect visually

Defer decision evidence:

- P1 and P2 are now `production_validated` for their bounded gates, while broader production-readiness evidence gaps remain in P3/P4/P6/P7/P8/P9.
- P5 remains `in_progress` and release quality gate is not green.
- P8 and P9 are local/test validated only; broader production runtime authority, production permission audit, production startup/read path, and runtime enforcement remain gaps.
- UI is explicitly not a prerequisite for MCP/read-path activation, authority writes, or production rollout.
- P7 HTML/visualization preference routing is read-path evidence only and must not be treated as P10 object browser/UI launch evidence.

Decision outcome:

- Do not start UI/object browser implementation in this roadmap run.
- Keep P10 open as a later product surface after the read path, authority lifecycle, and quality gates are production-proven.
- Do not defer the minimal candidate edit/review surface needed by P3/P4.
- If P10 is later started, the first slice must be read-only/local, must use existing object packs, and must not introduce production mutation or protected-value disclosure.

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

Evidence result labels such as `PASS`, `PASS_WITH_GAPS`, and `FAIL` can appear in phase summaries and notes. They do not replace the progress states below.

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
| P1 Production MCP Activation | `production_validated` | `PASS` for P1 activation; deployed/configured endpoint object-tool smoke, post-#115 image/source identity, deploy-button rollout, tools/list, source/review/readiness tool exposure, approved schema repair, production denied/no-mutation smokes, and six-route object-pack proof pass. Product-wide readiness remains `PASS_WITH_GAPS` because P3 projection join, P4 full supersession/replacement-current pilot, and P6/P7/P8/P9 runtime evidence remain gaps |
| P2 Living Reference Corpus Store | `production_validated` | `PASS`; local/test store/status gates, bounded production corpus ingest readiness evaluator, deployed schema support, live Palantir manifest count gate, approved production reference-only ingest, read-after-write status, redaction postcheck, and repeated-ingest idempotence pass. This does not promote reference material to accepted/current authority |
| P3 Processing And Object Extraction Pipeline | `live_review_loop_validated` | `PASS_WITH_GAPS`; local/test extraction previews, store-to-candidate CLI/MCP wiring, candidate review/edit pack, branch-local readiness evidence gate, and deployed P2-corpus source-to-candidate review-loop smoke pass, but live graph/Qdrant projection join remains a gap |
| P4 Review Queue And Authority Promotion | `approval_board_production_validated` | `PASS_WITH_GAPS`; local/test authority state, audit gates, rollback decision lineage, review/approval CLI/MCP chain, approval-board preview, local_test promotion preview, reviewer edit no-mutation proof, branch-local review-loop readiness gate, approval-board runtime evidence gap closure, bounded execution packet shape, deployed production gate schema/policy, live approval-board denial/no-mutation smoke, stable review queue postcheck, one-shot synthetic `RepoDocument` production reject write, fresh accepted-current rollback-to-archive write, post-#128 approval-board production promotion write, read-after-write, decision-history lineage, redacted provenance, and bounded-execution readiness claim pass; full supersession/replacement-current pilot remains a gap |
| P5 Continuous Golden Query Quality Gates | `in_progress` | `PASS_WITH_GAPS`; phase coverage, source-to-authority path gate, FR8 code-change-impact route gate, P7 HTML/visualization route evidence, and P2-P9 activation progress gate exist; local quality gate is `green`, but release quality gate remains `not_green` |
| P6 Session, Device, Project, And Work-Unit 360 | `local_validated` | `PASS_WITH_GAPS`; local/test rollup, handoff gates, temporal `brain_objects_query` `WorkUnit` route, branch-local P6 runtime evidence packet validation, and P5 product evidence gap surfacing pass; deployed/live multi-device runtime evidence remains a gap |
| P7 Preference, Style, And Artifact Memory | `local_validated` | `PASS_WITH_GAPS`; local/test artifact preference pack lanes, no-UI HTML artifact check, branch-local HTML/visualization preference route, post-deploy `html_visualization_preference` route-smoke contract, branch-local P7 runtime evidence packet validation, and P5 product evidence gap surfacing pass; deployed/live agent context pack and production authority promotion remain gaps |
| P8 Runtime Truth, Security, And Deployment Authority | `local_validated` | `PASS_WITH_GAPS`; local/test runtime authority policy, artifact identity join, private authority redaction, denial/no-mutation checks, sanitized source-to-candidate review-loop packet validation, sanitized bounded execution packet validation with required demotion step, sanitized activation-preview production-authority evidence gap closure, sanitized permission-sensitive audit packet validation, evidence provenance validation, current-session shadow evidence packet normalization, 6-route post-deploy route-smoke contract, branch-local collector packet generation, and one-step shadow readiness evaluation pass; broader deployed/live production runtime authority and permission audit remain gaps |
| P9 Agent Context Productization | `local_validated` | `PASS_WITH_GAPS`; local/test consumer compact packs, degraded/stale disclosure, surface policy, proposal-safe action hints, branch-local P9 startup/read-path packet validation, and P5 product evidence gap surfacing pass; deployed/live production startup/read path and runtime enforcement remain gaps |
| P10 UI And Object Browser Surface | `planned` | `PASS_WITH_GAPS` for start-readiness review; full object browser deferred, but minimal P3/P4 candidate edit/review surface is now a prerequisite |

Delivery integration status:

- PR #84부터 PR #95까지 `main`에 merge되었습니다. PR #97, PR #103, PR #105, PR #107, PR #109, PR #111, PR #113, PR #115, PR #119, PR #121, PR #122, PR #123, PR #124, PR #125, PR #126, and PR #128은 post-#95 source/docs/evidence-gate follow-up으로 `main`에 merge되었습니다.
- PR #95 merged the integrated P2-P9 roadmap branch at source head `5c301c6` with merge commit `32f4fec`. Review-follow-up commit `0c70111` addressed the production gate/type and corpus approval-hash review findings before merge. Its runtime-readiness surface includes a public-safe normalizer, branch-local read-only collector packet generation for route smokes plus local_test review-loop, P6 session/project/work-unit rollup, P7 preference/artifact memory evidence, P8 permission-sensitive audit evidence, P8 bounded execution protected-output postcheck validation, and P9 startup/read-path evidence, one-step readiness evaluator for current-session shadow evidence packets, P6 session/project/work-unit rollup packet validation, P7 preference/artifact memory packet validation, P8 permission-sensitive audit packet validation, and P9 startup/read-path packet validation. This is merge/source evidence only, not deploy or live runtime evidence.
- PR #103은 기존 sanitized shadow evidence normalizer/evaluator에 operator-facing post-deploy capture aliases를 추가했습니다. PR #105는 post-deploy capture alias metadata를 기록하되 이를 production readiness로 취급하지 않는 product evidence gates를 추가했습니다. PR #107은 P6/P7/P9 product evidence checks가 live gaps를 plain `PASS`가 아니라 `PASS_WITH_GAPS`로 보존하도록 바꾸었습니다. PR #109는 post-#107 configured read-path fallback gap을 기록했고, PR #111은 live authority overlay schema gap에서 `brain_objects_query`가 fail-open object pack을 반환하도록 고쳤습니다. PR #113은 post-#111/#11 live route proof를 문서화했고, PR #115는 approved production schema repair surface를 추가했습니다.
- Final head and merge SHAs below are GitHub delivery evidence only. They are not deploy, live runtime, or production readiness evidence.
- P1 through P10 phase branches were cleaned up or are eligible for cleanup after merge verification.
- This delivery record closes P1 production MCP activation after #115/#12/#9 deployment and schema repair postcheck, closes the bounded P2 production Palantir reference corpus ingest gate after #119/#121/#14/#11 deployment and live read-after-write/idempotence postchecks, records P3 deployed source-to-candidate review-loop validation, records P4 deployed production authority gate policy/no-mutation validation, records P4 single-object bounded production authority reject execution validation, records P4 rollback/demotion execution validation, and records P4 approval-board-to-production promotion validation after #128/#15/#12 deployment. It does not close the P3 live graph/Qdrant projection join, P4 full supersession/replacement-current pilot, P5 release-quality `not_green` status, or P6-P9 production/live proof gaps.
- Historical PR body previews and issue drafts remain in `pr-delivery-package.md`; use the delivery record below as the current SHA source.

Merged PR delivery record:

| Phase | PR | Branch | Final/current head | Merge commit | Base |
| --- | --- | --- | --- | --- | --- |
| P1 Production MCP Activation | #84 | `codex/p1-production-mcp-activation-live` | `9cf7f9b` | `dea6f8d` | `main` |
| P2 Living Reference Corpus Store | #85 | `codex/p2-living-reference-corpus-store` | `c0695ba` | `7295092` | `main` |
| P3 Processing And Object Extraction Pipeline | #86 | `codex/p3-processing-object-extraction-pipeline` | `09b88a2` | `2740766` | `main` |
| P4 Review Queue And Authority Promotion | #87 | `codex/p4-review-authority-promotion` | `db8caec` | `45eb6cd` | `main` |
| P5 Continuous Golden Query Quality Gates | #88 | `codex/p5-continuous-golden-query-quality` | `912e3bf` | `9d0c3cc` | `main` |
| P6 Session, Device, Project, And Work-Unit 360 | #89 | `codex/p6-session-device-project-workunit-360` | `cb1a016` | `9f4969b` | `main` |
| P7 Preference, Style, And Artifact Memory | #90 | `codex/p7-preference-style-artifact-memory` | `9409b4a` | `af08043` | `main` |
| P8 Runtime Truth, Security, And Deployment Authority | #91 | `codex/p8-runtime-truth-security-deployment-authority` | `8191d48` | `4ace498` | `main` |
| P9 Agent Context Productization | #92 | `codex/p9-agent-context-productization` | `c6854e3` | `2d9c92a` | `main` |
| P10 UI And Object Browser Surface | #93 | `codex/p10-ui-object-browser-defer-decision` | `8c483d0` | `7bda476` | `main` |
| Integrated P2-P9 Source-To-Candidate Activation | #95 | `codex/knowledge-object-review-flow-roadmap` | `5c301c6` | `32f4fec` | `main` |
| Post-#95 Roadmap Source State | #97 | `codex/post-95-roadmap-state` | `e9f44b5` | `de62106` | `main` |
| Post-Deploy Capture Evidence Input | #103 | `codex/lbrain-live-evidence-collector` | `e57d753` | `30ad7d9` | `main` |
| Post-Deploy Capture Product Gate | #105 | `codex/lbrain-post-deploy-capture-gate` | `a6a33c6` | `d3a609e` | `main` |
| P6-P9 Product Evidence Gap Surfacing | #107 | `codex/lbrain-activation-progress-next` | `9b46989` | `fa42134` | `main` |
| Post-#107 Live Gap Status | #109 | `codex/lbrain-post-107-roadmap-state` | `e1c94c6` | `063f751` | `main` |
| Authority Overlay Schema Gap Fail-Open | #111 | `codex/lbrain-live-overlay-schema-gap` | `767af5a` | `d4f121b` | `main` |
| Post-#111 Live Route Proof Docs | #113 | `codex/lbrain-post-live-route-proof-docs` | `612d0c2` | `c85bbf6` | `main` |
| Object Authority Schema Repair Surface | #115 | `codex/lbrain-authority-overlay-schema-bootstrap` | `52f722d` | `773ed7a` | `main` |
| P4 Bounded Execution Evidence | #125 | `codex/lbrain-p4-bounded-execution-roadmap` | `a79c342` | `d7f27d8` | `main` |
| P4 Rollback Execution Evidence | #126 | `codex/lbrain-p4-rollback-execution` | `2b2175c` | `4e06152` | `main` |
| P4 Approval-Board Production Gate | #128 | `codex/lbrain-p4-approval-board-production` | `65525ed` | `910a9cf` | `main` |

PR creation gate:

- Required: linked issue number or explicit user approval for GitHub PR mutation.
- Required PR body constraint: include a real closing reference such as `Closes #N`.
- If no linked issue exists, prepare PR body previews but do not create PRs.
- Do not claim merge, CI, deploy, or live runtime evidence from branch push alone.

## Next Design Targets

The next agentic-execution loop should keep the full product activation scope, not stop at P4:

```text
P2 source/corpus storage
→ P3 candidate graph extraction and editing
→ P4 approval-board promotion
→ P5 quality gates
→ P6 project/session/work-unit rollup
→ P7 preference/artifact memory
→ P8 runtime authority
→ P9 agent context productization
```

Continue production read-path validation when the configured LBrain MCP object-native route smokes can be proven from the active agent path. Production authority writes are preapproved, but must still execute only through the approval board, audit trail, rollback/supersession path, scoped promotion gate, redaction, and postcheck flow.

Recommended goal:

```text
LBrain source-to-candidate-graph product activation through P9, with full P10 object browser deferred and minimal P3/P4 edit/review surface included.
```

Expected outputs:

- source/corpus storage requirements
- candidate extraction design
- minimal candidate object/edge/evidence edit surface contract
- approval-board promotion contract
- candidate object/edge/evidence edit fixtures
- no-authority-mutation proof for extraction and reviewer edits
- local/test promotion read-after-write proof
- P5 quality gate evidence for source-to-graph, review, approval, and authority read paths
- P6 project/session/work-unit rollup evidence
- P7 preference/artifact memory evidence
- P8 runtime authority evidence with merge, CI, deploy, and live runtime separated
- P9 agent context pack evidence
- production promotion gate plan with human approval, audit, rollback/supersession, and scoped object classes
- golden query slices for source-to-graph, review, approval, and authority read paths
- no production mutation
- no protected content, credentials, topology, or raw external ID output
- clear PASS / PASS_WITH_GAPS / FAIL result with live gaps preserved

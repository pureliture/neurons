# LBrain Ontology-Style Knowledge Product Roadmap

## Status

이 roadmap은 calendar가 아니라 evidence gate를 기준으로 진행합니다.

이 문서는 percentage completion을 부여하지 않습니다. 첫 formal denominator는 여기서 시작합니다: 각 phase는 gate evidence가 있고 production/read-path 상태가 정직하게 label될 때만 완료됩니다.

Current state:

- Phase 1 substrate implementation: local/test scope에서는 완료되었습니다.
- P6까지 Production validation baseline: bounded production validation은 P1, P2, P6에만 적용됩니다. P3는 deployed review-loop/projection-join evidence만 검증된 `local_validated`이고, P4는 `replacement_current_production_validated`이며, P5는 `local_validated`이지만 release-quality evaluator gate가 `green`입니다. 아래에는 각 phase의 historical merge, image, deploy-button, live runtime evidence를 계속 기록하되, 이를 이후 source revision의 증거로 사용해서는 안 됩니다.
- Post-#200 P7-P9 최종 증거 checkpoint: 전체 결과는 `PASS_WITH_GAPS`이며 hard failure는 `0`, production mutation은 `0`입니다. Source delivery는 neurons PR #199 merge `8bbe093fbd27`과 review follow-up PR #200 merge `98065d2f7e3c`이고, follow-up 전 전체 worker regression `2091 passed, 10 skipped` 및 root Gradle `BUILD SUCCESSFUL`을 포함해 보고된 모든 source check를 통과했습니다. Canonical image build evidence는 component image tag `sha-98065d2f7e3c`입니다. 첫 graph-trigger GitOps push는 image verify/push 성공 후 실패했지만, 범위를 GitOps로 제한한 recovery가 성공했으므로 build와 GitOps update를 혼동하지 않습니다. Deployment desired state는 neurons-ops PR #42 merge `8242aa4ab23d`이며, Jenkins production `PRECHECK_ONLY` #28과 `SYNC_MAIN` #29는 모두 `SUCCESS`였고 실행된 모든 sync/rollout/postcheck stage도 `SUCCESS`였습니다. Live Argo는 별도로 `targetRevision=main`, `Synced`, `Healthy`, revision `8242aa4ab23d`를 보고했습니다. 의도한 source-tagged production image ref 8개는 모두 canonical image tag였고 stale target ref는 `0`이었으며, active target replica 5개는 모두 ready, paused worker의 desired 값은 `0`, restart total은 `0`, MCP/ingress health check는 `ok`였습니다.
- Post-#200 sanitized live read evidence: 최종 packet은 deployed MCP registry 전체 개수가 아니라 이번 capture에 필요한 allowlisted object-native tool 9개를 관측했습니다. `brain_objects_query` route 6개는 `object_pack.v1`을 반환했고, `code_style_preference`와 `html_visualization_preference`는 각각 `accepted_current` preference 하나를 노출했습니다. Agent context product에는 비어 있지 않은 current-authority/style-preference/active-work/guardrail/verification section이 포함되었고, deployed identity에는 예상 source commit, startup receipt validation에는 `validated`, artifact preference application receipt에는 `PASS`, protected-output flag에는 false가 기록되었습니다. In-process product evaluation은 P7을 `PASS`로 판정하지만, persisted P7 product-evidence replay는 non-serializable collector capability를 의도적으로 잃으므로 실패하지 않고 `PASS_WITH_GAPS`로 남습니다. 같은 activation-progress artifact의 최상위 P7 `phase_progress`에는 이전의 일반 gap인 `accepted_preference_context_pack_live_unproven`, `html_artifact_review_live_unproven`이 여전히 남아 있으므로, P7 complete를 선언하지 않고 product-evidence와 phase aggregation을 분리해 보고합니다. Persisted overall/P7/P8/P9 replay는 hard failure 없이 `PASS_WITH_GAPS`, `production_ready=false`를 유지합니다. `production_gate` 없이 수행한 `brain_approval_board_decide`, `brain_object_proposal_create`, `brain_object_decision_commit` live call은 모두 proposal/authority/production mutation false로 거부되었고 review-queue count/object-read hash도 변하지 않았습니다. P8에는 여전히 live audit-store recording evidence가 없고 capture packet은 별도로 검증된 GitOps state를 bind하지 않습니다. P9에는 Claude Code, Gemini, Hermes의 deployed startup proof, runtime action interception, 실제 Codex host startup hook, enforced consumer action-surface policy가 아직 없습니다. Full P10은 계속 deferred입니다.
- Post-#204 P8 GitOps/live evidence binding closure: 사용자가 지적한 in-packet binding gap 자체는 `PASS`로 닫혔고, P8 전체는 permission-sensitive audit-store 증거만 남아 `PASS_WITH_GAPS`입니다. Source delivery는 neurons PR #204 merge `73d440522e49`이며 GitHub PR checks와 review gate는 모두 green입니다. 별도 local source verification은 전체 worker regression `2166 passed, 10 skipped`와 root Gradle `BUILD SUCCESSFUL`입니다. Canonical build는 source `73d440522e49`에서 runtime component 6개의 verify/image push를 완료했습니다. Bulk semantic component의 최초 job은 build/push 이후 canary GitOps update 충돌로 전체 `FAILURE`였고, build를 반복하지 않은 bounded GitOps-only recovery가 `SUCCESS`였으므로 build와 GitOps recovery를 분리합니다. GitOps desired state는 neurons-ops PR #44 merge `4270ab24cc55`이며 active production image ref 8개가 모두 같은 source tag를 가리키고 stale ref는 `0`입니다. Jenkins production `PRECHECK_ONLY` #30과 non-prune `SYNC_MAIN` #31은 모두 `SUCCESS`; live Argo는 `targetRevision=main`, `Synced`, `Healthy`, revision `4270ab24cc55`를 보고했습니다. 동일 sanitized evidence packet 안에서 external expected source commit, desired ops revision, Argo reconciled revision, desired/live image-set hash, deployed identity를 결합했고 `deployment_evidence_binding.v1` claim은 `validated`, failed claim은 `0`입니다. 같은 packet의 read-only MCP smoke는 allowlisted tool 9개, `brain_objects_query` route 6개의 `object_pack.v1`, production mutation `0`, protected-output flag false를 기록했습니다. Packet SHA-256은 `9f2c7cc0fec47d973682df8cd96f7eca36d4c83366c1d6b785a0ba6de31f2900`입니다. 이 closure는 GitOps/live consistency proof이며 cryptographic provenance나 아직 없는 audit-store recording proof를 대신하지 않습니다.
- Post-deploy capture handoff: source-side CLI/MCP는 이제 기존 fail-closed runtime evidence normalizer/evaluator의 operator-friendly alias로 sanitized `post_deploy_capture` / `normalize_post_deploy_capture` 입력을 허용합니다. P8 product evidence checks는 alias packet/report metadata를 기록하고, post-deploy capture가 production readiness를 주장하거나 network-use provenance를 건너뛰거나 production mutation을 보고하면 fail closed로 처리합니다. 이는 ops runner handoff를 개선하지만 그 자체로 deploy, image identity, rollout, 또는 live runtime proof는 아닙니다.
- Post-#142 live rollout/capture: neurons PR #142 merged source commit `40261ef132e5`, neurons-ops PR #19 updated production desired state at `a35f53c`, and Jenkins production deploy button #16 synced `neurons-oci-production` with precheck/sync/postcheck `PASS`. The live read-only MCP capture on the deployed `sha-40261ef132e5` image returned 31 tools, sanitized `post_deploy_read_only_smoke` provenance, six implemented `brain_objects_query` route smokes, validated object-native tool hints, deployed identity containing the expected source commit, and `production_mutation_performed=false`. `source-to-candidate-runtime-readiness` replay is `PASS_WITH_GAPS` with `failed_claims=[]`, not `PASS`, because projection join, session/project rollup, preference/artifact memory, permission audit, startup/read-path, and production denial packets are not all populated. Activation progress now preserves that same partial live capture as `PASS_WITH_GAPS` instead of hard `FAIL`: an absent `session_project_rollup_runtime` is a P6 gap, while present malformed P6 evidence still fails closed.
- Post-#144 live rollout/capture: neurons PR #144는 source commit `6d830e374527`을 merge했습니다. neurons-ops PR #21은 production desired state를 `4feafd41c656`으로 업데이트했고, Jenkins production deploy button #17은 `neurons-oci-production`을 sync `PASS`로 sync했습니다. 전체 job 결과는 built-in postcheck가 `toolsList` gap을 기록했기 때문에 `PASS_WITH_GAPS` / Jenkins `UNSTABLE`입니다. Argo는 ops revision에서 `Synced` / `Healthy`를 보고했고, 영향을 받은 live runtime image는 `sha-6d830e374527`로 렌더링되었습니다. `bulk-semantic-trigger`는 desired replicas `0`으로 scale되었으므로 해당 workload에 대한 readiness claim은 하지 않습니다. 별도 read-only live MCP tools-list smoke는 `brain_objects_query`, `brain_context_resolve`, `brain_source_to_candidate_graph`, `brain_source_to_candidate_runtime_readiness`를 포함한 31개 tools를 반환했습니다. 배포된 `mcp-http` image는 `post_deploy_read_only_smoke`, `network_used=true`, `production_mutation_performed=false`, expected source commit을 포함하는 deployed identity, `agent_context_product_pack.v1`, 6개 route smokes와 함께 sanitized post-deploy MCP capture를 수집했습니다. `source-to-candidate-runtime-readiness` replay는 `failed_claims=[]`와 함께 `PASS_WITH_GAPS`이고, activation progress replay도 실패 phase 없이 `PASS_WITH_GAPS`입니다. 남은 gap은 P2 production corpus ingest evidence, P3 live graph/Qdrant projection join, P6 live session/project 및 multi-device rollup, P7 live preference/artifact memory, P8 verified runtime/permission/startup 및 shadow collection evidence, P9 production consumer context/action-surface proof입니다. 이 checkpoint에서 `llm-brain-tools` image는 `neuron-knowledge`를 실행할 수 있었지만 capture collector에 필요한 `mcp` client dependency를 포함하지 않았기 때문에, post-deploy capture는 `mcp-http` image에서 실행되었습니다.
- Post-#148 tools capture path: neurons PR #148은 source commit `ec91c94e485d`를 merge했습니다. neurons-ops PR #23은 main component CI definition을 업데이트했고, PR #24는 Jenkins job이 사용하는 `ops/oci-prod-verify` component CI branch를 업데이트했으며, PR #26은 production desired state를 `llm-brain-tools:sha-ec91c94e485d`로 업데이트했습니다. Jenkins `llm-brain-tools` build #26은 `worker/Dockerfile.tools`를 사용하고 `.[mcp-client]`를 설치하여 `sha-ec91c94e485d`를 push했습니다. Jenkins production deploy button #18은 ops revision `087534ccff29`에서 `neurons-oci-production`을 sync/postcheck `PASS`, `toolsList=passed`, `toolsCount=31`로 sync했습니다. Live Argo는 `Synced` / `Healthy`를 보고했고, live `llm-brain-tools` 및 eval cronjob image는 `sha-ec91c94e485d`로 렌더링되었으며, live tools container는 `mcp` client import를 통과했습니다. 이후 tools container는 sanitized post-deploy MCP capture를 직접 수집했습니다: 31 tools, `post_deploy_read_only_smoke`, `network_used=true`, `production_mutation_performed=false`, expected source commit을 포함하는 deployed identity, `agent_context_product_pack.v1`, 6개 route smokes, route collector failure 없음. Runtime readiness replay는 `failed_claims=[]`와 함께 `PASS_WITH_GAPS`로 유지되고, activation progress replay도 실패 phase 없이 `PASS_WITH_GAPS`로 유지됩니다. 이는 tools-image capture dependency gap을 해결하지만 product-wide production readiness를 의미하지는 않습니다.
- Post-#152 live P3 projection-join capture: neurons PR #152는 source commit `36f0a756e31f`를 merge했고 merge commit은 `a6e2249381ab`입니다. neurons-ops PR #27은 production desired state를 source-tagged images `sha-a6e2249381ab`로 업데이트했고 merge commit은 `3d13f780a981`입니다. Jenkins production deploy button #19는 precheck/sync/postcheck를 `PASS`로 완료했고 live runtime은 the source-tagged MCP/tools images를 ready 상태로 렌더링했습니다. Read-only post-deploy MCP capture는 31 tools, `post_deploy_read_only_smoke`, `network_used=true`, deployed identity containing `a6e2249381ab`, `production_mutation_performed=false`, and a promoted `projection_join` packet with `evidence_class=runtime_projection_join`, edge count `11`, graph hit count `3`, search hit count `8`, redacted postcheck, and no protected-output flags. Runtime readiness validates `live.source_to_candidate.projection_join`; activation-progress replay with this capture closes the P3 `live_graph_qdrant_projection_join_unproven` blocker. Product-wide status remains `PASS_WITH_GAPS` because P6/P7/P8/P9 live runtime packets remain open.
- P6 live rollup capture: a current read-only configured/deployed MCP capture collected through the `mcp-client` extra returned a sanitized `post_deploy_read_only_smoke` runtime packet with `network_used=true`, `production_mutation_performed=false`, promoted `session_project_rollup_runtime`, `session_project_rollup_runtime_evidence.v1`, `object_extraction_session_project_rollup_preview.v1`, `device_count=2`, `WorkUnit=1`, `temporal_work_recall` read-after-write `validated`, and no P6 claim gaps. Runtime-readiness replay validates `live.session_project.rollup`; activation-progress replay marks P6 `PASS`, removes P6 gaps, and advances `next_phase=P7`. This is P6 rollup/read-path proof only; source/image identity and broader runtime authority remain P8 gaps when not supplied in the capture.
- P4 replacement-current live pilot: the deployed tools image tagged from source `4366a76d3528` exposed 31 MCP tools, including the approval-board and object-authority write tools. A bounded one-shot `mcp-stdio` operator process intentionally performed production ledger mutation on two synthetic `RepoDocument` objects with per-call production gates, redacted provenance, scoped max object count `2`, read-after-write postcheck, and rollback/supersession plan. The prior object moved from `accepted_current` to `accepted_non_current` through `commit_supersession`, the successor object moved to `accepted_current`, and the sanitized packet reported no raw private evidence, secret, host topology, or raw external id output. Branch-local runtime-readiness replay validates `live.production.object_authority_replacement_current` and activation-progress marks P4 as `PASS`; overall product status remains `PASS_WITH_GAPS` because P6/P7/P8/P9 live runtime packets remain open.
- P1 Production MCP Activation: `PASS` for the activation gate / `production_validated`. Deployed HTTP MCP now exposes baseline object-native tools plus `brain_source_to_candidate_graph`, `brain_candidate_review_edit`, `brain_approval_board_decide`, and `brain_source_to_candidate_runtime_readiness`; deployed and configured read paths return public-safe `brain_objects_query.v1` / `object_pack.v1`; production proposal/source-to-candidate/approval-board write paths deny or run no-mutation previews without authority writes; Jenkins #19, neurons-ops PR #12, and Jenkins production deploy #9 tie the P1 activation checkpoint to source `773ed7a1a1cd`; approved `object-authority-schema-ensure` executed against the server-backed ledger and postcheck six-route smokes report `authority_state_overlay_status=available`. Product-wide status remains `PASS_WITH_GAPS` because P6/P7/P8/P9 runtime evidence remain gaps.
- P2 Living Reference Corpus Store: `PASS` / `production_validated`; local/test corpus policy, configured local/test store, first-class reference object rows, CLI/MCP status, idempotence, unscoped production-denial evidence, bounded production corpus ingest readiness evaluator, deployed `corpus-ingest` schema support, production deploy of source `9bdd780c2756`, live Palantir manifest count gate, bounded production ingest evidence, read-after-write corpus status, redaction postcheck, and repeated-ingest idempotence proof all passed. This is reference-corpus readiness only; it does not promote reference material to accepted/current authority and does not prove P3/P4 extraction/review/authority workflow readiness.
- P3 Processing And Object Extraction Pipeline: `PASS_WITH_GAPS` / `local_validated`; local/test reference corpus extraction preview는 deterministic objects, edges, public-safe chunk preview, strategy comparison, evaluator evidence, blocked-extraction gaps를 생성합니다. local_test `source-to-candidate-graph` CLI 및 `brain_source_to_candidate_graph` MCP tool은 configured reference corpus store를 candidate graph review pack으로 연결합니다. candidate graph review pack은 candidate objects/edges/evidence/confidence/supported edit actions를 surface하고 reviewer edit fixture는 authority mutation 없이 candidate object/edge/evidence state만 바꾸며 add/remove edge/evidence와 edge-ref sync를 검증합니다. `source-to-candidate-runtime-readiness` CLI 및 `brain_source_to_candidate_runtime_readiness` MCP tool은 post-deploy sanitized evidence packet을 PASS/PASS_WITH_GAPS/FAIL로 판정하고, `projection_join` packet field와 `live.source_to_candidate.projection_join` claim으로 graph/Qdrant projection join schema, runtime evidence class, non-empty edge count, no production mutation, redacted postcheck를 검증합니다. Deployed runtime now reads the production P2 Palantir reference corpus store into a `candidate_graph_review` pack and validates a live `source_to_candidate_review_loop_evidence.v1` packet through candidate-review edit, local_test approval-board decision, read-after-write, and redaction postcheck. Post-#152 deployed runtime also validates a live graph/Qdrant projection join with non-empty edge count and no production mutation. P3's bounded live gates remain valid; later P7-P9 partial evidence and gaps are accounted separately instead of being used to reclassify P3.
- P4 Review Queue And Authority Promotion: `PASS` / `replacement_current_production_validated`; local/test decision commit은 authority state/audit history를 기록하고, object queries는 local/test stale, superseded, retired, archive-only, rejected states를 surface하며, object explain은 local/test decision history를 반환합니다. local/test rollback decision은 accepted/current decision을 audit 삭제 없이 archive-only로 demote하고 `rollback_of_decision_id`를 decision/state/explain view에 보존합니다. `candidate-review-edit` / `approval-board-decide` CLI 및 `brain_candidate_review_edit` / `brain_approval_board_decide` MCP tools가 candidate edit에서 local_test approval-board preview까지 연결합니다. candidate review edit은 `target_scope`와 `mutation_mode=no_mutation`을 반환하고, production target 이름이 들어와도 pack preview만 바꾸며 authority/production mutation은 수행하지 않습니다. Runtime readiness는 이제 supplied review-loop evidence가 production ledger/corpus/runtime mutation, non-local authority scope, rejected edits, or raw private evidence를 보고하면 FAIL로 판정합니다. source-to-candidate activation preview는 sanitized `approval_board_runtime` evidence가 local_test authority write/read-after-write와 no-production-mutation을 증명할 때만 `approval_board_runtime_integration_unproven` gap을 제거합니다. Deployed HTTP MCP image `sha-910a9cf24a70` exposes `production_gate` schema for `brain_approval_board_decide`, keeps object-authority production writes default-disabled on the long-running HTTP service, requires the runtime flag `--allow-object-authority-production-writes` plus per-call `production_gate`, and live approval-board denial smoke reports `production_mutation_performed=false`, `proposal_write_performed=false`, `authority_write_performed=false`, `authoritative_memory_changed=false`, and `decision_count=0`. Bounded one-shot `mcp-stdio` operator executions opened the production write flag only for each process lifetime: prior smokes validated one synthetic `RepoDocument` reject and accepted-current rollback-to-archive execution, the post-#128 deployed approval-board smoke promoted one synthetic `RepoDocument` through `brain_approval_board_decide` with `production_gate`, and the replacement-current pilot demoted a prior synthetic `RepoDocument` from `accepted_current` to `accepted_non_current` while promoting a successor synthetic `RepoDocument` to `accepted_current`. Read-after-write, decision history, targeted queue statuses, redacted provenance, `live.production.object_authority_bounded_execution`, and `live.production.object_authority_replacement_current` all validated.
- P5 Continuous Golden Query Quality Gates: `PASS_WITH_GAPS` / `local_validated`; phase coverage report는 P1-P10 golden query families를 나열하고, source-to-authority quality gate는 source_to_candidate_graph, candidate_review_edit, approval_board_local_test, authority_read_after_write, production_decision_denial path를 검증합니다. candidate_review_edit path는 이제 object update뿐 아니라 add/remove evidence+edge, edge/evidence count, `target_scope=production`, `mutation_mode=no_mutation`, no rejected edits까지 gate evidence로 반환합니다. activation progress report는 P2-P9 scope, P2/P3/P4 minimum review-loop checkpoint, P5 `local_validated`, next phase P6, remaining P6-P9 gaps를 한 JSON gate로 반환합니다. `product_surface_checks`는 `brain_objects_query`, object-native MCP tool registry surface, runtime readiness tool, local_test/default production-denial policy를 함께 검증합니다. Branch-local `neuron-knowledge object-query` CLI는 MCP `brain_objects_query`와 같은 read-side route contract를 사용해 default authority/archive, style/preference, HTML/visualization preference, temporal work recall, code change impact, deploy/runtime gap routes를 반환하고, 각 returned object pack은 FR6 `route_trace`로 selected source lanes, confidence, stop reason, and missing evidence를 명시합니다. `code_change_impact` route는 파일 변경 질문을 `RepoFile`, `VerificationCommand`, `RuntimeSurface`, `McpTool` 및 `validated_by`/`requires_live_evidence` edges로 반환하고 `live_runtime_impact_unverified`, `source_freshness_unverified`, `production_mutation_forbidden` gaps를 유지합니다. `html_visualization_preference` route는 HTML review artifact 기준/선호 질문을 P7 artifact preference memory로 라우팅하고, accepted preference가 없으면 `accepted_html_preference_missing` 및 `visualization_preference_missing` gaps를 반환합니다. runtime readiness는 live `source_to_candidate.review_loop` claim으로 P3/P4 source→candidate→review→approval local_test loop smoke를 검증하고, live `object_authority_gate_policy` claim으로 production proposal/decision schema의 `production_gate`, runtime opt-in flag, per-call gate requirement를 확인하고, live `object_authority_bounded_execution` claim으로 sanitized `production_authority_execution` packet의 proposal/decision gate hash, single-object scope, read-after-write, rollback/supersession, postcheck, and protected-output false guards를 검증합니다. `live.evidence.provenance` claim은 evaluator 자체 `network_used=false`와 evidence 수집 경로의 `evidence_collection_network_used`를 분리하고, collection mode, mutation scope, redaction guard를 검증합니다. live agent context `tool_hints`는 suggest-only/no-execute/no-production-mutation safe targets와 approval-board scope blocker를 노출해야 합니다. `source-to-candidate-runtime-readiness --collect-shadow-evidence` and MCP `collect_shadow_evidence=true` now build a public-safe `source_to_candidate_runtime_evidence.v1` collector packet from branch-local read-only route smokes, a local_test source→candidate→review→approval shadow smoke, a local_test P6 session/project/work-unit rollup smoke, a local_test P7 preference/artifact memory smoke, a local_test P8 permission-sensitive audit denial/no-mutation smoke, and a local_test P9 agent-context startup/read-path smoke; the packet validates route-smoke/review-loop/session-rollup/preference-artifact/permission-audit/startup-read-path shape but keeps `collector_packet_not_live_evidence`, `network_used=false`, and `production_mutation_performed=false`. `product_evidence_checks`는 P2/P3/P4/P6/P7/P8/P9 evidence를 fail-closed로 검증하고, P2 evidence summary에는 `reference_corpus_production_ingest_readiness.v1`, P3 evidence summary에는 `source_to_candidate_projection_join_product_evidence.v1`, P4 evidence summary에는 `object_authority_replacement_current_evidence.v1`, P8 evidence summary에는 runtime evidence collection plan, `source_to_candidate_runtime_evidence_packet_template.v1` packet template, `source_to_candidate_runtime_shadow_collection_request.v1` route-smoke request, `source_to_candidate_runtime_shadow_collection_registration.v1` branch-local registration artifact, collector packet metadata, and post-deploy capture alias packet/report metadata를 포함합니다. Supplied live evidence가 없으면 P2 `p2_production_corpus_ingest_evidence_unverified`, P3 `p3_live_graph_qdrant_projection_join_unproven`, P4 `p4_replacement_current_execution_unverified`, P6 `p6_live_multi_device_rollup_unproven`, P7 `p7_accepted_preference_context_pack_live_unproven`, P7 `p7_html_artifact_review_live_unproven`, P8 `p8_runtime_evidence_collection_plan_not_live_evidence`, `p8_runtime_evidence_packet_template_not_live_evidence`, `p8_runtime_evidence_collector_not_live_evidence`, route별 `p8_shadow_route_smoke_collection_pending:<route>`, route별 `p8_shadow_collection_run_pending:<route>`, P9 `p9_runtime_evidence_unverified`, P9 `p9_production_consumer_context_pack_live_unproven`, and P9 `p9_consumer_action_surface_runtime_policy_unproven` gaps를 명시적으로 유지합니다. Supplied live P3/P4 evidence는 해당 phase check를 `PASS`로 승격하지만, product-wide `PASS_WITH_GAPS`는 P6/P7/P8/P9 live evidence가 붙기 전까지 유지합니다. post-deploy capture alias metadata는 `post_deploy_read_only_smoke`, `network_used=true`, `production_ready=false`, `production_mutation_performed=false`, and `PASS_WITH_GAPS`를 gate로 검증하며 live proof를 대신하지 않습니다. Partial post-deploy captures are now interpreted by phase-specific evidence presence: absent P6 rollup evidence remains `p6_live_session_project_rollup_unverified` / `p6_live_multi_device_rollup_unproven`, while present malformed/incomplete P6 packets still fail closed. runtime readiness는 injected route smoke의 `object_pack_route_not_implemented`를 expected deployed identity 포함 여부와 분리해 판정합니다: expected commit identity가 없으면 route-specific not-validated gap이고, expected commit identity가 있는데 route가 fallback이면 FAIL입니다. report는 `local_quality_gate=green`, `release_quality_gate=green`, `production_approval_gate=preapproved`, `production_mutation_execution=not_performed_by_local_gate`를 분리해서 반환합니다. release quality gate는 P5 evaluator/release gate scope에서 `green`이지만, product-wide production readiness는 여전히 `PASS_WITH_GAPS`입니다.
- Current P6 live rollup capture supersedes the no-supplied-evidence P5 baseline for the P6 activation gate: when the sanitized P6 capture is supplied, activation progress reports P6 `PASS`, `next_phase=P7`, and remaining phases `[P7,P8,P9]`.
- P6 Session, Device, Project, And Work-Unit 360: `PASS` / `production_validated` for the live rollup gate; local/test session project rollup preview는 Device/Session/Repository/Branch/WorkUnit/Spec/PullRequest/Commit objects를 생성하고, same-device와 all-device fixture rollup을 분리하며, safe handoff pack과 resume context를 반환합니다. runtime readiness는 P6 handoff의 visible/all-device session count 및 Session/WorkUnit ref count가 preview/resume evidence와 불일치하면 fail-closed로 처리합니다. activation progress product evidence는 sanitized live `session_project_rollup_runtime_evidence.v1` packet이 post-deploy/live provenance와 함께 주어질 때만 P6 `p6_live_multi_device_rollup_unproven` gap을 제거하고, local replay evidence는 `p6_session_project_rollup_evidence_not_live` gap으로 유지합니다. local CLI/MCP `brain_objects_query` temporal work recall route는 "어제 이 repo에서 뭐 했어?"류 질의를 `WorkUnit` object pack으로 반환하고, runtime readiness는 live `temporal_work_recall` route smoke를 요구합니다. Current configured/deployed MCP capture now includes promoted `session_project_rollup_runtime`; activation-progress replay reports P6 `PASS`, `failures=[]`, `gaps=[]`, and `next_phase=P7`. P6 PR/commit history enrichment remains local/test evidence, but the P6 live session/project/work-unit gate is no longer the product activation blocker.
- P7 Preference, Style, And Artifact Memory: `PASS_WITH_GAPS` / `local_validated`; source/local fail-closed gate와 deployed accepted-current style/HTML preference, 비어 있지 않은 style-preference context, bound artifact application receipt가 이제 증명되었습니다. Same-process live P7 product check는 `PASS`입니다. Persisted product evidence는 collector execution capability가 의도적으로 serializable하지 않으므로 `PASS_WITH_GAPS`입니다. 최상위 `phase_progress`에도 더 구체적인 product-evidence result와 아직 reconcile되지 않은 이전 generic live-preference gap 2개가 남습니다. 따라서 evidence portability와 phase aggregation은 validated runtime slice를 failure로 바꾸지 않으면서 phase 전체의 `production_validated` 선언을 막습니다.
- P8 Runtime Truth, Security, And Deployment Authority: `PASS_WITH_GAPS` / `local_validated`; source `73d440522e49`에 대한 source merge/check, canonical image build, ops desired-state merge, Jenkins deploy execution, Argo/live identity, target image rollout, route smoke, gate-less denial/no-mutation postcheck를 각각 증명했습니다. Post-#204 sanitized packet의 `deployment_evidence_binding.v1`이 GitOps desired state와 Argo reconciliation, desired/live image set, deployed identity를 join해 해당 gap은 `PASS`로 닫혔습니다. Permission-sensitive audit-store recording만 아직 증명되지 않았으므로 `production_ready=false`가 유지됩니다.
- P8 production-readiness interpretation guard: post-deploy live-mode provenance with `network_used=false` is not live evidence and cannot set `production_ready=true`; it remains `PASS_WITH_GAPS` with `live_evidence_provenance_network_not_used_for_live_mode`.
- P9 Agent Context Productization: `PASS_WITH_GAPS` / `local_validated`; deployed bounded collector는 Codex-oriented `agent_context_product_pack.v1`, 비어 있지 않은 current-authority/style-preference/active-work/guardrail/verification section, safe suggest-only tool hint, attested subprocess binding, mutation 없는 validated startup receipt를 이제 validate합니다. Claude Code, Gemini, Hermes startup, runtime interception, 실제 Codex host startup hook, deployed consumer action-surface enforcement은 live gap으로 남습니다.
- Product activation: 완료되지 않았습니다. Deployed source-to-candidate review-loop proof, P3 live graph/Qdrant projection-join proof, P4 production authority gate policy/no-mutation proof, P4 single-object bounded reject proof, P4 rollback/demotion execution proof, P4 approval-board promotion runtime integration proof, P4 replacement-current prior/successor proof, P5 release-quality evaluator proof, P6 live session/project/work-unit rollup proof, P8 GitOps/live packet binding proof가 추가되었습니다. P7 persisted attestation/phase aggregation, P8 permission-sensitive audit-store, P9 host/consumer/runtime-enforcement evidence는 여전히 필요합니다.
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

- deployed/configured HTTP LBrain MCP exposes object-native tools plus source/review/readiness tools, and the current Codex session can call six required `brain_objects_query` routes with implemented object packs; P3 source-to-candidate review-loop, graph/Qdrant projection-join evidence, P4 authority gates, P5 release-quality gate, and P6 session/project/work-unit rollup are validated, while remaining production gaps are broader P7/P8/P9 live evidence, not P1 activation.
- reference corpus store is configured and populated for the bounded Palantir reference corpus gate; this is reference-only corpus state, not accepted/current authority.
- golden queries are baseline red, not production-quality green.
- accepted/current promotion workflow is not open for production object decisions.
- object extraction and processing pipeline is wired through the deployed source-to-candidate review-loop preview and post-#152 live graph/Qdrant projection-join proof; the product-wide release/runtime gates are still not complete.
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

Deployed HTTP MCP runtime activation and configured endpoint smoke passed for object-native availability. Post-#115 image/source identity, production deploy-button rollout, tools/list, approved schema repair, production denied/no-mutation smokes, and six-route `brain_objects_query` route activation are proven for the deployed/configured read path. The latest deployed runtime smoke exposes the source-to-candidate review/readiness tools, returns public-safe `brain_objects_query.v1` / `object_pack.v1` responses for all required routes, and keeps production authority mutation denied unless a later bounded gate opens it. This is P1 `PASS`; it is not product-wide production readiness because P7/P8/P9 live evidence remain open.

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
- latest current Codex-session evidence keeps product-wide `production_ready=false` because P7/P8/P9 runtime packets remain unproven.
- raw live evidence, host topology, private ledger details, and raw dataset/document ids remain outside this public repo

Next gate:

- collect sanitized live packets for the remaining P7 preference/artifact memory, P8 permission-sensitive audit, and P9 startup/read-path evidence.
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
- Post-#152 deployed `source-to-candidate-runtime-readiness` replayed the sanitized live evidence packet with `evidence_is_live=true`, `evidence_collection_network_used=true`, `failed_claims=[]`, and validated both `live.source_to_candidate.review_loop` and `live.source_to_candidate.projection_join`.
- Post-#152 read-only post-deploy MCP capture promoted a `projection_join` packet only after the collector saw post-deploy network provenance, runtime projection evidence, graph/search hits joined to candidate targets, non-empty edge count, no production mutation, and redacted postcheck. The sanitized packet reported edge count `11`, graph hit count `3`, search hit count `8`, and `production_mutation_performed=false`.
- Activation-progress replay with the post-#152 capture now closes the P3 `live_graph_qdrant_projection_join_unproven` blocker. A prior deployed graph projection status with selected/projected counts `0` remains historical read-only status evidence and is superseded by the post-#152 projection-join capture for this gate.

PASS_WITH_GAPS rationale:

- Local/test P3 gate evidence is present for deterministic extraction, failed extraction gaps, strategy comparison, chunk preview, configured-store-to-candidate graph CLI wiring, candidate review/edit surface, evaluator reports, derived projection join authority separation, and a runtime-readiness evidence contract for sanitized projection join proof.
- Deployed/runtime P3 review-loop evidence is now present for production reference corpus read, candidate graph review pack creation, candidate edit no-mutation, local_test approval-board decision, read-after-write, and public-safe postcheck.
- Deployed/runtime P3 projection-join evidence is now present for graph/search projection hits, candidate-target joins, non-empty edge count, live/network provenance, and no production mutation.
- Without the supplied post-#152 live capture, local fixture tests and graph projection status alone still cannot close `live_graph_qdrant_projection_join_unproven`; with that capture, P3 projection-join product evidence is `PASS`.
- This phase did not perform or claim production authority, corpus, graph, search, or deployment mutation.

Remaining gaps:

- P3 is not product-complete by itself; local/test reference corpus extraction preview, repo-document extraction preview, documentation cleanup strategy comparison, runtime truth extraction preview, preference/style extraction preview, work-unit extraction preview, session-detail extraction preview, PR/commit detail extraction preview, graph/search projection join preview, and broader evaluator suite preview slices are implemented
- evaluator coverage is still local/test only; it covers reference corpus, repo-document cleanup, documentation cleanup, PR merge/deploy truth, preference/style, temporal work recall, session detail extraction, PR commit/test provenance, graph/search projection join, deterministic variance, and no-LLM model/prompt applicability
- graph/search projection join is live-proven for the post-#152 configured/deployed runtime capture, but regression protection still depends on the runtime-readiness evidence contract and activation-progress replay
- deployed/runtime source-to-candidate review-loop and projection-join edge-count gates are live-proven against the configured/deployed path; this does not make P7/P8/P9 runtime evidence green
- no production authority, graph, search, or deployment mutation has been performed or claimed during this P3 evidence collection

### P4. Review Queue And Authority Promotion

State: `PASS` / `replacement_current_production_validated`.

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

- production supersession/replacement-current pilot is validated through bounded synthetic prior/successor production authority evidence; P7/P8/P9 live runtime packets remain open
- approval-board-to-production authority integration is validated for a bounded synthetic promotion path; broader user-facing review/approval product readiness still depends on P7/P8/P9 evidence and later P10 surface work
- product-wide readiness remains `PASS_WITH_GAPS` until P6 rollup, P7 preference/artifact memory, P8 permission audit/runtime authority, and P9 startup/read-path evidence are attached together

### P5. Continuous Golden Query Quality Gates

State: `PASS_WITH_GAPS` / `local_validated`; continuous from P1 onward.

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
- current report status is `PASS_WITH_GAPS` and `release_quality_gate=green`; this is the P5 evaluator/release gate only and does not claim product-wide production readiness
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
- source-to-candidate runtime readiness CLI can evaluate sanitized post-deploy evidence for MCP read/review tools, `brain_objects_query` route smokes, source-to-candidate review-loop smokes, graph/search projection joins, agent context `tool_hints`, required agent context sections, deployed identity, production-denial/safety smokes, object authority production gate policy, bounded production authority execution evidence, replacement-current production authority evidence, and evidence provenance without network or mutation; required post-deploy object-query route smokes now include `authority_archive_separation`, `code_style_preference`, `temporal_work_recall`, `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth`; post-#115/#12/#9 populated P1 route/tool/denial activation proof, post-#152 supplied P3 projection-join proof, P4 single-object bounded reject, rollback/demotion, post-#128 approval-board production promotion, replacement-current evidence, and current P6 live rollup evidence are now attached, but the readiness surface remains `PASS_WITH_GAPS` until complete agent context and P7/P8/P9 evidence are supplied; agent-context tool hints must remain suggest-only/no-execute/no-production-mutation with safe targets and approval-board scope blockers, and production safety claims include source-to-candidate review-loop smoke, source-to-candidate denial, approval-board denial, proposal-create, decision-commit, object authority gate policy, object authority bounded execution, object authority replacement-current, and evidence provenance
- runtime readiness packet template now includes `source_to_candidate_review_loop` with `source_to_candidate_review_loop_evidence.v1`; supplied evidence must prove source-to-candidate graph pack creation, candidate review edit no-mutation, approval-board local_test decision, read-after-write object pack, and public-safe postcheck, while no supplied evidence remains `PASS_WITH_GAPS` with `live_source_to_candidate_review_loop_unverified`
- runtime readiness rejects review-loop evidence that reports production mutation, non-local authority scope, candidate review authority writes, rejected edits, incomplete read-after-write, or raw/private/secret/topology/raw external id return
- source-to-candidate activation preview now accepts sanitized runtime `projection_join` evidence and removes `live_projection_join_unproven` only when that evidence is `object_extraction_projection_join_preview.v1`, `runtime_projection_join`, `status=pass`, non-empty edge count, and no production mutation
- runtime readiness packet template now includes `projection_join`, and readiness reports include `live.source_to_candidate.projection_join`; the claim validates `object_extraction_projection_join_preview.v1`, `runtime_projection_join`, `status=pass`, non-empty edge count, no production mutation, and redacted postcheck, while missing evidence remains `live_graph_qdrant_projection_join_unproven`
- post-deploy MCP capture source path는 runtime-collected projection-join packet을 요청하고, collection mode가 `post_deploy_read_only_smoke`이며 network evidence가 있고 collector가 `collector_packet_not_live_evidence`로 표시되지 않았고 graph 및 Qdrant/search hit가 모두 candidate target에 join되며 `status=pass`이고 production mutation 또는 protected output이 없을 때만 top-level `projection_join` evidence로 승격합니다. Local/shadow projection evidence는 packet-shape evidence일 뿐이며, deploy와 live hit proof 전에는 P3 live graph/Qdrant projection-join gap을 닫으면 안 됩니다.
- source-to-candidate activation preview now also accepts sanitized runtime `approval_board_runtime` and `production_authority_write` evidence; it removes `approval_board_runtime_integration_unproven` only for local_test approval-board write/read-after-write/no-production-mutation proof, removes `production_authority_write_denied` only for bounded single-object authority execution proof with 64-hex approval ref hash, rollback/supersession, postcheck, and protected-output false flags, and keeps preview-local `production_mutation_performed=false`
- runtime readiness bounded production authority execution now fails closed unless the rollback/supersession path includes `demote_prior_object_to_accepted_non_current_or_archive_only`, approval ref hash is full `sha256:` + 64 hex, and postcheck explicitly reports no raw private evidence, secret, host topology, or raw external id return
- runtime readiness partial live evidence now decomposes broad `not_validated` states into actionable gap ids for missing live MCP tools, agent-context tool hints, agent-context sections, object-query routes, and deployed identity mismatch so post-deploy follow-up can target the exact missing proof without reading raw/private evidence
- activation progress report returns `lbrain_product_activation_progress.v1` with `scope_phases=[P2..P9]`, `minimum_review_loop_checkpoint.status=PASS_WITH_GAPS`, `next_phase=P6`, `remaining_phases=[P6..P9]`, `local_quality_gate=green`, `release_quality_gate=green`, `goal_complete=false`, `production_ready=false`, `production_approval_gate=preapproved`, `production_mutation_execution=not_performed_by_local_gate`, and `production_mutation_performed=false`
- activation progress keeps P10/deferred future-surface sentinels visible in phase coverage but excludes `future_phase_golden_query_slices_planned` and `future_phase_slices_planned` from P2-P9 `goal_completion_blockers`
- activation progress `product_evidence_summary` now includes P2 reference-corpus production-ingest readiness evidence, P3 projection-join product evidence derived from runtime-readiness projection claims, P6 session/project/work-unit rollup evidence, P7 artifact preference memory evidence, P8 runtime authority evidence, and P9 agent context product evidence as sanitized local previews
- P2 evidence summary includes `reference_corpus_production_ingest_readiness.v1`; without supplied live evidence it remains `PASS_WITH_GAPS`, `live_evidence_provided=false`, `production_mutation_performed=false`, and `production_corpus_ingest_evidence_unverified`
- P3 evidence summary includes `source_to_candidate_projection_join_product_evidence.v1`; without supplied live projection evidence it remains `PASS_WITH_GAPS`, `projection_join_claim_status=not_validated`, `evidence_is_live=false`, `production_mutation_performed=false`, `production_ready=false`, and `live_graph_qdrant_projection_join_unproven`; with the post-#152 read-only live capture it reports `PASS`, `projection_join_claim_status=validated`, non-empty edge count, `evidence_collection_network_used=true`, and no production mutation
- P6 evidence summary includes `object_extraction_session_project_rollup_preview.v1`, `object_count=8`, `edge_count=16`, `evidence_count=1`, and `session_project_handoff_pack.v1`
- supplied live P6 `session_project_rollup_runtime_evidence.v1` clears the P6 phase-progress `live_multi_device_rollup_unproven` gap, marks P6 `PASS`, and advances activation progress to P7 while preserving later P7/P8/P9 gaps
- `golden-query-eval --activation-progress` can now accept `--live-evidence-file` or `--post-deploy-capture-file`, so supplied sanitized live P3 projection-join and P6 runtime packets can be reflected in CLI product evidence while local replay and mutating captures remain fail-closed/gap-preserving
- P7 evidence summary includes `object_extraction_preference_style_preview.v1`, accepted artifact preference pack status `pass`, and source evidence refs without raw body
- P8 evidence summary keeps merge/deploy/runtime separated with `runtime_unverified_count=1`, `runtime_verified_count=0`, production promotion `permission=allowed`, `permission_reason=approved_scope_present`, `authority_write_performed=false`, a post-deploy evidence packet template, a shadow route-smoke request for `authority_archive_separation`, `code_style_preference`, `temporal_work_recall`, `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth`, and a branch-local collector packet that validates route-smoke plus local_test review-loop, P6 session/project/work-unit rollup, P7 preference/artifact memory, P8 permission-sensitive audit, and P9 startup/read-path packet shape without claiming live evidence
- P8 product evidence treats absent source/image identity as `p8_source_commit_matches_pr_head_unverified` gap but explicit `source_commit_matches_pr_head=false` as `p8_source_commit_mismatch_with_pr_head` hard failure, so image/source mismatch cannot be hidden behind `PASS_WITH_GAPS`
- P9 evidence summary includes `tool_hint_safe_target_count` and `unsafe_tool_hint_count`; product evidence fails closed if tool hints have missing/non-allowlisted safe targets, allow execution, allow production mutation, omit approval-board scope blockers, or omit sanitized runtime-readiness target/raw-private blockers
- activation progress `product_evidence_checks`는 supplied live evidence가 없으면 P2/P3/P4/P6/P7/P8/P9 모두 `result=PASS_WITH_GAPS`를 반환하며, P2 `p2_production_corpus_ingest_evidence_unverified`, P3 `p3_live_graph_qdrant_projection_join_unproven`, P4 `p4_replacement_current_execution_unverified`, P6 `p6_live_multi_device_rollup_unproven`, P7 `p7_accepted_preference_context_pack_live_unproven`, P7 `p7_html_artifact_review_live_unproven`, P8 `p8_runtime_evidence_unverified`, `p8_runtime_verified_evidence_missing`, `p8_runtime_evidence_collection_plan_not_live_evidence`, `p8_runtime_evidence_packet_template_not_live_evidence`, `p8_runtime_evidence_collector_not_live_evidence`, route-specific `p8_shadow_route_smoke_collection_pending:<route>`, route-specific `p8_shadow_collection_run_pending:<route>`, P9 `p9_runtime_evidence_unverified`, P9 `p9_production_consumer_context_pack_live_unproven`, and P9 `p9_consumer_action_surface_runtime_policy_unproven` gaps를 보존합니다. Post-#152 live projection evidence가 supplied 되면 P3 check는 `PASS`로 바뀌고 P3 projection-join blocker가 phase progress와 goal blockers에서 제거됩니다. P4 replacement-current evidence가 supplied 되면 P4 check는 `PASS`로 바뀌고 P4 replacement-current blocker가 phase progress와 goal blockers에서 제거됩니다. supplied live P6 session/project rollup evidence가 supplied 되면 P6 check는 `PASS`로 바뀌고 P6 multi-device rollup blocker가 phase progress와 goal blockers에서 제거되어 `next_phase=P7`로 전진합니다. required phase evidence가 없거나, P2가 supplied live evidence 없이 PASS를 주장하거나, P3 projection join evidence가 missing/unsafe/mutating이거나 live evidence 없이 PASS를 주장하거나, P4 replacement-current evidence가 project scope mismatch, missing prior demotion, missing successor current, unsafe provenance, or missing read-after-write를 보고하거나, P6 session/project rollup evidence가 malformed/local-only/mutating/private-output을 보고하거나, P9 tool hints가 unsafe이거나, permission audit/startup-read-path collector evidence가 없거나 불완전하거나, validating evidence 없이 mutation을 주장하거나, runtime evidence collection/template/collector/shadow request/registration이 network/live/mutation behavior를 주장하면 fail closed로 처리합니다.
- runtime readiness route-smoke claim now separates current-session deployment lag from deployed-regression evidence: an injected `deployment_runtime_truth` smoke with `object_pack_route_not_implemented` and no expected-commit identity is `PASS_WITH_GAPS` with `brain_objects_query_route_unimplemented:deployment_runtime_truth` plus `shadow_route_smoke_not_implemented:deployment_runtime_truth`; the same fallback with expected-commit identity is `FAIL`
- the same route-smoke claim now exposes `route_fallback_interpretation`, using `gap_until_deployed_identity_matches_expected_commit` before expected-commit identity is proven and `fail_expected_deployed_identity` when an expected-commit deployment still falls back to `object_pack_route_not_implemented`
- P9 evidence summary includes `agent_context_product_pack.v1`, Codex tool hints for `brain_objects_query` plus object-native review/readiness tools, style/preference section evidence, active work section evidence, and `mutation_allowed=false`
- `candidate_graph_review` packs state empty authority lanes explicitly so P5 strict axis checks do not hide candidate-vs-authority separation
- `code_change_impact` packs state empty authority lanes, Korean `런타임` runtime claims, runtime evidence gaps, and freshness gaps explicitly so FR8 strict-axis checks do not hide local-vs-live separation
- P7-P9 remain represented with local/test evidence plus production/live gaps, P6 remains visible as a supplied-live PASS gate, and P10 remains planned
- activation progress focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_green_release_gate_with_gaps tests/test_golden_query_eval.py::test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p2_to_p9_scope_visible tests/test_golden_query_eval.py::test_product_evidence_summary_fails_closed_when_required_phase_evidence_is_missing tests/test_golden_query_eval.py::test_product_evidence_summary_marks_p8_runtime_unverified_as_gap_not_pass tests/test_golden_query_eval.py::test_product_evidence_summary_fails_when_p8_collection_plan_is_missing_or_mutating tests/test_golden_query_eval.py::test_product_evidence_summary_fails_when_p9_active_work_is_missing`
- activation progress focused result: `7 passed, 1 warning`
- activation progress adjacent evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py tests/test_neuron_cli.py tests/test_extraction_pipeline.py tests/test_context_pack_builder.py`
- activation progress adjacent result: `90 passed, 1 warning`
- activation progress CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --activation-progress`
- activation progress CLI smoke result without supplied live evidence: `status=PASS_WITH_GAPS`, `local_quality_gate=green`, `release_quality_gate=green`, `minimum_review_loop_checkpoint.status=PASS_WITH_GAPS`, `next_phase=P6`, `remaining_phases=[P6,P7,P8,P9]`, `goal_complete=false`, `production_ready=false`, `product_evidence_status=PASS_WITH_GAPS`, `production_approval_gate=preapproved`, `production_mutation_execution=not_performed_by_local_gate`, `product_evidence_summary phases=P2/P3/P4/P6/P7/P8/P9`, no supplied live evidence keeps P2/P3/P4/P6/P7/P8/P9 `result=PASS_WITH_GAPS`, P2 gap `p2_production_corpus_ingest_evidence_unverified`, P3 gap `p3_live_graph_qdrant_projection_join_unproven`, P4 gap `p4_replacement_current_execution_unverified`, P6 gap `p6_live_multi_device_rollup_unproven`, P7 gaps `p7_accepted_preference_context_pack_live_unproven` and `p7_html_artifact_review_live_unproven`, P8 gap `p8_runtime_evidence_unverified`, P8 collection plan `source_to_candidate_runtime_evidence_collection_plan.v1`, P8 packet template `source_to_candidate_runtime_evidence_packet_template.v1`, P8 collector packet `source_to_candidate_runtime_evidence.v1`, P8 required bounded-authority demotion step `demote_prior_object_to_accepted_non_current_or_archive_only`, P8 collector gap `p8_runtime_evidence_collector_not_live_evidence`, P8 shadow request `source_to_candidate_runtime_shadow_collection_request.v1`, P8 registration artifact `source_to_candidate_runtime_shadow_collection_registration.v1`, P9 gaps `p9_runtime_evidence_unverified`, `p9_production_consumer_context_pack_live_unproven`, and `p9_consumer_action_surface_runtime_policy_unproven`, pending routes include `code_change_impact`, `html_visualization_preference`, and `deployment_runtime_truth`, route counts are 6, `network_used=false`, `mutation_allowed=false`, `readiness_claim=plan_only_not_runtime_evidence`, P9 `section_counts.active_work=1`, `production_mutation_performed=false`
- source-to-authority gate focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation`
- source-to-authority gate focused result: `1 passed, 1 warning`
- source-to-authority CLI evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_source_to_authority_gate`
- source-to-authority CLI result: `1 passed, 1 warning`
- source-to-authority CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --source-to-authority-gate`
- source-to-authority CLI smoke result: `status=PASS_WITH_GAPS`, `local_quality_gate=green`, `release_quality_gate=green`, `production_mutation_performed=false`, `authority_write_scope=local_test`
- runtime readiness CLI smoke: `cd worker && uv run neuron-knowledge source-to-candidate-runtime-readiness --expected-commit 789b95cd2c248ee89394dcb20917a8e13d89db89`
- runtime readiness CLI smoke result: `status=PASS_WITH_GAPS`, `live_evidence_provided=false`, `production_mutation_performed=false`, `network_used=false`, `evidence_collection_network_used=false`, live MCP read/review tools/object query route smokes/source-to-candidate review-loop/context tool hints/context product sections/deployed identity/production denial/safety/object authority gate policy/object authority bounded execution/evidence provenance claims `not_validated`, and top-level gaps now include actionable missing proof ids such as `live_mcp_tool_missing:<tool>`, `live_source_to_candidate_review_loop_unverified`, `live_agent_context_tool_hint_missing:<tool>`, `live_agent_context_section_missing:<section>`, `live_brain_objects_query_route_missing:<route>`, `bounded_production_authority_execution_unverified`, and `live_evidence_provenance_unverified`
- runtime readiness sanitized current-session shadow packet result after post-#107 recheck was `PASS_WITH_GAPS` with route-unimplemented gaps; post-#115/#12/#9 direct configured/deployed-path smokes supersede that route gap by returning implemented object packs for all six routes, exposing source/review/readiness tools in live HTTP MCP, and reporting authority overlay availability. Post-#152 live projection-join capture supersedes the P3 projection-join gap for supplied live evidence, the P4 replacement-current pilot supersedes the P4 successor-current gap, and current P6 live rollup capture supersedes the P6 multi-device rollup gap; missing live agent context sections and P7/P8/P9 runtime packets remain unverified
- runtime readiness sanitized execution packet smoke result: no-evidence CLI returns `PASS_WITH_GAPS` with `bounded_production_authority_execution_unverified` and `live_evidence_provenance_unverified`; sanitized evidence-file CLI returns `PASS`, `production_mutation_performed=true`, `live.production.object_authority_bounded_execution.status=validated`, and `live.evidence.provenance.status=validated`; this is local/sanitized evidence replay, not live production mutation by this session
- runtime readiness provenance mode/scope guard evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_read_only_provenance_claims_bounded_mutation_scope tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_evidence_provenance_hides_bounded_mutation_scope tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence`
- runtime readiness provenance mode/scope guard result: `3 passed, 1 warning`; a packet that labels collection as `post_deploy_read_only_smoke` but claims `bounded_production_authority_execution` now fails with `live_evidence_provenance_read_only_mode_mutation_scope_mismatch`, preventing read-only smoke evidence from being promoted to production-ready bounded-mutation proof
- runtime readiness provenance live/network guard evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_post_deploy_mode_without_network_does_not_claim_live_or_ready`
- runtime readiness provenance live/network guard result: `1 passed, 1 warning`; a packet that labels collection as post-deploy but reports `network_used=false` remains `PASS_WITH_GAPS`, `evidence_is_live=false`, and `production_ready=false`
- source-to-candidate activation preview focused evidence: `cd worker && uv run pytest -q tests/test_extraction_pipeline.py::test_source_to_candidate_graph_activation_preview_resolves_approval_and_production_gaps_from_evidence tests/test_extraction_pipeline.py::test_source_to_candidate_graph_activation_preview_resolves_projection_join_gap_when_evidence_present tests/test_neuron_cli.py::test_neuron_knowledge_source_to_candidate_graph_uses_configured_local_test_store`
- source-to-candidate activation preview focused result: `3 passed, 1 warning`
- projection-join runtime readiness contract evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_extraction_pipeline.py tests/test_golden_query_eval.py`
- projection-join runtime readiness contract result: `120 passed, 1 warning`; missing runtime evidence remains `live_graph_qdrant_projection_join_unproven`, sanitized `projection_join` evidence validates `live.source_to_candidate.projection_join`, unsafe/incomplete projection evidence fails closed, and P5 `product_evidence_checks` now surfaces P3 as `source_to_candidate_projection_join_product_evidence.v1`
- projection-join runtime readiness adjacent evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py tests/test_neuron_cli.py tests/test_extraction_pipeline.py tests/test_golden_query_eval.py`
- projection-join runtime readiness adjacent result: `240 passed, 1 warning`
- activation-progress smoke evidence: `cd worker && uv run neuron-knowledge golden-query-eval --activation-progress`
- activation-progress smoke result without supplied live evidence: `status=PASS_WITH_GAPS`, `goal_complete=false`, `production_ready=false`, `production_mutation_performed=false`, `release_quality_gate=green`, `next_phase=P6`, `remaining_phases=[P6,P7,P8,P9]`, `runtime_evidence_collection_plan_required_step_count=13`, `runtime_evidence_packet_template_required_field_count=15`, and no supplied live evidence keeps `live_graph_qdrant_projection_join_unproven` in blockers
- activation-progress post-#152 live capture replay result: `status=PASS_WITH_GAPS`, `production_ready=false`, `production_mutation_performed=false`, P3 product check `PASS`, P3 phase progress gaps empty, and `live_graph_qdrant_projection_join_unproven` no longer appears in goal blockers. After the P4 replacement-current pilot replay, P4 phase progress gaps are empty; remaining blockers include P6/P7/P8/P9 runtime evidence
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
- focused evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_green_release_gate_with_gaps`
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
- CLI smoke result: `status=PASS_WITH_GAPS`, `release_quality_gate=green`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1759 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`

Remaining gaps:

- P5 release-quality evaluator gate is green for local/release gate semantics, not product-wide production readiness
- P7-P9 production/live slices and P10 product surface are still intentionally reported as gaps where runtime evidence is missing
- product-wide status remains `PASS_WITH_GAPS` until P7/P8/P9 runtime, preference, and context lanes have live evidence

### P6. Session, Device, Project, And Work-Unit 360

State: `PASS` / production_validated for the live session/project/work-unit rollup gate.

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
- activation progress product evidence now accepts a sanitized live P6 runtime packet and removes `p6_live_multi_device_rollup_unproven` only when `live.session_project.rollup` is validated and evidence provenance is live; local replay evidence keeps `p6_session_project_rollup_evidence_not_live`
- activation progress CLI now accepts `--live-evidence-file` and `--post-deploy-capture-file`, reusing the post-deploy normalizer so a runner-supplied sanitized packet can update P6 product evidence without claiming that plan/template/local replay evidence is live
- `source-to-candidate-runtime-readiness --collect-post-deploy-mcp-capture --mcp-url ...` now provides a read-only deployed MCP capture runner contract: it initializes the Streamable HTTP MCP session, lists tools, runs required `brain_objects_query` route smokes, emits only sanitized `post_deploy_read_only_smoke` provenance with `network_used=true`, and does not include the MCP URL, host topology, secrets, raw transcript, or raw external ids in the capture
- current configured/deployed MCP read-only capture promotes the runtime-collected `session_project_rollup_runtime` packet into top-level post-deploy evidence only when the runtime packet is `post_deploy_read_only_smoke`, `network_used=true`, and `production_mutation_performed=false`; local/shadow collector packets remain non-live evidence and are not promoted
- local path sentinels and source bodies are not returned
- P5 phase coverage now marks P6 as `PASS_WITH_GAPS` with `live_multi_device_rollup_unproven`, not `handoff_pack_not_implemented`
- P5 product evidence checks mark P6 as `PASS_WITH_GAPS` with `p6_live_multi_device_rollup_unproven` without supplied live evidence; current configured/deployed P6 capture attaches that live evidence and marks P6 `PASS`
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
- P6 product evidence bridge evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_product_activation_progress_closes_p6_gap_with_live_session_project_rollup_evidence tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p6_gap_when_rollup_evidence_is_not_live tests/test_golden_query_eval.py::test_product_activation_progress_fails_p6_when_live_provenance_is_not_redacted tests/test_golden_query_eval.py::test_product_evidence_summary_fails_when_p6_claims_pass_without_live_evidence`
- P6 product evidence bridge result: `4 passed, 1 warning`
- P6/golden/runtime adjacent evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress`
- P6/golden/runtime adjacent result: `87 passed, 1 warning`
- P6/CLI/MCP adjacent evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_cli.py tests/test_neuron_mcp_stdio.py`
- P6/CLI/MCP adjacent result: `256 passed, 1 warning`
- P6 activation-progress CLI evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_accepts_live_evidence_file tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_keeps_local_replay_gap tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_accepts_post_deploy_capture_file tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_fails_mutating_post_deploy_capture`
- P6 activation-progress CLI result: `4 passed, 1 warning`
- P6 activation-progress focused evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_accepts_live_evidence_file tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_keeps_local_replay_gap tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_accepts_post_deploy_capture_file tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_fails_mutating_post_deploy_capture tests/test_golden_query_eval.py::test_product_activation_progress_closes_p6_gap_with_live_session_project_rollup_evidence tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p6_gap_when_rollup_evidence_is_not_live tests/test_golden_query_eval.py::test_product_activation_progress_fails_p6_when_live_provenance_is_not_redacted`
- P6 activation-progress focused result: `8 passed, 1 warning`
- P6 activation-progress adjacent evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py tests/test_golden_query_eval.py tests/test_source_to_candidate_runtime_readiness.py`
- P6 activation-progress adjacent result: `153 passed, 1 warning`
- P6 activation-progress CLI smoke: `cd worker && uv run neuron-knowledge golden-query-eval --activation-progress`
- P6 activation-progress CLI smoke result: `status=PASS_WITH_GAPS`, `production_ready=false`, `production_mutation_performed=false`, P6 gap `p6_live_multi_device_rollup_unproven` remains visible without supplied live evidence
- P6 partial post-deploy capture regression evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_treats_partial_post_deploy_capture_as_gap tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_fails_empty_p6_post_deploy_capture tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_fails_mutating_post_deploy_capture tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_post_deploy_capture_fails_empty_session_project_rollup_runtime tests/test_source_to_candidate_runtime_readiness.py::test_neuron_knowledge_runtime_readiness_cli_evaluates_post_deploy_capture_file tests/test_golden_query_eval.py::test_product_evidence_summary_fails_closed_when_required_phase_evidence_is_missing`
- P6 partial post-deploy capture regression result: `6 passed, 1 warning`
- P6 post-#142 live capture activation-progress replay: sanitized live capture replay returns `status=PASS_WITH_GAPS`, `product_evidence_status=PASS_WITH_GAPS`, `production_ready=false`, `production_mutation_performed=false`, P6 `failures=[]`, and P6 gaps `p6_live_session_project_rollup_unverified` plus `p6_live_multi_device_rollup_unproven`
- P6 current live capture supersedes the post-#142 partial replay for the P6 gate; P7/P8/P9 remain the current production/live blockers
- P6 post-deploy MCP capture runner focused evidence: `cd worker && uv run pytest -q tests/test_post_deploy_mcp_capture.py tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_fails_malformed_p6_post_deploy_capture tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_accepts_post_deploy_capture_file tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_treats_partial_post_deploy_capture_as_gap tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_fails_empty_p6_post_deploy_capture`
- P6 post-deploy MCP capture runner focused result: `11 passed, 1 warning`
- P6 post-deploy MCP capture runner adjacent evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_golden_query_eval.py`
- P6 post-deploy MCP capture runner adjacent result: `94 passed, 1 warning`
- P6 post-deploy MCP capture runner CLI evidence: `cd worker && uv run pytest -q tests/test_neuron_cli.py`
- P6 post-deploy MCP capture runner CLI result: `66 passed, 1 warning`
- P6 post-deploy MCP HTTP transport evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_http.py`
- P6 post-deploy MCP HTTP transport result: skipped in this local env because optional MCP HTTP extra was not installed; this is not live HTTP runtime proof
- P6 configured/deployed MCP capture evidence: `cd worker && uv run --extra mcp-client neuron-knowledge source-to-candidate-runtime-readiness --collect-post-deploy-mcp-capture --mcp-url <configured lbrain MCP url> --repository pureliture/neurons --branch main --consumer codex --expected-commit <origin/main>`
- P6 configured/deployed MCP capture result: capture `source_to_candidate_runtime_post_deploy_mcp_capture.v1`, runtime packet `source_to_candidate_runtime_evidence.v1`, `collector_readiness_claim=runtime_read_path_evidence`, `post_deploy_read_only_smoke`, `network_used=true`, promoted `projection_join`, promoted `session_project_rollup_runtime`, `session_project_rollup_runtime_evidence.v1`, preview `object_extraction_session_project_rollup_preview.v1`, `device_count=2`, `WorkUnit=1`, and `production_mutation_performed=false`
- P6 configured/deployed MCP readiness replay result: `status=PASS_WITH_GAPS`, `production_ready=false`, `evidence_is_live=true`, `production_mutation_performed=false`, `failed_claims=[]`, and `live.session_project.rollup.status=validated` with no claim gaps
- P6 configured/deployed activation replay result: `status=PASS_WITH_GAPS`, `next_phase=P7`, `remaining_phases=[P7,P8,P9]`, `production_ready=false`, `production_mutation_performed=false`, P6 product check `PASS`, P6 failures `[]`, P6 gaps `[]`, `device_count=2`, `visible_session_count=2`, `all_device_session_count=2`, and `read_after_write_status=validated`
- phase coverage evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_green_release_gate_with_gaps`
- phase coverage result: `1 passed, 1 warning`
- source-to-authority strengthened review-edit gate evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_source_to_authority_quality_gate_covers_review_approval_and_read_path_without_production_mutation`
- source-to-authority strengthened review-edit gate result: `1 passed, 1 warning`
- adjacent regression evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py tests/test_neuron_cli.py tests/test_extraction_pipeline.py tests/test_golden_query_eval.py tests/test_llm_brain_core_objects_subpackage.py`
- adjacent regression result: `234 passed, 1 warning`
- worker regression evidence: `cd worker && uv run pytest -q`
- worker regression result: `1761 passed, 9 skipped, 1 warning`
- root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- root regression result: `BUILD SUCCESSFUL`

Remaining gaps:

- P6 live session/project/work-unit rollup evidence is now collected and validated through the configured/deployed MCP read path
- PR/commit/test provenance enrichment is covered by local/test metadata fixtures, not live repository history; this is a rollup-enrichment gap, not the P6 activation blocker
- source/image identity for this current P6 capture was not promoted as part of the P6 rollup proof and remains a P8 runtime-truth concern when not supplied separately

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
- phase coverage gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_green_release_gate_with_gaps`
- phase coverage result: `1 passed, 1 warning`
- P7 post-deploy evidence consumption gate: `cd worker && uv run pytest -q tests/test_post_deploy_mcp_capture.py::test_collect_post_deploy_mcp_capture_promotes_live_p7_preference_artifact_memory_from_read_only_runtime tests/test_post_deploy_mcp_capture.py::test_collect_post_deploy_mcp_capture_blocks_p7_promotion_without_runtime_evidence_class tests/test_post_deploy_mcp_capture.py::test_collect_post_deploy_mcp_capture_blocks_promotions_when_runtime_reports_protected_output tests/test_post_deploy_mcp_capture.py::test_collect_post_deploy_mcp_capture_blocks_p7_promotion_when_artifact_body_is_returned tests/test_golden_query_eval.py::test_product_activation_progress_closes_p7_gap_with_live_preference_artifact_evidence tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p7_gap_without_runtime_preference_evidence_class tests/test_neuron_cli.py::test_neuron_knowledge_golden_query_eval_activation_progress_closes_p7_post_deploy_gap`
- P7 post-deploy evidence consumption result: `7 passed, 1 warning`
- P7 adjacent product/runtime regression evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py tests/test_source_to_candidate_runtime_readiness.py tests/test_post_deploy_mcp_capture.py tests/test_neuron_cli.py`
- P7 adjacent product/runtime regression result: `180 passed, 1 warning`
- P7 `ArtifactPreference` production authority source-gate evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_approval_board_production_gate_requires_allowed_object_class tests/test_neuron_mcp_stdio.py::test_mcp_approval_board_production_gate_promotes_artifact_preference_to_authority tests/test_neuron_mcp_stdio.py::test_mcp_object_authority_production_gate_accepts_artifact_preference tests/test_neuron_mcp_stdio.py::test_mcp_object_decision_commit_is_restricted_denied_by_default tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_accepts_bounded_production_execution_for_artifact_preference`
- P7 `ArtifactPreference` production authority source-gate result: `5 passed, 1 warning`
- P7 production authority allowlist fail-closed evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_object_authority_production_gate_rejects_unallowed_object_class tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_rejects_artifact_preference_when_scope_omits_target_class`
- P7 production authority allowlist fail-closed result: `2 passed, 1 warning`
- P7 production authority independent-review closure evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_object_authority_production_gate_rejects_proposed_object_mismatch tests/test_neuron_mcp_stdio.py::test_mcp_object_authority_production_gate_rejects_cross_project_decision tests/test_neuron_mcp_stdio.py::test_mcp_object_decision_commit_requires_matching_proposal_project tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_rejects_cross_project_bounded_execution_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_rejects_non_current_artifact_preference_execution tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_keeps_replacement_current_repo_document_only tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_accepts_bounded_production_execution_for_artifact_preference`
- P7 production authority independent-review closure result: `8 passed, 1 warning`
- P7 ledger project/scope fail-closed evidence: `cd worker && uv run pytest -q tests/test_ledger_core.py::test_object_authority_decision_fails_closed_without_matching_project_and_scope tests/test_neuron_mcp_stdio.py::test_mcp_object_decision_commit_requires_matching_proposal_project tests/test_neuron_mcp_stdio.py::test_mcp_object_authority_production_gate_accepts_artifact_preference`
- P7 ledger project/scope fail-closed result: `5 passed, 1 warning`
- P7 production authority source-gate adjacent evidence: `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py tests/test_source_to_candidate_runtime_readiness.py`
- P7 production authority source-gate adjacent result: `196 passed, 1 warning`
- P7 production authority source-gate worker regression evidence: `cd worker && uv run pytest -q`
- P7 production authority source-gate worker regression result: `1835 passed, 9 skipped, 1 warning`
- P7 production authority source-gate root regression evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- P7 production authority source-gate root regression result: `BUILD SUCCESSFUL`
- post-#200 configured/deployed P7 read-path evidence: canonical `sha-98065d2f7e3c` tools image가 production MCP에서 networked `post_deploy_read_only_smoke`를 수집했습니다. `code_style_preference`와 `html_visualization_preference` route는 각각 `accepted_current` `ArtifactPreference` 하나를 포함한 `object_pack.v1`을 반환했고, compact agent context는 mutation이 disabled인 `style_preference.object_count=1`을 반환했습니다.
- post-#200 artifact consumer evidence: 이름이 지정된 `brain_artifact_preference_evaluate` call은 `status=PASS`인 bound application receipt를 반환했습니다. Startup receipt validation은 `validated`였고, collector subprocess는 attested되었으며, read surface recheck도 validated되었습니다. Raw artifact body/protected output은 반환되지 않았고 production mutation은 false로 유지되었습니다.
- post-#200 P7 evaluation evidence: same-process live collector/evaluator는 P7 product evidence check를 P7 gap이나 failure 없이 `PASS`로 판정합니다. Persisted capture replay는 process-bound collector capability가 의도적으로 serializable하지 않기 때문에만 P7을 `PASS_WITH_GAPS`로 판정합니다. Replay에는 hard failure가 없으며 capability reissuance나 self-attestation도 허용하지 않습니다.

Implemented local/test scope:

- `ArtifactPreferencePack`, `PersonalCodeStyleProfile`, `RepoStyleProfile`, `HtmlReviewProfile`, and `VisualizationProfile` preview objects
- accepted versus proposal lane separation for preferences and style claims
- accepted preference context pack lane with public-safe evidence refs
- inferred preference and legacy style inertia routed to review/proposal lane first
- HTML review artifact summary/metrics preference check that does not require UI rendering and does not return artifact body
- diff/artifact review suggestions for HTML, visualization, and repo style drift
- runtime readiness now includes `live.preference_artifact.memory`, requiring a sanitized `preference_artifact_memory_runtime_evidence.v1` packet for accepted/proposal preference lane separation, accepted context-pack presence, explicit `html_visualization_preference` route smoke, no-UI/no-raw-body artifact review check, and public-safe postcheck before P7 live runtime proof can pass
- post-deploy capture promotion consumes P7 evidence only when the packet is explicitly marked `evidence_class=runtime_preference_artifact_memory`, collected through `post_deploy_read_only_smoke` with `network_used=true`, reports no production mutation, reports no protected output flags, has validated public-safe postcheck, and confirms `artifact_review_check.raw_artifact_body_returned=false`
- markerless shadow/local-test preference evidence is intentionally not promoted to P7 live product evidence, even when it is carried inside a deployed runtime collection packet
- missing P7 runtime packet evidence remains `PASS_WITH_GAPS` with `live_preference_artifact_memory_unverified` and `accepted_preference_context_pack_live_unproven`; unsafe or incomplete supplied evidence fails closed
- P5 product evidence checks now also mark P7 as `PASS_WITH_GAPS` with `p7_accepted_preference_context_pack_live_unproven` and `p7_html_artifact_review_live_unproven` until deployed/live consumer evidence is attached
- Issue #186 / PR #187 adds one shared source-side production authority class policy for `RepoDocument` and `ArtifactPreference`. Approval-board and low-level proposal/decision paths keep runtime opt-in, per-call gate, single-project/single-object scope, read-after-write, rollback/supersession, and protected-output guards. Low-level proposals reject `proposed_object` id/type mismatch, decisions must match the original proposal project/target/ledger scope, and the ledger repeats the project/scope guard. Runtime readiness requires approval/proposal/decision/scope project continuity, target-class scope inclusion, and an accepted-current decision/read-back/queue chain for `ArtifactPreference`. The replacement-current validator remains `RepoDocument`-only.

Remaining gaps:

- P7 runtime behavior는 in-process에서 live-proven이지만 persisted evidence artifact는 process-bound collector capability를 담을 수 없습니다. Evidence contract가 독립적으로 검증 가능한 serializable attestation을 갖출 때까지 roadmap accounting은 phase 전체의 production validation을 과장하지 않고 `local_validated` / `PASS_WITH_GAPS`로 유지됩니다.
- Persisted P7 product evidence에는 collector-capability gap만 있지만, 최상위 activation `phase_progress`에는 `accepted_preference_context_pack_live_unproven`, `html_artifact_review_live_unproven`이 여전히 남아 있습니다. 다음 evaluator pass는 누락된 capability를 reissue하거나 self-attest하지 않고 이 일반 phase gap을 validated product evidence와 reconcile해야 합니다.
- Bounded live proof는 deployed artifact evaluator와 compact Codex-oriented context projection을 다룹니다. 더 넓은 consumer startup과 runtime interception은 P7 receipt 결과를 약화할 이유가 아니라 P9 concern입니다.
- Full object-browser 또는 visual review UI는 P10에 속하며 P7 read-path/receipt gate의 prerequisite가 아닙니다.

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
- phase coverage gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_green_release_gate_with_gaps`
- phase coverage result: `1 passed, 1 warning`
- runtime evidence collection plan gate: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_evidence_collection_plan_is_public_safe_and_read_only tests/test_source_to_candidate_runtime_readiness.py::test_neuron_knowledge_runtime_readiness_cli_outputs_evidence_collection_plan tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_collection_plan`
- runtime evidence collection plan result: `3 passed, 1 warning`
- runtime evidence packet template gate: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_evidence_packet_template_is_public_safe_and_not_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_neuron_knowledge_runtime_readiness_cli_outputs_evidence_packet_template tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_packet_template`
- runtime evidence packet template result: `3 passed, 1 warning`
- runtime evidence collector gate: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_collector_builds_shadow_evidence_packet_without_mutation tests/test_source_to_candidate_runtime_readiness.py::test_neuron_knowledge_runtime_readiness_cli_collects_shadow_evidence tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_collects_shadow_evidence tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p2_to_p9_scope_visible tests/test_golden_query_eval.py::test_product_evidence_summary_marks_p8_runtime_unverified_as_gap_not_pass`
- runtime evidence collector result: `5 passed, 1 warning`
- runtime evidence collector CLI smoke: `source-to-candidate-runtime-readiness --collect-shadow-evidence` emits `source_to_candidate_runtime_evidence.v1` with `source_to_candidate_review_loop_evidence.v1`, `session_project_rollup_runtime_evidence.v1`, `preference_artifact_memory_runtime_evidence.v1`, `permission_sensitive_runtime_audit_evidence.v1`, and `agent_context_startup_runtime_evidence.v1`; evaluating that packet returns `status=PASS_WITH_GAPS`, `failed_claims=[]`, `live.brain_objects_query.route_smokes.status=validated`, `live.source_to_candidate.review_loop.status=validated`, `live.session_project.rollup.status=validated`, `live.preference_artifact.memory.status=validated`, `live.production.permission_sensitive_audit.status=validated`, and `live.agent_context.startup_read_path.status=validated` for local_test shadow evidence, `production_mutation_performed=false`, and `evidence_collection_network_used=false`
- P8 live authority evidence product gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_product_activation_progress_closes_p8_gap_with_live_runtime_authority_evidence tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p8_permission_audit_gap_when_identity_only tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p8_live_gap_for_local_replay_runtime_authority_evidence tests/test_golden_query_eval.py::test_product_activation_progress_fails_p8_when_permission_audit_returns_protected_values tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_does_not_treat_deployed_identity_as_permission_audit tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_permission_sensitive_audit_is_unsafe_or_incomplete tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_read_only_provenance_claims_bounded_mutation_scope`
- P8 live authority evidence product gate result: `7 passed, 1 warning`
- P8 adjacent activation/runtime regression evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_cli.py`
- P8 adjacent activation/runtime regression result: `173 passed, 1 warning`

Production validation evidence:

- Source delivery는 deployment와 분리합니다. PR #204는 immutable expected commit anchor와 `gitops_desired_state`, `argo_reconciliation`, `deployed_identity`를 한 packet에서 검증하는 P8 binding을 merge했고 source merge commit은 `73d440522e49`입니다. GitHub PR checks와 review gate는 모두 green이고, 별도 local source verification은 전체 worker regression `2166 passed, 10 skipped`와 root Gradle `BUILD SUCCESSFUL`입니다.
- Canonical artifact build는 source `73d440522e49`에서 runtime component 6개의 verify와 image push를 완료했습니다. Bulk semantic component의 최초 job은 image push 뒤 canary GitOps update 충돌로 전체 `FAILURE`였고, image를 다시 만들지 않은 GitOps-only recovery는 `SUCCESS`였습니다. 이 기록은 canonical build 성공과 최초 GitOps update 실패를 섞어 표현하지 않습니다.
- GitOps desired-state delivery는 neurons-ops PR #44 merge `4270ab24cc55`입니다. Active production manifest closure의 source-tagged image ref는 8/8이고 stale target ref는 `0`입니다.
- Deploy execution은 Jenkins production precheck #30 `SUCCESS`와 non-prune sync #31 `SUCCESS`입니다. Live Argo는 별도로 `targetRevision=main`, `Synced`, `Healthy`, revision `4270ab24cc55`를 보고했습니다.
- Live runtime은 desired/live image identity key set 8/8, 동일 image-set SHA-256, stale target ref `0`, active replica 5/5 ready, paused target desired `0`, restart total `0`입니다. Networked, sanitized `post_deploy_read_only_smoke`는 deployed identity에 `73d440522e49`를 포함하고, allowlisted object-native tool 9개와 `brain_objects_query` route 6개의 `object_pack.v1`을 확인했으며 mutation scope `none`, authority write 없음, protected-output flag 없음으로 보고됩니다.
- 동일 packet의 `deployment_evidence_binding.v1`은 external expected source commit, desired ops revision, Argo reconciled revision, desired/live image-set hash, deployed source identity를 연결합니다. Desired-state, Argo, deployed-identity, binding claim은 모두 `validated`, failed claim은 `0`이고 packet SHA-256은 `9f2c7cc0fec47d973682df8cd96f7eca36d4c83366c1d6b785a0ba6de31f2900`입니다. 이 consistency binding은 cryptographic authenticity claim이 아닙니다.
- Live gate policy evidence는 authority-changing tool schema 3개 모두에 `production_gate`가 있음을 보여 줍니다. 이 gate 없이 수행한 `brain_approval_board_decide`, `brain_object_proposal_create`, `brain_object_decision_commit` call은 proposal/authority/production mutation false로 거부되었고, smoke 전후 proposal queue count와 object read hash는 변하지 않았습니다.
- Persisted P8 product evidence는 GitOps/live packet-binding claim을 통과합니다. 다만 permission-sensitive audit-store evidence가 아직 없으므로 P8 전체는 failure 없이 `PASS_WITH_GAPS`, `production_ready=false`로 남습니다.

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
- runtime readiness validates independently supplied `gitops_desired_state_identity.v1`, `argo_reconciliation_identity.v1`, and deployed identity evidence, then mints `deployment_evidence_binding.v1` only when an external immutable 40-character source commit anchor, desired/reconciled ops revision equality, desired/live image-set hash equality, `Synced`/`Healthy`, positive desired count, zero stale live count, no mutation, and protected-output guards all pass
- missing binding evidence remains a visible gap, while malformed, mutable-ref, mismatched, unknown-field, protected-output, or mutation evidence fails closed; the binding proves internal packet consistency and does not claim cryptographic authenticity
- activation progress now consumes P8 live runtime-authority evidence only through the separated `deployed_identity`, `permission_sensitive_audit`, and `evidence_provenance` claims; deployed identity proves source/image identity only and cannot substitute for permission-sensitive audit evidence
- P8 product evidence removes `live_runtime_rollout_identity_unproven` and `production_permission_audit_live_unproven` only when the supplied packet has live provenance, deployed identity containing the expected commit, and a validated no-mutation permission-sensitive audit packet; local replay remains `PASS_WITH_GAPS`, and protected-value audit evidence fails closed
- `source-to-candidate-runtime-readiness --evidence-collection-plan` and `brain_source_to_candidate_runtime_readiness(evidence_collection_plan=true)` return a public-safe post-deploy read-only collection plan for the required MCP tools, `brain_objects_query` route smokes, deployed identity, production denied/no-mutation checks, authority gate policy, and evidence provenance schema
- `source-to-candidate-runtime-readiness --evidence-packet-template` and `brain_source_to_candidate_runtime_readiness(evidence_packet_template=true)` return a public-safe template for the sanitized `source_to_candidate_runtime_evidence.v1` packet that a post-deploy runner must fill; the template itself is marked `template_only_not_runtime_evidence`
- `source-to-candidate-runtime-readiness --collect-shadow-evidence` and `brain_source_to_candidate_runtime_readiness(collect_shadow_evidence=true)` generate a branch-local read-only collector packet from object-query route smokes, a local_test source-to-candidate review-loop smoke, a local_test P6 session/project/work-unit rollup smoke, a local_test P7 preference/artifact memory smoke, a local_test P8 permission audit smoke, and a local_test P9 startup/read-path smoke; the packet is evaluator-ready but marked `collector_packet_not_live_evidence`

Remaining gaps:

- Production gate denial 3건과 그 no-mutation postcheck는 live-proven이지만, deployed audit-store collector가 해당 denial event를 필요한 hashed actor/request reference와 함께 기록했음을 증명하지 못했습니다. 따라서 `permission_sensitive_audit_unverified`는 실제 P8 gap으로 남습니다.
- GitOps desired state, Argo reconciled revision, canonical/live image identity, deployed source identity는 post-#204 sanitized packet 안에서 bind되어 `p8_gitops_desired_state_unverified` gap을 닫았습니다.
- 이번 최종 checkpoint는 의도적으로 production authority mutation을 수행하지 않았습니다. 이전의 bounded P4 authority execution 및 P7 preference materialization evidence는 별개의 historical mutation proof이며, 누락된 current P8 audit-store evidence를 대체하지 않습니다.
- P8은 current-source permission-sensitive audit-store packet을 수집해 no-mutation denial event와 독립적으로 bind할 때까지 `local_validated` / `PASS_WITH_GAPS`, `production_ready=false`로 유지됩니다. 현재 P8 gap은 audit-store proof 하나이며 GitOps/live packet binding은 더 이상 gap이 아닙니다.

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
- post-deploy MCP agent-context capture gate: `cd worker && uv run pytest -q tests/test_post_deploy_mcp_capture.py`
- post-deploy MCP agent-context capture result: `5 passed, 1 warning`
- post-deploy MCP adjacent readiness/CLI gate: `cd worker && uv run pytest -q tests/test_post_deploy_mcp_capture.py tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_cli.py`
- post-deploy MCP adjacent readiness/CLI result: `133 passed, 1 warning`
- post-deploy MCP post-change worker full evidence: `cd worker && uv run pytest -q`
- post-deploy MCP post-change worker full result: `1743 passed, 9 skipped, 1 warning`
- post-deploy MCP post-change root evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- post-deploy MCP post-change root result: `BUILD SUCCESSFUL`
- post-deploy MCP activation-progress smoke evidence: `cd worker && uv run neuron-knowledge golden-query-eval --activation-progress`
- post-deploy MCP activation-progress smoke result: `status=PASS_WITH_GAPS`, `production_ready=false`, `production_mutation_performed=false`, P9 gaps `p9_runtime_evidence_unverified`, `p9_production_consumer_context_pack_live_unproven`, and `p9_consumer_action_surface_runtime_policy_unproven` remain visible
- runtime readiness startup/read-path packet gate evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_evidence_packet_template_is_public_safe_and_not_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_without_live_evidence_preserves_gaps_and_no_mutation tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_passes_with_sanitized_live_evidence tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_agent_context_startup_runtime_is_unsafe_or_incomplete`
- runtime readiness startup/read-path packet gate result: `4 passed, 1 warning`
- runtime/MCP startup/read-path packet gate adjacent evidence: `cd worker && uv run pytest -q tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_evaluates_sanitized_evidence_without_mutation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_collection_plan tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_returns_evidence_packet_template tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_accepts_bounded_execution_evidence_from_local_production_gate_simulation tests/test_neuron_mcp_stdio.py::test_mcp_source_to_candidate_runtime_readiness_without_evidence_preserves_live_gaps`
- runtime/MCP startup/read-path packet gate adjacent result: `46 passed, 1 warning`
- phase coverage gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_phase_golden_query_coverage_reports_green_release_gate_with_gaps`
- phase coverage result: `1 passed, 1 warning`
- degraded agent context actionability gate: `cd worker && uv run pytest -q tests/test_context_pack_builder.py::test_builder_marks_empty_required_agent_context_sections_as_actionable_gaps`
- degraded agent context actionability result: `1 passed, 1 warning`
- degraded agent context adjacent regression evidence: `cd worker && uv run pytest -q tests/test_context_pack_builder.py tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_requires_live_agent_context_product_sections tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_live_agent_context_product_contract_is_incomplete tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_live_agent_context_allows_mutation tests/test_source_to_candidate_runtime_readiness.py::test_runtime_readiness_fails_when_agent_context_startup_runtime_is_unsafe_or_incomplete`
- degraded agent context adjacent regression result: `12 passed, 1 warning`
- degraded context MCP/CLI read-surface regression evidence: `cd worker && uv run pytest -q tests/test_context_pack_builder.py tests/test_neuron_mcp_stdio.py tests/test_neuron_cli.py`
- degraded context MCP/CLI read-surface regression result: `187 passed, 1 warning`
- degraded agent context full worker evidence: `cd worker && uv run pytest -q`
- degraded agent context full worker result: `1778 passed, 9 skipped, 1 warning`
- degraded agent context root evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- degraded agent context root result: `BUILD SUCCESSFUL`
- P9 live agent-context evidence-consumption gate: `cd worker && uv run pytest -q tests/test_golden_query_eval.py::test_product_activation_progress_closes_p9_gap_with_live_agent_context_evidence tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p9_gap_for_empty_live_agent_context_sections tests/test_golden_query_eval.py::test_product_activation_progress_keeps_p9_live_gap_for_local_agent_context_replay tests/test_golden_query_eval.py::test_product_activation_progress_fails_p9_when_agent_context_tool_hint_is_unsafe`
- P9 live agent-context evidence-consumption result: `4 passed, 1 warning`
- P9 live agent-context adjacent activation/runtime regression evidence: `cd worker && uv run pytest -q tests/test_golden_query_eval.py tests/test_source_to_candidate_runtime_readiness.py tests/test_neuron_cli.py`
- P9 live agent-context adjacent activation/runtime regression result: `179 passed, 1 warning`
- P9 live agent-context full worker evidence: `cd worker && uv run pytest -q`
- P9 live agent-context full worker result: `1782 passed, 9 skipped, 1 warning`
- P9 live agent-context root evidence: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- P9 live agent-context root result: `BUILD SUCCESSFUL`

Production validation evidence:

- PR #195는 `42b264c89637`에서 bounded startup adapter를 merge했고, PR #197은 `d79fefa6bf18`에서 route/receipt binding stabilization을 merge했습니다. 이후 PR #199/#200은 tamper, unsafe-output, mutation, zero-current failure를 약화하지 않고 capability-only persisted replay를 visible gap으로 유지했습니다. 최종 deployed source는 `98065d2f7e3c`입니다.
- Deployed collector는 별도의 attested subprocess를 실행하고 route projection 6개를 startup product에 bind한 뒤 `receipt_validation.status=validated`를 반환했습니다. 이는 상태를 숨기지 않고 current authority `1`, style preference `1`, active work `1`, guardrails `6`, required verification `1`, 명시적으로 비어 있는 reference-object section을 포함한 `consumer=codex`용 `agent_context_product_pack.v1`을 load했습니다.
- Startup surface는 read-only입니다. Direct execution과 production mutation은 false이고 raw private context blocking과 approval-scope blocking이 enabled이며, tool hint는 safe target을 가진 suggest-only이고 protected-output flag는 반환되지 않았습니다. 이 validation 동안 executor invocation이나 production mutation은 발생하지 않았습니다.
- In-process P9 evaluation은 `PASS`가 아닌 `PASS_WITH_GAPS`입니다. Product section과 tool hint는 validate되지만 실제 Codex host startup hook과 runtime action interception은 관측되지 않았고 Claude Code, Gemini, Hermes startup consumer도 live-validated되지 않았습니다. Persisted replay에는 non-serializable startup collector capability gap도 남습니다. 어느 표현에도 hard failure는 보고되지 않습니다.

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
- `source-to-candidate-runtime-readiness --collect-post-deploy-mcp-capture` now calls read-only deployed `brain_context_resolve` and stores the sanitized `agent_context_product_pack.v1` inside `source_to_candidate_runtime_post_deploy_mcp_capture.v1`; missing or errored context capture becomes a public-safe `agent_context_product_capture_failed` gap instead of a hidden pass
- post-deploy capture readiness replay can validate `live.agent_context.tool_hints` and `live.agent_context.product_sections` from the captured product, but this remains runner/source evidence until a deployed MCP capture packet is collected from the live rollout
- post-#142 deployed MCP capture replay validates live object-native `tool_hints` from the sanitized `agent_context_product_pack.v1`, but `active_work` and `style_preference` product sections are empty in that capture, so production agent context productization remains `PASS_WITH_GAPS`
- runtime readiness fails unsafe live agent context `tool_hints` when a required object-native tool allows direct execution, allows production mutation, omits safe targets, advertises non-allowlisted safe targets, or when `brain_approval_board_decide` lacks the `approved_scope_required` blocker
- runtime readiness fails incomplete live agent context products when schema/consumer/degraded gap disclosure or `missing_evidence_before_promotion` is absent, and requires the runtime-readiness tool hint to target `sanitized_evidence_packet` while blocking `raw_private_runtime_evidence`
- runtime readiness surfaces `live.production.object_authority_bounded_execution` so agent context/product-readiness checks can distinguish preapproved authority mutation from actual bounded execution evidence
- runtime readiness surfaces `live.evidence.provenance` so agent context/product-readiness checks can distinguish evaluator-local no-network execution from external live evidence collection
- runtime readiness exposes a plan/template mode for agents and operators to collect the missing post-deploy evidence without performing production mutation or returning protected values
- degraded `agent_context_product_pack.v1` now turns empty required sections into actionable gaps: `agent_context_style_preference_missing`, `agent_context_active_work_missing`, and `agent_context_required_verification_missing` are attached to the relevant section, product degraded gaps, missing-evidence-before-promotion blockers, and `request_missing_evidence` action hints instead of leaving a silent empty section
- activation progress now consumes supplied P9 live agent-context evidence through `live.agent_context.tool_hints`, `live.agent_context.product_sections`, `live.agent_context.startup_read_path`, and `live.evidence.provenance`; complete live evidence can close the P9 phase gaps, empty required sections remain `PASS_WITH_GAPS`, local replay keeps `p9_agent_context_evidence_not_live`, and unsafe tool/startup/protected-output evidence fails closed

Remaining gaps:

- Deployed bounded collector는 compact Codex-oriented pack과 startup receipt를 증명하지만 실제 Codex host startup hook은 증명하지 않습니다. 이 gap을 닫으려면 real Codex startup 중 host integration이 pack을 load하고 consume해야 합니다.
- Claude Code, Gemini, Hermes는 source/local contract coverage만 있습니다. Codex 결과를 상속하지 말고 각각의 deployed startup receipt가 필요합니다.
- Runtime action interception은 관측되지 않았고 consumer action-surface policy도 deployed enforcement로 증명되지 않았습니다. Suggest-only projection은 policy shape의 증거일 뿐 host executor가 이를 우회할 수 없다는 증거는 아닙니다.
- Production authority-changing action은 bounded scope, audit, rollback/supersession, redaction, postcheck로 계속 별도 gate됩니다. 이 read-only P9 validation은 production write를 실행하거나 재승인하지 않았습니다.

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

- P1, P2, and P6 are `production_validated` for their bounded gates. P7 has passing same-process live product evidence but remains `local_validated` / `PASS_WITH_GAPS` because persisted collector capability is not independently serializable.
- P5 is `local_validated` and the release-quality evaluator gate is green. Product-wide production readiness remains `PASS_WITH_GAPS` because P8 permission-sensitive audit-store and P9 host/consumer/runtime-enforcement evidence are incomplete.
- P8 and P9 now have bounded deployed/live evidence, not local/test evidence only. P8의 GitOps/live in-packet join은 post-#204 proof로 닫혔고 남은 P8 gap은 permission-sensitive audit-store recording 하나입니다. P9에는 non-Codex consumer startup, runtime interception, actual Codex host startup, and consumer-policy enforcement gap이 남습니다.
- UI is explicitly not a prerequisite for MCP/read-path activation, authority writes, or production rollout.
- P7 HTML/visualization preference routing is read-path evidence only and must not be treated as P10 object browser/UI launch evidence.

Decision outcome:

- Do not start UI/object browser implementation in this roadmap run.
- Keep P10 open as a later product surface after the read path, authority lifecycle, and quality gates are production-proven.
- Do not defer the minimal candidate edit/review surface needed by P3/P4.
- If P10 is later started, the first slice must be read-only/local, must use existing object packs, and must not introduce production mutation or protected-value disclosure.

## Temporal Recall Correctness Corrective Checkpoint (2026-07-16)

Status: `in_progress`. 이 checkpoint는 기존 P6의 route/object 존재 smoke를 temporal recall semantic correctness 증명으로 해석하지 않습니다. 아래 acceptance evidence가 모두 수집되기 전에는 temporal recall, production `brain.query` relevance, projection currentness를 `PASS`로 선언하지 않습니다.

확정된 결함 경로:

- `temporal_work_recall`은 날짜를 route 선택에만 사용하고 event/observed time 필터에는 적용하지 않았습니다.
- live ingress는 `observed_at_start` / `observed_at_end`를 source model에 보존하지 않았고, 이미 `PROJECTED`인 session에 distinct chunk가 추가되어도 dirty/pending으로 되돌리지 않았습니다.
- session-memory builder와 graph projection은 source revision/coverage hash 대신 session identity와 완료 상태에 의존해 stale projection을 current로 오판할 수 있었습니다.
- global limit와 deterministic ordering은 processed session을 반복 선택해 backlog starvation을 만들었습니다.
- production `brain.query`는 strict relevance와 semantic result lane을 실제 결과에 결합하지 않아 unrelated current-card padding을 허용했습니다.
- 기존 smoke는 route와 object 존재만 확인했고, empty/mismatched temporal evidence에도 gap 없는 고정 confidence를 줄 수 있었습니다.

Corrective contract:

- source는 observed/event time, materialization revision, source revision 또는 coverage hash를 보존합니다. Exact duplicate는 idempotent하게 유지하고 distinct content는 session-memory와 graph projection을 모두 invalidate합니다.
- projection state는 projected source hash와 current source hash를 비교하며, same-session artifact 최신성은 materialization revision으로 결정합니다.
- public query contract는 `as_of`, `date_from`, `date_to` 또는 동등한 명시 selector를 제공하고 invalid range를 거부합니다. Offset이 없는 bare date와 inferred today/yesterday는 UTC calendar day로 해석하고, offset을 포함한 instant는 UTC로 normalize합니다. Temporal evidence가 없거나 selector와 불일치하면 최신 객체로 fallback하지 않고 빈 결과, 명시적 gap, fail-closed confidence를 반환합니다.
- production `brain.query`는 semantic ranker가 bound/used되었음을 audit하고 threshold 이상인 expected Qdrant semantic hit을 실제 result lane에 정확히 한 건 반영합니다. Positive probe는 expected/observed result fingerprint, `semantic_match` reason, score threshold를 raw query/result 원문 없이 검증합니다. Nonsense query에는 unrelated current cards를 채우지 않습니다.
- route/object existence smoke는 `temporal_recall_corrective_checkpoint.v1` semantic acceptance packet이 없으면 `temporal_work_recall`을 validated route로 세지 않습니다. Legacy packet은 계속 읽을 수 있지만 corrective checkpoint 없이 validated로 승격하지 않습니다.
- runtime aggregate는 source/projection hash currentness, stale projected session count, artifact currentness, entity coverage/backlog/error count를 public-safe aggregate로만 기록합니다. Operator가 acceptance 기대값이나 receipt로 주입한 aggregate는 신뢰하지 않으며, production read path가 현재 source/projection authority와 정상 종료된 최신 graph run을 직접 읽어 만든 `live_mcp_runtime_packet`만 acceptance evidence로 사용합니다. Exact argv, backup/restore, bounded postcheck receipt는 mutation audit에는 필요하지만 MCP semantic acceptance aggregate를 대체하지 않습니다.
- graph run evidence는 opaque run id의 start/complete pair, project scope hash, provider scope, entity extraction level, started/completed timestamp를 결합합니다. `neurons` project 전체를 대상으로 정상 종료했고 configured freshness window 안에 있는 run만 runtime acceptance에 사용하며, 오래됐거나 다른 scope의 성공 로그는 fail-closed로 거부합니다.
- same-session temporal artifact는 cumulative session bounds와 별도로 source revision이 새로 도입한 event-time window를 기록합니다. Legacy source-event identity를 migration-safe하게 비교해 이전 chunk를 새 revision으로 오인하지 않으며, 누락된 historical observed time은 retained redacted ingress payload에서 bounded dry-run/apply backfill한 뒤 projection을 invalidate합니다.
- 한 materialization에 여러 event-time interval이 있으면 각 interval과 revision-local search-term hash를 결합해 보존합니다. 서로 다른 interval의 term union을 공유하지 않으며, supplied bound가 malformed이거나 일부 source event의 observed time이 누락되면 temporal lane은 fail-closed합니다. Tool-evidence bundle도 서로 다른 또는 유효하지 않은 event time을 하나의 bounded bundle로 합치지 않습니다.
- `couchdb-projection-invalidation-canary`는 stable synthetic canary session 하나에서 distinct chunk의 source-hash 변경, session-memory/graph dirty 및 재선택, 양쪽 projected-hash catch-up, exact duplicate nonselection을 bounded public-safe receipt로 증명합니다. Dry-run plan digest, exact argv approval, 외부 hard timeout, fresh-nonce restore/retry, destructive mutation 금지가 release gate입니다.

Production mutation boundary:

- 이 corrective run에는 사용자가 bounded production ledger/corpus/runtime mutation을 사전승인했습니다. 이는 기존 read-only evidence run과 분리된 현재 작업의 명시적 scope override입니다.
- 허용 범위는 additive migration, dry-run count, backup/rollback 또는 restore 경로, bounded timeout/abort 기준, resumable reprojection, bounded artifact rebuild, entity-extraction canary, postcheck입니다.
- Destructive delete/GC, secret/host topology/raw external ID/raw transcript 출력, 승인 범위 밖 authority mutation은 포함하지 않습니다.

Pending acceptance evidence:

- [ ] Focused temporal/relevance/currentness regression tests와 전체 `worker` test suite가 통과합니다.
- [ ] Neurons PR의 review와 CI가 통과하고 merge됩니다.
- [ ] Ops PR의 bounded canary/fair scheduling/postcheck 변경이 review와 CI를 통과하고 merge됩니다.
- [ ] Canonical image build, GitOps desired-state update, reconciliation, rollout을 각각 분리된 evidence로 확인합니다.
- [ ] Date A와 Date B가 서로 다른 expected/observed whole-object fingerprint와 stable object identity fingerprint를 반환하고, range boundary와 invalid range 계약이 통과합니다.
- [ ] Empty/mismatched temporal evidence가 object count 0, non-empty gap, fail-closed confidence를 반환합니다.
- [ ] Nonsense `brain.query`가 result/current/accepted lane에 unrelated card를 반환하지 않고 semantic ranker가 bound/used됩니다.
- [ ] Positive semantic `brain.query`가 expected fingerprint와 일치하는 Qdrant result를 정확히 한 건 반환하고, `semantic_match` reason, minimum score, semantic ranker bound/used를 모두 충족합니다.
- [ ] Distinct new chunk 뒤 source hash와 projected hash가 다시 일치하고 stale projected session count와 artifact currentness postcheck가 통과합니다. Exact duplicate는 재선택되지 않습니다.
- [ ] Entity extraction bounded canary 뒤 coverage가 증가하거나 backlog가 감소하며 error count와 abort 기준을 함께 기록합니다.
- [ ] Merge, source CI, image build, GitOps desired state, rollout, live semantics, cleanup을 분리 보고하고 최종 상태를 `PASS`, `PASS_WITH_GAPS`, `FAIL` 중 하나로 판정합니다.

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

P5는 evaluator/release gate 자체에 대해서만 `green`을 선언할 수 있습니다. Product-wide production readiness에는 여전히 P7 evidence-portability, P8 permission-sensitive audit-store, P9 host/consumer/enforcement evidence가 필요하며, bounded live proof를 검증된 slice 밖으로 일반화해서는 안 됩니다.

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
| P1 Production MCP Activation | `production_validated` | `PASS` for P1 activation; deployed/configured endpoint object-tool smoke, post-#115 image/source identity, deploy-button rollout, tools/list, source/review/readiness tool exposure, approved schema repair, production denied/no-mutation smokes, and six-route object-pack proof pass. Product-wide readiness remains `PASS_WITH_GAPS` because P7/P8/P9 runtime evidence remain gaps |
| P2 Living Reference Corpus Store | `production_validated` | `PASS`; local/test store/status gates, bounded production corpus ingest readiness evaluator, deployed schema support, live Palantir manifest count gate, approved production reference-only ingest, read-after-write status, redaction postcheck, and repeated-ingest idempotence pass. This does not promote reference material to accepted/current authority |
| P3 Processing And Object Extraction Pipeline | `local_validated` | `PASS_WITH_GAPS`; local/test extraction previews, store-to-candidate CLI/MCP wiring, candidate review/edit pack, branch-local readiness evidence gate, deployed P2-corpus source-to-candidate review-loop smoke, and post-#152 live graph/Qdrant projection-join smoke pass. P3 projection-join product evidence is `PASS` when the sanitized live capture is supplied, but product-wide P7/P8/P9 runtime evidence remain gaps |
| P4 Review Queue And Authority Promotion | `replacement_current_production_validated` | `PASS`; local/test authority state, audit gates, rollback decision lineage, review/approval CLI/MCP chain, approval-board preview, local_test promotion preview, reviewer edit no-mutation proof, branch-local review-loop readiness gate, approval-board runtime evidence gap closure, bounded execution packet shape, deployed production gate schema/policy, live approval-board denial/no-mutation smoke, stable review queue postcheck, one-shot synthetic `RepoDocument` production reject write, fresh accepted-current rollback-to-archive write, post-#128 approval-board production promotion write, replacement-current prior/successor write, read-after-write, decision-history lineage, redacted provenance, bounded-execution readiness claim, and replacement-current readiness claim pass |
| P5 Continuous Golden Query Quality Gates | `local_validated` | `PASS_WITH_GAPS`; phase coverage, source-to-authority path gate, P3/P4/P6 supplied-live-evidence product check, FR8 code-change-impact route gate, P7 HTML/visualization route evidence, P2-P9 activation progress gate, partial post-deploy capture interpretation guard, P3/P4/P6 live-capture activation-progress gap closure가 있습니다. Local quality gate와 release-quality evaluator gate는 `green`이고 P5 phase는 `PASS`입니다. P7 evidence portability, P8 permission-sensitive audit-store, P9 host/consumer/enforcement proof가 열려 있으므로 product-wide readiness는 `PASS_WITH_GAPS`로 남습니다. |
| P6 Session, Device, Project, And Work-Unit 360 | `production_validated` | `PASS`; local/test rollup, handoff gates, temporal `brain_objects_query` `WorkUnit` route, branch-local P6 runtime evidence packet validation, P5 product evidence gap surfacing, post-deploy MCP capture runner contract, and current configured/deployed read-only P6 rollup capture pass. Runtime-readiness validates `live.session_project.rollup`, activation progress marks P6 `PASS`, and `next_phase=P7`; source/image identity remains a separate P8 proof when not supplied |
| P7 Preference, Style, And Artifact Memory | `local_validated` | Phase accounting은 `PASS_WITH_GAPS`입니다. PR #187/#189/#191/#193은 bounded production authority, canonical materialization, artifact consumer receipt, PostgreSQL compatibility를 확립했습니다. 최종 `sha-98065d2f7e3c` deployed read path는 accepted-current style/HTML preference 하나, 비어 있지 않은 style-preference context section, validated `PASS` artifact receipt를 반환합니다. Same-process live P7 product evidence는 `PASS`이지만 persisted product evidence에는 non-serializable collector-capability gap이 남고, 최상위 `phase_progress`에도 aggregation reconciliation 전의 이전 generic live-preference gap 2개가 남습니다. 이 phase를 `production_validated`로 과도하게 올리지 않습니다. |
| P8 Runtime Truth, Security, And Deployment Authority | `local_validated` | `PASS_WITH_GAPS`; neurons PR #204 source merge, GitHub checks, canonical image build, ops PR #44 desired state, Jenkins #30/#31, Argo exact revision/health, desired/live image identity 8/8, deployed source identity, read-only route 6개를 분리해 증명했습니다. 같은 sanitized packet의 `deployment_evidence_binding.v1`이 expected source, desired/reconciled ops revision, desired/live image-set hash, deployed identity를 연결해 GitOps/live binding gap은 `PASS`로 닫혔습니다. Permission-sensitive audit-store recording만 live-proven이 아니므로 P8 전체는 `PASS_WITH_GAPS`, `production_ready=false`입니다. |
| P9 Agent Context Productization | `local_validated` | `PASS_WITH_GAPS`; deployed bounded collector는 비어 있지 않은 current-authority/style-preference/active-work/guardrail/verification section, attested subprocess binding, validated startup receipt, safe tool hint, no mutation을 가진 Codex-oriented startup pack을 validate합니다. Claude Code, Gemini, Hermes startup receipt, runtime action interception, 실제 Codex host startup-hook integration, deployed consumer action-surface enforcement은 아직 증명되지 않았습니다. |
| P10 UI And Object Browser Surface | `planned` | `PASS_WITH_GAPS` for start-readiness review; full object browser deferred, but minimal P3/P4 candidate edit/review surface is now a prerequisite |

Delivery integration status:

- PR #84부터 PR #95까지 `main`에 merge되었습니다. PR #97, PR #103, PR #105, PR #107, PR #109, PR #111, PR #113, PR #115, PR #119, PR #121, PR #122, PR #123, PR #124, PR #125, PR #126, PR #128, PR #142, PR #148, PR #150, PR #152, PR #162, PR #164, PR #166, PR #168, PR #170, PR #174, PR #176, PR #178, PR #180, PR #182, PR #184, PR #187, PR #189, PR #191, PR #193, PR #195, PR #197, PR #199, PR #200, PR #204는 post-#95 source/docs/evidence-gate follow-up으로 `main`에 merge되었습니다.
- PR #95 merged the integrated P2-P9 roadmap branch at source head `5c301c6` with merge commit `32f4fec`. Review-follow-up commit `0c70111` addressed the production gate/type and corpus approval-hash review findings before merge. Its runtime-readiness surface includes a public-safe normalizer, branch-local read-only collector packet generation for route smokes plus local_test review-loop, P6 session/project/work-unit rollup, P7 preference/artifact memory evidence, P8 permission-sensitive audit evidence, P8 bounded execution protected-output postcheck validation, and P9 startup/read-path evidence, one-step readiness evaluator for current-session shadow evidence packets, P6 session/project/work-unit rollup packet validation, P7 preference/artifact memory packet validation, P8 permission-sensitive audit packet validation, and P9 startup/read-path packet validation. This is merge/source evidence only, not deploy or live runtime evidence.
- PR #103은 기존 sanitized shadow evidence normalizer/evaluator에 operator-facing post-deploy capture aliases를 추가했습니다. PR #105는 post-deploy capture alias metadata를 기록하되 이를 production readiness로 취급하지 않는 product evidence gates를 추가했습니다. PR #107은 P6/P7/P9 product evidence checks가 live gaps를 plain `PASS`가 아니라 `PASS_WITH_GAPS`로 보존하도록 바꾸었습니다. PR #109는 post-#107 configured read-path fallback gap을 기록했고, PR #111은 live authority overlay schema gap에서 `brain_objects_query`가 fail-open object pack을 반환하도록 고쳤습니다. PR #113은 post-#111/#11 live route proof를 문서화했고, PR #115는 approved production schema repair surface를 추가했습니다.
- PR #142 added deployed MCP post-deploy agent-context capture support and merged at source `40261ef132e5`; neurons-ops PR #19 updated production desired state at `a35f53c`, and Jenkins production deploy button #16 reported precheck/sync/postcheck `PASS`. The live capture is read-only evidence for tools/routes/context-hints/deployed identity, not complete P6-P9 production readiness.
- PR #148 added the tools-image post-deploy capture dependency path and later ops rollout proved the tools container can collect sanitized MCP capture directly; this closes the tools-image capture dependency gap but not product-wide readiness.
- PR #152 added the post-deploy projection-join collector path and merged at source `36f0a756e31f` / merge `a6e2249381ab`; neurons-ops PR #27 updated production desired state at `3d13f780a981`, and Jenkins production deploy button #19 reported precheck/sync/postcheck `PASS`. The live capture is read-only evidence for P3 graph/Qdrant projection-join edge-count proof with no production mutation.
- Current activation-progress aggregation work consumes the post-#152 projection capture, P4 replacement-current capture, and current P6 live rollup capture as supplied live evidence, removing the P3 projection-join blocker, P4 replacement-current blocker, and P6 live rollup blocker from phase progress and goal blockers while preserving `PASS_WITH_GAPS` for P7-P9 runtime evidence.
- PR #162는 runtime-marked preference evidence consumer를 도입하고 당시 비어 있던 deployed preference lane을 의도적으로 gap으로 남겼습니다. 이후 PR #187/#189/#191/#193은 bounded `ArtifactPreference` authority, canonical materialization, 이름이 지정된 artifact consumer receipt, PostgreSQL read compatibility를 추가했습니다. Post-#200 deployed capture는 이제 비어 있지 않은 accepted-current preference evidence를 제공하지만 persisted capability attestation은 visible gap으로 남습니다.
- PR #164 and PR #166 consume future supplied live runtime-authority packets only through separated `deployed_identity`, `permission_sensitive_audit`, and `evidence_provenance` claims. Identity-only evidence keeps the permission-audit gap, local replay keeps the live-evidence gap, protected-value audit evidence fails closed, and collector-side no-deploy identity evidence stays a gap unless explicit mismatch is proven. This is P8 evidence-consumption preparation, not deployed P8 production validation.
- PR #168/#170은 degraded P9 gap을 actionable하게 만들고 supplied-live evidence consumption을 추가했습니다. PR #176/#178/#182/#184는 current-authority, startup-load, style-preference lane, diagnostic gate를 추가했습니다. PR #195는 bounded startup adapter를 추가했고, PR #197은 route/receipt binding을 수정했으며, PR #199/#200은 capability-only replay classification을 바로잡았습니다. Post-#200 live collector는 Codex-oriented startup product와 receipt를 validate하지만 Claude Code/Gemini/Hermes startup, runtime interception, 실제 Codex host startup hook, deployed consumer action enforcement은 증명하지 않습니다.
- PR #174 adds a P8 source-side GitOps desired-state evidence field and claim. A sanitized ops manifest/source-tag summary can validate `gitops_desired_state` without closing `live.deployed_identity`, while explicit desired-state mismatch fails closed. This is still source-side evidence modeling, not deploy or live runtime proof.
- PR #204는 P8 GitOps/live evidence binding을 fail-closed로 구현했습니다. Immutable expected source commit, `gitops_desired_state`, `argo_reconciliation`, desired/live image-set identity, `deployed_identity`를 독립 claim으로 검증하고 `deployment_evidence_binding.v1` consistency hash로 같은 packet 안에 연결합니다. Mutable commit ref, mismatch, malformed/unknown/protected field, stale live ref, mutation evidence는 실패하며 missing evidence만 visible gap으로 남습니다.
- Current delivery layer는 분리되어 있습니다. Neurons PR #204 merge `73d440522e49`는 source delivery이고 GitHub checks와 local regression은 source verification입니다. Canonical component build는 source-tagged images를 생성했고, 최초 canary GitOps update 실패와 GitOps-only recovery 성공은 별도 build/update evidence입니다. Neurons-ops PR #44 merge `4270ab24cc55`는 desired-state delivery이고 Jenkins #30/#31은 production precheck/deploy execution입니다. Argo exact revision/health, desired/live image set, deployed source identity, route smoke, denial/no-mutation postcheck는 live runtime evidence입니다. Same-packet binding은 이 독립 evidence의 일치성을 검증하지만 어느 layer도 다른 layer를 대체하지 않습니다.
- Final head and merge SHAs below are GitHub delivery evidence only. They are not deploy, live runtime, or production readiness evidence.
- P1 through P10 phase branches were cleaned up or are eligible for cleanup after merge verification.
- 이 delivery record는 위에서 설명한 bounded P1-P6 gate를 닫고 current-source P7-P9 live evidence를 추가합니다. P7 same-process product evidence는 통과하지만 persisted capability attestation이 의도적으로 없고 최상위 phase aggregation에 generic live-preference gap 2개가 남아 있으므로 phase accounting은 `PASS_WITH_GAPS`입니다. P8의 in-packet GitOps/live binding은 post-#204 proof로 닫혔지만 permission-sensitive audit-store proof가 아직 없습니다. P9 live Codex-oriented startup evidence도 부분적이며 다른 consumer receipt, runtime interception, actual host startup, enforcement proof가 아직 없습니다. 따라서 product-wide status는 `PASS`가 아니라 `PASS_WITH_GAPS`입니다.
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
| P3 Live Projection Join Evidence | #152 | `codex/lbrain-p3-live-projection-join` | `36f0a75` | `a6e2249` | `main` |
| P7 Live Preference Evidence Consumption | #162 | `codex/lbrain-p7-live-preference-memory-current` | `6018524` | `bec7b38` | `main` |
| P8 Runtime Authority Evidence Consumption | #164 | `codex/lbrain-p8-runtime-authority-current` | `a96414a` | `b5eef6b` | `main` |
| P8 Runtime Authority Review Follow-up | #166 | `codex/lbrain-p8-runtime-authority-review-fix` | `39a5b0f` | `82c26e7` | `main` |
| P9 Degraded Agent Context Actionability | #168 | `codex/lbrain-p9-agent-context-current` | `fd1aff9` | `cc293b8` | `main` |
| P9 Live Agent Context Evidence Consumption | #170 | `codex/lbrain-p9-live-evidence-current` | `6931db4` | `e290495` | `main` |
| P8 GitOps Desired-State Evidence Separation | #174 | `codex/lbrain-p8-gitops-desired-state` | `064fc78` | `07c00cd` | `main` |
| P9 Current Authority Product Gate | #176 | `codex/lbrain-p9-current-authority-gate` | `254b520` | `e2815c6` | `main` |
| P9 Startup Current Authority Load Gate | #178 | `codex/lbrain-p9-startup-current-authority-gate` | `6d2ee15` | `a3634d0` | `main` |
| P7 ArtifactPreference Production Gate | #187 | `codex/lbrain-p7-artifact-preference-production-gate` | `35c86d2` | `e87d5b1` | `main` |
| P7 Canonical Preference Materialization | #189 | `codex/lbrain-p7-artifact-preference-materialization` | `1bce039` | `bb86ba5` | `main` |
| P7 Artifact Preference Consumer Receipt | #191 | `codex/lbrain-p7-artifact-preference-consumer` | `5ec2f44` | `3d28bda` | `main` |
| P7 PostgreSQL Live Read Compatibility | #193 | `codex/lbrain-p7-live-proof` | `6712451` | `5a7c36e` | `main` |
| P9 Bounded Agent Context Startup | #195 | `codex/lbrain-p9-agent-context-startup` | `6f5e15a` | `42b264c` | `main` |
| P9 Live Route And Receipt Binding | #197 | `codex/lbrain-p9-live-binding-fix` | `c94c4d2` | `d79fefa` | `main` |
| Live Evidence Gap Classification | #199 | `codex/lbrain-live-evidence-gap-classification` | `3786feb` | `8bbe093` | `main` |
| Replay Classification Review Follow-up | #200 | `codex/lbrain-live-evidence-gap-classification-review-followup` | `e6cdc27` | `98065d2` | `main` |
| P8 GitOps/Live Evidence Binding | #204 | `codex/p8-gitops-evidence-binding` | `2dc4e12` | `73d4405` | `main` |

PR creation gate:

- Required: linked issue number or explicit user approval for GitHub PR mutation.
- Required PR body constraint: include a real closing reference such as `Closes #N`.
- If no linked issue exists, prepare PR body previews but do not create PRs.
- Do not claim merge, CI, deploy, or live runtime evidence from branch push alone.

## Next Design Targets

Integrated P2-P9 implementation loop은 final live-evidence checkpoint에 도달했습니다. 다음 loop는 완료된 phase를 다시 열거나 full P10을 앞당기지 않고 남은 production gap만 닫아야 합니다.

```text
P7 persisted attestation and phase-progress aggregation reconciliation
→ P8 permission-sensitive audit-store collection
→ P9 actual Codex host startup-hook proof
→ P9 Claude Code/Gemini/Hermes startup receipts
→ P9 runtime action interception and consumer-policy enforcement
```

Configured/deployed LBrain MCP object-native route smoke, P7 receipt, gate-less denial behavior는 이제 live-proven입니다. Production authority write는 계속 preapproved이지만 read-path evidence를 닫기 위해 추가 write가 필요한 것은 아닙니다. 향후 write는 반드시 approval board, audit trail, rollback/supersession path, scoped promotion gate, redaction, postcheck flow를 통해서만 실행해야 합니다.

Recommended goal:

```text
Fail-closed authority policy를 유지하고 full P10 object browser는 deferred로 둔 채, 남은 P7/P8/P9 production evidence gap을 닫습니다.
```

Expected outputs:

- deployed permission-sensitive audit event와 audit-store recording evidence
- capability reissuance나 self-attestation 없이 수행하는 P7 product-evidence와 phase-progress gap reconciliation
- 실제 Codex host startup-hook receipt와 runtime interception evidence
- 분리된 Claude Code, Gemini, Hermes startup receipt
- deployed consumer action-surface enforcement proof
- merge, source CI, canonical image build, ops desired state, deploy execution, live runtime evidence의 분리 유지
- 별도로 bounded되고 완전히 gated된 authority action이 필요한 경우를 제외한 production mutation 없음
- no protected content, credentials, topology, or raw external ID output
- clear PASS / PASS_WITH_GAPS / FAIL result with live gaps preserved

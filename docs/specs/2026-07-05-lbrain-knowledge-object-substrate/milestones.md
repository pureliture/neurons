# Milestones — LBrain Knowledge Object Substrate

## M0 Spec lock and baseline eval
- status: done
- orchestration: normal, shared contract risk, single executor plus architecture research, code-change, failing-first tests planned, SoT change false
- evidence: `uv run neuron-knowledge golden-query-eval --baseline` returned `status=baseline_red` for 10 golden queries; `git diff --check` passed.

## M1 Core object substrate
- status: done
- orchestration: normal, shared model risk, single executor then review, code-change, failing-first tests planned, SoT change false
- evidence: `uv run pytest -q tests/test_knowledge_objects.py -q` passed as part of focused and full worker suites. `AuthorityDecision` and `ReviewProposal` serialization now distinguish preview from actual writes.

## M2 Reference corpus ingest and status
- status: done
- evidence: `uv run pytest -q tests/test_reference_corpus.py tests/test_neuron_cli.py -q` passed in focused/full suites. `corpus-ingest local_test` reports planned/no mutation until a store is configured; production target denies with no mutation/network.

## M3 Documentation cleanup object pack
- status: done
- evidence: `uv run pytest -q tests/test_object_packs.py tests/test_document_authority_read_paths.py -q` passed in focused/full suites. Documentation cleanup pack separates lanes, actions, evidence views, gaps, and confidence.

## M4 Cross-use-case object packs
- status: done
- evidence: runtime truth pack tests keep merge and deployment evidence separate and require typed `runtime_verified` evidence before verified claims. Agent context packs include documentation, reference corpus, preferences, style, current work, required verification, and guardrail sections.

## M5 MCP context and agent pack integration
- status: done
- evidence: `uv run pytest -q tests/test_neuron_mcp_stdio.py tests/test_context_authority_pack.py tests/test_context_pack_builder.py -q` passed in focused/full suites. MCP schemas expose object tools, `gemini` consumer, local/test ledger proposal write, production denial, manifest-ref gap, and restricted decision denial.

## M6 Golden query evaluator and OKF export
- status: done
- evidence: `uv run pytest -q tests/test_golden_query_eval.py tests/test_okf_export.py -q` passed in focused/full suites. CLI smoke for `golden-query-eval --baseline` and `okf-export --root okf` returned expected JSON.

## M7 Integration review and production-validation handoff
- status: done
- evidence: `uv run pytest -q` passed with `1406 passed, 9 skipped, 1 warning`; `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test` passed; `git diff --check` passed. `code_simplifier` and `codebase_architecture_manager` reviews were addressed, including false write-claim fixes and degraded object-store status wording.

## M8 Temporal recall corrective activation
- status: in progress
- contract: `agentic-execution`; requirements and design remain `requirements.md` and `design.md` in this specification. The active observable slice is one selected legacy source identity producing one bounded dry-run plan without a CouchDB write.
- observed blocker and amendment: the live dry-run applied `target_document_limit=1` to the provider/project-wide snapshot before locating the selected legacy target. The corrective path first derives bounded exact identities from one staged Codex source, then queries only matching empty-locator legacy records; missing, multiple, malformed, over-limit, or timed-out evidence fails closed. Nested-provider layouts remain explicitly unsupported by this one-entry operation rather than being partially scanned.
- source evidence: focused historical repair tests passed (`88 passed`); `cd worker && uv run pytest -q` passed (`3304 passed, 12 skipped`); the Java `gradle test` gate passed with an isolated temporary Gradle cache.
- next gate: merge and deploy the source image, merge the matching ops receipt contract, reapply the canonical Jenkins configuration, then run only the bounded `PRECHECK_ONLY` dry-run. CouchDB execute remains blocked until fresh plan, snapshot/restore, approval, and postcheck evidence are present.

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

## M9 근거 기반 사실 교정
- 공통 resolution 보완: v2 batch, Git 직접 검증, 관측/assertion/head 분리, 관리 대상 autopilot 합류, 동일 transaction의 queue 복구, fact view/최소 generation을 구현했다. [2026-10-26 부분 검증](../../validation/2026-10-26-lbrain-common-fact-resolution.md)을 추가했다. 일반 의미 경로·모든 writer·실제 graph 수렴은 남아 있다.
- 자동 재검토 부분 증거: PG 예약·lease·판단 cache·독립 감시 코어를 추가하고 [Ubuntu 격리 runtime 실증](../../validation/2026-10-25-lbrain-recheck-runtime.md)을 완료했다. 프로세스 종료·DB 복구·지연된 구조화 근거 수집·중복 없는 교정과 만료 cache 갱신을 확인했다. 실제 의미 판단 모델·정규 intake 연결·B 읽기 정책은 미완료다.
- 상태: 전체 구현 진행 중. 초기 adapter의 좁은 Ubuntu canary는 PASS했으나 전체 기능의 완료 판정을 철회한다. LLM 변경안, 수집 전 근거, 지연 세션과의 합류 및 추가 결함은 [합류 설계 감사](../../validation/2026-10-25-lbrain-correction-reconciliation-audit.md)를 따른다. main 반영·운영 활성화는 미수행이다.
- 전체 달성 추적: [G01~G10 및 C01~C36](correction-acceptance.md). 표시 범위·필드 검증·native 관측 anchor·Git 검증·typed 모델 연결의 [첫 보완](../../validation/2026-10-25-lbrain-correction-foundation.md)을 기록했다. 공통 resolution과 정규 전체 경로는 아직 미완료다.
- 근거 스펙: `requirements.md` FR13, `design.md` `brain_object_correct`.
- 목표: agent가 작업 중 authoritative evidence로 accepted/current 사실의 불일치를 확인하면, 적격한 교정을 건별 사람 승인 없이 반영한다.
- 적격 경로: source 정책, 정확한 target/project/repository, expected revision/hash, freshness·integrity·field 대응을 검증한다. 멱등 successor, `correct_fact` 감사, `supersedes`를 원자 기록하고 재조회한다.
- 실패 경로: stale·불완전·malformed·충돌·비권위·cross-scope·CAS 실패는 authority 변경 없이 typed review/conflict를 반환한다.
- privacy: evidence refs/hash만 결속하며 raw source body, secret, private path, raw external ID를 응답·감사에 넣지 않는다.
- 구현 gate: sanitized fixture와 격리 DB로 먼저 검증한다. production 활성화는 별도 gate이며, 활성화한 대상의 적격 교정에는 건별 승인을 요구하지 않는다.
- 코드 완료 증거: HTTP/stdio schema·dispatch, source/field 정책, successor·감사·supersession의 단일 authority 트랜잭션, idempotency·CAS·rollback 테스트, worker/Java 전체 검사와 `git diff --check`.
- 접근 정책: 전용 correction identity와 source/field allowlist를 사용한다. admin identity를 agent에게 부여하거나 교정마다 `approved=true`를 요구하지 않는다. service 정책이 없으면 비활성이다.
- 전체 goal 완료 증거: source commit → image/build → Ubuntu `llm-brain-server` deployed MCP identity를 결속한다. `tools/list`를 확인하고, 실제 LLM이 synthetic object 하나에서 조회 → source 확인 → 교정 도구 호출 → 동일 route 재조회를 수행한 receipt를 남긴다.
- live 부정 경로: stale·unverifiable·conflicting·cross-project·CAS 실패마다 canonical before/after 무변경과 denial audit를 입증한다. 보호된 내용은 노출하지 않는다.
- 정리: append-only audit를 보존하고 synthetic object를 non-current로 전환한 뒤 재조회한다. non-synthetic project/object 무변경도 확인한다.
- production-validation은 릴리스 절차상 별도 gate여도 같은 사용자 goal에 포함된다. 로컬 test 또는 배포 성공만으로 전체 goal을 닫지 않는다.
- 현재 source 검사: worker `5200 passed, 607 skipped`(환경 선택형 검사 포함), PostgreSQL 교정 test 파일 `32 passed`, 기존 PG steward 회귀를 포함한 별도 검사 `41 passed`, 최신 main freshness 전용 DB 검사 `8 passed`, Java `gradle test` 통과.
- 배포 증거: source `f552bf5`의 설치된 wheel/schema와 이미지 일치를 검증했다. 실제 root LLM의 HTTP 조회→source 확인→교정→동일 route 재조회, stdio CLI 재시도, 5개 부정 경로 canonical 무변경, 감사 보존 및 synthetic 정리를 통과했다. graph backend 소비와 운영 서비스 활성화는 이 결과에 포함하지 않는다.

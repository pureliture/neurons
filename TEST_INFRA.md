# LBrain Architecture Rationalization: Test Architecture & Infrastructure Specification (TEST_INFRA.md)

- **Target Worktree**: `/Users/ddalkak/Projects/neurons/.worktrees/lbrain-architecture-rationalization-spec`
- **Specification Version**: v2.2 (`requirements.md`, `design.md`, `review.md`)
- **Methodology**: 4-Tier Requirement-Driven Test Architecture
- **Test Suite Location**: `worker/tests/e2e/`

---

## 1. Overview & Test Architecture

This document defines the complete end-to-end verification infrastructure for the LBrain Architecture Rationalization (Phases 1 & 2). The test architecture is structured into 4 distinct verification tiers ensuring total requirements coverage, boundary resilience, cross-feature coherence, and real-world operational fidelity.

```
+-------------------------------------------------------------------------+
|                  Tier 4: Real-World Application Scenarios               |
|   (5 Multi-Step End-to-End Operational & Developer Workflow Scenarios)   |
+-------------------------------------------------------------------------+
                                    ^
+-------------------------------------------------------------------------+
|                Tier 3: Cross-Feature Combinations (Pairwise)            |
|   (Interactions between MCP, Serializer, Postgres, Outbox, DAG, Bench)  |
+-------------------------------------------------------------------------+
                                    ^
+-------------------------------------------------------------------------+
|               Tier 2: Boundary & Corner Cases (>=5 / feature)           |
|   (60 Test Cases: Empty, Corrupted, Overflow, Races, Cycles, GUC Flaws)  |
+-------------------------------------------------------------------------+
                                    ^
+-------------------------------------------------------------------------+
|                 Tier 1: Feature Coverage (>=5 / feature)                |
|   (60 Test Cases: Core Happy Paths & Contract Asserts for Features 1-12)|
+-------------------------------------------------------------------------+
```

---

## 2. Feature Inventory (12 Features)

| Feature ID | Feature Name | Description | Target Milestone |
|---|---|---|---|
| **F1** | `brain.resolve` Tool | Unified public read tool with modes `context`, `query`, `list` and response modes `slim`, `with_evidence` | M1 |
| **F2** | `memory_candidate_create` Tool | Proposal-only write tool strictly enforcing `candidate/disabled` lifecycle state | M1 |
| **F3** | `agent_memory_admin` Isolation | Admin commit/audit tool isolation with `lbrain_admin` identity, rejecting public calls fail-closed | M1 |
| **F4** | Tiered Slim Serializer (`slim`) | Default serializer returning core decisions/preferences <= 1.2 KB (soft token budget 250~500 tokens) | M2 |
| **F5** | Tiered Slim Serializer (`with_evidence`) | Opt-in serializer returning SHA-256 hash chains, DAG edges, and source refs | M2 |
| **F6** | Schema Rationalization | Elimination of 7 empty lane arrays, `route_spec` dumps, and 3-way duplicate object keys | M2 |
| **F7** | PostgreSQL DDL & pgvector Schema | DDL for `memory_cards`, `memory_edges`, `session_memory_chunks`, `embedding_outbox` on PG17+ | M3 |
| **F8** | GUC `relaxed_order` | `SET LOCAL hnsw.iterative_scan = 'relaxed_order';` support for accelerated filtered vector search | M3 |
| **F9** | Outbox Worker Concurrency & CAS | `FOR UPDATE SKIP LOCKED` lease claiming and Compare-And-Swap write-back (`WHERE content_hash = :enqueued_hash`) | M3 |
| **F10** | Recursive DAG Traversal CTE | Cycle-safe recursive CTE query traversing `memory_edges` with `depth < 5` and visited path array | M3 |
| **F11** | Qdrant -> PostgreSQL Backfill | One-shot migration script backfilling existing session chunks and cards into PostgreSQL | M4 |
| **F12** | Dual-Read Shadow Benchmark | Dual-read verification harness comparing Qdrant vs pgvector hybrid search (`Recall@5 >= 0.95`, `P95 <= 20ms`) | M4 |

---

## 3. Test Suite Mapping & Detailed Test Matrix

### 3.1. Tier 1: Feature Coverage Matrix (60 Test Cases)

| Test ID | Feature | Test Case Name | Objective & Assertion |
|---|---|---|---|
| `test_f1_01` | F1 | `test_brain_resolve_context_mode_default` | Default `mode="context"` returns slim envelope with active decisions, preferences, and guardrails. |
| `test_f1_02` | F1 | `test_brain_resolve_query_mode_keyword` | `mode="query"` performs keyword/semantic matching and scopes results strictly to project. |
| `test_f1_03` | F1 | `test_brain_resolve_list_mode` | `mode="list"` lists stored decisions and preferences in deterministic order. |
| `test_f1_04` | F1 | `test_brain_resolve_with_evidence_mode` | `response_mode="with_evidence"` returns hash chains and DAG edge structures. |
| `test_f1_05` | F1 | `test_brain_resolve_as_of_temporal_point` | `as_of` timestamp performs point-in-time recall filtering records where `valid_from <= as_of <= valid_to`. |
| `test_f2_01` | F2 | `test_candidate_create_decision` | Creates decision candidate; asserts `lifecycle_state="candidate"` and `authorization_status="disabled"`. |
| `test_f2_02` | F2 | `test_candidate_create_preference` | Creates preference candidate with project scope and returns valid generated `memory_id`. |
| `test_f2_03` | F2 | `test_candidate_create_task` | Creates task candidate with content_hash validation and disabled authorization. |
| `test_f2_04` | F2 | `test_candidate_create_evidence_with_source_ref` | Creates evidence candidate preserving source_ref structure without leaking raw transcript. |
| `test_f2_05` | F2 | `test_candidate_create_proposer_tag` | Proposer tag (e.g. `gemini`, `codex`, `claude-code`) accurately recorded in proposal. |
| `test_f3_01` | F3 | `test_admin_tools_hidden_from_public_list` | Public `tools/list` returns exactly 2 tools (`brain.resolve`, `memory_candidate_create`). |
| `test_f3_02` | F3 | `test_admin_approve_rejected_from_public_agent` | Invoking `memory_candidate_approve` via public endpoint returns fail-closed error `-32601`. |
| `test_f3_03` | F3 | `test_admin_supersede_rejected_from_public_agent` | Invoking `memory_supersede_commit` via public endpoint returns fail-closed error `-32601`. |
| `test_f3_04` | F3 | `test_admin_audit_probe_rejected_from_public_agent` | Invoking `brain_permission_sensitive_audit_probe` via public endpoint rejected fail-closed. |
| `test_f3_05` | F3 | `test_admin_endpoint_with_lbrain_admin_identity` | Admin endpoint with `lbrain_admin` identity token successfully approves candidate to `active`. |
| `test_f4_01` | F4 | `test_slim_serializer_size_budget` | Serialized slim payload size <= 1.2 KB on standard context query. |
| `test_f4_02` | F4 | `test_slim_serializer_essential_fields` | Slim payload contains `schema_version`, `project`, `recent_context`, `decisions`, `preferences`, `active_guardrails`, `gaps`. |
| `test_f4_03` | F4 | `test_slim_serializer_pagination_first_page` | `limit=2` pagination returns `has_more=True` and valid opaque `next_cursor`. |
| `test_f4_04` | F4 | `test_slim_serializer_pagination_second_page` | Subsequent call with `next_cursor` returns next records and `has_more=False`. |
| `test_f4_05` | F4 | `test_slim_serializer_token_budget_estimation` | Soft token budget validated (250~500 estimated tokens). |
| `test_f5_01` | F5 | `test_with_evidence_content_hash` | Includes deterministic `content_hash` matching `^sha256:`. |
| `test_f5_02` | F5 | `test_with_evidence_evidence_hashes_chain` | Includes `evidence_hashes` array preserving SHA-256 integrity chain. |
| `test_f5_03` | F5 | `test_with_evidence_dag_edges` | Includes `edges` structure containing `src_id`, `rel_type`, `dst_id`, `provenance_hash`. |
| `test_f5_04` | F5 | `test_with_evidence_source_refs` | Includes `source_ref` locator and span references. |
| `test_f5_05` | F5 | `test_with_evidence_hash_integrity_verification` | Recalculated SHA-256 matches declared `content_hash`. |
| `test_f6_01` | F6 | `test_schema_rationalization_no_empty_lanes` | Output JSON contains 0 empty lane schema arrays (`lane_1`..`lane_7`). |
| `test_f6_02` | F6 | `test_schema_rationalization_no_route_spec` | Output JSON does not dump static `route_spec` structures. |
| `test_f6_03` | F6 | `test_schema_rationalization_no_duplicate_task_keys` | Output JSON eliminates 3-way duplicate task objects. |
| `test_f6_04` | F6 | `test_schema_rationalization_key_count_reduction` | Key count reduced by >= 80% compared to legacy 68.5 KB payload. |
| `test_f6_05` | F6 | `test_schema_rationalization_conformance` | Output strictly adheres to `lbrain_slim_context.v1` JSON schema. |
| `test_f7_01` | F7 | `test_ddl_table_creation` | DDL creates `memory_cards`, `memory_edges`, `session_memory_chunks`, `embedding_outbox`. |
| `test_f7_02` | F7 | `test_ddl_vector_1536_dimensions` | Vector column configured with exact 1536 dimensions. |
| `test_f7_03` | F7 | `test_ddl_hnsw_cosine_indexes` | HNSW index uses `vector_cosine_ops` with `m=16, ef_construction=64`. |
| `test_f7_04` | F7 | `test_ddl_partial_indexes` | Partial indexes created for active cards and queued outbox jobs. |
| `test_f7_05` | F7 | `test_ddl_foreign_key_on_delete_restrict` | `memory_edges` references `memory_cards` with `ON DELETE RESTRICT`. |
| `test_f8_01` | F8 | `test_guc_relaxed_order_statement_executed` | `SET LOCAL hnsw.iterative_scan = 'relaxed_order';` executed prior to search. |
| `test_f8_02` | F8 | `test_guc_hybrid_search_with_metadata_filter` | Filtered vector search (`authorization_status='active'`) executes successfully. |
| `test_f8_03` | F8 | `test_guc_cosine_similarity_score_range` | Similarity score `1 - (embedding <=> query)` is strictly within [0.0, 1.0]. |
| `test_f8_04` | F8 | `test_guc_project_isolation_filter` | Vector query strictly filters by `project` identifier. |
| `test_f8_05` | F8 | `test_guc_transaction_scope_isolation` | GUC setting is local to current transaction and does not leak. |
| `test_f9_01` | F9 | `test_outbox_claim_skip_locked` | Worker claims queued jobs with `FOR UPDATE SKIP LOCKED` and sets `status='processing'`. |
| `test_f9_02` | F9 | `test_outbox_cas_writeback_matching_hash` | CAS update writes embedding and sets `embedding_state='ready'` when content_hash matches. |
| `test_f9_03` | F9 | `test_outbox_mark_completed` | Outbox row marked `status='completed'` upon successful CAS write. |
| `test_f9_04` | F9 | `test_outbox_cas_stale_hash_noop` | CAS update skips write when card content_hash was modified concurrently (stale write prevention). |
| `test_f9_05` | F9 | `test_outbox_lease_expiry_reclaim` | Expired lease (`lease_until < NOW()`) reclaimed by subsequent worker poll. |
| `test_f10_01` | F10 | `test_dag_cte_single_hop` | CTE traverses single-hop direct relationship. |
| `test_f10_02` | F10 | `test_dag_cte_multi_hop_provenance` | CTE traverses 3-level deep ancestry chain returning full path. |
| `test_f10_03` | F10 | `test_dag_cte_multi_parent_convergence` | Card derived from 2 parents returns both ancestry branches. |
| `test_f10_04` | F10 | `test_dag_cte_temporal_edge_filtering` | Point-in-time traversal filters edges outside `valid_from/valid_to`. |
| `test_f10_05` | F10 | `test_dag_cte_relation_types_classification` | Correctly differentiates `supersedes`, `derived_from`, `contradicts`, `supports`. |
| `test_f11_01` | F11 | `test_backfill_read_qdrant_collection` | Migration script reads vectors and payloads from source Qdrant collection. |
| `test_f11_02` | F11 | `test_backfill_map_session_chunks` | Maps Qdrant session chunks into `session_memory_chunks` preserving IDs and vectors. |
| `test_f11_03` | F11 | `test_backfill_map_memory_cards` | Maps cards into `memory_cards` table preserving content hashes and metadata. |
| `test_f11_04` | F11 | `test_backfill_dry_run_mode` | Dry-run mode outputs count and sample records without writing to Postgres. |
| `test_f11_05` | F11 | `test_backfill_outbox_enqueue_unvectorized` | Enqueues missing embeddings to `embedding_outbox` for unvectorized cards. |
| `test_f12_01` | F12 | `test_dual_read_execute_both_stores` | Dual-read harness executes search against both Qdrant and pgvector stores. |
| `test_f12_02` | F12 | `test_dual_read_top5_overlap_calculation` | Calculates top-5 rank overlap and Recall@5 metric between stores. |
| `test_f12_03` | F12 | `test_dual_read_recall_at_5_threshold` | Asserts `Recall@5 >= 0.95` across authority query fixtures corpus. |
| `test_f12_04` | F12 | `test_dual_read_latency_percentiles` | Measures P50, P95, P99 latency for both backends; asserts P95 <= 20ms. |
| `test_f12_05` | F12 | `test_dual_read_discrepancy_reporting` | Generates detailed report on rank shifts and score divergences. |

---

### 3.2. Tier 2: Boundary & Corner Cases Matrix (60 Test Cases)

| Test ID | Feature | Test Case Name | Boundary / Edge Condition |
|---|---|---|---|
| `test_f1_b01` | F1 | `test_brain_resolve_empty_query_context` | Empty query with `mode="context"` safely returns project base context. |
| `test_f1_b02` | F1 | `test_brain_resolve_nonexistent_project` | Unknown project returns clean empty envelope without raising unhandled exception. |
| `test_f1_b03` | F1 | `test_brain_resolve_invalid_mode_error` | Invalid mode string rejected with standard JSON-RPC `-32602` error. |
| `test_f1_b04` | F1 | `test_brain_resolve_limit_clamping` | Negative limit or limit > 100 clamped/validated safely. |
| `test_f1_b05` | F1 | `test_brain_resolve_malformed_as_of_iso` | Malformed `as_of` string returns validation error. |
| `test_f2_b01` | F2 | `test_candidate_create_override_active_ignored` | Argument attempting `authorization_status="active"` is overridden to `"disabled"`. |
| `test_f2_b02` | F2 | `test_candidate_create_override_accepted_ignored` | Argument attempting `lifecycle_state="human_accepted"` overridden to `"candidate"`. |
| `test_f2_b03` | F2 | `test_candidate_create_invalid_hash_prefix` | `content_hash` missing `sha256:` prefix rejected. |
| `test_f2_b04` | F2 | `test_candidate_create_empty_title_summary` | Empty string title or summary rejected with validation error. |
| `test_f2_b05` | F2 | `test_candidate_create_unknown_card_type` | Invalid `card_type` not in enum rejected fail-closed. |
| `test_f3_b01` | F3 | `test_admin_missing_auth_header` | Request without authorization token rejected with 401/fail-closed. |
| `test_f3_b02` | F3 | `test_admin_spoofed_agent_identity` | Request with standard `agent_user` token rejected with 403 Forbidden. |
| `test_f3_b03` | F3 | `test_admin_wildcard_method_injection` | Malicious method names (`*`, `../admin`) rejected with `-32601`. |
| `test_f3_b04` | F3 | `test_admin_approve_nonexistent_card` | Approving non-existent `memory_id` returns 404 / entity not found error. |
| `test_f3_b05` | F3 | `test_admin_reject_already_superseded` | Rejecting already superseded card handled idempotently. |
| `test_f4_b01` | F4 | `test_slim_serializer_empty_project_size` | Empty project returns valid slim JSON envelope <= 300 bytes. |
| `test_f4_b02` | F4 | `test_slim_serializer_hard_limit_enforcement` | Extremely large card collections deterministically truncated at hard limit 3 KB. |
| `test_f4_b03` | F4 | `test_slim_serializer_unicode_control_chars` | Special characters, emojis, and newlines serialized without escaping corruption. |
| `test_f4_b04` | F4 | `test_slim_serializer_malformed_cursor` | Invalid base64 or corrupted `next_cursor` handled gracefully with reset. |
| `test_f4_b05` | F4 | `test_slim_serializer_limit_one_boundary` | `limit=1` pagination boundary executes correctly. |
| `test_f5_b01` | F5 | `test_with_evidence_empty_evidence_hashes` | Empty evidence hashes serialized as `[]`, not omitted or null. |
| `test_f5_b02` | F5 | `test_with_evidence_no_raw_transcript_leak` | Verifies no raw private transcript/body appears in `source_ref`. |
| `test_f5_b03` | F5 | `test_with_evidence_deeply_nested_payload` | Deeply nested `typed_payload` serialized intact. |
| `test_f5_b04` | F5 | `test_with_evidence_broken_provenance_flag` | Flagged broken provenance recorded without crashing serializer. |
| `test_f5_b05` | F5 | `test_with_evidence_deduplicate_edges` | Multiple identical edges deduplicated before wire transmission. |
| `test_f6_b01` | F6 | `test_rationalize_legacy_lane_pruning` | Input with legacy 7 empty lanes completely stripped. |
| `test_f6_b02` | F6 | `test_rationalize_route_spec_stripping` | Legacy `route_spec` dumps stripped before serialization. |
| `test_f6_b03` | F6 | `test_rationalize_duplicate_key_resolution` | Conflicting duplicate task keys resolved to single canonical representation. |
| `test_f6_b04` | F6 | `test_rationalize_null_tree_omission` | Null object trees omitted rather than dumped recursively. |
| `test_f6_b05` | F6 | `test_rationalize_zero_semantic_loss` | Semantic roundtrip verifies zero information loss of essential knowledge. |
| `test_f7_b01` | F7 | `test_ddl_wrong_vector_dimensions` | Attempting to insert 768-dim vector into 1536-dim column rejected by DB. |
| `test_f7_b02` | F7 | `test_ddl_temporal_inversion_check` | Inserting `valid_to < valid_from` fails check constraint. |
| `test_f7_b03` | F7 | `test_ddl_outbox_duplicate_index_conflict` | Re-enqueuing active `(target_id, content_hash)` fails unique index constraint. |
| `test_f7_b04` | F7 | `test_ddl_restrict_active_edge_deletion` | Deleting `memory_cards` row with active `memory_edges` blocked by FK RESTRICT. |
| `test_f7_b05` | F7 | `test_ddl_invalid_enum_state_values` | Invalid `lifecycle_state` string rejected by enum/check constraint. |
| `test_f8_b01` | F8 | `test_guc_zero_matches_empty_result` | Zero matches under `relaxed_order` returns empty list without error. |
| `test_f8_b02` | F8 | `test_guc_zero_vector_similarity` | Zero magnitude vector query handled safely. |
| `test_f8_b03` | F8 | `test_guc_fallback_unsupported_pgvector` | Graceful fallback when pgvector < 0.8.0 without crashing application. |
| `test_f8_b04` | F8 | `test_guc_high_ef_search_boundary` | High `ef_search` setting executes within memory bounds. |
| `test_f8_b05` | F8 | `test_guc_concurrent_session_isolation` | Concurrent sessions modifying GUC do not cross-contaminate. |
| `test_f9_b01` | F9 | `test_outbox_concurrent_workers_no_overlap` | 3 concurrent workers polling simultaneously receive distinct non-overlapping jobs. |
| `test_f9_b02` | F9 | `test_outbox_max_retry_dead_letter` | Max retry limit reached (retry_count >= 5) -> marked `dead_letter`. |
| `test_f9_b03` | F9 | `test_outbox_empty_payload_handling` | Empty payload text handled without worker crash. |
| `test_f9_b04` | F9 | `test_outbox_worker_crash_lease_recovery` | Worker crash during processing recovered by another worker after lease timeout. |
| `test_f9_b05` | F9 | `test_outbox_rapid_cas_updates_latest_wins` | Rapid sequential updates: only latest hash succeeds CAS update. |
| `test_f10_b01` | F10 | `test_dag_cycle_prevention_direct` | Direct cycle (A -> B -> A) terminates safely without infinite loop. |
| `test_f10_b02` | F10 | `test_dag_depth_limit_enforcement` | Deep chain (> 5 levels) terminates strictly at depth = 5. |
| `test_f10_b03` | F10 | `test_dag_isolated_node_no_edges` | Isolated node with no edges returns depth 1 without crashing. |
| `test_f10_b04` | F10 | `test_dag_self_referential_edge` | Self-loop edge (A -> A) safely detected and skipped. |
| `test_f10_b05` | F10 | `test_dag_diamond_dependency_convergence` | Diamond graph (A -> B -> D, A -> C -> D) handles convergent nodes cleanly. |
| `test_f11_b01` | F11 | `test_backfill_empty_qdrant_collection` | Migrating empty Qdrant collection succeeds with 0 records copied. |
| `test_f11_b02` | F11 | `test_backfill_missing_optional_payload_fields` | Payload with missing optional fields defaults cleanly in Postgres. |
| `test_f11_b03` | F11 | `test_backfill_idempotent_rerun` | Re-running migration on same dataset is idempotent (no duplicate rows). |
| `test_f11_b04` | F11 | `test_backfill_corrupted_vector_quarantine` | Corrupted vector (NaN/wrong dim) routed to dead letter without failing batch. |
| `test_f11_b05` | F11 | `test_backfill_network_retry_resumption` | Network failure during migration triggers retry and resumes from checkpoint. |
| `test_f12_b01` | F12 | `test_dual_read_single_store_timeout_resilience`| Qdrant timeout does not crash dual-read harness (records error). |
| `test_f12_b02` | F12 | `test_dual_read_zero_match_recall` | Query returning zero matches in both stores yields Recall@5 = 1.0. |
| `test_f12_b03` | F12 | `test_dual_read_score_tie_breaking` | Equal similarity scores resolved with deterministic secondary sort (memory_id). |
| `test_f12_b04` | F12 | `test_dual_read_large_corpus_memory_bound` | Benchmark on 200+ queries executes in bounded memory. |
| `test_f12_b05` | F12 | `test_dual_read_strict_gate_failure` | Hard assertion fails cleanly when Recall@5 drops below 0.95 threshold. |

---

### 3.3. Tier 3: Cross-Feature Combinations (Pairwise & Multi-Way)

| Test ID | Features Combined | Description |
|---|---|---|
| `test_tier3_01` | F1 + F4 + F6 | Public `brain.resolve(slim)` query produces rationalized payload <= 1.2 KB with zero empty lanes. |
| `test_tier3_02` | F1 + F5 + F10 | `brain.resolve(with_evidence)` triggers recursive DAG CTE to return complete provenance hash chains. |
| `test_tier3_03` | F2 + F3 | Agent creates candidate via public tool; agent cannot approve; admin approves via `agent_memory_admin`. |
| `test_tier3_04` | F2 + F9 | Agent creates candidate -> automatically enqueued to `embedding_outbox` -> worker claims via SKIP LOCKED and writes embedding with CAS. |
| `test_tier3_05` | F7 + F8 + F1 | Pgvector DDL schema queried via `brain.resolve` using `relaxed_order` iterative scan. |
| `test_tier3_06` | F7 + F9 + F10 | Concurrent card modification during outbox embedding update preserves DAG edge consistency and CAS integrity. |
| `test_tier3_07` | F11 + F12 | Migration script backfills Qdrant data to PostgreSQL -> Dual-read benchmark verifies `Recall@5 >= 0.95`. |
| `test_tier3_08` | F1 + F2 + F3 + F7 + F8 + F9 | Full End-to-End Knowledge Lifecycle: Proposal -> Outbox Embed -> Admin Approval -> PGVector Indexing -> Slim Resolve. |

---

### 3.4. Tier 4: Real-World Application Scenarios

| Test ID | Scenario Name | Workflow Steps & Description |
|---|---|---|
| `test_tier4_scenario_1` | **Developer Feature Implementation Cycle** | 1. Agent calls `brain.resolve(slim)` to load concise decisions, preferences, and guardrails (~1.2 KB).<br>2. Agent proposes architectural decision card via `memory_candidate_create`.<br>3. Verifies proposal is created in `candidate/disabled` state.<br>4. Re-resolves context; verifies disabled candidate does NOT pollute active agent context. |
| `test_tier4_scenario_2` | **Admin Review & Supersession Workflow** | 1. Operator queries pending review queue via admin surface.<br>2. Operator approves candidate, promoting it to `active/human_accepted`.<br>3. Operator creates superseding edge in `memory_edges` replacing older decision card.<br>4. Agent calls `brain.resolve(slim)`; verifies new decision is active and superseded card is excluded. |
| `test_tier4_scenario_3` | **Outbox Concurrency & Stale Generation Protection** | 1. Multiple agents create candidates simultaneously.<br>2. 3 worker threads process outbox concurrently with `FOR UPDATE SKIP LOCKED`.<br>3. One card is modified concurrently before embedding write-back.<br>4. CAS update detects hash mismatch, skips stale embedding write, and preserves consistency. |
| `test_tier4_scenario_4` | **Provenance Audit & Temporal Investigation** | 1. Auditor queries `brain.resolve(with_evidence, as_of="2026-08-15T00:00:00Z")`.<br>2. System traverses 4-hop recursive DAG ancestry chain.<br>3. Validates point-in-time state and full SHA-256 evidence hash chain back to root decision. |
| `test_tier4_scenario_5` | **Zero-Downtime Qdrant to PGVector Cutover** | 1. Qdrant store contains active session chunks and card vectors.<br>2. Migration backfill script runs and populates PostgreSQL pgvector store.<br>3. Dual-read shadow harness executes 50 benchmark queries asserting `Recall@5 >= 0.95` and `P95 <= 20ms`.<br>4. Worker search backend switched to `postgres_pgvector` with zero downtime. |

---

## 4. Test Execution Guidelines

To run the complete E2E test suite:

```bash
cd worker
uv run pytest -q tests/e2e
```

To run individual tiers:

```bash
# Tier 1: Feature Coverage (60 tests)
uv run pytest -q tests/e2e/test_tier1_features.py

# Tier 2: Boundary & Corner Cases (60 tests)
uv run pytest -q tests/e2e/test_tier2_boundaries.py

# Tier 3: Cross-Feature Combinations (8 tests)
uv run pytest -q tests/e2e/test_tier3_combinations.py

# Tier 4: Real-World Scenarios (5 tests)
uv run pytest -q tests/e2e/test_tier4_scenarios.py
```

# Model Connectors Residual Debt

This audit/implementation note is the M6 slice for GitHub issue #40. It does not perform live Graphiti, Neo4j, Qdrant, RetiredIndexBridge, deploy, Docker, systemd, or firewall mutation.

## Finding

The model connector debt is not a full unseparated connector problem anymore. The remaining risk is residual drift between Graphiti structured response normalization and the shared OpenAI-compatible connector path, plus weak test coverage for reranker `logprobs` scoring.

## Implemented Slice

Timestamp: 2026-07-05 20:09:09 KST

- Added `worker/lib/agent_knowledge/model_connectors/structured_response.py` as the shared structured response normalization contract.
- Kept `worker/lib/agent_knowledge/llm_brain_core/graphiti_adapter.py` private compatibility helpers as wrappers, while delegating the normalization logic to `model_connectors.structured_response`.
- Updated `worker/lib/agent_knowledge/model_connectors/openai_compatible.py` to use the same shared normalizer.
- Added `worker/tests/test_model_connectors.py` coverage for:
  - shared structured response normalization
  - single-list response wrapping
  - `entity_text` / `entity_name` alias normalization
  - `episode_indices` coercion
  - `duplicate_facts` filtering against valid existing fact indexes
  - reranker `top_logprobs` scoring for `true` and inverted `false` tokens via the public `OpenAICompatibleRerankerClient.ascore()` API

## Verification

- `cd worker && uv run pytest -q tests/test_model_connectors.py -k structured_response_normalizer_contract`
- `cd worker && uv run pytest -q tests/test_graphiti_neo4j_adapter.py -k 'structured_response or existing_fact_idx_values or is_list_annotation'`
- `cd worker && uv run pytest -q tests/test_model_connectors.py tests/test_graphiti_backend.py`
- `cd worker && uv run pytest -q tests/test_model_connectors.py tests/test_graphiti_backend.py tests/test_graphiti_neo4j_adapter.py -k 'model_connectors or graphiti or structured_response or logprob or reranker or fallback'`
- `cd worker && uv run pytest -q tests/test_eval_readiness.py tests/test_eval_loop.py tests/test_eval_notify_discord.py tests/test_golden_grader.py tests/test_neuron_cli.py tests/test_model_connectors.py tests/test_graphiti_backend.py tests/test_graphiti_neo4j_adapter.py tests/test_couchdb_build_cli.py tests/test_couchdb_migration_cli.py tests/test_couchdb_migration_flow_cli.py tests/test_couchdb_shadow_cutover.py tests/test_couchdb_index_fallback.py tests/test_session_memory_backfill_planning.py`
- `uv run python scripts/test_runtime_verifiers.py`

## Residual Risk

- This is fake/no-network test evidence only. It is not a live Graphiti extraction canary.
- Reranker providers that do not honor `logprobs=True` still fall back to message content scoring; provider capability policy can be tightened in a later slice if the product wants fail-closed behavior instead.
- The shared normalizer is intentionally lower-layer and does not import `llm_brain_core` or `rag_ingress`.

## Next Candidate

Continue with MCP Server and Tools spec coupling, or return to an older active backlog item only after recording why it has higher risk/priority.

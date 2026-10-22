# RAGFlow Retirement

RAGFlow is retired from the active `neurons` runtime. `RAGFLOW_API_KEY`, `RAGFLOW_`, `--ragflow-url`, and `--token-env` are historical names and must not appear outside this retirement record.

The active runtime baseline is Qdrant plus graph-backed `neurons` services. Legacy `ragflow_*` state is migrated to backend-neutral index naming with data preservation.

### Documented exception: lowercase `ragflow` in migration and redaction paths

No uppercase `RAGFLOW_` token, `RAGFLOW_API_KEY`, `--ragflow-url` or `--token-env` appears outside this record. The lowercase `ragflow` string does remain in exactly two places, and both are sanctioned by the paragraph above:

- **Legacy column migration** — `agent_knowledge/ledger.py` copies the historical `ragflow_dataset_id` / `ragflow_document_id` / `ragflow_run` / `ragflow_memory_id` / `ragflow_disabled_at` / `ragflow_document_id_hash` columns and the `ragflow_datasets` table into their backend-neutral `index_*` equivalents. Renaming the source column names would break the migration.
- **Public-safe redaction patterns** — `knowledge_search_service.py` and `llm_brain_core/objects/{artifact_preference_evaluator,post_deploy_mcp_capture,runtime_readiness}.py` match secret-like key names such as `ragflow_dataset:` and `ragflow_document_id`. These are detection patterns for values that must never reach a public surface, not references to the retired product.

Both are lower-case, both are inert with respect to the retired service, and neither can select, configure or reach it.

## Current state

The retirement is complete on the **runtime, CLI, env and backend-option** surfaces. RAGFlow is no longer selectable, configurable, or reachable from a default-enabled path.

| Surface | State |
| --- | --- |
| Delivery backend | `INGRESS_DELIVERY_BACKEND` accepts `couchdb` only. The `retired_index_bridge` option is gone from the live worker (`rag_ingress/shadow_worker.py`) and from the drain CLI (`rag_ingress/state_cli.py`). |
| Deploy credentials | No `RETIRED_INDEX_BRIDGE_*` key exists in `compose.yaml`, `.env.example` or `worker/deploy/session-memory/compose.yaml`. |
| Java ingress | The `adapter.ext.retired_index_bridge` package (gateway, target adapter, pressure policy, status mapper, delivery cache) is deleted. |
| Python adapter | The RAGFlow adapter that implemented the `IndexAdapter` protocol is deleted. The protocol itself survives, backend-neutral, in `rag_ingress/index_adapter.py`. |
| Wire protocol | No retired-bridge endpoint shape (`/api/v1/datasets`, `/api/v1/retrieval`, `/api/v1/memories`, `/api/v1/messages`, `/api/v1/chat/completions`) remains in active source. |
| Session-memory projection | `SESSION_MEMORY_PROJECTION_BACKEND` accepts `qdrant` or `postgres_pgvector`; the RAGFlow projection branch and `couchdb_source/index_projector.py` are deleted. |
| Session-memory GC/backfill lane | The `session-memory-worker` container no longer injects a bridge credential and no longer schedules the gc/backfill lanes (`deploy/session-memory/entrypoint.sh`). |
| Recovery tooling | `couchdb_source/index_fallback.py` (a RAGFlow HTTP reader) is deleted; with no bridge there is nothing to recover from. |
| Live verification | `scripts/live-index-verify.py` is deleted. |
| Legacy operator-tool package | `agent_knowledge/legacy/index_client.py` and the operator tooling it served are deleted. The GC 3종, `transcript-backfill`, `cleanup-readiness`, the native-memory write path, the legacy builder and their `legacy/` transport are gone; only the read-only planners and the CouchDB-native builder remain. |

The regression guard for all of the above is `worker/tests/test_retired_bridge_deploy_surface_guard.py`.

## What survives, and why

One thing intentionally remains, and it is not the RAGFlow protocol:

1. **A backend-neutral adapter boundary.** `rag_ingress/index_adapter.py` defines the `IndexAdapter` protocol, `IndexStatus` and the natural-key rules. The CouchDB adapter (`rag_ingress/couchdb_index_adapter.py`) and the Qdrant adapter (`rag_ingress/qdrant_docling_mirror.py`) implement it. The previous RAGFlow implementation of the same protocol is gone.

A second, deliberately unchanged item is the **logical** target-profile contract: the `index-*` profile ids, their dataset roles, and the `BackendKind` enum value are routing labels, not a backend implementation. They stay so the public enqueue contract does not move.

A third item is the **data**: the `native_memory_mirror` and `memory_gc_audit` ledger tables and their schema stay, so existing rows are preserved. Only the Python helpers that read or wrote them through the retired bridge are gone.


## Operator follow-up (live deployment)

These changes are code-and-config only; no live mutation was performed.

The live runtime was **already bridge-free** before this change, and that was verified read-only against the running k3s cluster:

- the live ingress worker selects `INGRESS_DELIVERY_BACKEND=couchdb`;
- no `RETIRED_INDEX_BRIDGE_*` or `RAGFLOW_*` key exists in any live deployment env or secret;
- the live ingress-api runs with `SPRING_PROFILES_ACTIVE=api`, and the ingress-api **canary** with `api,retired-index-bridge` — but with no bridge credential the bridge adapter reported `isConfigured()==false`, so it already failed every delivery (`failed("retired_index_bridge delivery unavailable")`) and reported `closed("pressure_read_failed")` pressure;
- the live session-memory build lane runs with `SESSION_MEMORY_PROJECTION_BACKEND=postgres_pgvector` (the code default is now `qdrant`; neither value is the retired bridge).

What is still owed by the owner of the private `neurons-ops` overlay:

1. **Rebuild and redeploy three images.** `neurons-ingress-worker` (the `INGRESS_DELIVERY_BACKEND` default changed to `couchdb`), `neurons-session-memory-worker` (the entrypoint no longer schedules the gc/backfill lanes and no longer injects a bridge credential) and `neurons-ingress-api` (the Java adapter package is gone, so both the `api` profile and the `api,retired-index-bridge` canary now resolve `UnavailableTargetAdapter`).
2. **Check the private overlay for leftover `RETIRED_INDEX_BRIDGE_*` references.** The public repo is clean and the live secrets carry no such key, but the private overlay is not visible from this repository.
3. **The ingress-api canary's `/status` strings change** (`pressure_read_failed` → `not_configured`), because its adapter switches from the unconfigured bridge adapter to `UnavailableTargetAdapter`. `not_configured` is the reason the `api` profile is designed to report when no real target adapter is wired (`docs/specs/2026-06-30-ingress-api-profile-startup/`), so this moves the canary *onto* the documented behaviour rather than away from it. Anything asserting on those strings needs to follow.


# RAGFlow Retirement

RAGFlow is retired from the active `neurons` runtime. `RAGFLOW_API_KEY`, `RAGFLOW_`, `--ragflow-url`, and `--token-env` are historical names and must not appear outside this retirement record.

The active runtime baseline is Qdrant plus graph-backed `neurons` services. Legacy `ragflow_*` state is migrated to backend-neutral index naming with data preservation.

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

The regression guard for all of the above is `worker/tests/test_retired_index_bridge_protocol_guard.py`.

## What survives, and why

Two things intentionally remain, neither of which is the RAGFlow protocol:

1. **A backend-neutral adapter boundary.** `rag_ingress/index_adapter.py` defines the `IndexAdapter` protocol, `IndexStatus` and the natural-key rules. The CouchDB adapter (`rag_ingress/couchdb_index_adapter.py`) and the Qdrant adapter (`rag_ingress/qdrant_docling_mirror.py`) implement it. The previous RAGFlow implementation of the same protocol is gone.
2. **A legacy operator-tool package.** `agent_knowledge/legacy/index_client.py` still speaks the retired wire protocol. It is used only by operator-side archive/GC tooling that has no other backend, and it is not importable from the ingress runtime, the recall surfaces, or the deploy configuration.

A third, deliberately unchanged item is the **logical** target-profile contract: the `index-*` profile ids, their dataset roles, and the `BackendKind` enum value are routing labels, not a backend implementation. They stay so the public enqueue contract does not move.

## Operator follow-up (live deployment)

These changes are code-and-config only; no live mutation was performed. On the live host (`llm-brain-server` / `ragflow-ubuntu`, k3s) the retired bridge was already absent — no service listened on the retired default port and the k3s workload inventory lists the legacy external memory surface as removed from the target runtime. Two follow-ups are still owed by whoever owns the private `neurons-ops` overlay:

1. **Drop `INGRESS_DELIVERY_BACKEND=retired_index_bridge` from the live env-file** if it is still set. The worker now rejects any value other than `couchdb` and exits with a clear message.
2. **Drop `RETIRED_INDEX_BRIDGE_*` keys from the live env-file** if they are still set. They are now ignored; leaving them set would make the deployment look bridge-configured when it is not.

The `session-memory-worker` deployment also needs its image rebuilt: the container entrypoint no longer schedules the gc/backfill lanes, and its compose file no longer injects a bridge credential.

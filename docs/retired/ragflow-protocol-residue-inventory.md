# RAGFlow Protocol Residue Inventory

Classification of every surface that still carried the retired external index
bridge (RAGFlow) wire protocol, the credentials that fed it, or a backend
option that selected it.

Each row states the decision (**제거** removed / **중립화** renamed to a
backend-neutral name / **유지** kept with the reason) and the evidence behind
it. The authoritative record of the retirement itself is
[`ragflow-retirement.md`](ragflow-retirement.md); this file is the audit trail.

## How the decisions were reached

Three questions decided every row:

1. **Is the surface reachable from a default-enabled runtime path?** A path that
   needs an operator to switch on a profile, set a credential, or pass a flag is
   not a runtime surface.
2. **Does a backend-neutral replacement already exist?** A surface with no
   replacement is not deleted on the strength of a rename.
3. **Is the target still there?** The live runtime has no bridge credential at
   all: no `RETIRED_INDEX_BRIDGE_*` or `RAGFLOW_*` key exists in any live
   deployment or secret, the live worker selects
   `INGRESS_DELIVERY_BACKEND=couchdb`, and the k3s workload inventory lists the
   legacy external memory surface as *already removed from the target runtime*.
   The Java bridge beans existed only under a Spring profile that no repo
   manifest sets; the one live manifest that did set it (the ingress-api
   canary) left it unconfigured, so it already failed every delivery.

## Runtime / deploy / config surfaces — removed

| Surface | Decision | Evidence |
| --- | --- | --- |
| `rag_ingress/retired_index_bridge.py` — RAGFlow adapter class | 제거 | The only implementation of the `IndexAdapter` protocol that spoke the retired wire protocol. The live worker built it for the `retired_index_bridge` backend, which had no reachable target. |
| `rag_ingress/delivery_backend.py` — `RetiredIndexBridgeDeliveryBackend` | 제거 | Never constructed outside tests: `state_cli._build_live_backend` returned `None` for the `retired_index_bridge` selection, so the live drain path was already blocked. Its backend-neutral part (the payload hash gate) was kept and moved. |
| `rag_ingress/retired_index_bridge.py` → `rag_ingress/index_adapter.py` | 중립화 | The protocol (`IndexAdapter`, `IndexStatus`, `BackendDocumentHandle`, `BackendSubmitResult`, `BackendStatusDetail`) is backend-neutral and is implemented by the CouchDB and Qdrant adapters. Renamed so the module name no longer claims to be the retired bridge. |
| `rag_ingress/couchdb_retired_index_bridge.py` → `rag_ingress/couchdb_index_adapter.py` | 중립화 | The content was already a CouchDB implementation of the adapter protocol; only the name carried the retired-bridge wording. Class and factory renamed with it. |
| `rag_ingress/delivery_backend.py` → `rag_ingress/delivery_payload_gate.py` | 중립화 | Only the backend-neutral payload gate survived the removal; it is still used by the CouchDB delivery backend, the drain and the temporal rebuild tools. |
| `src/main/java/.../adapter/ext/retired_index_bridge/**` (10 files) | 제거 | Both beans were `@Profile("retired-index-bridge")`. No compose file in this repo sets that profile, and the k3s workload inventory records the Java worker as `activation: retired-profile`, `migrateToK3s: false`, `scaleCategory: not-a-target`; the compose service was already behind the `retired` compose profile. The live k3s ingress-api **canary** does set it (`SPRING_PROFILES_ACTIVE=api,retired-index-bridge`), but with no bridge credential it already failed every delivery (`isConfigured()==false` → `failed("retired_index_bridge delivery unavailable")`) and reported `closed("pressure_read_failed")` pressure — behaviorally identical to the `UnavailableTargetAdapter` that replaces it. `UnavailableTargetAdapterConfiguration` was widened to `api & !worker` so that canary keeps a `RagTargetAdapter` bean after the package is deleted. |
| `couchdb_source/index_projector.py` | 제거 | The RAGFlow session-memory projector. The live build lane projects into Postgres+pgvector (`SESSION_MEMORY_PROJECTION_BACKEND=postgres_pgvector`); the RAGFlow branch was only reachable when the env var was unset, because the code default was `retired_index_bridge`. The code default is now `qdrant`. |
| `couchdb_source/index_fallback.py` | 제거 | A RAGFlow HTTP reader used to reconstruct sessions whose originals were gone. With no bridge there is nothing to recover from. (The repo's own audit had classified it as *archive/recovery compatibility surface*; the recovery-path contract is retired here.) |
| `scripts/live-index-verify.py` | 제거 | An operator script that verified a live RAGFlow instance. |
| `worker/deploy/session-memory/gc-run.py`, `backfill.py` | 제거 | The container's gc/backfill lanes read the bridge and, for gc, issued a hard delete against it under a self-generated standing approval. A retired backend must not be the target of an automatically approved live delete. The container now runs the build lane only. |
| `compose.yaml` — `x-retired-index-bridge-env` anchor and the nine `RETIRED_INDEX_BRIDGE_*` keys | 제거 | `CLAUDE.md`: retired external index bridge credentials are not part of active runtime configuration. |
| `.env.example` — `RETIRED_INDEX_BRIDGE_*` block | 제거 | Same guardrail; the template is the deploy surface's credential contract. |
| `compose.yaml` — `INGRESS_DELIVERY_BACKEND` default `retired_index_bridge` | 제거 | Replaced with `couchdb`, the only sink the worker supports. |
| `compose.yaml` — `RETIRED_INDEX_BRIDGE_DELIVERY_ENABLED`, four `RETIRED_INDEX_BRIDGE_PRESSURE_*` keys | 제거 | They configured the deleted Java adapter. |
| `compose.yaml` — `NATURAL_KEY_BROAD_SCAN_PAGES` | 제거 | It tuned the deleted RAGFlow adapter's natural-key scan. |

## Operator tooling — kept, but quarantined

These modules exist only to operate a retired backend, so they cannot be
"repointed" to CouchDB or Qdrant without becoming different tools. They keep
working against the retired protocol, but the transport now lives in an
explicitly-legacy package and is not importable from any runtime path.

| Surface | Decision | Evidence |
| --- | --- | --- |
| `agent_knowledge/index_client.py` → `agent_knowledge/legacy/index_client.py` | 유지 (격리) | The wire-protocol transport itself. Moving it under `legacy/` makes the boundary structural; the guard test proves no runtime, ingress or recall module imports it. |
| `session_memory/session_memory_gc.py`, `transcript_session_gc.py`, `transcript_volume_gc.py`, `gc_backup.py` | 유지 | GC against the retired backend. They are operator-gated CLI tools (approval JSON + `--execute`), not runtime. |
| `session_memory/transcript_backfill.py` | 유지 | Backfill into the retired backend. Operator CLI, not routed by `COMMAND_HANDLERS`. |
| `session_memory/cleanup_readiness.py` | 유지 | Dataset census against the retired backend. Operator CLI. |
| `session_memory/neuron_session_memory.py`, `dirty_session_memory_sync.py` | 유지 | Legacy session-memory builder that reads the retired backend. The CouchDB-native builder (`couchdb-session-memory-build`) is the live path. |
| `session_memory/native_memory_writer.py`, `native_memory_reconcile.py` | 유지 | The native-memory feature is built on the retired bridge's Memory API. Removing the feature is a product decision, not a protocol cleanup. |
| `dataset_contract.py` (`CANONICAL_RETIRED_INDEX_BRIDGE_DATASET_NAMES`, `DEPRECATED_RETIRED_INDEX_BRIDGE_DATASET_PREFIXES`) | 유지 | A dataset-*naming* contract, not a protocol. Consumed by the legacy GC tool and its own test. |

## Names and contracts — kept on purpose

| Surface | Decision | Evidence |
| --- | --- | --- |
| `llm_brain_core/document_bridge.py` (`RetiredIndexBridgeDocumentBridge`) | 유지 | The M9 brain-bridge seam. It is transport-agnostic: it takes an injected object with a `retrieve(...)` method and never imports the client. `test_autopilot_no_index_client_before_m9.py` already exempts it from the "no index client" rule. |
| `knowledge_search_service.DisabledRetiredIndexBridgeClient` | 유지 | The recall path's no-op client. `build_index_client()` returns it, so the MCP recall surface never reaches the retired backend. |
| `BackendKind.RETIRED_INDEX_BRIDGE` (Java) and `TargetProfileRegistry` | 유지 | A logical routing label. The registry's own docstring says the adapter is not loaded by the default profiles and the logical profiles preserve the public enqueue contract. |
| `index-*` target profile ids and dataset roles (`docs/contracts/target-profiles.yaml`, `application.yml`) | 유지 | Public routing contract. The retired bridge's per-profile dataset env keys are gone; the logical ids stay. |
| `docs/contracts/target-profiles.yaml` — `retiredIndexBridgeDatasetEnv` field | 유지 | The public contract artifact still records which env key *used to* carry each profile's physical dataset id. Dropping the field changes a published contract shape, which is a separate decision from this cleanup. The values are no longer provided anywhere (compose and `.env.example` are clean, and the guard test pins that). |
| `couchdb_source/document_model.py` — `INDEX_RECALL_PROFILE` (renamed from `RETIRED_INDEX_BRIDGE_RECALL_PROFILE`) | 중립화 | The value (`session-memory`) did not change; only the constant name did. |
| `session_memory/brain_query.py` — `"source": "retired_index_bridge"` | 유지 | A label in the public `brain.query` response shape. The lane is the M9 bridge seam, and changing the label would change the response contract. |
| `session_memory/transcript_parsers/evidence.py` — the `/api/v1/` regex | 유지 | A generic URL-shape matcher used to spot live-proof markers in transcripts. Not the retired protocol. |
| `session_memory/index_projection.py` · `session_memory/regeneration_index_sync.py` · `rag_ingress/file_ingest.py` | 유지 | They call the retired bridge's document API (`upload_document` / `update_metadata` / `request_parse` / `list_documents` / `get_document_status`) through an **injected** client. They import no transport, so they cannot reach the bridge on their own, and they are not on the live recall path. `session_memory/index_projection.py` is re-exported as public API by `session_memory/__init__.py`. |
| `couchdb_source/document_model.py` — `RETIRED_TRANSCRIPT_MEMORY_PROFILE` (renamed from `RETIRED_RETIRED_INDEX_BRIDGE_PROFILE`) | 중립화 | A pre-existing double-prefixed name (a leftover of an earlier rename), not introduced here. The value (`transcript-memory`) is unchanged; only the constant name became backend-neutral. |
| `ledger.py`, `rag_ingress/state_db.py` — legacy `ragflow_*` column names | 유지 | The data-preserving migration that copies historical `ragflow_dataset_id` / `ragflow_document_id` / `ragflow_run` / `ragflow_datasets` into their `index_*` equivalents. Renaming the source column names would break the migration. |
| `knowledge_search_service.py`, `llm_brain_core/objects/*` — `ragflow_dataset:` / `ragflow_document:` redaction patterns | 유지 | Secret-like key-name *detection* patterns for values that must never reach a public surface. They reference no product and can select or reach nothing. |
| `worker/docs/qdrant-readiness/**` | 유지 | Point-in-time audit documents. Their "RetiredIndexBridge" wording describes the state at the time; the live status is `docs/retired/ragflow-retirement.md`. |

## Guards added

| Guard | Location |
| --- | --- |
| No retired-bridge transport outside the legacy operator allowlist | `worker/tests/test_retired_index_bridge_protocol_guard.py` |
| The ingress runtime imports no legacy transport | same |
| No retired wire-protocol endpoint shape in active source (Python or Java) | same |
| The Java retired-bridge adapter package stays deleted | same |
| No `RETIRED_INDEX_BRIDGE_*` in the deploy surface | same + `ComposeConfigTest.retiredIndexBridgeEnvIsAbsentFromComposeAndEnvExample` |
| The delivery backend selector accepts `couchdb` only | same + `ComposeConfigTest` + `test_runtime_image_provenance_contract.py` |
| The session-memory container has no gc/backfill lane | `worker/tests/test_session_memory_deploy_script.py` |
| The session-memory projection backend rejects the retired bridge | `worker/tests/test_couchdb_build_cli.py` |

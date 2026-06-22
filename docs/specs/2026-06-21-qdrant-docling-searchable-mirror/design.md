# Qdrant+Docling Searchable Mirror PoC Design Spec

## Overview

Qdrant+Docling은 RAGFlow의 searchable mirror 역할만 대체하는 PoC다. Canonical authority는 CouchDB/ledger PG/Neo4j에 남고, RAGFlow failover는 digest-bound dual-write/read-compare evidence packet과 operator approval 전까지 금지한다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- 핵심: searchable mirror only, local-first, `IndexBackendAdapter` 호환, evidence packet gate, production `NO-GO`

## Audit Summary

- Java `RagTargetAdapter` contract는 `pressureSnapshot`, `deliver`, `getStatus` 3개 메서드와 logical `targetProfile` routing을 보존해야 한다.
- Python `rag_ingress.index_backend.IndexBackendAdapter`는 이미 future Qdrant/OpenSearch/LanceDB adapter를 전제로 한 가장 얇은 PoC seam이다.
- `RagReadyDocument`는 backend-neutral이며 physical dataset/resource id를 알면 안 된다.
- `session_memory.brain_query`는 local ledger precedence를 유지하고 mirror hit은 archive/evidence lane으로만 취급한다.
- `llm_brain_core.ontology`는 graph/ontology projection 전에 public-safe redaction과 project brain_id scoping을 강제한다.
- `state_shadow_readiness`는 dry-run/redact, immutable read-only, no network/mutation, `production_authority_status=NO-GO` contract를 이미 가진다.

## Architecture

```text
RagReadyDocument
    |
    v
QdrantDoclingMirrorAdapter  (IndexBackendAdapter)
    |-- Docling normalizer   body/html/md -> markdown
    |-- EmbeddingProvider    markdown -> vector
    |-- Qdrant client        local :memory:/path or remote client
    |
    v
Qdrant collection (searchable_runtime_mirror)

Canonical authorities stay outside this adapter:
    CouchDB transcript source
    ledger PG MemoryCard/state
    Neo4j/Graphiti derived ontology graph
```

## Data Flow

1. Existing ingress path builds or recovers a `RagReadyDocument`.
2. Adapter normalizes the document body through Docling when Docling is installed, or an injected normalizer in tests.
3. Adapter rejects normalized text that still contains private path/credential patterns.
4. Adapter embeds normalized markdown with an injected embedder.
5. Adapter upserts one Qdrant point by stable UUID derived from `targetProfile`, `idempotencyKey`, and `contentHash`.
6. Adapter returns only generic `BackendSubmitResult` / `BackendStatusDetail`.
7. Mirror query returns only mirror candidates with `canonical_resolution_required=True`; product reads must join canonical authorities before use.
8. Gate report remains `NO-GO` until a redacted evidence packet validates dual-write/read-compare, Apple Silicon local smoke, Ubuntu host dry-run, and operator approval evidence.

## Component Details

| Component | Responsibility | Non-authority guard |
| --- | --- | --- |
| `QdrantDoclingMirrorAdapter` | `IndexBackendAdapter` implementation over Qdrant | no canonical writes, no failover |
| `DoclingMarkdownNormalizer` | optional Docling conversion to markdown | import only on PoC path |
| `HashEmbeddingProvider` | deterministic local vector for tests/smoke | PoC quality only |
| `query_mirror_candidates` | filtered Qdrant candidate lookup | requires later canonical join |
| `build_searchable_mirror_gate_report` | dry-run production gate summary | always `NO-GO`; booleans alone never ready |
| Tests | protocol, privacy, natural key, gate behavior | no network, no real secrets |

## Error Handling

- Missing Qdrant/Docling optional dependency raises a bounded `SearchableMirrorUnavailable` only when that PoC path is constructed.
- Blank natural keys return `None` instead of scanning all points.
- Normalized text leak categories and nested secret-like metadata keys raise `ValueError` before any upsert.
- Qdrant collection probe treats only not-found errors as create-needed; auth/transport/compat errors fail with bounded `SearchableMirrorUnavailable`.
- Query embedding vectors are size-checked and `target_profile` is pushed into the Qdrant payload filter before `limit`.
- Qdrant client errors propagate to caller so delivery remains retryable/uncertain rather than falsely successful.
- Gate report rejects non-dry-run input, validates a digest-bound redacted evidence packet, and never prints raw ids, raw paths, or raw content.

## Testing Strategy

- Unit tests use a fake Qdrant client and fake normalizer. No network.
- Adapter tests cover submit/upsert, status, natural-key lookup, blank-key fail-closed, filtered mirror candidate search, vector size guards, collection probe errors, and privacy fail-closed.
- Gate tests cover `NO-GO`, evidence packet requirement, read-compare mismatch blockers, dry-run enforcement, and approval-required state.
- Existing focused regressions to keep green:
  - `worker/tests/test_rag_ingress_delivery_prep.py`
  - `worker/tests/test_rag_ingress_readiness.py`
  - `worker/tests/test_ontology_episode_batch.py`
  - `worker/tests/test_brain_query.py`

## External Docs Used

- [Qdrant Python client local mode](https://github.com/qdrant/qdrant-client): supports `QdrantClient(":memory:")` and `QdrantClient(path="...")` for local tests/prototyping.
- [Qdrant local quickstart](https://qdrant.tech/documentation/quickstart/): server mode remains a separate Docker/service decision and is not part of this PoC.
- [Qdrant filtering](https://qdrant.tech/documentation/search/filtering/): payload filters are used for logical `target_profile` candidate isolation.
- [Qdrant Query Points API](https://api.qdrant.tech/api-reference/search/query-points): query requests include `filter`, `limit`, `with_payload`, and vector query fields.
- [Docling `DocumentConverter`](https://docling-project.github.io/docling/reference/document_converter/): `convert_string` supports Markdown/HTML/DocLang string input and returns a result whose document can export markdown.
- [Docling installation](https://docling-project.github.io/docling/getting_started/installation/): `docling` is the documented install package and supports macOS/Linux arm64/x86_64; PoC keeps it optional.

## Milestones

- M0: Contract audit and SoT docs
- M1: Adapter protocol and fake-client tests
- M2: Docling normalization and privacy fail-closed
- M3: Qdrant natural-key/status/search behavior
- M4: Digest-bound dual-write/read-compare gate report
- M5: Apple Silicon local-first smoke shape
- M6: Ubuntu host production-gate report shape
- M7: RAGFlow failover blocked until approval

## Open Questions

- Production embedding model and vector dimension remain outside this PoC.
- Physical Qdrant collection naming remains adapter-private and can be finalized after local/Ubuntu smoke.
- Evidence packet retention location remains outside this PoC; the gate accepts a redacted JSON packet path.

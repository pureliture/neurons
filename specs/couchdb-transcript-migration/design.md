# CouchDB Transcript Source Migration Design Spec

## Overview

`transcript-memory`를 RetiredIndexBridge dataset에서 CouchDB-backed source/evidence store로 이전한다. RetiredIndexBridge는 normal brain path에서 `session-memory`만 제공하며, CouchDB는 transcript/tool evidence의 rebuild, audit, rollback source로 남는다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- Preview companion: `requirements.html`
- Existing architecture HTML preserved: `docs/architecture/ledger-review-deepdive-20260614.html`
- Updated architecture artifact: `docs/architecture/ledger-review-deepdive-20260617-couchdb-transcript-source.html`
- 핵심 요구사항:
  - RetiredIndexBridge `transcript-memory` 신규 write/read 의존을 완전히 제거한다.
  - Provider 원본 transcript를 기준 source로 삼고 RetiredIndexBridge/ledger는 coverage 검증에 쓴다.
  - RetiredIndexBridge에는 `session-memory`만 남긴다.
  - `tool_evidence_summary`는 bounded bundle로 CouchDB에 보존하고, `session-memory`에는 충분히 materialize한다.
  - Codex, Claude Code, Gemini CLI, agy, Antigravity를 migration 대상으로 한다.
  - Gemini CLI는 historical import only이고, Codex/Claude Code/agy/Antigravity는 live pipeline cutover까지 완료 기준에 포함한다.

## Approach Proposal

### Recommended: CouchDB Source Store + RetiredIndexBridge Session Ontology

Provider 원본 transcript를 다시 parse/redact/rebuild해 CouchDB에 source/evidence document로 저장한다. `session-memory` builder는 CouchDB source를 읽어 tool evidence를 충분히 materialize한 session-memory document를 만들고, RetiredIndexBridge에는 이 derived `session-memory`만 project한다.

장점은 RetiredIndexBridge가 Palantir Ontology처럼 brain-facing recall surface로 유지되고, CouchDB가 noisy source/evidence lifecycle을 전담한다는 점이다. 단점은 migration 동안 CouchDB source model, coverage verifier, RetiredIndexBridge projection gate를 함께 도입해야 한다는 점이다.

### Alternative: RetiredIndexBridge Metadata Repair First

기존 RetiredIndexBridge `transcript-memory`의 project metadata를 보정한 뒤 CouchDB로 옮긴다. 초기 구현은 줄지만, 이미 오염된 RetiredIndexBridge metadata를 migration source로 재사용하는 위험이 크다.

### Alternative: Long-Term Dual Store

CouchDB와 RetiredIndexBridge `transcript-memory`를 계속 병행한다. rollback은 쉽지만, RetiredIndexBridge `session-memory` only 목표와 맞지 않고 운영/검색 의미가 계속 겹친다.

## Architecture

```text
Provider transcript sources on PC
  - Codex
  - Claude Code
  - Gemini CLI historical import only
  - agy
  - Antigravity
        |
        v
Transcript Source Rebuilder
  parse -> server redaction/leak check -> project authority resolver
        |
        v
CouchDB Transcript Source Store
  conversation_chunk docs
  tool_evidence_bundle docs
  coverage_manifest docs
  projection_state docs
        |
        +----------------------+
        |                      |
        v                      v
Cold Full Redacted Archive   Session-Memory Materializer
                              full tool evidence summary materialization
                                      |
                                      v
                              RetiredIndexBridge session-memory
                              brain-facing recall surface
```

RetiredIndexBridge `transcript-memory` remains only as a temporary comparison input during migration. It is not a source of truth and is not part of the final runtime path.

## Data Flow

### Historical Import

1. Enumerate source transcript files for the 5 provider/tool lanes.
2. Parse and redact provider transcript sources server-side.
3. Resolve `project` through hierarchy authority: capture metadata, provider source path/workspace marker, then server inference.
4. Write conversation chunks and tool evidence bundles into CouchDB.
5. Compare CouchDB coverage with RetiredIndexBridge and ledger/ingress state without trusting RetiredIndexBridge project metadata.
6. Build `session-memory` from CouchDB source.
7. Project only `session-memory` to RetiredIndexBridge.
8. Run coverage, rebuild, and representative recall smoke before retirement.

### Live Cutover

1. Start CouchDB shadow write for Codex, Claude Code, agy, and Antigravity live lanes.
2. Keep existing RetiredIndexBridge `transcript-memory` write only as a short-lived comparison path.
3. Observe a short stability window with mixed provider/project traffic.
4. Switch live writes to CouchDB-only.
5. Remove RetiredIndexBridge `transcript-memory` write and read dependencies.

### Normal Recall

1. LLM or brain query reads RetiredIndexBridge `session-memory`.
2. RetiredIndexBridge `session-memory` contains sufficient tool evidence summary materialization.
3. Normal recall does not fetch CouchDB evidence refs.
4. CouchDB refs remain for provenance, audit, rollback, and debug.

## Component Details

### Provider Source Enumerator

- Input: provider/tool name, source root, historical/live scope.
- Output: redacted-safe source locator records.
- Dependencies: provider-specific source layout knowledge.
- Notes: Gemini CLI is historical import only; no live pipeline is required for that lane.

### Transcript Source Rebuilder

- Input: source locator records.
- Output: normalized conversation chunks and raw-to-redacted coverage metadata.
- Dependencies: existing provider parsers and server-side redaction/leak checks.
- Failure mode: fail closed if parsing or redaction confidence is insufficient.

### Project Authority Resolver

- Input: capture metadata, provider source path, cwd/workspace marker, session context.
- Output: canonical project plus ambiguity state.
- Dependencies: no RetiredIndexBridge project metadata as single authority.
- Failure mode: ambiguous project requires explicit ambiguity marker and is excluded from irreversible retirement proof.

### CouchDB Transcript Source Store

- Input: conversation chunks, tool evidence bundles, coverage manifests, projection state.
- Output: document revisions and queryable coverage state.
- Dependencies: CouchDB container owned by neurons, not RetiredIndexBridge internal DB/Redis/MinIO/Elasticsearch.
- Expected document families:
  - `transcript_session`
  - `conversation_chunk`
  - `tool_evidence_bundle`
  - `coverage_manifest`
  - `projection_state`
  - `retention_manifest`

### Tool Evidence Bundler

- Input: redacted tool evidence items for a session.
- Output: bounded `tool_evidence_bundle` documents with evidence index ranges and content/coverage hashes.
- Dependencies: existing tool evidence extraction logic.
- Constraint: smaller than session-level, larger than item-level.

### Session-Memory Materializer

- Input: CouchDB conversation chunks and tool evidence bundles.
- Output: derived `session-memory` documents.
- Dependencies: existing session-memory builder behavior.
- Constraint: materialize enough tool evidence summary for RetiredIndexBridge-only normal recall.

### RetiredIndexBridge Session Projection

- Input: derived `session-memory` documents.
- Output: RetiredIndexBridge `session-memory` documents.
- Dependencies: RetiredIndexBridge adapter and dataset resolution.
- Constraint: no `transcript-memory` projection after cutover.

### Coverage And Retirement Verifier

- Input: CouchDB coverage manifests, ledger/ingress state, RetiredIndexBridge comparison data, session-memory rebuild outputs, recall smoke outputs.
- Output: retirement readiness report.
- Gate:
  - coverage pass
  - rebuild pass
  - representative recall smoke pass

### Retention Manager

- Input: source age, coverage state, cold archive state, session-memory projection state.
- Output: hot-store full retention, hot-store manifest-only retention, or cold archive references.
- Constraint: hot query path stays small; full redacted source archive remains available outside CouchDB hot queries.

## Error Handling

- RetiredIndexBridge project metadata mismatch: ignore as authority, record as comparison mismatch, and resolve via hierarchy authority.
- Provider source missing on PC: mark source unavailable and block irreversible retirement for affected coverage.
- Raw leak detection: reject write, record redacted failure metadata only, and do not project to RetiredIndexBridge.
- CouchDB revision conflict: retry idempotent upsert using deterministic document id and content hash.
- Tool evidence bundle overflow: split deterministically and preserve evidence index ranges.
- Session-memory materialization loss: fail retirement gate if any source chunk or tool evidence bundle is dropped.
- RetiredIndexBridge projection failure: keep CouchDB source intact and mark projection state failed.
- Recall smoke failure: block RetiredIndexBridge `transcript-memory` retirement until rebuild or materialization is corrected.
- Gemini CLI live event detected: treat as scope violation unless separately approved.

## Testing Strategy

- Unit tests:
  - project authority resolver conflict cases
  - deterministic CouchDB document ids
  - tool evidence bundle splitting
  - retention state transitions
- Integration tests:
  - historical import from provider source fixtures
  - CouchDB write/read coverage queries
  - session-memory materialization from CouchDB source
  - RetiredIndexBridge session-memory projection using fake/recording adapter
- Migration verification:
  - coverage comparison against ledger/ingress and RetiredIndexBridge candidate set
  - rebuild no-loss checks for conversation chunks and tool evidence bundles
  - representative recall smoke on RetiredIndexBridge `session-memory`
- Live cutover checks:
  - short shadow window across Codex, Claude Code, agy, and Antigravity
  - Gemini CLI historical import only check
  - no new RetiredIndexBridge `transcript-memory` writes after CouchDB-only switch

## Milestones

- M1: Source model and CouchDB ownership contract - document families, deterministic ids, redaction boundaries, and ownership rules are testable.
- M2: Historical import and project authority - provider source rebuild covers Codex, Claude Code, Gemini CLI, agy, and Antigravity historical data with project mismatch reporting.
- M3: Tool evidence and session-memory materialization - bounded bundles are stored in CouchDB and fully materialized into RetiredIndexBridge `session-memory` for normal recall.
- M4: Shadow live cutover - Codex, Claude Code, agy, and Antigravity shadow writes prove CouchDB coverage during a short stability window.
- M5: RetiredIndexBridge transcript-memory retirement - coverage, rebuild, and recall smoke gates pass; new writes and reads no longer depend on RetiredIndexBridge `transcript-memory`.
- M6: Retention and archive - hot CouchDB source compacts old data to manifests while cold full redacted archive remains available for audit/rollback.

## Updated Architecture Artifact

The existing HTML design artifact remains unchanged.

The existing HTML design artifact remains unchanged:

- `docs/architecture/ledger-review-deepdive-20260614.html`

The updated CouchDB transcript-source migration artifact is generated separately:

- `docs/architecture/ledger-review-deepdive-20260617-couchdb-transcript-source.html`

## Open Questions

- None for Phase 2 design approval. Implementation may still discover SoT changes; those must return to grill-to-spec rather than changing this design silently.

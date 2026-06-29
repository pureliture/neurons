# Neurons Public / Private Separation

`neurons`와 `dendrite`의 장기 목표는 사용자가 자기 환경에서 세션을 수집하고,
기억을 적재하고, 최신 recall을 사용할 수 있는 제품 경로를 제공하는 것이다.

이 repo의 public surface는 제품의 동작 방식과 local bootstrap 경로를 설명한다.
실제 운영 상태, 개인 데이터, runtime evidence, secret, host topology는 public
repo에 두지 않는다.

## Boundary

Public repo에 둘 수 있는 것은 제품 코드와 샘플 실행 경로다.

- `neurons` core source code
- `dendrite`와 맞물리는 public ingest/MCP contract
- `MemoryCard` schema and recall policy code
- session ingest schema and contract tests
- local/dev compose for CouchDB, Qdrant, queue, workers, and MCP
- `.env.example` with placeholder values only
- sample data, synthetic fixtures, and fake/local adapters
- sanitized architecture docs, install guide, and local demo runbook

Private repo 또는 private storage에만 둘 것은 실제 운영 권위와 증빙이다.

- raw transcript and real session archive
- real `MemoryCard` ledger and accepted/current authoritative memory
- private user preference and private source refs
- live deployment evidence and private runbook execution logs
- production k3s values, overlays, and host topology
- real queue state, DB backup, Qdrant payload, and RetiredIndexBridge mappings
- secret, token, cookie, bearer string, API key, raw `dataset_id`, and raw `document_id`

## Repo Roles

`neurons` public keeps product source, local bootstrap, sample adapters,
contract tests, and public-safe docs. It does not need to reproduce the live
brain runtime to be useful; it should move toward `clone -> configure -> run`
with sample values.

`neurons-ops` private is the operations source of truth. It owns production
k3s manifests, live rollout evidence, secret loading policy, backup/restore,
GC approval records, and private authority ledger evidence.

`dendrite` public remains the host-native thin client. It owns provider capture
hooks, local spool/outbox, redaction before ship, and POST to an approved
ingress endpoint. It does not own server runtime, RetiredIndexBridge direct write,
session-memory promotion, or GC.

## Compose And Configuration

Public compose files may expose the shape of the stack: CouchDB or compatible
local store, Qdrant, queue, MCP, ingest worker, session-memory worker, and
sample LLM adapter.

Public compose files must not embed real credentials, production volumes, live
host topology, real dataset mappings, raw `dataset_id`, raw `document_id`, or
private API endpoints. Those belong in `neurons-ops` or other private storage.

## Decision Rule

When a file or value is ambiguous, keep it private if the product can still run
with a sample replacement, or if the value reveals real operating state.

It may be public when it describes a product interface, schema, contract,
sanitized architecture, or local demo path without revealing private state.

## Transition Strategy

The current transition is conservative:

1. Treat the current `neurons` repo as private until public-safe extraction is
   complete.
2. Use this document as the public/private boundary contract.
3. Move sanitized source, sample config, local compose, and public docs toward
   the future public repo.
4. Keep live operational material in `neurons-ops`.
5. Make full public `clone -> configure -> run` a follow-up goal, not a reason
   to publish private state early.

# RetiredIndexBridge Adapter Placement Guard

Status: superseded (2026-10-22) — the guarded package was deleted.

Tracker: GitHub issue #40.

## Superseded By

The retired external index bridge left the `neurons` runtime, so the Java
`adapter.ext.retired_index_bridge` package no longer exists. With it gone, the
two `ArchitectureRulesTest` rules that guarded its placement ("target ports must
not depend on the retired bridge implementation", "retired bridge
implementation classes stay in the ext adapter package") had no subject and were
removed as well.

The surviving boundary is the **Python** adapter contract in
`worker/lib/agent_knowledge/rag_ingress/index_adapter.py`, which is
backend-neutral and implemented by the CouchDB and Qdrant adapters. Its
invariants are pinned by
`worker/tests/test_retired_index_bridge_protocol_guard.py`.

See `docs/retired/ragflow-retirement.md` (retirement record) and
`docs/retired/ragflow-protocol-residue-inventory.md` (per-surface decisions)
for the full change.

## Original Scope (historical)

Guard current Java package placement for retired external index bridge adapter code.

## Non-Goals

- No package move unless a test proves current placement is wrong.
- No live retired bridge call.
- No target port API break.

## Original Test Plan (historical)

- Add a Java package-boundary test using ArchUnit or source/static inspection.
- Keep targeted adapter tests green.

## Original Done Criteria (historical)

- Placement guard passes.
- Evidence is recorded in `milestones.md`.

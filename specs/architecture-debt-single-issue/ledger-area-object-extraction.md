# Ledger Area-Object Extraction

Status: done.

Tracker: GitHub issue #40.

## Scope

Extract the first behavior-preserving Ledger area object behind a narrow seam. The preferred first candidate is the memory-promotion dirty-marking side effect because the current implementation already has a small `_memory_promotion_area` seam.

## Non-Goals

- No full Ledger inheritance removal.
- No public `Ledger` API break.
- No GC/live data mutation.
- No durable-state storage format change.

## Required Invariants

- Public `Ledger` methods keep the same behavior.
- Transaction and durable-state tests keep passing.
- Ingress code cannot call memory-promotion dirty-marking methods through inherited Ledger methods.
- The extracted object remains owned by `Ledger`, not by callers.

## Test Plan

- Add a failing boundary test that requires a concrete Ledger area object for memory promotion.
- Guard that ingress mixin code routes memory-promotion side effects through the area object seam.
- Run Ledger boundary/core/transaction tests.
- Run full worker verification after the extraction.

## Done Criteria

- A first area object owns the memory-promotion side-effect boundary.
- Existing Ledger behavior is preserved by tests.
- Broader mixin removal remains explicitly deferred.
- Evidence is recorded in `milestones.md`.

## Evidence

- `MemoryPromotionArea` now owns session/project dirty-memory marking behind the private Ledger area seam.
- `Ledger._memory_promotion_area` returns a concrete area object instead of `self`.
- Public `Ledger.mark_session_memory_dirty()` and `Ledger.mark_project_memory_dirty()` remain compatibility delegators.
- Ingress still routes indexed conversation-chunk dirty side effects through `_memory_promotion_area`.
- Ledger boundary guard now fails if `_memory_promotion_area` returns `self`.
- Focused Ledger tests and full worker suite passed.

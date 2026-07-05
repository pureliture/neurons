# RetiredIndexBridge Adapter Placement Guard

Status: done.

Tracker: GitHub issue #40.

## Scope

Guard current Java package placement for retired external index bridge adapter code.

## Non-Goals

- No package move unless a test proves current placement is wrong.
- No live retired bridge call.
- No target port API break.

## Required Invariants

- Retired bridge implementation classes live under `adapter.ext.retired_index_bridge`.
- `target.port` remains backend-neutral and does not depend on retired bridge implementation classes.
- Historical `targetAdapter` wording cannot reintroduce old placement.

## Test Plan

- Add a Java package-boundary test using ArchUnit or source/static inspection.
- Keep targeted adapter tests green.

## Done Criteria

- Placement guard passes.
- Evidence is recorded in `milestones.md`.

## Evidence

- `ArchitectureRulesTest` guards `target.port` against dependencies on `adapter.ext.retired_index_bridge`.
- `ArchitectureRulesTest` keeps RetiredIndexBridge implementation classes under `adapter.ext.retired_index_bridge`.

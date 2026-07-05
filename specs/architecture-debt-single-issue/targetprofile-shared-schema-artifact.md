# TargetProfile Shared Schema Artifact

Status: done.

Tracker: GitHub issue #40.

## Scope

Create a public-safe target profile contract artifact and guard Java, Python, compose, and `.env.example` against drift.

## Non-Goals

- No physical dataset ids.
- No live retired bridge delivery.
- No secret or private ops value.
- No public enqueue API break.

## Required Invariants

- The artifact is the reviewable public contract for logical profiles.
- Java `TargetProfileRegistry.DEFAULT` and `application.yml` match the artifact.
- Python `env_profile_dataset_resolver` resolves env keys named by the artifact.
- `compose.yaml` and `.env.example` mention every expected env key.

## Test Plan

- Java TargetProfile tests load the artifact and compare it with `TargetProfileRegistry.DEFAULT` and `application.yml`.
- Python shadow-worker tests load the same artifact and compare resolver/compose/env-example coverage.

## Done Criteria

- TargetProfile shared artifact exists and has no private identifiers.
- Targeted Java/Python tests pass.
- Evidence is recorded in `milestones.md`.

## Evidence

- `docs/contracts/target-profiles.yaml` defines the public-safe logical profile artifact.
- `docs/contracts/ingress-contract.md` defines the artifact as the machine-readable child of the backend-neutral ingress contract.
- Java TargetProfile tests load the artifact and compare it with `TargetProfileRegistry.DEFAULT` and `application.yml`.
- Python shadow-worker tests load the same artifact and compare resolver behavior, compose coverage, and `.env.example` coverage.

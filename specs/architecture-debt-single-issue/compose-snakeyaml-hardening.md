# Compose SnakeYAML Hardening

Status: done.

Tracker: GitHub issue #40.

## Scope

Harden compose env anchor tests by parsing resolved YAML merge output.

## Non-Goals

- No compose runtime start/stop.
- No live delivery env activation.
- No secret value addition.

## Required Invariants

- Java ingress services and Python ingress worker resolve common retired bridge env keys.
- Common retired bridge env keys are still declared once in the shared anchor.
- Live queue/delivery controls remain service-local.

## Test Plan

- Strengthen `ComposeConfigTest` with SnakeYAML parsing.
- Keep string guards only for raw anchor declaration and comments that are not represented in parsed YAML.

## Done Criteria

- Targeted compose config tests pass.
- Evidence is recorded in `milestones.md`.

## Evidence

- `ComposeConfigTest` parses `compose.yaml` with SnakeYAML and checks resolved service environment maps.
- The shared retired bridge env anchor remains common while live queue/delivery controls remain service-local.

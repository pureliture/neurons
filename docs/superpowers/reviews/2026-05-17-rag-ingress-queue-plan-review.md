# rag-ingress-queue MVP Plan Review

Date: 2026-05-17
Reviewed plan: `docs/superpowers/plans/2026-05-17-rag-ingress-queue-mvp.md`

## Reviewers

| Reviewer | Perspective | Result |
|---|---|---|
| Subagent 1 | Superpowers writing-plans, TDD, verification | P0 none, P1 granularity/evidence gaps |
| Subagent 2 | Architecture and system-design | P0 none, P1 idempotency/no-fetch/status gaps |
| Subagent 3 | Security, redaction, tech debt, deploy | P0 none, P1 compose/log/DTO/provisioning gaps |

## P0 Findings

None.

## Required Changes Applied

- Added per-task subagent handoff packet requirements with RED/GREEN/REGRESSION/RUNTIME evidence fields.
- Added explicit subagent model policy for implementation and review.
- Added shared redaction denylist file requirement and required reuse in tests, postcheck, and final scans.
- Added `SafeJobSummary`, domain `toString()` leakage guard, and log capture tests.
- Added target-neutral versioned payload envelope and reserved `redacted_document_ref` disabled-extension behavior.
- Added idempotency key acceptance and conflict tests.
- Added canonical content hash mismatch rejection.
- Added missing-source, publish-failure-to-503, log-capture, and reserved-ref API tests.
- Added `JetStreamProvisioner` responsibility for idempotent stream/consumer creation and drift detection.
- Split Testcontainers integration into RED and GREEN steps.
- Added no-fetch fail-closed worker tests for `THROTTLED` and `CLOSED`.
- Added external status summary fields for indexed candidate and authorization counts.
- Added postcheck JSON schema, timeout, abort criteria, evidence path, and schema test.
- Added compose NATS URL override `nats://nats-jetstream:4222`.
- Added local MVP persistence and non-HA caveat plus restart-without-volume-removal smoke requirement.
- Added README/runbook requirement to align `docs/requirements.md` public payload example with `redacted_rag_ready_document`.

## Remaining Execution Notes

- Local Java/Maven/Docker Compose runtime is currently unavailable or incomplete, so implementation verification must be marked `blocked` until a Java 25 + Maven or equivalent container/CI path exists.
- Live RAGFlow smoke remains separate and requires an explicit approval packet.
- The current plan is suitable as an execution scaffold, but implementation subagents must still keep each patch small and stop on any RED/GREEN mismatch.

# rag-ingress-queue MVP Spec Review

Date: 2026-05-17
Reviewed spec: `docs/superpowers/specs/2026-05-17-rag-ingress-queue-mvp-spec.md`

## Reviewers

| Reviewer | Perspective | Result |
|---|---|---|
| Subagent 1 | Architecture and system design | P0 none, P1/P2 boundary clarifications |
| Subagent 2 | Testing strategy and deploy checklist | P0 none, P1/P2 verification clarifications |
| Subagent 3 | Security, redaction, and tech debt | P0 none, P1/P2 guardrail clarifications |

## P0 Findings

None.

## Required Changes Applied

- Replaced public `payload.kind=ragflow_ready_document` with target-neutral `redacted_rag_ready_document`.
- Preserved the future `redacted_document_ref` extension point while keeping inline document as the MVP implementation path.
- Removed `AUTHORIZED` from generic target indexing states and documented authorization as an external document status table/reconcile-client state.
- Changed MVP target pressure behavior to fail-closed: only `OPEN` creates new delivery; `THROTTLED` and `CLOSED` do not create new upload/parse requests.
- Clarified that JetStream does not automatically own a separate DLQ; project-defined terminal/quarantine policy is required.
- Strengthened redaction validation from marker/size checks to producer proof, allowed schema, denylist scanner, log/status/postcheck scans, and raw fixture rejection.
- Required raw token, raw target ID, raw dataset/document ID, private path, and private locator rejection at ingress.
- Replaced public absolute project path in the spec with `<repo>`.
- Added docs consulted date/version policy, dependency pin guardrails, JetStream persistence limits, local bind/auth posture, and rollback owner/procedure.
- Added explicit TDD evidence format for RED/GREEN/REFACTOR and runtime evidence boundaries.
- Added live RAGFlow smoke as approval-gated and non-authorizing.

## Plan Carry-Forwards

Implementation plan must include:

- RED/GREEN evidence per task with exact command and expected failure/pass evidence.
- Unit tests for validation, redaction, idempotency, pressure, adapter privacy, and status/authorization split.
- Web/API tests with response/log/status output secret absence assertions.
- Testcontainers NATS integration for publish ack, durable pull consumer, explicit ack, nak/retry, max-deliver, and quarantine candidate behavior.
- Fake RAGFlow adapter/server tests for pressure states, upload/status mapping, and adapter-private raw ID handling.
- Compose smoke that starts only the `rag-ingress-queue` compose project and verifies NATS, API, stream/consumer, redacted enqueue, worker pressure gate, and output redaction.
- Live RAGFlow smoke only behind explicit approval, with evidence that `DONE -> INDEXED candidate` does not imply authorization.
- Deploy gate with command list, timeout, abort criteria, expected local evidence path, rollback owner, and rollback procedure.

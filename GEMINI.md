# GEMINI.md

이 파일은 Gemini/Antigravity 계열 agent가 `neurons`를 읽을 때 따르는
repo-local provider overlay다. 공통 운영 계약은 `AGENTS.md`를 우선한다.

## Contract

- 자연어 응답과 문서는 한국어로 작성한다.
- 코드 식별자, CLI 이름, 파일명, endpoint 이름은 영어 원문을 유지한다.
- `neurons`는 provider capture UX가 아니라 server/brain authority다.
- `dendrite`가 보낸 redacted payload 이후의 ingress, queue, state DB,
  brain/session-memory, native-memory, GC safety lane을 소유한다.

## Boundary

- Mac provider hook, locator-only spool/outbox, thin shipper, Antigravity
  capture ergonomics는 `dendrite` 책임이다.
- Server worker, `ledger.py`, `TranscriptIngestWorker`, Qdrant/graph recall,
  brain.query, MemoryCard, native memory, GC planners는 `neurons` 책임이다.
- Historical component 이름으로 판단하지 말고 동작으로 판단한다.

## Safety

- Retired external index bridge credentials are not part of active runtime configuration.
- Raw transcript body, private locator/path, token, raw dataset_id,
  raw document_id를 출력하지 않는다.
- Retired external index bridge write/delete/disable, live GC, Docker/systemd/firewall/package
  mutation은 explicit user intent와 postcheck/rollback plan 없이 실행하지 않는다.

## Checks

- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- `cd worker && uv run pytest -q`

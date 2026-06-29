# AGENTS.md

이 저장소는 OpenClaw/LLM-brain의 server/brain repo인 `neurons`를 소유한다.
역사적 `rag-ingress-queue` 이름은 이 repo 안의 ingress service/runtime lane으로
남지만, repo identity는 Mac client가 아니라 server-side authority다.

## Identity

- 자연어 응답과 문서는 한국어로 작성한다.
- 코드 식별자, 파일명, CLI 이름, endpoint 이름은 영어 원문을 유지한다.
- `neurons`는 `dendrite`가 만든 redacted capture/enqueue payload를 받아
  server-side ingest, queue, state DB, brain/session-memory, native-memory, GC
  safety lane을 소유한다.

## Source Of Truth

- 공통 repo 계약: `AGENTS.md`
- Claude provider overlay: `CLAUDE.md`
- Gemini/Antigravity provider overlay: `GEMINI.md`
- repo boundary와 command surface: `README.md`, `worker/README.md`
- public/private repo split boundary: `docs/public-private-separation.md`
- server boundary regression guard: `worker/tests/test_server_boundary.py`,
  `worker/tests/test_repo_instructions.py`

## Owned Here

- ingress API/queue/runtime/worker and durable state DB
- `ledger.py`, transcript ingest worker, replay/reconcile/backfill server state
- session-memory/project-memory build and read surfaces
- brain.query, MemoryCard, native-memory mirror/sync/reconcile
- RAG target adapters, including current RAGFlow adapter
- GC safety planners and fail-closed GC command surfaces:
  `session-memory-gc`, `transcript-memory-gc`, `transcript-session-gc`,
  `transcript-volume-gc`, `session-memory-quarantine-terminal-skipped`,
  `session-memory-repair-zombie-snapshots`

## Not Owned Here

- provider hook installation on Mac
- locator-only local capture spool/outbox
- Mac thin shipper ergonomics
- `POST 18080` client-side enqueue command surface
- Antigravity/headless client capture UX

Those client responsibilities belong to `dendrite`.

## Runtime And Safety Lines

- Public repo에는 제품 코드, local/dev compose, sample config, contract tests,
  sanitized docs만 둔다. 실제 운영값, private ledger, raw transcript,
  live evidence, host topology, secret, raw `dataset_id`, raw `document_id`는
  `neurons-ops` 또는 private storage에 둔다.
- Keep RAGFlow credential env name to `RAGFLOW_API_KEY`; do not introduce
  `RAGFLOW_WRITE_TOKEN` or `RAGFLOW_READ_TOKEN`.
- Do not print raw host, private path, token, cookie, bearer string, API key,
  raw transcript body, raw dataset_id, or raw document_id.
- RAGFlow compose project, DB, Redis, MinIO, Elasticsearch, and volumes are not
  queue internals. Do not mutate them as part of ordinary code work.
- Live RAGFlow write/delete/disable, live GC execute, Docker/systemd/firewall,
  package install/remove, credential edit, and host mutation require current
  evidence, explicit user intent, exact argv, bounded timeout, redaction,
  postcheck, and rollback/abort criteria.
- GC is a safety lane, not thin-client cleanup. Dry-run first; live
  disable/delete requires coverage proof, retention/stability window,
  backup/rollback evidence, recall regression gate, and approval record.
- Do not read raw private transcript/source contents unless the user explicitly
  asks for that exact access.

## Workflow

- Java/Gradle checks use the repo Gradle wrapper or configured Gradle command.
- Python worker execution and tests use `uv`.
- Use `rg` for search and `apply_patch` for manual edits.
- Keep public CLI/API compatibility unless the user explicitly approves a break.
- Do not add skip/xfail just to make tests pass.
- Before claiming completion, run relevant checks:
  - root service: `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
  - worker: `cd worker && uv run pytest -q`
- If `graphify-out/graph.json` exists and the user asks codebase questions,
  prefer `graphify query`, `graphify path`, or `graphify explain` before broad
  source browsing.

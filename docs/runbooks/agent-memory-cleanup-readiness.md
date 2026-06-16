# Agent Memory Cleanup Readiness

이 runbook은 잘못 매핑된 `transcript-memory` / `session-memory` RAGFlow
문서를 정리하기 전 준비 절차다. 여기서는 live disable/delete를 실행하지 않는다.

## Gate Order

1. Runtime drain 확인
   - `dendrite` capture hook과 Mac LaunchAgent가 정상 종료한다.
   - `rag-ingress-live` worker가 멈추지 않고 drain한다.
   - queue pressure가 `OPEN`이고 `deadLetter`가 0이다.

2. Read-only readiness 확인
   - `cleanup-readiness`를 실행한다.
   - `status=ready_for_disable_candidate_refresh`가 아니면 정리 실행 금지.
   - 출력은 raw dataset/document id와 raw content를 포함하면 안 된다.

3. Session-memory canary
   - corrected `transcript-memory`가 `DONE` coverage를 가진 뒤에만 수행한다.
   - 먼저 shadow-log meta probe로 canary source가 어느 project/provider에 있는지
     확인한다.
   - 먼저 dry-run으로 session 1건을 빌드한다.
   - sync/live write는 별도 approval record가 있을 때만 수행한다.

4. Candidate refresh
   - `transcript-memory-gc` dry-run으로 disable 후보를 산출한다.
   - `session-memory-gc` dry-run으로 hard-delete 후보를 산출한다.
   - raw ids는 chat/log에 출력하지 않는다. approval-bound private artifact에만 둔다.

5. Approval packet
   - exact argv, bounded timeout, abort criteria, postcheck, rollback path를 묶는다.
   - hard delete는 `--backup-dir` evidence 없이 금지한다.

## Commands

Read-only readiness:

```bash
RAGFLOW_API_KEY=... uv run neuron-knowledge cleanup-readiness \
  --ragflow-url http://127.0.0.1:19380 \
  --token-env RAGFLOW_API_KEY \
  --projects neurons,dendrite
```

Session-memory canary dry-run:

```bash
RAGFLOW_API_KEY=... uv run neuron-knowledge neuron-session-memory-build \
  --dry-run \
  --probe-meta \
  --shadow-db <private-shadow-snapshot-db> \
  --watermark-file <private-watermark-file> \
  --ragflow-url http://127.0.0.1:19380 \
  --token-env RAGFLOW_API_KEY \
  --limit 50
```

If the meta probe finds corrected `neurons`/`dendrite` conversation chunks but
`memory-regeneration build-session-memory --all-sessions` reports
`sessions_available=0`, the blocker is source-lane mismatch: the delivered
transcript source is present in shadow/RAGFlow read-SoT, but the ledger
`transcript_chunks` source used by the legacy dry-run builder is empty.

```bash
uv run neuron-knowledge memory-regeneration build-session-memory \
  --ledger <private-ledger> \
  --project <project> \
  --provider <provider> \
  --session-id-hash <redacted-session-hash>
```

Transcript-memory disable dry-run:

```bash
uv run neuron-knowledge transcript-memory-gc \
  --ledger <private-ledger> \
  --dataset-id <private-transcript-memory-dataset-id> \
  --session-memory-dataset-id <private-session-memory-dataset-id> \
  --ragflow-url http://127.0.0.1:19380 \
  --declared-retention-policy private_indefinite_until_disabled \
  --candidate-scope exact-coverage \
  --max-items 25
```

Session-memory hard-delete dry-run:

```bash
RAGFLOW_API_KEY=... uv run neuron-knowledge session-memory-gc \
  --ledger <private-ledger> \
  --dataset-id <private-session-memory-dataset-id> \
  --ragflow-url http://127.0.0.1:19380 \
  --token-env RAGFLOW_API_KEY \
  --declared-retention-policy supersede_or_disable \
  --backup-dir <private-backup-dir> \
  --max-items 25
```

## Stop Conditions

- `cleanup-readiness`가 `blocked`를 반환한다.
- corrected docs에 `FAIL` 또는 장기 `RUNNING`이 남아 있다.
- session-memory canary가 provider-scoped `agent_id`로 재생성되지 않는다.
- backup/rollback evidence가 없다.
- approval-bound exact argv가 현재 readiness digest와 묶여 있지 않다.

## Current Known Blockers

- `corrected_session_memory_done_coverage_missing`
- `corrected_transcript_memory_has_non_done_runs`
- `memory-regeneration build-session-memory --all-sessions` can return
  `sessions_available=0` while shadow/RAGFlow read-SoT still has corrected
  `neurons`/`dendrite` conversation chunks.

이 blocker가 남아 있으면 disable/delete 실행은 금지한다.

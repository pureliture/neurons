# CLAUDE.md

이 파일은 Claude Code가 `neurons`에서 작업할 때 따르는 provider overlay다.
공통 운영 계약은 `AGENTS.md`를 우선한다.

## Contract

- 자연어 응답과 문서는 한국어로 작성한다.
- 코드 식별자, CLI 이름, 파일명, endpoint 이름은 영어 원문을 유지한다.
- `neurons`를 server/brain repo로 다룬다. Mac thin-client 책임은 `dendrite`로
  보낸다.
- RAGFlow app health, queue/NATS state, worker delivery, ledger/state authority,
  recall/product surface를 서로 다른 plane으로 분리해서 판단한다.

## Claude Guardrails

- `RAGFLOW_API_KEY` 하나만 사용한다. `RAGFLOW_WRITE_TOKEN` /
  `RAGFLOW_READ_TOKEN`을 새로 만들지 않는다.
- Live RAGFlow write/delete/disable, live GC, Docker/systemd/firewall/package
  mutation은 current evidence, explicit user intent, exact argv, timeout,
  redaction, postcheck, rollback/abort criteria 없이 실행하지 않는다.
- Healthy runtime에서는 repair/restart를 실행하지 않는다.
- GC 작업은 dry-run evidence, coverage proof, retention/stability window,
  backup/rollback evidence, recall regression gate를 분리해서 보고한다.
- Raw private transcript/source, private path, token, raw dataset_id,
  raw document_id를 public output에 쓰지 않는다.

## Checks

- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test`
- `cd worker && uv run pytest -q`
- `cd worker && uv run neuron-knowledge --show-boundary`

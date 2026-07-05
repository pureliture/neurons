# CouchDB Session-Memory Projection Pagination Requirements

## 승인 대상

- Source of truth: `requirements.md`
- Design companion: `design.md`
- GitHub issue: `#62 task: paginate CouchDB session-memory projection selection`
- 승인 상태: 사용자 사전 승인. 본 문서는 자문자답과 읽기 전용 코드/운영 경계 조사로 확정한 Phase 1 source다.

## 질문-답변 흐름

### Q: 이번 작업은 어떤 운영 위험을 줄이나?

`couchdb-session-memory-build`의 session selection이 CouchDB 전체 corpus를 한 번에 읽은 뒤 Python에서 `--limit`, `--project`, `--provider`를 적용하는 위험을 줄인다.

현재 `CouchDBHttpSourceStore.find_by_type`는 `_find` 요청 하나에 `limit: 200000`을 넣는다. 이 방식은 corpus가 커지면 단일 요청 cap 위의 문서를 놓치거나, `--limit`이 작아도 전체 session corpus를 먼저 끌어오는 비용을 만든다. #62는 projection scheduler가 큰 CouchDB corpus에서도 누락 없이 bounded scan을 할 수 있게 만드는 작업이다.

### Q: CouchDB 쪽 filtering은 어디까지 해야 하나?

`projection_state` 조회는 `projection_status == projected`를 DB-side selector로 좁히고, 가능한 경우 `project`와 `provider`도 selector에 포함한다.

`transcript_session` 조회도 `project`와 `provider`가 주어지면 selector에 포함한다. 이미 projected session을 제외하는 anti-join은 이번 범위에서 CouchDB Mango만으로 강제하지 않는다. 대신 projected state set은 DB-side filtered, bookmark-paginated scan으로 만들고, session scan은 bounded iterator로 흘려보내며 `--limit`에 도달하면 멈춘다.

### Q: store interface는 깨도 되나?

아니다. 기존 `find_by_type(doc_type, fields=...)` caller는 계속 동작해야 한다.

새 behavior는 backward-compatible optional arguments 또는 iterator helper로 추가한다. 기존 call sites가 list return을 기대하는 점을 유지하고, session-memory build path만 paginated iteration과 selector를 적극 사용한다.

### Q: in-memory store semantics는 어떻게 맞추나?

In-memory store도 selector, limit, iteration semantics를 같은 방식으로 지원한다.

이렇게 해야 focused tests가 HTTP store와 build CLI의 실제 selection semantics를 같이 검증할 수 있고, 이미 projected skip, authoritative projection state `_id`, project/provider scoping이 fake에서는 통과하지만 HTTP에서는 깨지는 차이를 막을 수 있다.

### Q: live CouchDB나 runtime을 직접 건드리나?

아니다. 이번 작업은 public repo code와 tests만 바꾼다.

라이브 CouchDB read/write, graph/Qdrant mutation, deployment, K8s/Docker/systemd 변경, credential/env 편집은 범위 밖이다. 필요한 증거는 local tests와 GitHub/PR 상태로 제한한다.

## 기능 요구사항

- `CouchDBHttpSourceStore`는 Mango bookmark pagination을 지원하고, 단일 `_find` request cap 위의 문서를 조용히 누락하지 않아야 한다.
- 기존 `find_by_type(doc_type, fields=...)` API는 계속 list를 반환해야 한다.
- store read API는 optional selector, limit, page size를 지원해야 한다.
- HTTP store는 selector를 `doc_type`과 병합하고, optional `fields`와 `bookmark`를 올바르게 전송해야 한다.
- HTTP store는 page마다 bounded `limit`을 사용하고, CouchDB가 `bookmark`를 반환하면 다음 page를 이어 읽어야 한다.
- In-memory store는 HTTP store와 같은 selector equality semantics, field projection, limit semantics를 지원해야 한다.
- `_select_sessions_needing_projection`은 `projection_state`에서 `projection_status == projected`를 selector로 내려보내야 한다.
- `_select_sessions_needing_projection`은 `project`와 `provider`가 주어지면 `projection_state`와 `transcript_session` selector에 모두 포함해야 한다.
- `_select_sessions_needing_projection`은 authoritative projection state `_id == projection_state_doc_id(session_id_hash)` 조건을 유지해야 한다.
- `_select_sessions_needing_projection`은 `transcript_session` scan을 paginated/bounded iterator로 처리하고, `--limit`에 도달하면 추가 session page를 읽지 않아야 한다.
- `--limit 0`은 기존처럼 no cap이어야 한다.
- existing selection behavior, dry-run/live report schema, approval gate, materialization/projector behavior는 유지해야 한다.

## 비기능 요구사항

| 항목 | 요구값 |
| --- | --- |
| Source control | `main`이 아니라 `codex/62-paginate-couchdb-session-memory-projection` branch와 전용 worktree에서만 수정한다. |
| TDD | code-changing milestone은 focused failing tests를 먼저 추가하고 red -> green -> refactor로 진행한다. |
| Compatibility | `find_by_type(doc_type, fields=...)` 기존 caller와 runtime protocol compatibility를 유지한다. |
| Safety | live CouchDB/graph/Qdrant/K8s mutation, deployment, credential edit, Docker/systemd mutation은 하지 않는다. |
| Pagination | single request cap에 의존하지 않고 bookmark를 따라가야 한다. |
| Bounded selection | `--limit`이 작은 경우 session corpus 전체를 materialize하지 않아야 한다. |
| Review | `codebase_architecture_manager`와 `code_simplifier`를 중간 품질 보정에 사용한다. |
| Language | 자연어 문서와 보고는 한국어로 작성하고 코드 식별자는 영문 유지. |

## 사용자 시나리오

- 운영자가 큰 CouchDB corpus에서 `couchdb-session-memory-build --dry-run --limit 10`을 실행해도 projection scheduler가 전체 session corpus를 한 번에 로드하지 않는다.
- 운영자가 `--project neurons --provider codex`로 scoped run을 준비하면 HTTP store가 project/provider selector를 CouchDB에 전달한다.
- maintainer가 기존 `store.find_by_type(SourceDocType.TRANSCRIPT_SESSION, fields=[...])` caller를 보더라도 return type이나 기본 behavior가 바뀌지 않는다.
- test author가 InMemory store로 selection helper를 검증할 때 HTTP store와 같은 selector/limit semantics를 기대할 수 있다.

## 미결정 항목

없음. 사용자가 `requirements.md`와 `design.md`를 모두 사전 승인했다.

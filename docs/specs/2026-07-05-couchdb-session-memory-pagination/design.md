# CouchDB Session-Memory Projection Pagination Design Spec

## Overview

#62는 CouchDB session-memory projection selection을 단일 `_find` 요청과 Python-side corpus filtering에서 bookmark-paginated, selector-aware selection으로 바꾼다. 핵심은 store read seam을 backward-compatible하게 넓히고, `couchdb-session-memory-build`가 그 seam을 사용해 projected state와 transcript session scan을 bounded하게 수행하도록 만드는 것이다.

## Requirements Reference

- Phase 1 source: `requirements.md`
- 승인 상태: 사용자 사전 승인
- 핵심 요구사항:
  - `projection_state` lookup은 `projection_status == projected`와 optional project/provider scope를 DB-side selector로 적용한다.
  - `transcript_session` selection은 paginated/bounded scan이어야 하며 `--limit` 전에 full corpus를 list로 만들지 않는다.
  - CouchDB HTTP store는 bookmark pagination으로 single request cap 위의 docs를 놓치지 않는다.
  - In-memory store/tests는 same selection semantics를 유지한다.
  - live CouchDB/runtime mutation은 수행하지 않는다.

## Approach

추천 접근은 **backward-compatible paginated read seam**이다.

1. 추천: `iter_by_type` + enriched `find_by_type`
   - `CouchDBSourceStore` protocol에 `iter_by_type`을 추가한다.
   - `find_by_type`은 기존 list API를 유지하되 optional `selector`, `limit`, `page_size`를 받는다.
   - HTTP store의 `find_by_type`은 `iter_by_type`을 list로 감싼다.
   - build CLI는 session scan에는 `iter_by_type`, projected state scan에는 selector-aware `find_by_type` 또는 iterator를 사용한다.
   - 장점: 기존 caller compatibility를 유지하면서 `--limit` short-circuit이 가능하다.
   - 단점: protocol surface가 넓어진다.

2. 대안: `find_by_type` 내부만 자동 pagination
   - 장점: caller 변경이 작다.
   - 단점: build CLI가 여전히 full list를 받은 뒤 `--limit`을 적용하므로 bounded selection 요구를 충족하지 못한다.

3. 대안: build CLI 전용 HTTP query 함수
   - 장점: scope가 좁다.
   - 단점: store seam을 우회해 in-memory/HTTP semantics가 갈라지고 future caller가 같은 문제를 반복한다.

이번 design은 1번을 채택한다.

## Architecture

```text
build_cli._select_sessions_needing_projection
  -> store.iter_by_type(TRANSCRIPT_SESSION, selector=scope, fields=..., page_size=...)
  -> store.find_by_type(PROJECTION_STATE, selector=projected + scope, fields=..., page_size=...)

CouchDBHttpSourceStore
  -> POST /{db}/_find { selector, fields, limit, bookmark? }
  -> repeat while bookmark advances and docs are returned

InMemoryCouchDBSourceStore
  -> same selector equality, field projection, limit, sorted deterministic iteration
```

Dependency direction stays unchanged:

```text
couchdb_source build CLI -> CouchDBSourceStore protocol -> HTTP/InMemory store
```

No live runtime, credential, or deployment surface is added.

## Data Flow

### Projected state scan

```text
state_selector = {
  "projection_status": "projected",
  optional "project": project,
  optional "provider": provider,
}

states = store.find_by_type(
  PROJECTION_STATE,
  selector=state_selector,
  fields=["_id", "session_id_hash", "projection_status"],
)

projected_session_ids =
  state.session_id_hash
  where state._id == projection_state_doc_id(state.session_id_hash)
```

The `_id` authority check remains mandatory. A stale or non-authoritative projected state must not suppress a session.

### Transcript session scan

```text
session_selector = {
  optional "project": project,
  optional "provider": provider,
}

for session in store.iter_by_type(
  TRANSCRIPT_SESSION,
  selector=session_selector,
  fields=["_id", "session_id_hash", "provider", "project"],
):
  skip empty session_id_hash
  skip if already projected
  append pending session
  stop once len(selected) == limit when limit > 0
```

This keeps `--limit 0` as no cap and avoids materializing full transcript session corpus before applying a positive limit.

## Component Details

### `CouchDBSourceStore`

- Keeps existing methods.
- Adds `iter_by_type(doc_type, fields=None, selector=None, limit=0, page_size=10000) -> Iterator[dict]`.
- Extends `find_by_type(doc_type, fields=None, selector=None, limit=0, page_size=10000) -> list[dict]`.
- `selector` is additional equality constraints merged with `doc_type`.
- `limit <= 0` means no total cap.

### `CouchDBHttpSourceStore`

- Builds Mango selectors by merging `{"doc_type": doc_type}` with optional selector.
- Sends page request bodies with `selector`, optional `fields`, and bounded per-page `limit`.
- Includes `bookmark` on follow-up pages.
- Yields docs from every page until no docs are returned, no bookmark is returned, requested total limit is reached, or bookmark stops advancing.
- Raises `CouchDBError` on non-200 `_find` responses.

### `InMemoryCouchDBSourceStore`

- Applies the same equality selector constraints as HTTP store.
- Uses deterministic `_id` sorting for stable test behavior.
- Applies field projection after selector match.
- Honors total `limit` for both `iter_by_type` and `find_by_type`.

### `build_cli`

- Adds tiny helper functions for selector construction and iterator fallback if needed.
- Pushes `projection_status`, `project`, and `provider` filters into store selector calls.
- Uses `iter_by_type` for session scan and stops at positive `limit`.
- Keeps dry-run/live JSON schema and approval behavior unchanged.

## Error Handling

- Invalid or unsupported selector operators are not introduced; selectors are equality-only dictionaries.
- HTTP `_find` non-200 responses continue to raise `CouchDBError`.
- A missing `iter_by_type` on legacy test doubles may fall back to `find_by_type` only where necessary, but built-in stores implement the iterator.
- Projection state docs without `session_id_hash`, with non-authoritative `_id`, or with non-projected status do not mark sessions projected.
- SoT or acceptance scope changes require returning to grill-to-spec instead of widening implementation ad hoc.

## Testing Strategy

- `test_couchdb_http_store.py`
  - `find_by_type` follows bookmark pagination across multiple `_find` responses.
  - selector, fields, per-page limit, and bookmark request bodies are asserted.
  - `limit` stops pagination once enough docs are yielded.
- `test_couchdb_build_cli.py`
  - selection passes projected-status and project/provider selectors to projection-state lookup.
  - selection uses scoped transcript-session selector.
  - positive `limit` stops session iteration early and does not require full corpus materialization.
  - authoritative projection state `_id` and already-projected skip semantics remain covered.
- Existing worker tests should keep passing.

## TDD Strategy

1. Add focused tests for HTTP bookmark pagination and build CLI selector/bounded iteration; confirm they fail on current code.
2. Implement in-memory and HTTP store read seam changes.
3. Update build CLI selection to use selector-aware projected-state lookup and session iterator.
4. Run focused tests, then broader worker tests.
5. Run requested review agents and fold in actionable simplification/architecture feedback.

## Milestones

- M1: Spec lock
  - Done: `requirements.md` and `design.md` exist under `docs/specs/2026-07-05-couchdb-session-memory-pagination/`.
- M2: Pagination seam
  - Done: HTTP and in-memory stores support selector-aware bookmark pagination while preserving `find_by_type` compatibility.
- M3: Build selection hardening
  - Done: `couchdb-session-memory-build` applies DB-side projected-state filtering, project/provider scoping, authoritative `_id` checks, and bounded session iteration.
- M4: Review and verification
  - Done: focused tests, worker tests, root checks, and requested subagent reviews complete.

## Design Self-Review

- 기존 `find_by_type` caller compatibility를 유지한다.
- `--limit`을 full corpus materialization 뒤에 적용하지 않는다.
- projected state authority는 `projection_status`만이 아니라 deterministic `_id`로 계속 검증한다.
- live runtime mutation, deployment, credential handling은 추가하지 않는다.
- HTTP와 InMemory semantics를 같은 tests로 닫는다.

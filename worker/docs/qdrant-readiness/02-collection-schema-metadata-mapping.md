# 02. Qdrant collection / schema / metadata filter 매핑 설계

RAGFlow는 dataset 단위로 격리되고 parse 시점에 server-side로 chunk·embed한다.
Qdrant로 옮기면 collection/payload/vector를 **worker가 직접 소유**한다. 이 문서는
교체 전에 확정해야 할 schema/metadata 결정을 정리한다. 현재 PoC
(`QdrantDoclingMirrorAdapter`)의 선택을 출발점으로, production cutover에 필요한
보강을 명시한다.

## 0. 적재 범위 (scope) — 무엇을 미러링하는가

> 정본은 기존 spec이다. 본 절은 그것을 재기술할 뿐이다:
> `docs/specs/2026-06-21-qdrant-docling-searchable-mirror/{requirements,design,milestones}.md`,
> `specs/recall-cutover/{requirements,design}.md`.

Qdrant가 대체하는 것은 RAGFlow의 **vector retrieval(searchable mirror)**이다. 따라서
적재 대상 = **현재 recall surface가 RAGFlow retrieval로 검색하는 dataset**이다.

- `recall-cutover/requirements.md` Q1(완료, RC1–RC6 done): **컷오버 후 normal
  recall의 권위 surface는 RAGFlow `session-memory`다.** transcript-memory는 은퇴
  ·삭제됐고, recall 진입점은 session-memory로 재배선됐다(RC3 done).
- 따라서 **session-memory가 Qdrant 미러의 1차 적재 대상이다.** session recap도
  session-memory dataset(`ragflow-session-memory` target_profile)으로 흘러 같은
  surface에 들어간다.
- brain.query에서 mirror hit은 **archive/evidence lane**으로만 쓰이고
  current/accepted MemoryCard recall은 local ledger precedence가 승리한다
  (qdrant design.md Audit Summary). 즉 미러는 후보 recall이지 권위가 아니다 —
  적재는 하되 결과는 항상 ledger/CouchDB join을 거친다.
- **현재 코드 상태(검증됨)**: `mcp_server.KnowledgeSearchService.brain_query`의
  권위 read model은 **ledger**(`LegacyLedgerBrainReadModel`)이고, RAGFlow
  `retrieve` lane은 `dataset_ids`가 주어질 때만 켜지는 **옵션**이다
  (`ragflow_search = self._brain_query_ragflow_search if self.dataset_ids else None`).
  `cli.py`의 `--state-db-recall` / `--ragflow-direct-recall` 플래그는 현재
  **파싱만 하고 버려지는 no-op**이라 recall 경로가 더는 그걸로 분기하지 않는다.
- **결론(이 readiness의 운용 기본값)**: recall은 **ledger-first**이며 RAGFlow
  벡터 retrieval은 launch에 `--dataset-id`가 있을 때만 도는 archive/evidence
  보조 lane이다. 따라서 **session-memory의 권위 recall은 ledger이고, Qdrant
  searchable mirror의 역할은 archive/evidence 후보 lane으로 한정**한다. recall-
  cutover(RC done)에서 session-memory가 RAGFlow retrieval surface였던 것은 그
  시점 상태이고, 이후 ledger-first로 옮겨졌다.
- **유일하게 남은 런타임 확인**: 라이브 recall mcp-stdio가 `--dataset-id` 없이
  떠 있으면 RAGFlow 벡터 recall은 사실상 미사용이다. 이 launch 인자는 repo 밖
  operator config(redacted)라 코드로는 못 박지 못한다 — 런타임 점검 사항이다.
- §1.2의 "14 logical role"은 *물리 보관* 기준이지 *미러 적재 범위*가 아니다.

## 1. collection 전략

### 1.1 현재 PoC

- 단일 collection `neurons_searchable_mirror_poc`에 모든 profile을 담고,
  `target_profile`을 payload filter로 분리(`_target_profile_filter`).
- vector size = `DEFAULT_VECTOR_SIZE=64`(HashEmbeddingProvider 기본), distance =
  `Cosine`. 둘 다 PoC 값이며 collection 생성 시 고정된다.

### 1.2 production 결정 사항

| 항목 | 결정 | 근거 |
| --- | --- | --- |
| collection 수 | **단일 collection + payload filter**를 v1 기본으로, `privacy_class` 격리가 정책상 필요하면 privacy 단위 분리를 옵션으로 | RAGFlow는 14개 logical role(`dataset_contract.py`)을 dataset로 분리하지만, Qdrant payload index로 동등 필터 가능. multi-tenant/접근정책 격리가 hard 요구면 collection 분리 |
| collection 이름 | PoC 이름 재사용 금지. 새 이름 + embedding model/version 접미 | vector size/distance는 collection 재생성 없이는 못 바꾼다 → 모델 바뀌면 새 collection |
| vector size | embedding model 확정 후 고정(예: MiniLM=384, bge-m3=1024) | [03 문서](03-embedding-ownership-and-lifecycle.md) |
| distance | Cosine 유지(정규화 임베딩 가정), 모델별 재평가 | PoC가 Cosine. dot/euclid는 모델 특성에 따라 재검토 |
| logical→collection 매핑 authority | ledger에 `qdrant_collections` 레지스트리 신설(아래 4절) | 현재 ragflow_datasets만 있음 |

## 2. point id (natural key) 매핑

- point id = `point_id_for_natural_key(target_profile, idempotency_key, content_hash)`
  = `uuid5(NAMESPACE_URL, "neurons:qdrant_docling:{target_profile}\n{idempotency_key}\n{content_hash}")`.
- 동일 natural key 재upsert는 같은 point로 떨어져 **암묵적 dedup/overwrite**가 된다.
  RAGFlow가 `update_metadata`로 meta_fields에 content_hash+idempotency_key를 심어
  list_documents 스캔으로 dedup하던 것을, Qdrant는 O(1) point 존재 확인으로 대체한다.
- `find_by_natural_key`는 point 존재 후 payload의 `target_profile` /
  `idempotency_key` / `content_hash` 3개가 모두 일치할 때만 handle을 돌려준다
  (false-dedup 방지). 이 정확성 체크는 이미 구현돼 있다.

## 3. payload schema와 metadata filter 매핑

### 3.1 현재 PoC payload (`_payload_for_document`)

top-level: `authority`, `backend`, `target_profile`, `document_kind`,
`artifact_kind`, `content_hash`, `idempotency_key`, `source_namespace`,
`source_alias`, `privacy_class`, `content_type`, `redaction_version`, `text`(전체
redacted markdown), `summary`(앞 512자), `metadata`(RagReadyDocument.metadata
중첩 dict). 모든 값은 `public_safe_text`/`ensure_public_safe` redaction을 통과한다.

### 3.2 RAGFlow filter parity 요구

`ragflow_client.retrieve`/`transcript_memory_records_from_ragflow` 등이 실제로 거는
meta_fields filter는 다음이다: `result_type`(또는 `type`), `project`, `provider`,
`session_id_hash`, `domain`. 그런데 PoC는 이들을 **중첩 `metadata` dict 안에만**
넣어 둔다(top-level 아님). cutover 전에 다음을 결정·반영해야 한다.

| RAGFlow filter key | Qdrant payload 위치(현재) | production 권고 |
| --- | --- | --- |
| `result_type` / `type` | `metadata.result_type` (불일치 명명) | top-level `result_type`로 승격, `document_kind`와 별도 보존 |
| `project` | `metadata.project` | top-level `project`로 승격 + index |
| `provider` | `metadata.provider` | top-level `provider`로 승격 + index |
| `session_id_hash` | `metadata.session_id_hash` | top-level 승격 + index |
| `domain` | (없음) | 필요 시 top-level `domain` 추가 |
| `target_profile` | top-level ✅ | index 선언 |
| `privacy_class` | top-level ✅ (index 없음) | **필수 filter** → index 선언 |
| `content_hash`/`idempotency_key` | top-level ✅ | dedup 조회용 index |

> 명명 일관성: RAGFlow는 `result_type`과 `type`을 혼용한다
> (`meta.get("result_type") or meta.get("type") or meta.get("kind")`). Qdrant는
> **`result_type` 하나를 canonical**로 정하고, 매핑은 ingest 시
> `metadata.result_type or metadata.type or document_kind` 순으로 해소한다.

### 3.3 payload index 선언 (현재 공백)

PoC는 어떤 `create_payload_index`도 호출하지 않는다. collection 생성 시 아래 필드를
index로 선언해야 full-scan filter를 피한다:

- keyword index: `target_profile`, `privacy_class`, `result_type`, `project`,
  `provider`, `session_id_hash`, `content_hash`, `idempotency_key`,
  `document_kind`, `redaction_version`.

## 4. ledger collection 레지스트리 (신설 필요)

RAGFlow는 ledger `ragflow_datasets`(컬럼: `logical_name`, `dataset_id`,
`embedding_model`, `chunk_method`, `metadata_policy_version`, `contract_version`,
`enabled`, `disabled_at`)로 logical→physical과 enable 상태를 관리한다. Qdrant에는
대응물이 없다. cutover 전 ledger에 다음을 신설할 것을 권고한다(additive migration):

```
qdrant_collections(
  logical_name TEXT PK,      -- target_profile 또는 logical role
  collection   TEXT,         -- 물리 collection 이름
  embedding_model TEXT,
  vector_size  INTEGER,
  distance     TEXT,         -- Cosine 등
  payload_index_version TEXT,
  enabled      INTEGER,
  disabled_at  TEXT
)
```

이 레지스트리는 collection health, enable 상태, logical-name→collection 매핑의
authority가 된다(현재 `dataset_contract.py`의 14 role과 1:1 매핑 가능).

## 5. mirror hit의 authority 규약

`SearchableMirrorHit`는 항상 `canonical_resolution_required=True`,
`authority_join_status="not_checked"`로 표시된다. 즉 **Qdrant 결과는 절대
authoritative하지 않다**. RAGFlow 경로가 `ledger.authorize_document`로 모든 후보를
검증하듯, Qdrant hit도 product 사용 전 ledger/CouchDB join이 강제돼야 한다. 이
ledger join은 아직 미구현이며, read cutover(문서 04, Stage 4)의 진입 조건이다.

## 6. 미해결 schema 결정 체크리스트

- [ ] 단일 vs privacy-class 분리 collection (정책 입력 필요)
- [ ] embedding model → vector size/distance 확정 (문서 03)
- [ ] top-level 승격 필드 집합과 payload index 버전 확정
- [ ] `result_type` canonical 명명과 ingest 매핑 규칙 확정
- [ ] ledger `qdrant_collections` 레지스트리 migration 설계
- [ ] Qdrant hit ledger-join 구현 (authority 규약 충족)
- [ ] Docling 정규화의 production 필수/옵션 여부와 default extra 포함 여부

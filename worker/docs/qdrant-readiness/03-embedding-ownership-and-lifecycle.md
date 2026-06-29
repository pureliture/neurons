# 03. embedding ownership과 ingestion/update/delete lifecycle

## 1. embedding ownership 전환 (가장 중요한 변화)

| | RetiredIndexBridge | Qdrant |
| --- | --- | --- |
| embedding 주체 | **RetiredIndexBridge server**가 `request_parse` 시점에 chunk+embed | **worker**가 upsert 전에 vector를 직접 생성 |
| 모델 위치 | RetiredIndexBridge 내부(dataset의 embedding_model) | `EmbeddingProvider` seam(worker 소유) |
| worker가 보내는 것 | raw markdown 본문 | `[float]` vector + payload |

즉 Qdrant 교체는 단순 store 교체가 아니라 **embedding ownership을 RetiredIndexBridge에서
worker로 가져오는 일**이다. 현재 PoC의 `HashEmbeddingProvider`는 토큰 UUID5 해시를
64차원에 분산한 **결정적 placeholder**다 — dedup smoke에는 쓰지만 **semantic
검색 품질이 전혀 없다**. recall parity(문서 04)는 이 provider로는 원천적으로
달성 불가하다.

### 1.1 provider 결정 기준

**새 모델 결정 아님 — 이미 있는 OpenAI-compatible embedder/reranker 재사용.**

- repo에는 이미 OpenAI-compatible embedding 구성이 있다:
  `llm_brain_core/graphiti_adapter.py`의 `GraphitiNeo4jConfig`가
  `embedding_model`(`LLM_BRAIN_EMBEDDING_MODEL`/`EMBEDDING_MODEL`),
  `embedding_base_url`(`LLM_BRAIN_EMBEDDING_BASE_URL`/`OPENAI_BASE_URL`),
  `embedding_api_key`, `embedding_dim`(기본 1024)을 env에서 읽고,
  `graphiti_core.embedder`의 OpenAI embedder + `graphiti_core.cross_encoder`의
  OpenAI-compatible reranker(cross-encoder)를 쓴다.
- Qdrant `EmbeddingProvider`는 **이 동일 endpoint를 재사용**한다 — 같은
  `OPENAI_BASE_URL`/`EMBEDDING_MODEL`로 `/v1/embeddings` 호출. vector size =
  `embedding_dim`(기본 1024), distance = Cosine. rerank가 필요하면 동일하게 이미
  있는 OpenAI-compatible reranker를 mirror 후보 재정렬에 쓴다(RetiredIndexBridge `rerank_id`
  자리 대체).
- Gemini Flash 계열은 아니다(임베딩 모델 + cross-encoder reranker이지 Flash chat
  모델이 아니다). 별도 secret 신설 없이 기존 `LLM_BRAIN_*`/`OPENAI_*` env를 쓴다.
- 따라서 spec의 "embedding model 미결정"은 운영상 닫힌다: **기존 OpenAI-compatible
  embedder 채택, dim 1024.** 모델/dim 변경 시에만 collection 재생성(문서 02).

### 1.2 seam (이미 존재)

`EmbeddingProvider` protocol(`size` 프로퍼티 + `embed(text)->[float]`)이 이미
adapter 주입점이다. production provider는 이 protocol만 구현하면 되고,
`HashEmbeddingProvider`는 test 전용으로 남긴다. adapter는 이미 vector 길이 ==
`provider.size`를 강제(불일치 시 `ValueError`)하므로, 잘못된 차원은 fail-closed다.

### 1.3 결정성/재현성

- 같은 본문은 같은 vector를 내야 read-compare 재현이 쉽다(배치 정렬, 버전 고정).
- embedding model 버전을 payload(`redaction_version`처럼 `embedding_version`)와
  ledger `qdrant_collections.embedding_model`에 기록해 drift를 추적한다.

## 2. ingestion lifecycle

RetiredIndexBridge는 upload → update_metadata → request_parse → (server embed) → poll DONE의
다단계였다. Qdrant는 단일 atomic upsert로 압축된다.

```
normalize(Docling/Passthrough) → _validate_mirror_text(leak 검사+redact)
  → EmbeddingProvider.embed → payload 구성(redaction) → client.upsert(point_id)
```

- point_id가 natural key이므로 upload+metadata 2단계가 1단계로 합쳐진다
  (RetiredIndexBridge는 meta_fields를 별도 PATCH했지만 Qdrant는 payload에 원자적으로 포함).
- `submit_document`은 즉시 `IndexStatus.INDEXED`를 반환한다 — RetiredIndexBridge의 비동기
  parse(UNSTART→RUNNING→DONE)와 달리 upsert는 동기 완료다. 따라서
  `mark_index_done_*` 류 status reconciler는 Qdrant 경로에서 불필요해진다(문서
  01의 reconciler stuck blocker가 자연 해소).
- leak 검사(`public_ingress_leak_violations`)와 secret-like metadata 거부
  (`_assert_no_secret_like_metadata_tree`)가 upsert 전에 fail-closed로 동작한다.

## 3. update(-in-place) lifecycle

- RetiredIndexBridge에는 update-in-place가 **없다**(본문 변경=새 문서 upload).
- Qdrant에서 **같은 natural key(=같은 content_hash) 재ingest**는 같은 point_id로
  overwrite되는 idempotent upsert다.
- **본문이 바뀌면 content_hash가 바뀌어 새 point_id가 생긴다.** 즉 supersede는
  "새 point upsert + 옛 point delete"의 2-step이다(자동 in-place 갱신 아님). 이
  delete가 4절의 seam을 필요로 한다.

## 4. delete / disable lifecycle (이번에 추가한 seam)

### 4.1 기존 공백

- backend-neutral `RetiredIndexBridgeAdapter` protocol에는 **delete가 없다**. RetiredIndexBridge
  document delete는 adapter를 우회해 `gc_safety_auditor.hard_delete_documents`
  단일 chokepoint(→ `index.delete_documents`)를 지난다.
- RetiredIndexBridge에는 `disable_document`(soft, enable=0)가 있어 hard delete 전 가시성
  차단/rollback 표적으로 쓰인다. **Qdrant point에는 enable/disable 플래그가 없다.**

### 4.2 이번 readiness에서 추가한 draft seam (`qdrant_docling_mirror.py`)

- `MirrorDeletionResult{status, document_ref, existed}` — status는
  `deleted`(존재→제거) / `absent`(미존재, 안전 no-op) / `collection_mismatch`.
- `MirrorDeletionCapable` protocol — `RetiredIndexBridgeAdapter` 위에 얹는 **optional**
  delete 능력. neutral 경계(`retired_index_bridge.py`)는 건드리지 않고
  `qdrant_docling_mirror.py`에 두었다(boundary regression 최소화). 다음 단계에서
  retired_index_bridge로 승격할지는 별도 결정.
- `QdrantDoclingMirrorAdapter.delete_document(handle, missing_ok=True)` —
  collection 일치 확인 → 존재 확인 → `client.delete(points_selector)`. idempotent.
- `delete_by_natural_key(target_profile, idempotency_key, content_hash)` —
  결정적 point_id 해소 후 delete.
- **어떤 live GC route에도 연결되지 않았다.** GC가 mirror에 대해 target할 지점의
  초안일 뿐이다.

### 4.3 disable 메커니즘 — 두 layer (현황 정리)

Qdrant에는 native per-point disable이 없다. 두 layer가 있고, **M3에서 실제 구현된
것은 (B) collection-level뿐**이다.

- **(A) per-point soft-disable** — payload `enabled` bool + 모든 read filter에
  `enabled=true` 강제. 단일 stale point(supersede의 "옛 point 내림")용. **M3에서
  미구현, M9로 연기.** 그때까지 **supersede의 옛 point 정리는 hard-delete-only**
  (delete seam). RetiredIndexBridge의 `disable_document`(per-doc soft)에 대응하는 per-point
  소프트 레버는 아직 없다.
- **(B) collection-level enable** — ledger `qdrant_collections.enabled` /
  `disable_qdrant_collection`, fail-closed(`_qdrant_collection_is_enabled`). **M3
  구현됨.** collection 단위 rollback/quarantine 레버다. 단 현재 어떤 read/write
  경로도 이를 consult하지 않는다(enforcement는 M8 read-cutover에서 배선; 그 전까진
  intended state 기록일 뿐).

요약: M3는 collection-level enable(B)을 authority로 두고, per-point soft-disable(A)은
M9로 연기한다. 그 사이 supersede/정리는 delete seam(hard delete)로만 한다.
requirements Q4의 "payload enabled 필드" 결정은 M9 항목으로 미룬다.

### 4.4 GC chokepoint 정합

production GC가 Qdrant를 지우려면 `hard_delete_documents` 수준의 **단일
chokepoint**를 Qdrant용으로 확장하거나 병렬 추가해야 한다(현재 부재). 이번 delete
seam이 그 chokepoint가 호출할 adapter-level 표적이다. dry-run/coverage proof/
retention window/backup/rollback/recall regression gate 분리 보고 원칙(AGENTS.md
GC 규약)은 Qdrant에도 그대로 적용한다.

## 5. lifecycle 요약 표

| 단계 | RetiredIndexBridge | Qdrant(목표) | 현황 |
| --- | --- | --- | --- |
| create | upload+meta+parse(async embed) | upsert(point_id, worker embed) | ✅ PoC |
| dedup | meta_fields 스캔 | point 존재 + 3-key 일치 | ✅ PoC |
| update-in-place | 없음(재upload) | 없음(같은 hash=overwrite, 새 hash=새 point) | ✅ 의미 동일 |
| supersede | 새 upload + (disable/delete 옛) | 새 upsert + 옛 disable/delete | ⚠️ delete seam만 초안 |
| disable(soft) | `disable_document`(per-doc) | (A) per-point payload `enabled` = M9 연기 / (B) collection-level `qdrant_collections.enabled` = M3 구현(미배선, M8 enforcement) | ⚠️ (B)만 구현 |
| delete(hard) | `delete_documents`(chokepoint) | `delete_document` seam | ⚠️ seam 초안, chokepoint 미연결 |
| status | poll DONE/FAIL | upsert 동기 INDEXED | ✅ reconciler 불필요화 |

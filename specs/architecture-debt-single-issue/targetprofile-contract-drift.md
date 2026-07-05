# TargetProfile Contract Drift First Pass

## 상태

- issue: #40
- status: first-pass done
- scope: Java `TargetProfileRegistry` / retired index bridge adapter / Python shadow-worker env resolver / compose env coverage
- live runtime mutation: 없음

## 확인한 drift

1. Java `TargetProfileRegistry.DEFAULT`와 `application.yml`은 `index-session-memory`와 `index-session-summary`를 서로 다른 logical dataset role로 선언한다.
2. `RetiredIndexBridgeTargetAdapter`는 `index-session-memory` dataset id가 비어 있을 때 `index-session-summary` dataset id로 fallback했다.
3. Python `env_profile_dataset_resolver`는 `index-session-memory`를 `INDEX_SESSION_MEMORY_DATASET_ID`로 계산했지만, compose와 `.env.example`의 공개 계약은 `RETIRED_INDEX_BRIDGE_SESSION_MEMORY_DATASET_ID`다.

## 적용한 guard

- Java adapter는 `index-session-memory`가 자기 dataset id만 사용하도록 변경했다. session-memory id가 비면 session-summary로 대체하지 않고 fail-closed 된다.
- Python shadow worker resolver는 `index-*` target profile을 `RETIRED_INDEX_BRIDGE_<ROLE>_DATASET_ID` env key로 매핑한다.
- Python characterization test는 Java `application.yml`의 7개 target profile을 읽고, resolver mapping과 `compose.yaml` / `.env.example` env coverage를 함께 검증한다.

## 검증

- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests "com.local.ragingressqueue.adapter.ext.retired_index_bridge.RetiredIndexBridgeTargetAdapterTest.sessionMemoryDatasetDoesNotFallbackToSessionSummaryDataset"`
  - RED: 기존 adapter가 session-summary fallback을 사용해 실패
  - GREEN: fallback 제거 후 통과
- `JAVA_HOME="$(/usr/libexec/java_home -v 25)" gradle test --tests "com.local.ragingressqueue.adapter.ext.retired_index_bridge.RetiredIndexBridgeTargetAdapterTest" --tests "com.local.ragingressqueue.ingest.domain.TargetProfileRegistryTest" --tests "com.local.ragingressqueue.ingest.domain.validation.IngestJobValidatorTest"`
  - 통과
- `cd worker && uv run pytest -q tests/test_shadow_worker.py -k env_profile_dataset_resolver`
  - 통과

## 남은 리스크

- 아직 shared schema artifact를 만들지는 않았다. 현재 guard는 Java `application.yml`과 compose/env, Python resolver를 cross-check하는 낮은 위험의 characterization layer다.
- `DocumentIndexTargetProfile`은 Java 7개 profile 전체를 모델링하지 않는다. 실제 필요가 확인되면 별도 milestone에서 Python target-profile value object를 확장해야 한다.
- 실제 RetiredIndexBridge/RAGFlow live delivery proof는 이 pass 범위가 아니다.

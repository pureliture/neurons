# ADR-0006: Dataset contract를 불변 외부 설정으로 분리

## Status
Accepted

## Context

`worker/lib/agent_knowledge/dataset_contract.py`는 logical dataset role, canonical name, 권한 성격, projection target 같은 계약을 코드 안에 고정한다. 이 구조는 런타임 안정성은 좋지만, dataset contract 변경이 항상 코드 변경과 재배포로 이어진다.

반대로 애플리케이션 내부에서 hot reload, polling, webhook listener를 직접 구현하면 runtime complexity와 cache invalidation 위험이 커진다. neurons의 dataset contract는 권한과 recall 품질에 영향을 주므로, 변경 가능성보다 검증 가능한 배포 경계를 우선해야 한다.

## Decision

- dataset contract 명세는 YAML/JSON 또는 Kubernetes ConfigMap 같은 외부 설정 artifact로 분리한다.
- 애플리케이션은 프로세스 시작 시 설정을 한 번 로드하고, 실행 중에는 같은 설정을 불변으로 취급한다.
- 설정 변경 반영은 애플리케이션 hot reload가 아니라 orchestration layer의 rolling update가 담당한다.
- 설정 artifact에는 secret, raw dataset id, raw document id를 직접 노출하지 않는 redaction rule을 둔다.
- k3s/Kubernetes migration이 끝나기 전에는 동일한 contract를 local/compose runtime에서 읽을 수 있는 fallback path를 둔다.

## Consequences

### Positive

- dataset policy 변경 경로가 코드 배포와 분리된다.
- hot reload 구현을 피하면서도 cloud-native rolling update 모델과 맞출 수 있다.
- contract artifact를 review 가능한 운영 evidence로 남길 수 있다.

### Negative

- 실제 무중단 설정 변경은 orchestration pipeline 준비가 필요하다.
- compose runtime과 k3s runtime이 공존하는 동안 설정 주입 경로를 명확히 관리해야 한다.

## Follow-up

- M3: 현재 `dataset_contract.py`의 역할과 권한 의미를 외부 설정 schema로 정리한다.
- M3: startup load, validation failure, redaction rule을 테스트한다.
- k3s migration: ConfigMap/rolling update 경로를 non-production target에서 먼저 검증한다.

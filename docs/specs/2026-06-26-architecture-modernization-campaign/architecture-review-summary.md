# Architecture Modernization 리뷰 요약

## 개요

- 날짜: 2026-06-26
- 목표: `neurons` 코드베이스에서 비대해진 persistence/runtime 경계를 확인하고, 안전하게 깊은 모듈로 이동할 수 있는 실행 campaign을 정리한다.

## 결정된 축

### 1. DB dialect isolation과 Repository pattern

`ledger.py` 안에 SQLite/PostgreSQL 의존성, 원시 SQL, 도메인 상태 전이가 강하게 결합되어 있다. 바로 대규모 분해를 시작하지 않고, 먼저 transaction failure safety를 characterization test로 고정한다. 이후 `UnitOfWork`와 domain repository port를 도입해 service layer가 DB 세부 구현에 직접 묶이지 않도록 만든다.

### 2. Dataset contract의 불변 외부 설정화

`dataset_contract.py`의 logical role과 권한 계약은 코드에서 외부 설정 artifact로 옮긴다. 단, 애플리케이션 내부 hot reload는 만들지 않는다. 프로세스 시작 시 한 번 로드하고, 변경 반영은 compose/k3s 같은 orchestration layer의 재시작 또는 rolling update가 담당한다.

### 3. `CurationService` transaction boundary

`CurationService.approve`는 여러 ledger write를 순차 호출하기 때문에 중간 실패 시 partial write 위험을 드러내기 좋은 첫 target이다. M1에서는 이 흐름을 private transaction seam으로 감싸고, rollback behavior를 테스트로 고정한다. 공개 `UnitOfWork` 전환은 M2 이후에 진행한다.

## 다음 작업

- ADR-0005를 기준으로 M1 transaction seam과 M2 repository extraction을 분리한다.
- ADR-0006을 기준으로 dataset contract 외부 설정 schema와 k3s/compose 주입 경로를 정리한다.
- 기존 public CLI/API와 DB behavior를 깨지 않는 regression gate를 유지한다.

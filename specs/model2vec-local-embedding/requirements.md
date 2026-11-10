# Neurons 로컬 Model2Vec 임베딩 전환 요구사항 (정본)

- **Status**: Canonical specification for Model2Vec local embedding transition (M1–M3 isolated implementation approved; M4–M5 production rollout/cutover requires separate operational approval)
- **Date**: 2026-11-01 KST
- **Target Repository**: `neurons` (Server/Brain Authority)
- **Base Commit**: `d8acb36` (`d8acb3686da12952dc5c1123da71877254d4378b`)
- **Canonical Spec Directory**: `specs/model2vec-local-embedding/`
  - 요구사항 정본: `specs/model2vec-local-embedding/requirements.md` (본 문서)
  - 상세 설계 정본: `specs/model2vec-local-embedding/design.md` (원본 초안 `design-v2.md` SHA-256 `fa9b4574cdc4811305f88eeeb13247608553eb2e15bcfa8c2416b05ceb2640cb` 기반)
  - 마일스톤 및 인계 정본: `specs/model2vec-local-embedding/milestones.md`
  - 심사 이력: `specs/model2vec-local-embedding/review.md`
- **선행 스펙과의 관계**: `specs/lbrain-architecture-rationalization/` (`requirements.md`, `design.md`)의 권위 경계(PostgreSQL SoT), Graph-first 조회 구조, MCP 2-Tier 계약, retired-vector-index 이관 이력은 그대로 유지한다. 단, `lbrain-architecture-rationalization` R1.2, AC1, design §3.1, §5.1의 Gemini 3,072차원 단일 임베딩 프로필 조항에 대해, 본 스펙은 기존 3,072차원 데이터(`lbrain-memory-gemini-embedding-2-v1`, `lbrain-memory-gemini-embedding-2-v2-bounded8k`)를 삭제·변조하지 않고 보존한 채 로컬 Model2Vec 256차원 프로필(`lbrain-memory-model2vec-256-v1`)을 가산적(additive)으로 도입·전환하는 정본 계약이다.

---

## 목표

Gemini 임베딩 사용료를 지속 지불하지 않고 Neurons의 기억 저장·검색 및 Graphiti 경로에 로컬 Model2Vec을 적용한다. 기존 기억 원문 및 권위 데이터는 보존한다. 임베딩 외 LLM 제공자 변경은 범위 밖이다.

## 확인된 사실

- 운영 `d8acb36` 기준 PG 카드·세션 embedding은 `halfvec(3072)`, 2026-11-01 감사 기준 ready 카드 238, ready 세션 8719. 수량은 진행 시 재조회한다.
- Mac M4에서 `potion-multilingual-128M` / `model2vec 0.10.0`을 합성 자료로 시험. 혼합 30초 직접 호출 약 7354건/초, 프로세스 최대 RSS 1.93GiB. 네트워크·Graphiti·실제 DB 시험 아님. 임베딩 결과 256차원.
- 해당 라이브러리 기본 길이 제한 512토큰: 해제 전에는 긴 입력의 뒷부분이 실제로 누락됐다. 길이 제한을 해제한 경우 뒷부분의 변화가 벡터에 반영됨. 이는 검색 품질 보증이 아니다.
- 합성 검색 10문항 중 top1 7건, top5 9건. 기존 Gemini 직접 비교 없음.
- 초기 작업 공간의 별도 검토 문서, 벤치마크 요약, 비용 감사는 배경 근거이며, 구현자와 검토자가 읽어야 하는 단일 정본은 `specs/model2vec-local-embedding/` 아래 문서들이다.

## 제품 요구사항

- **R1**: Neurons 레포에서 로컬 임베딩 모델을 설치·실행·설정·검증하는 코드, 의존성, 버전 고정, 배포 지침 및 테스트를 제공한다. 모델 가중치·자격증명은 Git에 저장하지 않는다. Neurons 레포만으로 설치 절차를 알 수 있어야 한다.
- **R2**: Mac에 상주하는 모델 서비스 1개를 Neurons의 실제 임베딩 소비자가 사용할 수 있게 한다. 서비스 위치/호스트 경로는 설정 가능해야 한다. 동일 서비스의 네트워크 인터페이스, endpoint와 응답 계약은 `specs/model2vec-local-embedding/design.md` D1을 따른다.
- **R3**: 카드 쓰기·검색, 세션 투영·검색, outbox, Graphiti 쓰기·검색 및 실제 활성 bulk 경로를 식별해 모두 새 프로필과 일관되게 처리한다. 활성 여부 미확인 기능은 확인 후 분류하며 은퇴한 경로를 재활성화하지 않는다.
- **R4**: 256차원 새 모델과 3072차원 기존 모델의 벡터·인덱스·캐시를 서로 혼합하지 않는다. 내용 hash·원본 revision·모델 revision·전처리·차원·입력 역할을 명확히 묶는다. 데이터 변경 도중 원본 변경 및 재시도는 멱등 처리한다.
- **R5**: 기존 원문·권위 데이터·기존 벡터를 보존한 상태에서 새 벡터를 생성한다. 변경 중 누락·실패·대기 개수와 검색 가능 범위를 관찰할 수 있어야 하며, 원본과 동일성을 확인한다. 원본 삭제/기존 벡터 폐기는 범위 밖이다.
- **R6**: 실제 API 경로를 통한 검색, 긴 본문 뒷부분, 한국어 의미·부정문·과거/현재 결정, 코드 심볼, 잘못된 엔티티 병합을 검증한다. 적합성의 수치 기준과 실제 정답 집합은 운영 전환 승인 전 확정한다. Gemini가 정답이라는 가정과 신규 유료 API 호출은 금지한다.
- **R7**: 모델 unavailable/timeout/과부하 때 유료 Gemini로 자동 전환하지 않는다. 사용자에게 드러나는 오류/대기·재시도·백로그 정책은 호출 경로별로 실측 후 확정한다. 데이터 손실이나 무제한 재시도를 허용하지 않는다.
- **R8**: 서비스는 Tailscale/사설 경로만 사용하고 인터넷에 직접 공개하지 않는다. 새 IAM 권한, 클라우드 유료 서비스, 새로운 시크릿 값 생성은 기본 범위 밖이다.
- **R9**: 운영 비용 0에 대한 검증은 라우팅 구성과 실제 사용량/브리지 장부를 구분하고 모든 임베딩 소비자에 대해 수행한다. 임베딩 외 모델 과금까지 0이라는 의미는 아니다.

## 기존 정본(`specs/lbrain-architecture-rationalization/`)과의 관계 및 정본 읽기 규칙

1. **보존되는 기존 계약**:
   - `specs/lbrain-architecture-rationalization/requirements.md` 및 `design.md`가 정의한 PostgreSQL 권위 저장소(R1.1, R1.3, R1.7), Graphiti/Neo4j 유지 및 Graph-first 조회(R2), MCP 2-Tier 및 3KB slim serializer(R3), Thin client 및 재구축 가능성(R4), 기존 retired-vector-index 이관 이력(§3)은 삭제하거나 무효화하지 않는다.
2. **임베딩 프로필 조항의 관계**:
   - 기존 `lbrain-architecture-rationalization` R1.2, AC1, design §3.1, §5.1은 Gemini 3,072차원 프로필(`lbrain-memory-gemini-embedding-2-v1`, `halfvec(3072)`)을 초기 정본으로 정의했다.
   - 본 스펙(`specs/model2vec-local-embedding/`)은 기존 `memory_cards.embedding`, `session_memory_chunks.embedding`, `embedding_outbox`, Neo4j `name_embedding`/`fact_embedding`, representation receipt v1/v2를 그대로 보존한 상태에서, 로컬 Model2Vec 256차원 프로필(`lbrain-memory-model2vec-256-v1`, `local-model2vec-256-v1`)을 별도 테이블·속성·큐·receipt v3로 가산(additive) 도입한다.
3. **구현자·검토자 정본 기준**:
   - 로컬 Model2Vec 임베딩 서비스(M1), 프로필 분리 저장·검색·outbox·receipt v3·Graphiti 0.30.1 vendored fork(M2), 백필·전환 게이트·품질 검증(M3)의 구현과 검토는 반드시 `specs/model2vec-local-embedding/requirements.md`, `design.md`, `milestones.md`를 정본으로 읽는다.
   - 권위 승인 모델, MCP 2-Tier 응답 크기 제한, Graph-first 라우팅의 일반 원칙은 `specs/lbrain-architecture-rationalization/`을 함께 참조한다.

## 비범위 / 별도 승인

운영 Mac 서비스 자동 등록(`launchd`), 운영 DB·Neo4j·Kubernetes 리소스 변경, 실데이터 재색인, 공개 인그레스/방화벽 변경, 시크릿/IAM 변경, 배포, 보호 브랜치 push/merge, 유료 API 호출/비용 발생, 기존 인덱스/데이터/PVC 삭제는 M1–M3 격리 구현 범위에 포함되지 않으며 별도 운영 승인(M4–M5) 대상이다. 롤백 시에도 승인 없는 Gemini 자동 호출 재개는 금지한다.

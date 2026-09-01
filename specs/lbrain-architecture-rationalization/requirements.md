# LBrain Architecture Rationalization: Requirements & Problem Statement

- **Status**: Draft / Proposed RFC
- **Date**: 2026-09-01
- **Target Repository**: `neurons` (Server/Brain Authority)
- **Review Target**: Multi-Agent Architecture Review & Peer AI Review

---

## 1. Executive Summary

`LBrain` (LLM-Brain) 시스템은 에이전트 메모리 및 아키텍처 결정 보존을 목표로 발전해왔으나, 점진적 기능 확장의 결과로 **4중 분산 데이터 저장소**, **MCP 도구 과다 노출 (34개)**, **극심한 응답 페이로드 비대화 (30~50KB/호출)**라는 심각한 아키텍처적 부채(Architectural Debt)를 안고 있다.

본 RFC는 다음 세 가지 핵심 방향으로 시스템을 정격화(Rationalize)하고 간소화하는 요구사항을 정의한다:
1. **데이터 계층 단일화**: Neo4j/Graphiti 및 CouchDB, Qdrant를 단계적으로 정리하고, **`PostgreSQL` (`pgvector` + `JSONB` + 재귀 CTE) 단일 데이터 엔진**으로 통합한다.
2. **MCP 인터페이스 2-Tier 분리**: 코딩 에이전트에게 필요한 **3~4개 핵심 도구**와 내부 운영/관리자/CI 도구를 엄격히 격리한다.
3. **Slim Serializer 도입**: 호출당 1KB 미만의 에이전트 전용 미니멀 페이로드를 제공하여 토큰 낭비와 컨텍스트 잠식을 해소한다.

---

## 2. Current State & Problem Statement (As-Is)

### 2.1. 4중 저장소 분산 및 운영 복잡도 (Storage Fragmentation)
- **현재 구조**: 
  - `PostgreSQL/SQLite`: 관계형 Ledger (36개 테이블)
  - `CouchDB`: 트랜스크립트 원본 및 6개 문서 계열
  - `Qdrant`: 세션 메모리 및 문서 벡터 검색
  - `Neo4j / Graphiti`: 시간축 지식 그래프
- **문제점**:
  - 소규모/팀 단위 에이전트 메모리 도메인에 비해 인프라 유지보수 및 k3s 리소스 소모가 과도함.
  - 저장소 간 데이터 동기화 지연 및 이중 쓰기(Dual-Write) 정합성 불일치 리스크 존재.

### 2.2. Graphiti / Neo4j의 실질적 유효성 상실 (The Graph RAG Dilemma)
- **관찰된 사실**:
  - 현재 런타임에서 Graphiti는 기본 비활성화(dual-gated) 상태이며, 호출 시 `status: degraded (edge_provenance_unresolved)`로 반환됨.
  - LLM 기반 엔티티/엣지 추출은 쓰기 지연(최대 300초)과 토큰 비용이 막대함.
  - 추출된 비정형 관계의 품질이 낮고, 원본 추적(Provenance)이 불확실하여 시스템의 결정론적 증거 철학(SHA-256 Hash)과 충돌함.
  - 시스템의 핵심 관계(결정 간 대체 `supersedes`, 증거 연결 `evidence_hashes`)는 이미 PostgreSQL Ledger가 0.1ms 수준으로 완벽하게 처리 중임.

### 2.3. MCP 도구 과다 노출 (Tool Proliferation, 34개 툴)
- **관찰된 사실**:
  - `agent_memory` MCP 서버 하나에 34개의 도구가 단일 네임스페이스로 노출됨.
  - 에이전트가 호출해서는 안 되는 관리자 승인 도구(`memory_candidate_approve` 등)와 CI/CD 전용 프로브(`brain_permission_sensitive_audit_probe` 등)가 일반 코딩 에이전트 프롬프트에 포함됨.
- **문제점**:
  - 도구 스키마만으로 매 턴 수천 토큰의 시스템 프롬프트 오버헤드 발생.
  - 유사 검색 도구 난립(`brain.query`, `brain_memory_search`, `knowledge.search`)으로 에이전트의 도구 라우팅 혼선 및 실패율 증가.

### 2.4. 응답 페이로드 비대화 (Fat Response Anti-Pattern)
- **관찰된 사실**:
  - `brain_context_resolve`: 호출당 **31.8 KB** (~8,000 토큰).
  - `compact` 모드에서도 **30.3 KB**로 거의 줄어들지 않음.
- **원인**:
  - 비어 있는 7~8개 레인 스키마(`lanes: {accepted_current: [], ...}`) 반복 출력.
  - 동일한 선호도/규칙이 3개 이상의 하위 객체(`authority`, `object_packs`, `agent_context_product`)에 중복 복제.
  - 내부 거버넌스 문구(`eval_assertions`, `route_spec`, 해시 리스트) 과다 포함.
- **문제점**:
  - LLM 컨텍스트 윈도우의 급격한 소모, 집중도(Attention) 분산, 지연시간 및 API 비용 폭증.

---

## 3. Goals & Non-Goals

### 3.1. Goals (목표)
- **G1 (단일 DB 수렴)**: PostgreSQL 1개로 상태 관리, 관계형 Ledger, 벡터 시맨틱 검색(`pgvector`), 계층 트리 탐색을 통합한다.
- **G2 (도구 축약)**: 에이전트 노출 MCP 도구를 3~4개 핵심 도구(`brain.query`, `brain_context_resolve`, `memory_candidate_create`)로 축소한다.
- **G3 (페이로드 경량화)**: 기본 에이전트 컨텍스트 응답 크기를 30KB ➡️ **1KB 미만**으로 95% 이상 감축한다.
- **G4 (결정론적 일관성)**: 단일 트랜잭션 쿼리로 메타데이터 필터링과 벡터 유사도 검색을 원자적으로 수행한다 (Dual-Write 제거).

### 3.2. Non-Goals (비목표)
- 수천만 건 이상의 초대규모 빅데이터 분산 벡터 클러스터링 구축 (현재 도메인은 수만 건 규모의 에이전트 메모리).
- LLM 기반 비정형 온톨로지 자동 추출 유지 (필요 시 코드 AST 및 정적 분석 도구 `Graphify` 활용).
- 기존 Ledger의 엄격한 거버넌스(Human Approval, Redaction, SHA-256 Audit) 모델 완화 (오히려 단순화하여 거버넌스 강화).

---

## 4. Acceptance Criteria & Evaluation Rubric

### AC1. Storage Footprint & Dependencies
- [ ] Neo4j 및 CouchDB 컨테이너 의존성이 제거되거나 완전히 선택적(Optional)으로 격리되어야 한다.
- [ ] PostgreSQL 단일 인스턴스에서 `pgvector` 기반 HNSW 인덱스로 시맨틱 검색이 10ms 이내에 수행되어야 한다.

### AC2. MCP Tool Clarity & Efficiency
- [ ] 에이전트용 MCP 서버(`agent_memory`)는 최대 4개의 도구만 노출해야 한다.
- [ ] 관리자/승인/CI 도구는 분리된 내부 서피스(`agent_memory_admin` 또는 CLI)로 이동해야 한다.

### AC3. Response Payload Diet
- [ ] `brain_context_resolve`의 기본 경량 모드 응답은 **1.5KB / 400 토큰 이하**여야 한다.
- [ ] 응답에는 빈 배열 스키마나 내부 거버넌스 assertion 문구가 포함되지 않아야 한다.

### AC4. Zero Regression on Recall Quality
- [ ] 기존 Qdrant 및 Ledger 기반의 주요 결정 및 세션 메모리 회상(Recall) 정확도가 100% 동등해야 한다.

---

## 5. Review Questions for Peer AI / Architects

1. **Postgres pgvector의 적합성**: 수만 건 규모의 세션 메모리/결정 카드 환경에서 PostgreSQL HNSW 인덱스가 Qdrant 대비 성능, 운영 비용, 트랜잭션 일관성 면에서 확실한 우위를 가지는가?
2. **Graphiti/Neo4j 제거의 리스크**: 코드 정적 분석(`Graphify`)과 RDBMS 외래키 체계가 이미 존재하는 상황에서, Graphiti/Neo4j를 완전히 퇴역(Deprecate)시킬 때 발생할 수 있는 잠재적 기능 공백이 있는가?
3. **MCP 2-Tier 분리 전략**: 도구를 Agent Surface와 Admin Control Plane으로 분리하는 구조가 에이전트의 안정성과 프롬프트 효율성을 극대화하는 표준 패턴으로 적절한가?

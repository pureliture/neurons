# 실행 마일스톤과 인계 (정본)

- **Status**: M0 정본 스펙 등재 완료, M1–M3 격리 환경 구현 진행, M4–M5 운영 승인 대기
- **Date**: 2026-11-01 KST
- **Canonical Spec Directory**: `specs/model2vec-local-embedding/`
  - `requirements.md`: 제품 요구사항 및 선행 스펙과의 관계
  - `design.md`: 상세 설계 정본 (`design-v2.md` SHA-256 `fa9b4574cdc4811305f88eeeb13247608553eb2e15bcfa8c2416b05ceb2640cb` 기반)
  - `review.md`: 독립 설계 심사 이력

---

## M0 — 스펙 정합화 및 정본 등재

- 담당: Atlas 설계·조율, 독립 검토, Antigravity 정본 등재.
- 실운영 이미지/데이터 양/비용 및 repo·worktree 재확인, 현재 정본 Gemini 3072 계약(`specs/lbrain-architecture-rationalization/`)과의 관계 명시, `specs/model2vec-local-embedding/` 정본 위치 확정.
- 증거: `specs/model2vec-local-embedding/{requirements.md,design.md,milestones.md,review.md}` 및 `specs/lbrain-architecture-rationalization/{requirements.md,design.md}` 상호 참조 명시.

## M1 — Model2Vec 실행 서비스

- 담당: Antigravity 구현 / Atlas 독립 검증.
- 전용 clean worktree에서 Neurons 소스·설치·설정·문서·lock·테스트 구현. 로딩/정확 버전/길이/단건·배치/실제 client contract(`float` 및 `base64`)/에러/사설 접근을 검증한다. 실제 모델을 활용한 격리 시험과 종료를 증명한다.
- 증거: 코드+테스트 결과+정확 revision 및 실행 지침. 운영 Mac 서비스 설치/노출 금지.

## M2 — Neurons 저장·검색·Graphiti 연동

- 담당: Antigravity 구현 / 독립 검토 Atlas.
- 프로필/모델·차원 격리, PG 및 Graphiti 소비 경로, outbox lease/idempotency, representation receipt v3, vendored `graphiti_core` 0.30.1 fork, 재시작·긴 본문·모델 불가/유료 fallback 차단 등 계약 구현.
- 증거: 실제 격리 PG·Neo4j와 실제 클라이언트 및 공개 MCP 경로의 통합 테스트, CI 테스트 발견, regression. mock-only는 불충분. 실 운영 DB·graph 접근/변경 금지.

## M3 — 데이터 전환 도구 및 품질 평가

- 담당: Antigravity 구현, Atlas 평가·검토.
- 제한된 migration dry-run/일시 중지·재시작/변경 동시성·실패 분류, authority 보존 및 새 인덱스 분리를 구현·실험한다. 합의된 실제 시나리오에 대한 품질·false merge·지연 검증. 사전 승인 없는 실데이터 생성/쓰기 및 유료 API 금지.
- 증거: 재현 가능한 격리 테스트 및 데이터별 카운트 분류, 테스트/질문 출처와 기준, 수용·미해결 항목.

## M4 — 운영 준비 및 명시 승인

- 담당: Atlas. Mac 상주·사설 접근, 운영 영향, 시작/절전·RAM, 정확한 배포 대상·버전, DB/Neo4j 영향과 데이터 범위, 현재 cost, 최소 안전장치, 롤백과 검증 절차를 확정한다.
- 증거: 사용자 승인된 프리플라이트, 변경 대상/승인 기록. 하드 스톱: 데이터 유실 위험, 의도되지 않은 비용, 외부 서비스 다운타임, 복구 불가능한 변경.

## M5 — 승인된 운영 적용·전환·검증

- 담당: Atlas. 운영 전환의 정확한 승인에 한정. 기존 데이터 보존·무단 Gemini fallback 방지, 재색인 진행과 누락/실패 수, LBrain 실제 공개 MCP 검색/graph·PG 경로, Mac 장애, 비용/과금 경로 확인.
- 증거: 배포 SHA/digest readback, 새 프로필 coverage 및 검색 결과, 실측 비용과 known gaps. 승인받지 않은 기존 index/PVC/데이터 삭제 금지.

## 작업 상태 전달

각 마일스톤은 시작/완료 시 `status`, source SHA, worktree, 수정·미추적 파일, 실제 시험 명령/결과, artifact 위치, blockers와 다음 담당자를 기록한다. 작업자는 자기 완료 주장을 직접 승인하지 않으며, 오케스트레이터가 독립 readback/test 후 수용한다. 실패/중단은 미완료로 남긴다.

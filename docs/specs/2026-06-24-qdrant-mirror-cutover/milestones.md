# Milestones — qdrant-mirror-cutover (Stage 1, code-only)

Stage 1만 이 루프에서 실행한다(M1–M5, 가역·test-evidence). M6–M10은 라이브 게이트라
별도 operator 승인.

## M1 OpenAIEmbeddingProvider (기존 OpenAI-compatible endpoint 재사용)
- status: done
- evidence: tests/test_qdrant_embedding_provider.py 7 passed (config 재사용/size fail-closed/adapter protocol end-to-end, no network)

## M2 payload top-level filter fields + payload index
- status: done
- evidence: tests/test_qdrant_payload_schema.py + 기존 qdrant test 36 passed (top-level 승격, payload index 선언, privacy_class/result_type/project 다중 필터, 무회귀)

## M3 ledger qdrant_collections registry (additive migration)
- status: done
- evidence: tests/test_ledger_qdrant_collections.py 5 passed (upsert/get/list/enable fail-closed/reopen, additive 옆 index_targets)

## M4 Qdrant hit ledger-join (authority gate)
- status: done
- evidence: tests/test_qdrant_authority_join.py 5 passed (resolved flip, unresolved drop/flag, status gate, end-to-end query→join)

## M5 mirror reranker reuse seam (기존 OpenAI-compatible reranker)
- status: done
- evidence: tests/test_qdrant_rerank.py 6 passed (reorder/top_n/score-guard/config reuse/query→rerank→join compose)
- note(replan): ledger DDL 추가가 ledger_areas partition guard를 깨 → qdrant_collections를 AREA_D(native_memory)에 등록(SoT 변경 아님, 루프 내 replan). 전체 worker suite 889 passed.

## Stage 1 종료
- M1–M5 all done. 라이브 mutation 0.
- M6–M10(라이브 게이트)은 별도 operator 승인 — 이 루프 범위 밖.

## 적대적 검증(4-dim) 후속 수정
- BLOCKER(authority): mirror resolver가 status-only로 superseded/disabled/expired를
  못 걸러 권위로 샜음 → ledger에 `authorize_document_by_content_hash` 추가(canonical
  `_authorize_knowledge_item` 술어 재사용, behavior-preserving 추출). resolver가 그걸
  위임 → 미러가 canonical authority와 절대 발산 안 함. join이 권위 레코드로
  privacy/project/provider/currentness reconcile.
- MAJOR(privacy): query를 fail-closed scoping(미scoped 쿼리 거부) + `privacy_class`
  파라미터 + SearchableMirrorHit에 privacy_class 노출.
- MAJOR(test): server-side filter-shape 검증, enable disabled-branch, end-to-end
  unresolved-drop(real ledger) 테스트 추가.
- minor: collection enable fail-closed-all(any-disabled), config api_key 미반환,
  area count 주석, schema_migrations seed, embedding 우선순위/submit size-guard/rerank
  order/to_dict 테스트.
- 전체 worker suite 896 passed, 9 skipped. authority refactor 무회귀.

## 3-리뷰(CodeRabbit + codebase-arch-mgr + system-arch-mgr) 후속 수정
- upsert_qdrant_collection이 disabled row를 자동 부활시키던 것 수정(메타데이터만
  갱신, enable 전환은 disable_qdrant_collection 등 명시 동사). +no-revive 테스트.
- _filter_conditions_dict: filters가 명시 target_profile을 덮어쓰지 못하게(충돌 시
  ValueError). +테스트.
- join_mirror_hits_to_authority: mirror privacy_class가 권위 privacy_level과 다르면
  relabel 대신 drop(privacy_mismatch). +테스트.
- 정직성: qdrant_collections registry는 intended state 기록이고 read/write
  enforcement는 M8 배선이라고 docstring 명시(과대표현 제거).
- 문서 정합: doc 00 recall=ledger-first로 통일, doc 04 단계번호 정본=cutover spec
  포인터, doc 03 disable 2-layer(B collection-level=M3 / A per-point=M9) 정리,
  design.md open-question에 M8 배선 항목(brain_query 수렴/registry enforcement/
  embedding_model 검증) 추가.
- skip(사유): 코드 docstring 영어 지적 — 이 패키지 컨벤션이 영어 docstring이라
  주변 코드와 일관 유지(자연어 markdown 문서는 한국어 유지). rerank 라이브 stub은
  follow-on 표기됨.
- 전체 worker suite 899 passed, 9 skipped.

## M6/M7 code-only seam (branch codex/qdrant-mirror-m6-dualwrite-shadow; main 머지 후 분기)
- Stage 1이 main에 머지(PR #22, merge 200a2c0). 이 브랜치는 갱신된 main 기준.
- MirrorDualWriteBackend(`qdrant_dual_write.py`): primary(RetiredIndexBridge/CouchDB) authority +
  best-effort Qdrant mirror. mirror 실패는 primary를 안 깸, find/status는 primary 전용.
  shadow_worker 미배선(활성화 env 분기는 Qdrant 배포 시 추가).
- read-compare harness(`qdrant_read_compare.py`): primary vs mirror top-k content_hash
  overlap + recall@k + exact-match(read_compare evidence packet 형), recall_parity_passes
  게이트 헬퍼. 주입 fetcher라 no-network.
- 테스트 11개, 전체 worker suite 910 passed, 9 skipped. 라이브 mutation 0.
- 남은 라이브 활성화(operator/Ubuntu): Qdrant 호스트 배포 + shadow_worker env 분기 +
  per-action 게이트 + 실데이터 parity 측정.

## M6 배포 배선 + M9/M10 전제조건 (오토파일럿, code-only)
- dual-write 활성화 hook: shadow_worker default-off env 분기(MIRROR_DUAL_WRITE=1 +
  QDRANT_URL), build_remote_qdrant_docling_mirror_adapter, build_qdrant_mirror_from_env.
- compose.yaml: `qdrant` 서비스(profile searchable-mirror, 127.0.0.1:6333, qdrant_data
  볼륨, `${QDRANT_IMAGE:-qdrant/qdrant:latest}`) + ingress-worker-py dual-write env
  (전부 default-off) + 임베딩 env passthrough. 바 `compose up` 무변.
- worker/Dockerfile: lean deps(qdrant-client + openai; docling 제외 — Passthrough).
- 문서 05: RetiredIndexBridge 벡터 미러 은퇴(M9/M10) 전제조건 — "안 쓴다/불필요"가 아직 거짓인
  audit 근거(라이브 writer + no-fallback reader 잔존), 삭제 전 blocker 체크리스트,
  비가역 per-action 증거 게이트, 가역/비가역 경계.
- 전체 worker suite 915 passed. 라이브 mutation 0(플래그 off·Qdrant 미배포·delete 미실행).
- 호스트 테스트: 브랜치 checkout → `docker compose build ingress-worker-py` →
  `--profile searchable-mirror up -d qdrant` → 워커 env에 MIRROR_DUAL_WRITE=1+QDRANT_URL.
  머지 불필요(이미지가 소스 빌드).
- M9/M10 라이브 delete/disable: 이 세션 미실행(비가역+호스트+per-action 증거 필요).

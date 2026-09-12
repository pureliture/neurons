# Milestones — lbrain-memory-recall-fix

Spec: `docs/specs/lbrain-memory-recall-fix-spec.md` (APPROVED). Contract: agentic-execution.

| Slice | 내용 | Status | Evidence |
|-------|------|--------|----------|
| 1 | Fix 1: `ledger.py` `list_llm_brain_memory_cards`에 accepted_only/limit 추가 | done | brain_steward/autopilot 계열 52 passed |
| 2 | Fix 4: `terms.py` 신설 + 양쪽 호출처 통합 | done | test_llm_brain_terms_module + graphiti/mcp 13 passed |
| 3 | Fix 2: `brain_memory_search` date_from/date_to + adapter 시간 창 | done | test_brain_memory_search_date_window 3 passed + temporal/mcp 120 passed |
| 4 | Fix 3: fan-out 축소 (`_episode_fanout`, 기본 ×1.0, LBRAIN_EPISODE_FANOUT_X로 확장) | done | graphiti_backend/neo4j 71 passed |
| 5 | Fix 5: stale GraphFact null 제외 (is_null OR 제거) | done | graphiti neo4j adapter 어설션 갱신 후 71 passed |
| 6 | 전체 `uv run pytest -q` green | done | 3937 passed, 218 skipped (package_depth manifest에 terms 등록) |

## Amendment 기록 (requirements-preserving)

- Fix 2: 날짜 창 필터를 개별 adapter가 아닌 `context.py` 공용 레벨에서 적용 — Fake/Null/Graphiti 모든 adapter에서 균일 동작, 최소 수정.
- Fix 3: 부족 시 1회 재조회 확장 대신 환경변수 `LBRAIN_EPISODE_FANOUT_X` 확장으로 단순화 (재조회 경로 신설은 slice에 불필요한 신규 코드).
- Fix 5: 기존 `is_null` 어설션 테스트는 승인된 동작 변경에 따라 갱신됨(회귀가 아님).

Next: 커밋 체크포인트.

# Milestones — arch quick-wins (issue #40)

## M1 shim 12개 삭제 + caller 치환 (#1)
- status: done
- evidence: 12 shim 삭제(루트 41->29), caller 44곳(tests/eval) + lib 부모상대 2곳(brain_query/llm_brain_miner의 ..memory_card, research 누락분) 치환; worker pytest 1275 passed/9 skipped.
- note(replan): research가 절대형만 봐서 lib 상대 import 2건 누락 -> in-loop 수정.

## M2 compose env anchor 2개 (#3)
- status: done
- evidence: x-ingress-java-env(13키)/x-llm-brain-worker-env(19키) 도입, 5개 서비스 적용. PyYAML merge 해석으로 resolved env가 pre/post 완전 동일(EXTRACT_ENTITIES override·서비스 고유키 보존). gradle ComposeConfigTest green.
- note: docker compose 미가용 -> resolved-env 동일성을 PyYAML로 검증. permanent guard(ComposeConfigTest)는 raw-string이라 anchor 해석 미검증 -> OQ3(follow-up).

## M3 .env.example 정합 + coverage 가드 (#7)
- status: done
- evidence: ComposeConfigTest.envExampleCoversAllRequiredComposeVars assertion red(누락 MCP_HTTP_HOST,LLM_BRAIN_ENV_FILE)->green; 필수 2 + parity RETIRED_INDEX_BRIDGE_TASK_SUMMARY_DATASET_ID + MCP_HTTP_PORT + optional 주석 섹션 추가. gradle 전수 green.

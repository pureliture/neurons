# Milestones — arch quick-wins (issue #40)

## M1 shim 12개 삭제 + caller 치환 (#1)
- status: done
- evidence: 12 shim 삭제(루트 41->29), caller 44곳(tests/eval) + lib 부모상대 2곳(brain_query/llm_brain_miner의 ..memory_card, research 누락분) 치환; worker pytest 1275 passed/9 skipped.
- note(replan): research가 절대형만 봐서 lib 상대 import 2건 누락 -> in-loop 수정.

## M2 compose env anchor 2개 (#3)
- status: pending

## M3 .env.example 정합 + coverage 가드 (#7)
- status: pending

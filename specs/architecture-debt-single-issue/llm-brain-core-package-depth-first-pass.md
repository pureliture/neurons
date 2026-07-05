# llm_brain_core Package Depth First Pass

## 상태

- issue: #40
- status: first-pass done
- scope: eval-only package-depth guard and root module classification
- live runtime mutation: 없음

## 확인한 결합

1. `llm_brain_core` still has a broad root package surface with 45 non-`__init__` root modules.
2. Public imports from `agent_knowledge.llm_brain_core` are used widely, so a large file move would be high-risk.
3. Existing tests already guard selected layering/import concerns, but there was no source-level manifest requiring new root modules to be classified.

## 적용한 guard

- Added `worker/eval/llm_brain_core_package_depth.py`.
- The lint classifies root modules into:
  - `domain_contracts`
  - `runtime_services`
  - `graph_projection`
  - `adapters`
  - `cli`
- The lint fails when:
  - a new root module is unclassified
  - the manifest contains a stale module
  - `llm_brain_core.__init__` re-exports CLI modules
- Added `worker/tests/test_llm_brain_core_package_depth.py` with falsifiable checks for unclassified modules and CLI re-export leakage.

## 검증

- `cd worker && uv run pytest -q tests/test_llm_brain_core_package_depth.py`
  - RED: lint module missing before implementation
  - GREEN: package-depth lint tests pass
- `cd worker && uv run python eval/llm_brain_core_package_depth.py`
  - 통과
- `cd worker && uv run python eval/llm_brain_core_package_depth.py --report`
  - root modules classified as 20 domain contracts, 6 runtime services, 4 graph/projection modules, 5 adapters, 10 CLI modules
- `cd worker && uv run pytest -q tests/test_llm_brain_core_layering.py tests/test_public_safe_util_decouple.py tests/test_autopilot_no_index_client_before_m9.py`
  - 통과

## 남은 리스크

- This pass does not move files into subpackages.
- `llm_brain_core.__init__` still re-exports heavy graph/adapter symbols; this pass only blocks CLI re-export leakage and unclassified root growth.
- A follow-up object-substrate subpackage slice can move `knowledge_objects.py`, `object_packs.py`, `reference_corpus.py`, `golden_query_eval.py`, `okf_export.py`, and `object_cli.py` behind compatibility shims.

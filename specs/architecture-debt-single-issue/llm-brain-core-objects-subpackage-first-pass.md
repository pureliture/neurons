# llm_brain_core Objects Subpackage First Pass

## 상태

- issue: #40
- status: first-pass done
- scope: object-substrate package-depth extraction with public import compatibility
- live runtime mutation: 없음

## 변경한 구조

- Added `agent_knowledge.llm_brain_core.objects` as the object-substrate package.
- Moved implementation modules under the new package:
  - `objects.knowledge_objects`
  - `objects.object_packs`
  - `objects.reference_corpus`
  - `objects.golden_query_eval`
  - `objects.okf_export`
  - `objects.object_cli`
- Kept root modules with compatibility shims:
  - `llm_brain_core.knowledge_objects`
  - `llm_brain_core.object_packs`
  - `llm_brain_core.reference_corpus`
  - `llm_brain_core.golden_query_eval`
  - `llm_brain_core.okf_export`
  - `llm_brain_core.object_cli`

## 보존한 호환성

- Existing imports from `agent_knowledge.llm_brain_core.<object_module>` remain valid.
- Root shims re-export the moved implementation symbols, so old-path and new-path imports point to the same function/class objects.
- `agent_knowledge.llm_brain_core.__init__` still does not re-export object CLI entrypoint functions.
- Repo-internal production imports now prefer the new `llm_brain_core.objects` path for object CLI, object pack, reference-corpus, and MCP JSON-RPC surfaces.

## 적용한 guard

- Added `worker/tests/test_llm_brain_core_objects_subpackage.py`.
- Extended `worker/eval/llm_brain_core_package_depth.py` to check:
  - expected object-subpackage modules exist
  - unexpected object-subpackage modules are classified before use
  - root object modules remain compatibility shims that import from `objects.<module>`
- Extended `worker/tests/test_llm_brain_core_package_depth.py` with falsifiable missing-subpackage and non-shim checks.

## 검증

- `cd worker && uv run pytest -q tests/test_llm_brain_core_objects_subpackage.py tests/test_llm_brain_core_package_depth.py`
  - RED: `agent_knowledge.llm_brain_core.objects` package missing before implementation
  - GREEN: object-subpackage contract and package-depth guard tests pass
- `cd worker && uv run python eval/llm_brain_core_package_depth.py`
  - 통과
- `cd worker && uv run pytest -q tests/test_knowledge_objects.py tests/test_object_packs.py tests/test_reference_corpus.py tests/test_golden_query_eval.py tests/test_okf_export.py tests/test_neuron_cli.py tests/test_neuron_mcp_stdio.py`
  - 통과

## 남은 리스크

- Root compatibility shims intentionally keep the old import path alive; this pass reduces implementation locality debt but does not remove the legacy surface.
- `llm_brain_core.__init__` still has broad non-object re-export surface for graph/runtime/adapters.
- Object-substrate internals still share broad helper modules; this pass does not split object schema, pack builders, corpus planning, eval, and export into narrower domain packages.
- Live runtime behavior is not proven; evidence is source-level and test-level only.

## 리뷰 결론

- `code_simplifier` reviewed the slice and kept the compatibility shims; only a behavior-preserving helper extraction was applied inside `objects.object_packs`.
- `codebase_architecture_manager` classified this as a real package-depth first slice, not a folder-only move.
- Recommended next slice: MCP handler registry extraction. It has a clearer seam and lower blast radius than immediate Ledger area-object extraction because `ToolContract` and `tool_contract_registry()` already exist while the remaining debt is concentrated in `mcp_jsonrpc.py` dispatch.

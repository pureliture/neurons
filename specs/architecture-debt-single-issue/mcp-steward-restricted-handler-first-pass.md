# MCP Steward Restricted Handler First Pass

## 상태

- issue: #40
- status: first-pass done
- scope: steward restricted MCP handler registry and cache-invalidation ordering
- live runtime mutation: 없음

## 확인한 결합

1. M14 moved top-level MCP tool dispatch into `tool_handler_registry()`.
2. `_dispatch_steward_tool()` still kept restricted write-tool branching, permission denial handling, and cache invalidation in one internal function.
3. Restricted tools have stricter ordering requirements than read/proposal tools:
   - denied path must not write
   - denied path must not invalidate the session brain-card cache
   - success path must invalidate the cache after the steward write succeeds

## 적용한 변경

- Added `steward_restricted_handler_registry()` in `worker/lib/agent_knowledge/mcp_jsonrpc.py`.
- Kept `restricted_steward_handler_registry()` as a compatibility alias for tests/transition.
- Moved restricted steward tool branching into a table of dispatch functions:
  - `memory_candidate_approve`
  - `memory_candidate_reject`
  - `memory_candidate_auto_accept`
  - `memory_supersede_commit`
  - `memory_stale_commit`
- Added a shared restricted handler binder that:
  - obtains `service.brain_steward()`
  - converts `StewardPermissionError` to `steward.restricted_denied_payload(tool_name)`
  - returns denied payload without cache invalidation
  - calls `service.invalidate_brain_card_cache()` once after successful restricted dispatch
- `_dispatch_steward_tool()` now delegates restricted tools through the restricted handler registry instead of direct restricted `if`/`elif` comparisons.

## 적용한 guard

- Added `worker/tests/test_mcp_steward_handler_registry.py`.
- The guard checks:
  - restricted registry key set exactly matches `STEWARD_RESTRICTED_TOOL_NAMES`
  - handlers are callable
  - denied path converts `StewardPermissionError` to the expected denied payload
  - denied path does not invalidate the cache
  - success path calls the fake steward method and then invalidates exactly once
  - `_dispatch_steward_tool()` does not keep direct restricted tool-name comparisons

## 검증

- `cd worker && uv run pytest -q tests/test_mcp_steward_handler_registry.py`
  - GREEN after implementation
- `cd worker && uv run pytest -q tests/test_mcp_steward_handler_registry.py tests/test_mcp_handler_registry.py tests/test_neuron_mcp_stdio.py tests/test_adversarial_mcp.py`
  - 통과
- `cd worker && uv run --extra mcp-http pytest -q tests/test_neuron_mcp_http.py -k 'to_sdk_tools or list_tools or dispatch_call_tool'`
  - 통과

## 리뷰 결론

- `code_simplifier` kept `steward_restricted_handler_registry()` as the canonical name and retained `restricted_steward_handler_registry()` as a compatibility alias.
- `codebase_architecture_manager` classified this as a real reduction in restricted write-path coupling.
- Evidence is source/test-level only; no live MCP runtime activation or HTTP server runtime proof was performed.

## 남은 리스크

- Tests still know private registry names and some AST shape.
- `mcp_jsonrpc.py` remains large.
- Public schema, dispatch-owner metadata, and handler callables are still declared separately.
- M16 moved steward read/proposal tools behind `steward_read_proposal_handler_registry()`.
- The alias policy for `restricted_steward_handler_registry()` is deferred.

## 다음 후보

- Run cumulative verification before another architecture slice.
- After verification, the remaining MCP design question is whether schema, dispatch owner, and handler callable should share one internal definition.

# MCP Steward Read/Proposal Handler First Pass

## 상태

- issue: #40
- status: first-pass done
- scope: steward read/proposal MCP handler registry separated from restricted write handling
- live runtime mutation: 없음

## 확인한 결합

1. M15 moved restricted steward write tools behind `steward_restricted_handler_registry()`.
2. `_dispatch_steward_tool()` still routed steward read/proposal tools through direct `if` branches.
3. Read/proposal tools must stay separate from restricted write handling because:
   - they do not use `StewardPermissionError` denied conversion
   - they must not invalidate the session brain-card cache
   - candidate/supersede proposal tools must use `steward.select_source_span(arguments)`

## 적용한 변경

- Added `_STEWARD_READ_PROPOSAL_TOOL_NAMES` as the explicit non-restricted steward tool set.
- Added `steward_read_proposal_handler_registry()` in `worker/lib/agent_knowledge/mcp_jsonrpc.py`.
- Moved read/proposal steward tool dispatch into table-driven handlers:
  - `memory_authority_pack_read`
  - `memory_review_queue_list`
  - `memory_candidate_create`
  - `memory_stale_mark`
  - `memory_supersede_propose`
- `_dispatch_steward_tool()` now tries the read/proposal registry first, then the restricted registry.
- Read/proposal registry validation fails closed if it is missing a non-restricted steward tool, contains a stale tool, or overlaps with `STEWARD_RESTRICTED_TOOL_NAMES`.

## 적용한 guard

- Added `worker/tests/test_mcp_steward_read_proposal_registry.py`.
- The guard checks:
  - read/proposal registry entrypoint exists
  - key set matches the five non-restricted steward tools
  - key set is disjoint from restricted tool names
  - handlers are callable
  - each handler calls the expected fake steward method
  - read/proposal handlers do not invalidate the cache
  - candidate/supersede proposal handlers call `select_source_span()`
  - `_dispatch_steward_tool()` no longer keeps direct read/proposal or restricted tool-name comparison chains

## 검증

- `cd worker && uv run pytest -q tests/test_mcp_steward_read_proposal_registry.py`
  - GREEN after implementation
- `cd worker && uv run pytest -q tests/test_mcp_steward_read_proposal_registry.py tests/test_mcp_steward_handler_registry.py tests/test_mcp_handler_registry.py tests/test_neuron_mcp_stdio.py tests/test_adversarial_mcp.py`
  - 통과
- `cd worker && uv run --extra mcp-http pytest -q tests/test_neuron_mcp_http.py -k 'to_sdk_tools or list_tools or dispatch_call_tool'`
  - 통과

## 리뷰 결론

- `code_simplifier` extracted explicit `_STEWARD_READ_PROPOSAL_TOOL_NAMES` and simplified repeated test checks without behavior changes.
- `codebase_architecture_manager` classified M16 as a real reduction in steward read/proposal dispatch coupling.
- Evidence is source/test-level only; no live MCP runtime activation or HTTP server runtime proof was performed.

## 남은 리스크

- Tests still know private registry names and AST shape.
- `mcp_jsonrpc.py` remains large.
- Public schema, dispatch-owner metadata, and handler callables are still not a single internal definition.
- Proposal-only safety is still primarily owned by `BrainStewardService`; this pass guards handler-level routing and source-span selection.
- JSON schema plus transport integration proof remains targeted, not full live runtime proof.

## 다음 후보

- Pause for cumulative verification.
- If continuing MCP later, consider a single internal definition for schema, dispatch owner, and handler callable.
- Avoid MCP module split until the internal definition seam is clearer.

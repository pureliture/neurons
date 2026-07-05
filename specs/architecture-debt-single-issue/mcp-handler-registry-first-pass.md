# MCP Handler Registry First Pass

## 상태

- issue: #40
- status: first-pass done
- scope: top-level MCP JSON-RPC tool dispatch registry
- live runtime mutation: 없음

## 확인한 결합

1. M11 introduced `ToolContract` and `tool_contract_registry()` for public schema and dispatch-owner coverage.
2. `mcp_jsonrpc.py` still routed tool calls through a top-level `dispatch_tool_call()` if-chain.
3. Public tool schema, dispatch-owner metadata, and handler callables were still separate sources that could drift.

## 적용한 변경

- Added `tool_handler_registry()` in `worker/lib/agent_knowledge/mcp_jsonrpc.py`.
- `dispatch_tool_call()` now performs:
  - tool name extraction
  - `tool_handler_registry()` lookup
  - unknown-tool rejection
  - handler invocation
- Handler registry entries are validated against `tool_contract_registry()` so missing or stale handlers fail closed.
- Public tool schema remains owned by `mcp_tools.py`; handler callables are not exposed through `list_tools()` or `ToolContract.to_tool()`.
- Existing tool-specific behavior was moved into named private dispatch functions without changing response shape.

## 적용한 guard

- Added `worker/tests/test_mcp_handler_registry.py`.
- The guard checks:
  - handler registry keys match `tool_names()` and `tool_contract_registry()`
  - handler registry values are callable
  - public `list_tools()` and `ToolContract.to_tool()` do not expose handler callables or dispatch-only metadata
  - `knowledge.search` dispatch can be monkeypatched through the registry path, proving `dispatch_tool_call()` uses the registry

## 검증

- `cd worker && uv run pytest -q tests/test_mcp_handler_registry.py`
  - RED: `tool_handler_registry()` missing and dispatch did not route through a registry
  - GREEN: handler registry contract passes
- `cd worker && uv run pytest -q tests/test_mcp_handler_registry.py tests/test_neuron_mcp_stdio.py tests/test_adversarial_mcp.py`
  - 통과
- `cd worker && uv run --extra mcp-http pytest -q tests/test_neuron_mcp_http.py -k 'to_sdk_tools or list_tools or dispatch_call_tool'`
  - 통과

## 리뷰 결론

- `code_simplifier` reduced the registry binding into a `(tool_name, dispatch)` table and removed an overly structural AST assertion from the test.
- `codebase_architecture_manager` classified M14 as a real top-level dispatch seam, not just moving the if-chain into helper functions.
- This is still a first pass: steward tools continue to route through `_dispatch_steward_tool()` with its own internal if/elif chain.

## 남은 리스크

- Public schema, dispatch-owner metadata, and handler callables are still not a single internal definition.
- M15 moved steward restricted write paths behind `steward_restricted_handler_registry()`, but steward read/proposal tools still route through `_dispatch_steward_tool()`.
- The new handler-registry tests know private module shape; that is acceptable for a first-pass architecture guard but not a final public interface.
- Live MCP runtime activation and live HTTP server proof were not part of this pass.

## 다음 후보

- Run targeted cumulative verification before another MCP slice.
- Then consider a steward read/proposal registry that remains separate from restricted write handling.

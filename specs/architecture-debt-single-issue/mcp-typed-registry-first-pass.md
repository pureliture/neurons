# MCP Typed Registry First Pass

## 상태

- issue: #40
- status: first-pass done
- scope: MCP tool typed contract metadata and dispatch ownership coverage
- live runtime mutation: 없음

## 확인한 결합

1. `mcp_tools.py` owns public tool schema through `list_tools()`.
2. `mcp_jsonrpc.py` owns execution through a separate `dispatch_tool_call()` if-chain over the same tool names.
3. M7 added `tool_registry()` and `tool_names()`, but listed tools could still drift from dispatch ownership without a typed internal contract.

## 적용한 guard

- Added `ToolContract` as an internal typed view with `name`, `description`, `input_schema`, and `dispatch_owner`.
- Added `tool_contract_registry()` to create typed contracts from the public `tool_registry()`.
- Added fail-closed checks for:
  - listed tools missing dispatch-owner metadata
  - stale dispatch-owner metadata for tools no longer listed
- Public `list_tools()` and SDK tool conversion continue to expose only `name`, `description`, and `inputSchema`.

## 검증

- `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py::test_mcp_tool_contract_registry_tracks_dispatch_ownership tests/test_neuron_mcp_stdio.py::test_mcp_tool_registry_matches_listed_tools_without_duplicate_names`
  - RED: `tool_contract_registry` did not exist before the implementation
  - GREEN: typed contracts match public registry and carry dispatch ownership
- `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py -k 'tool_registry or tool_contract or public_tool_list'`
  - 통과
- `cd worker && uv run pytest -q tests/test_adversarial_mcp.py tests/test_neuron_mcp_stdio.py -k 'tool_registry or tool_contract or dispatch or tools/list or tools/call or mcp_tool_list'`
  - 통과
- `cd worker && uv run --extra mcp-http pytest -q tests/test_neuron_mcp_http.py -k 'to_sdk_tools or list_tools or dispatch_call_tool'`
  - 통과

## 남은 리스크

- M14 added `tool_handler_registry()` and moved top-level `dispatch_tool_call()` to a registry lookup, but steward tools still route through an internal `_dispatch_steward_tool()` if-chain.
- `mcp_tools.py` intentionally does not import `KnowledgeSearchService`, steward services, or transport code.
- Live MCP server activation and live HTTP runtime proof were not part of this pass.

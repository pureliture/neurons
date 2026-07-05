# MCP Server / Tools Coupling Audit

This audit/implementation note is the M7 slice for GitHub issue #40. It does not perform live MCP server activation, HTTP exposure, deploy, Docker, systemd, firewall, or credential mutation.

## Finding

MCP tool schema is centralized in `worker/lib/agent_knowledge/mcp_tools.py`, while JSON-RPC dispatch lives in `worker/lib/agent_knowledge/mcp_jsonrpc.py` as a separate if-chain over the same tool names. Stdio and HTTP transports already share the same `list_tools()` and dispatch seam, but there was no small registry helper to assert that listed tool names are unique and observable as a contract before larger dispatch refactors.

## Implemented Slice

Timestamp: 2026-07-05 20:16:44 KST

- Added `tool_registry()` and `tool_names()` in `worker/lib/agent_knowledge/mcp_tools.py`.
- Added `worker/tests/test_neuron_mcp_stdio.py` coverage proving:
  - listed MCP tool names are unique
  - `tool_registry()` keys match `list_tools()`
  - `tool_names()` matches listed names
  - registry entries preserve the same description and `inputSchema` as `list_tools()`

Follow-up first pass:

- Added internal `ToolContract` and `tool_contract_registry()` in `worker/lib/agent_knowledge/mcp_tools.py`.
- `tool_contract_registry()` preserves the public `list_tools()` shape while requiring dispatch-owner metadata for every listed tool.
- Added fail-closed tests for missing/stale dispatch-owner metadata and for public tool-list metadata leakage.

Handler-registry first pass:

- Added `tool_handler_registry()` in `worker/lib/agent_knowledge/mcp_jsonrpc.py`.
- Top-level `dispatch_tool_call()` now routes through the handler registry.
- Handler registry keys are validated against `tool_contract_registry()`.
- Public tool schema output still does not expose handler callables or dispatch-only metadata.

Steward restricted handler first pass:

- Added `steward_restricted_handler_registry()` in `worker/lib/agent_knowledge/mcp_jsonrpc.py`.
- Restricted write tools now share one binder for `StewardPermissionError` denial and success-path cache invalidation.
- Denied restricted calls do not invalidate the session brain-card cache.
- Successful restricted calls invalidate the cache after steward dispatch returns.

Steward read/proposal handler first pass:

- Added `steward_read_proposal_handler_registry()` in `worker/lib/agent_knowledge/mcp_jsonrpc.py`.
- Steward read/proposal tools now route through a registry separate from restricted write tools.
- Read/proposal handlers do not invalidate the session brain-card cache.
- Candidate and supersede proposal handlers still call `steward.select_source_span(arguments)`.

## Verification

- `cd worker && uv run pytest -q tests/test_neuron_mcp_stdio.py -k tool_registry`
- `cd worker && uv run pytest -q tests/test_adversarial_mcp.py tests/test_neuron_mcp_stdio.py`
- `cd worker && uv run --extra mcp-http pytest -q tests/test_neuron_mcp_http.py -k 'to_sdk_tools or list_tools or dispatch_call_tool'`

## Residual Risk

- Top-level dispatch is registry-backed. Steward restricted and read/proposal dispatch are registry-backed.
- Tool schema, dispatch ownership, and handler callables are validated together but still declared separately.
- HTTP transport remains optional-extra gated; this slice only ran targeted optional tests.

## Next Candidate

Run cumulative verification before continuing MCP work. If continuing MCP, next candidate is a single internal definition for schema, dispatch owner, and handler callable.

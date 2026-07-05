# MCP Single Internal Definition

Status: done.

Tracker: GitHub issue #40.

## Scope

Unify MCP tool schema, dispatch owner, and handler callable into one internal contract definition per tool.

The internal contract may include private dispatch metadata, but public tool-listing output must keep the existing MCP schema surface.

## Non-Goals

- No live MCP proposal write.
- No runtime server mutation.
- No public schema break.
- No unrelated steward policy change.

## Required Invariants

- Every public MCP tool has exactly one internal contract.
- Each internal contract has a public schema and one handler callable.
- Dispatch owner metadata is private.
- Restricted steward write tools remain disjoint from steward read/proposal tools.
- Cache invalidation remains limited to successful restricted steward writes.

## Test Plan

- Add a failing contract test that proves handler callable, dispatch owner, and public schema cannot drift independently.
- Keep or update targeted MCP dispatch tests for top-level, restricted steward, and read/proposal steward dispatch.
- Run focused MCP tests before broad worker verification.
- Run optional HTTP schema conversion tests when the extra is available.

## Done Criteria

- Public `list_tools()` remains compatible.
- MCP dispatch no longer depends on parallel tool-name maps for handler lookup.
- Focused and full worker tests pass.
- Evidence is recorded in `milestones.md`.

## Evidence

- `tool_runtime_contract_registry()` now owns the internal runtime contract source for public schema, dispatch owner, and handler callable.
- `tool_handler_registry()`, `steward_read_proposal_handler_registry()`, and `steward_restricted_handler_registry()` are derived from runtime contracts.
- Public `list_tools()` still omits `dispatch_owner` and handler callable metadata.
- Focused MCP/stdio tests passed.
- Optional MCP HTTP tests passed.
- Full worker suite passed.

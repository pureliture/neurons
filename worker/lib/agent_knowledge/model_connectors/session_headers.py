"""OpenCode-family session affinity headers for OpenAI-compatible clients.

The OpenCode zen/go relay rejects every request that lacks an
``x-opencode-session`` header with HTTP 400 ``MissingSessionID``. Other
providers (the internal gateway, Vertex, Ollama) must NOT receive that header:
it is meaningless there and couples the worker to a single vendor. The header
is therefore applied only when the endpoint host belongs to the OpenCode
family.
"""

from __future__ import annotations

import urllib.parse
import uuid

OPENCODE_SESSION_HEADER = "x-opencode-session"
OPENCODE_SESSION_DOMAIN = "opencode.ai"

# Lane-scoped session id for the Graphiti worker path. Stable for the process
# lifetime so OpenCode keeps routing one worker lane to one upstream backend.
GRAPH_WORKER_SESSION_ID = "neurons-graph-worker"

# Generated once at import: every request of this process shares one id. A
# per-request id would defeat the routing/pinning the relay performs.
_PROCESS_SESSION_ID = f"neurons-worker-{uuid.uuid4().hex[:16]}"


def is_opencode_base_url(base_url: str) -> bool:
    """True when ``base_url`` addresses the OpenCode relay (opencode.ai family).

    Compares hostnames instead of substring-matching the raw URL, so
    ``https://opencode.ai.evil.test/v1`` and ``https://proxy.test/opencode.ai/v1``
    are not treated as OpenCode endpoints.
    """

    host = _hostname(base_url)
    if not host:
        return False
    return host == OPENCODE_SESSION_DOMAIN or host.endswith("." + OPENCODE_SESSION_DOMAIN)


def opencode_session_id() -> str:
    """Return this process' stable OpenCode session id."""

    return _PROCESS_SESSION_ID


def opencode_session_headers(base_url: str, *, session_id: str = "") -> dict[str, str]:
    """Return ``{"x-opencode-session": <id>}`` for OpenCode endpoints, else ``{}``."""

    if not is_opencode_base_url(base_url):
        return {}
    value = str(session_id or "").strip() or _PROCESS_SESSION_ID
    return {OPENCODE_SESSION_HEADER: value}


def _hostname(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw if "://" in raw else f"//{raw}")
    return (parsed.hostname or "").lower().rstrip(".")

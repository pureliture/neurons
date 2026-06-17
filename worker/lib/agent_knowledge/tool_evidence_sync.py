"""Compatibility alias for :mod:`agent_knowledge.session_memory.tool_evidence_sync`."""

import sys as _sys

from .session_memory import tool_evidence_sync as _impl

_sys.modules[__name__] = _impl

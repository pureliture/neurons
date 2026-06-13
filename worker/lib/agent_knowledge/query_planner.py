"""Compatibility alias for :mod:`agent_knowledge.session_memory.query_planner`."""

import sys as _sys

from .session_memory import query_planner as _impl

_sys.modules[__name__] = _impl

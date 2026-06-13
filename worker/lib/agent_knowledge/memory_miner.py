"""Compatibility alias for :mod:`agent_knowledge.session_memory.memory_miner`."""

import sys as _sys

from .session_memory import memory_miner as _impl

_sys.modules[__name__] = _impl

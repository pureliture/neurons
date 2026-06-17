"""Compatibility alias for :mod:`agent_knowledge.session_memory.backfill`."""

import sys as _sys

from .session_memory import backfill as _impl

_sys.modules[__name__] = _impl

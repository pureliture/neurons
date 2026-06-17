"""Compatibility alias for :mod:`agent_knowledge.session_memory.curation`."""

import sys as _sys

from .session_memory import curation as _impl

_sys.modules[__name__] = _impl

"""Compatibility alias for :mod:`agent_knowledge.session_memory.transcript_chunking`."""

import sys as _sys

from .session_memory import transcript_chunking as _impl

_sys.modules[__name__] = _impl

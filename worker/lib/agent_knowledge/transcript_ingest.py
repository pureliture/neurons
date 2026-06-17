"""Compatibility alias for :mod:`agent_knowledge.session_memory.transcript_ingest`."""

import sys as _sys

from .session_memory import transcript_ingest as _impl

_sys.modules[__name__] = _impl

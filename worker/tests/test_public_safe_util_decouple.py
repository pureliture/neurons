"""Guard: the searchable-mirror adapter must not drag in the heavy llm_brain_core
package, so it stays importable from the ingress/delivery worker's vendored subset.

This is the regression that broke the live dual-write deploy: retired_vector_index_docling_mirror
imported public-safe helpers from llm_brain_core._util, whose package __init__ pulls
graphiti/neo4j — absent in the delivery worker.
"""

from __future__ import annotations

import subprocess
import sys


def test_public_safe_util_has_helpers_and_util_reexports():
    from agent_knowledge import public_safe_util as ps
    from agent_knowledge.llm_brain_core import _util

    for name in ("ensure_public_safe", "hash_payload", "public_safe_text", "require_sha256"):
        assert hasattr(ps, name)
        # llm_brain_core._util keeps re-exporting the SAME objects (back-compat)
        assert getattr(_util, name) is getattr(ps, name)

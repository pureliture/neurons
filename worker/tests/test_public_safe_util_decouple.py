"""Guard: the searchable-mirror adapter must not drag in the heavy llm_brain_core
package, so it stays importable from the ingress/delivery worker's vendored subset.

This is the regression that broke the live dual-write deploy: qdrant_docling_mirror
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


def test_importing_mirror_does_not_load_llm_brain_core():
    # fresh interpreter: importing the mirror must NOT pull agent_knowledge.llm_brain_core
    code = (
        "import sys; import agent_knowledge.rag_ingress.qdrant_docling_mirror as _m; "
        "leaked = sorted(k for k in sys.modules if 'llm_brain_core' in k); "
        "assert not leaked, leaked"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_importing_dual_write_builder_chain_does_not_load_llm_brain_core():
    code = (
        "import sys; "
        "from agent_knowledge.rag_ingress.qdrant_dual_write import build_qdrant_mirror_from_env as _b; "
        "from agent_knowledge.rag_ingress.qdrant_embedding import build_openai_embedding_provider as _e; "
        "leaked = sorted(k for k in sys.modules if 'llm_brain_core' in k); "
        "assert not leaked, leaked"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

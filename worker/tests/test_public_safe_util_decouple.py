"""Guard the public-safe helper API and its compatibility re-exports.

The ingress and delivery modules consume these helpers without depending on the
llm_brain_core implementation.
"""

from __future__ import annotations


def test_public_safe_util_has_helpers_and_util_reexports():
    from agent_knowledge import public_safe_util as ps
    from agent_knowledge.llm_brain_core import _util

    for name in ("ensure_public_safe", "hash_payload", "public_safe_text", "require_sha256"):
        assert hasattr(ps, name)
        # llm_brain_core._util keeps re-exporting the SAME objects (back-compat)
        assert getattr(_util, name) is getattr(ps, name)

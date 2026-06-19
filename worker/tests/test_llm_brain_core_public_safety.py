from __future__ import annotations

import pytest

from agent_knowledge.llm_brain_core._util import ensure_public_safe


def test_public_safety_rejects_windows_drive_paths():
    with pytest.raises(ValueError, match="private or raw"):
        ensure_public_safe(r"C:\Users\ddalkak\Projects\secret.txt", "path")


def test_public_safety_rejects_windows_unc_paths():
    with pytest.raises(ValueError, match="private or raw"):
        ensure_public_safe(r"\\server\share\secret.txt", "path")

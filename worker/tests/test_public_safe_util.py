"""ensure_public_safe: per-value raw scanning (no JSON-escape false positives).

Regression guard for the bug where json.dumps escaped real newlines as ``\\n`` so a
benign body line ending in ``word:`` followed by a newline matched the Windows-path
alternative ``[A-Za-z]:\\`` in PRIVATE_OUTPUT_RE -- blocking ~92% of code/markdown
session-memory bodies from the searchable mirror.
"""

from __future__ import annotations

import pytest

from agent_knowledge.public_safe_util import ensure_public_safe


def test_newline_after_colon_is_not_a_false_positive():
    # real newline (0x0A) after "Summary:" — was serialized to "Summary:\n" and
    # matched [A-Za-z]:\\ under the old JSON-blob scan. Raw per-value scan: clean.
    payload = {"text": "Summary:\nProject:\nNotes:\n- did a thing\nDone:\n"}
    ensure_public_safe(payload, "p")  # must not raise


def test_backslash_n_literal_and_tabs_pass():
    payload = {"text": "regex: \\d+ then \\w* and a \\t tab, ratio a:b"}
    ensure_public_safe(payload, "p")  # single backslashes / colons: clean


def test_real_private_path_still_caught_in_value():
    with pytest.raises(ValueError):
        ensure_public_safe({"text": "see /Users/alice/secret.md"}, "p")


def test_real_windows_path_still_caught():
    with pytest.raises(ValueError):
        ensure_public_safe({"text": "open C:\\Windows\\notes"}, "p")


def test_real_unc_path_still_caught():
    with pytest.raises(ValueError):
        ensure_public_safe({"text": "share at \\\\host\\dir"}, "p")


def test_bearer_and_secret_assignment_still_caught():
    with pytest.raises(ValueError):
        ensure_public_safe({"a": "Authorization: Bearer abc.def"}, "p")
    with pytest.raises(ValueError):
        ensure_public_safe({"a": "RAGFLOW_API_KEY=xyz"}, "p")


def test_private_pattern_in_dict_key_is_caught():
    # keys are scanned too (the old whole-blob scan caught keys incidentally;
    # the recursive scan keeps that).
    with pytest.raises(ValueError):
        ensure_public_safe({"/Users/x": "value"}, "p")


def test_nested_list_values_scanned():
    with pytest.raises(ValueError):
        ensure_public_safe({"items": [{"t": "ok"}, {"t": "~/private"}]}, "p")
    ensure_public_safe({"items": [{"t": "ok"}, {"t": "fine:\nnext"}]}, "p")

"""ensure_public_safe: 값 단위 raw 스캔(JSON-escape 거짓 양성 없음).

json.dumps가 실제 newline을 ``\\n``으로 escape하는 바람에 ``word:`` 뒤에 newline이
오는 무해한 body 라인이 PRIVATE_OUTPUT_RE의 Windows-path 대안 ``[A-Za-z]:\\``에
매칭되어 code/markdown session-memory body의 약 92%가 searchable mirror에서 차단되던
버그에 대한 regression guard.
"""

from __future__ import annotations

import pytest

from agent_knowledge.public_safe_util import ensure_public_safe


def test_newline_after_colon_is_not_a_false_positive():
    # "Summary:" 뒤의 실제 newline(0x0A) — 예전 JSON-blob 스캔에서는 "Summary:\n"으로
    # 직렬화되어 [A-Za-z]:\\에 매칭됐다. 값 단위 raw 스캔에서는 clean.
    payload = {"text": "Summary:\nProject:\nNotes:\n- did a thing\nDone:\n"}
    ensure_public_safe(payload, "p")  # raise하면 안 됨


def test_backslash_n_literal_and_tabs_pass():
    payload = {"text": "regex: \\d+ then \\w* and a \\t tab, ratio a:b"}
    ensure_public_safe(payload, "p")  # 단일 backslash / colon: clean


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
        ensure_public_safe({"a": "RETIRED_INDEX_BRIDGE_API_KEY=xyz"}, "p")


def test_private_pattern_in_dict_key_is_caught():
    # key도 스캔된다(예전 whole-blob 스캔은 key를 부수적으로 잡았고,
    # 재귀 스캔은 그 동작을 유지한다).
    with pytest.raises(ValueError):
        ensure_public_safe({"/Users/x": "value"}, "p")


def test_nested_list_values_scanned():
    with pytest.raises(ValueError):
        ensure_public_safe({"items": [{"t": "ok"}, {"t": "~/private"}]}, "p")
    ensure_public_safe({"items": [{"t": "ok"}, {"t": "fine:\nnext"}]}, "p")


def test_public_safe_text_bounds_large_input_before_redaction_and_redacts_boundary(monkeypatch):
    import agent_knowledge.public_safe_util as public_safe_util

    seen_lengths: list[int] = []
    original = public_safe_util.redact_public_ingress_text

    def spy_redact(value: str) -> str:
        seen_lengths.append(len(value))
        return original(value)

    monkeypatch.setattr(public_safe_util, "redact_public_ingress_text", spy_redact)

    # 1. Clean 200KB string should scan only bounded prefix (~8192 + margin), not all 200KB.
    clean = ("alpha beta gamma delta " * 9000).strip()
    assert len(clean) > 200_000
    bounded = public_safe_util.public_safe_text(clean, max_chars=8192)
    assert len(bounded) <= 8192
    assert seen_lengths and seen_lengths[-1] <= 8192 + 16384 + 128

    # 2. A secret starting just before max_chars and extending past it MUST still be redacted.
    prefix = "ok " * 2728  # ~8184 chars
    boundary_secret = prefix + "Authorization: Bearer super_secret_token_crossing_cut_boundary tail words"
    redacted_boundary = public_safe_util.public_safe_text(boundary_secret, max_chars=8192)
    assert "super_secret_token" not in redacted_boundary

    # 3. A huge secret earlier in the string that shrinks length under max_chars falls back cleanly.
    long_bearer = "Authorization: Bearer " + ("A" * 20000) + " "
    shrink_text = long_bearer + ("trailing " * 3000)
    redacted_shrink = public_safe_util.public_safe_text(shrink_text, max_chars=8192)
    assert "AAAA" not in redacted_shrink
    assert "trailing" in redacted_shrink
    assert len(redacted_shrink) == 8192


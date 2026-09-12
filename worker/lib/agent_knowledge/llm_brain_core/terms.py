"""공용 쿼리 토크나이저 (SoT).

_llm_brain_core 내 카드 필터와 graph 에피소드 필터가 서로 다른 tokenizer를
쓰던 이중 구현(context.py 1776 vs graphiti_adapter.py 1364)을 통합한다.
한글 포함 정규식 분리가 canonical이며, 최소 길이 cutoff는 호출처가 지정한다.

근거: docs/specs/lbrain-memory-recall-fix-spec.md Fix 4.
"""

from __future__ import annotations

import re
from typing import Any

_TERM_SPLIT_RE = re.compile(r"[^a-zA-Z0-9_가-힣]+")

DEFAULT_MIN_TERM_LENGTH = 3


def terms(value: Any, *, min_length: int = DEFAULT_MIN_TERM_LENGTH) -> list[str]:
    """쿼리/텍스트를 검색 토큰으로 분리한다 (소문자화, 한글 유지)."""
    return [
        term
        for term in _TERM_SPLIT_RE.split(str(value or "").lower())
        if len(term) >= min_length
    ]


def matches(value: Any, terms_: list[str]) -> bool:
    """terms 중 하나라도 value 텍스트에 포함되면 True. terms가 비면 통과."""
    if not terms_:
        return True
    text = str(value or "").lower()
    return any(term in text for term in terms_)

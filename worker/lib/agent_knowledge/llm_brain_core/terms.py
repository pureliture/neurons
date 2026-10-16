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


_HANGUL_2CHAR_RE = re.compile(r"^[가-힣]{2}$")

_KOREAN_2CHAR_STOPWORDS = frozenset({
    "최근", "지금", "오늘", "어제", "내일", "이번", "다음", "이전", "이후",
    "내가", "너는", "나는", "우리", "그냥", "매우", "아주", "정말", "진짜",
    "혹시", "아마", "계속", "다시", "먼저", "이미", "아직", "모든", "어떤",
    "이런", "그런", "저런", "무슨", "어디", "언제", "누구", "얼마", "있어",
    "없어", "맞아", "아니", "같아", "보다", "위해", "통해", "대한", "대해",
    "관한", "관해", "따라", "인해", "의해", "부터", "까지", "에서", "으로",
    "에게", "한테", "처럼", "만큼", "대로", "밖에", "말고", "하고", "이고",
    "라고", "하며", "하면", "해서", "해도", "했던", "하는", "할까", "할지",
    "건지", "인지", "는지", "은지", "지만", "거나", "든지", "라도", "마저",
    "조차", "던데", "는데", "은데", "텐데", "건데", "뭔데", "뭐야", "뭔가",
    "뭘까", "됐어", "됐나", "해줘", "줘봐", "봐줘", "보자", "볼까", "할래",
})

_KOREAN_CONVERSATIONAL_STOPWORDS = frozenset({
    "알려줘", "알려주세요", "해줘요", "해주세요", "어떻게", "어떡해",
    "무엇인가", "무엇인가요", "무엇인지", "뭔가요", "있나요", "없나요",
    "인가요", "할까요", "될까요", "맞나요", "같은데", "있을까", "있을까요",
    "보여줘", "보여주세요", "찾아줘", "찾아주세요", "검색해줘", "조회해줘",
    "설명해줘", "설명해주세요", "확인해줘", "확인해주세요",
})


def terms(value: Any, *, min_length: int = DEFAULT_MIN_TERM_LENGTH) -> list[str]:
    """쿼리/텍스트를 검색 토큰으로 분리한다 (소문자화, 한글 유지)."""
    return [
        term
        for term in _TERM_SPLIT_RE.split(str(value or "").lower())
        if len(term) >= min_length
    ]


def query_terms(value: Any) -> list[str]:
    """검색 질의에서 의미 있는 토큰을 추출한다.

    기본 길이(>=3) 토큰에 더해 2글자 한글 명사 토큰을 포함하며,
    전체가 짧은 단어로만 이루어진 질의는 길이 2 -> 1 순으로 폴백한다.
    """
    raw_tokens = [
        token
        for token in _TERM_SPLIT_RE.split(str(value or "").lower())
        if token
    ]
    if not raw_tokens:
        return []
    candidates = [
        token
        for token in raw_tokens
        if (
            len(token) >= DEFAULT_MIN_TERM_LENGTH
            and token not in _KOREAN_CONVERSATIONAL_STOPWORDS
        )
        or (
            _HANGUL_2CHAR_RE.fullmatch(token) is not None
            and token not in _KOREAN_2CHAR_STOPWORDS
        )
    ]
    if candidates:
        return list(dict.fromkeys(candidates))
    for min_len in (DEFAULT_MIN_TERM_LENGTH, 2, 1):
        fallback = [token for token in raw_tokens if len(token) >= min_len]
        if fallback:
            return list(dict.fromkeys(fallback))
    return []


def matches(value: Any, terms_: list[str]) -> bool:
    """terms 중 하나라도 value 텍스트에 포함되면 True. terms가 비면 통과."""
    if not terms_:
        return True
    text = str(value or "").lower()
    return any(term in text for term in terms_)


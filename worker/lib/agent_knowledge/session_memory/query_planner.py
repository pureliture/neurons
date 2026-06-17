from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


HASH_PREFIX = "sha256:"
MAX_QUERY_CHARS = 600
DEFAULT_CONTEXT_ITEMS = 8
MAX_CONTEXT_ITEMS = 10
ALLOWED_FILTER_KEYS = {"project", "provider", "domain", "type", "session_id_hash"}
_LOCAL_PATH_RE = re.compile(r"(?:/Users/|/private/|/var/folders/|~/)")
_SENSITIVE_VALUE_RE = re.compile(r"(secret|token|bearer|api[_-]?key|password)", re.IGNORECASE)


@dataclass(frozen=True)
class ContextQueryPlan:
    prompt_hash: str
    query: str
    query_hash: str
    filters: dict[str, str]
    max_items: int
    retrieval_limit: int
    include_private: bool
    private_allowed: bool


def sha256_text(value: str) -> str:
    return HASH_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()


def plan_context_query(
    prompt: str,
    *,
    filters: dict[str, Any] | None = None,
    max_items: int = DEFAULT_CONTEXT_ITEMS,
    include_private: bool = False,
    allow_private_results: bool = False,
) -> ContextQueryPlan:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("knowledge.context_for_prompt requires a non-empty prompt")

    bounded_items = _bounded_limit(max_items)
    query = _bounded_query(prompt)
    private_allowed = True
    return ContextQueryPlan(
        prompt_hash=sha256_text(prompt),
        query=query,
        query_hash=sha256_text(query),
        filters=_sanitize_filters(filters),
        max_items=bounded_items,
        retrieval_limit=bounded_items,
        include_private=bool(include_private),
        private_allowed=private_allowed,
    )


def _bounded_query(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    return normalized[:MAX_QUERY_CHARS]


def _bounded_limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_CONTEXT_ITEMS
    return max(1, min(MAX_CONTEXT_ITEMS, limit))


def _sanitize_filters(filters: dict[str, Any] | None) -> dict[str, str]:
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    sanitized: dict[str, str] = {}
    for key, value in filters.items():
        if value is None:
            continue
        key_text = str(key)
        value_text = str(value)
        if key_text not in ALLOWED_FILTER_KEYS:
            continue
        if _LOCAL_PATH_RE.search(value_text) or _SENSITIVE_VALUE_RE.search(value_text):
            continue
        sanitized[key_text] = value_text
    return sanitized

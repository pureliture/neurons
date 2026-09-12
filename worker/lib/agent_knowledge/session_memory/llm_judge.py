"""LLM judge for eval-loop: relevance + groundedness scoring via OpenAI-compatible bridge.

Calls a chat model (default: gemini-3.7-flash) through the existing
LLM_BRAIN_LLM_BASE_URL bridge endpoint to judge retrieval quality.

Two axes:
  1. relevance    — is each retrieved passage relevant to the query?
  2. groundedness — does the retrieved context support answering the query?

The judge does NOT mutate MemoryCards, Qdrant, Neo4j, or eval_queries.
It returns structured scores that the eval loop stores in eval_runs.metrics_json.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Mapping, Sequence

DEFAULT_JUDGE_MODEL = "gemini-3.7-flash"
JUDGE_SCHEMA_VERSION = "llm_judge.v1"


def _post_json(url: str, body: dict, *, api_key: str = "", timeout: int = 60) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _extract_json(text: str) -> dict[str, Any] | None:
    """Tolerantly extract a JSON object from LLM output (may be wrapped in ```json fences)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    # Try to find first { ... } block
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            result = json.loads(cleaned[start : end + 1])
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _judge_single(
    *,
    base_url: str,
    model: str,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 200,
) -> dict[str, Any]:
    """Call the judge model and return parsed result + usage."""
    resp = _post_json(
        f"{base_url}/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        },
        api_key=api_key,
    )
    choice = resp.get("choices", [{}])[0]
    text = choice.get("message", {}).get("content", "")
    usage = resp.get("usage", {})
    return {
        "raw_text": text[:500],
        "parsed": _extract_json(text),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def _truncate(text: str, max_chars: int = 2000) -> str:
    """Truncate text to avoid excessive prompt sizes."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"


def judge_relevance(
    *,
    base_url: str,
    model: str,
    api_key: str,
    query_text: str,
    passage_text: str,
) -> dict[str, Any]:
    """Judge whether a passage is relevant to a query."""
    system = (
        "You are a relevance judge for a memory retrieval system. "
        "Decide whether a retrieved passage is relevant to the query. "
        'Respond with ONLY a JSON object: {"relevant": true/false, "reason": "one sentence"}'
    )
    user = (
        f"QUERY:\n{_truncate(query_text)}\n\n"
        f"PASSAGE:\n{_truncate(passage_text)}\n\n"
        "ANSWER:"
    )
    result = _judge_single(
        base_url=base_url,
        model=model,
        api_key=api_key,
        system_prompt=system,
        user_prompt=user,
    )
    parsed = result.get("parsed") or {}
    return {
        "axis": "relevance",
        "relevant": bool(parsed.get("relevant", False)),
        "reason": str(parsed.get("reason", ""))[:200],
        "tokens": result["total_tokens"],
        "prompt_tokens": result["prompt_tokens"],
        "completion_tokens": result["completion_tokens"],
    }


def judge_groundedness(
    *,
    base_url: str,
    model: str,
    api_key: str,
    query_text: str,
    context_text: str,
) -> dict[str, Any]:
    """Judge whether the retrieved context is sufficient to answer the query."""
    system = (
        "You are a groundedness judge for a memory retrieval system. "
        "Decide whether the retrieved context provides enough grounding to answer the query — "
        "meaning it does not make claims beyond what the context supports. "
        'Respond with ONLY a JSON object: {"grounded": true/false, "reason": "one sentence"}'
    )
    user = (
        f"QUERY:\n{_truncate(query_text)}\n\n"
        f"RETRIEVED CONTEXT:\n{_truncate(context_text)}\n\n"
        "ANSWER:"
    )
    result = _judge_single(
        base_url=base_url,
        model=model,
        api_key=api_key,
        system_prompt=system,
        user_prompt=user,
    )
    parsed = result.get("parsed") or {}
    return {
        "axis": "groundedness",
        "grounded": bool(parsed.get("grounded", False)),
        "reason": str(parsed.get("reason", ""))[:200],
        "tokens": result["total_tokens"],
        "prompt_tokens": result["prompt_tokens"],
        "completion_tokens": result["completion_tokens"],
    }


class LLMJudgeClient:
    """Stateful judge client that accumulates token usage across calls."""

    def __init__(
        self,
        *,
        base_url: str = "",
        model: str = DEFAULT_JUDGE_MODEL,
        api_key: str = "",
    ) -> None:
        self._base_url = base_url or os.environ.get("LLM_BRAIN_LLM_BASE_URL", "")
        self._model = model
        self._api_key = api_key or os.environ.get("LLM_BRAIN_LLM_API_KEY", "")
        self._total_tokens = 0
        self._call_count = 0

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    @property
    def call_count(self) -> int:
        return self._call_count

    def judge_relevance(self, *, query_text: str, passage_text: str) -> dict[str, Any]:
        result = judge_relevance(
            base_url=self._base_url,
            model=self._model,
            api_key=self._api_key,
            query_text=query_text,
            passage_text=passage_text,
        )
        self._total_tokens += result.get("tokens", 0)
        self._call_count += 1
        return result

    def judge_groundedness(self, *, query_text: str, context_text: str) -> dict[str, Any]:
        result = judge_groundedness(
            base_url=self._base_url,
            model=self._model,
            api_key=self._api_key,
            query_text=query_text,
            context_text=context_text,
        )
        self._total_tokens += result.get("tokens", 0)
        self._call_count += 1
        return result

    def close(self) -> None:
        pass


def build_llm_judge_client(
    *,
    model: str = DEFAULT_JUDGE_MODEL,
    environ: Mapping[str, str] | None = None,
) -> LLMJudgeClient | None:
    """Build a live LLM judge client from env. Returns None if base_url is not configured."""
    env = environ or os.environ
    base_url = env.get("LLM_BRAIN_LLM_BASE_URL", "")
    if not base_url:
        return None
    api_key = env.get("LLM_BRAIN_LLM_API_KEY", "")
    return LLMJudgeClient(base_url=base_url, model=model, api_key=api_key)


__all__ = [
    "JUDGE_SCHEMA_VERSION",
    "DEFAULT_JUDGE_MODEL",
    "LLMJudgeClient",
    "build_llm_judge_client",
    "judge_relevance",
    "judge_groundedness",
]
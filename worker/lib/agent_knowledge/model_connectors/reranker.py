from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from .env import resolve_reranker_spec
from .policy import ModelConnectorConfigError, ModelPolicy
from .specs import ModelConnectionConfig, RerankerSpec

# rank_fn(query, [text, ...]) -> [score, ...] aligned with the input order.
RankFn = Callable[[str, list[str]], list[float]]


class RerankerClient(Protocol):
    """Shared reranker component consumed by graph and mirror integrations."""

    def score(self, query: str, texts: list[str]) -> list[float]:
        """Return scores aligned with ``texts``."""

    async def ascore(self, query: str, texts: list[str]) -> list[float]:
        """Async version for event-loop based consumers such as Graphiti."""


class FunctionRerankerClient:
    """RerankerClient backed by an injected local scoring function."""

    def __init__(self, rank_fn: RankFn, *, timeout_seconds: float | None = None) -> None:
        self._rank_fn = rank_fn
        self._timeout_seconds = timeout_seconds

    def score(self, query: str, texts: list[str]) -> list[float]:
        if self._timeout_seconds is None:
            scores = list(self._rank_fn(str(query or ""), list(texts)))
        else:
            scores = _run_callable_with_timeout(
                lambda: list(self._rank_fn(str(query or ""), list(texts))),
                timeout_seconds=self._timeout_seconds,
            )
        _require_score_count(scores, texts)
        return [float(score) for score in scores]

    async def ascore(self, query: str, texts: list[str]) -> list[float]:
        return self.score(query, texts)


class OpenAICompatibleRerankerClient:
    """OpenAI-compatible shared reranker using a boolean relevance prompt."""

    def __init__(
        self,
        spec: RerankerSpec,
        *,
        api_key: str = "",
        openai_client: Any | None = None,
        policy: ModelPolicy | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        validator = policy or ModelPolicy()
        validator.validate(spec, capability="rerank")
        if spec.provider != "ollama" and not str(spec.model or "").strip():
            raise ModelConnectorConfigError("model connector missing required config: LLM_BRAIN_LLM_MODEL")

        from openai import AsyncOpenAI

        base_url = spec.base_url or ("http://localhost:11434/v1" if spec.provider == "ollama" else "")
        self._model = spec.model or ("deepseek-r1:7b" if spec.provider == "ollama" else "")
        self._timeout_seconds = timeout_seconds
        self._client = openai_client or AsyncOpenAI(
            api_key=api_key or ("ollama" if spec.provider == "ollama" else None),
            base_url=base_url or None,
        )

    def score(self, query: str, texts: list[str]) -> list[float]:
        return _run_coroutine(self.ascore(query, texts), timeout_seconds=self._timeout_seconds)

    async def ascore(self, query: str, texts: list[str]) -> list[float]:
        passages = list(texts)
        if not passages:
            return []
        scores = await asyncio.gather(
            *[self._score_passage(str(query or ""), passage) for passage in passages]
        )
        _require_score_count(scores, passages)
        return [float(score) for score in scores]

    async def _score_passage(self, query: str, passage: str) -> float:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": "Decide whether a passage is relevant to a query. Answer True or False.",
                },
                {
                    "role": "user",
                    "content": (
                        'Respond with "True" if PASSAGE is relevant to QUERY and "False" otherwise.\n'
                        f"<PASSAGE>\n{passage}\n</PASSAGE>\n"
                        f"<QUERY>\n{query}\n</QUERY>"
                    ),
                },
            ],
            temperature=0,
            max_tokens=1,
            logprobs=True,
            top_logprobs=2,
        )
        return _score_from_response(response)


class GraphitiCrossEncoderAdapter:
    """Graphiti cross_encoder facade over the shared RerankerClient."""

    def __init__(self, reranker: RerankerClient) -> None:
        self._reranker = reranker

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        scores = await self._reranker.ascore(str(query or ""), list(passages))
        _require_score_count(scores, passages)
        ranked = [(passage, float(score)) for passage, score in zip(passages, scores, strict=True)]
        ranked.sort(reverse=True, key=lambda item: item[1])
        return ranked


class CandidateReranker:
    """Reorders dictionary candidates with a shared RerankerClient."""

    def __init__(self, client: RerankerClient, *, text_key: str = "summary") -> None:
        self._client = client
        self._text_key = str(text_key or "summary")

    def rerank(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
        top_n: int = 5,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        texts = [str(item.get(self._text_key) or item.get("summary") or "") for item in candidates]
        scores = self._client.score(str(query or ""), texts)
        _require_score_count(scores, candidates)
        order = sorted(range(len(candidates)), key=lambda index: float(scores[index]), reverse=True)
        ranked: list[dict[str, Any]] = []
        for index in order[: max(1, int(top_n))]:
            item = dict(candidates[index])
            item["rerank_score"] = float(scores[index])
            ranked.append(item)
        return ranked


def resolve_reranker_config(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve reranker connection config. API keys stay at client-build edges.

    The returned base_url can identify a host; do not log it without redaction.
    """

    spec = resolve_reranker_spec(environ)
    return {
        "model": spec.model,
        "base_url": spec.base_url,
    }


def build_reranker_client(
    config: ModelConnectionConfig,
    *,
    api_key: str = "",
    openai_client: Any | None = None,
    rank_fn: RankFn | None = None,
    policy: ModelPolicy | None = None,
    timeout_seconds: float | None = None,
) -> RerankerClient:
    if rank_fn is not None:
        return FunctionRerankerClient(rank_fn, timeout_seconds=timeout_seconds)
    return OpenAICompatibleRerankerClient(
        config.reranker,
        api_key=api_key,
        openai_client=openai_client,
        policy=policy,
        timeout_seconds=timeout_seconds,
    )


def build_candidate_reranker(
    config: ModelConnectionConfig,
    *,
    api_key: str = "",
    rank_fn: RankFn | None = None,
    policy: ModelPolicy | None = None,
    timeout_seconds: float | None = None,
) -> CandidateReranker:
    return CandidateReranker(
        build_reranker_client(
            config,
            api_key=api_key,
            rank_fn=rank_fn,
            policy=policy,
            timeout_seconds=timeout_seconds,
        )
    )


def _require_score_count(scores: list[Any], texts: list[Any]) -> None:
    if len(scores) != len(texts):
        raise ValueError("reranker returned wrong score count")


def _run_coroutine(awaitable: Awaitable[list[float]], *, timeout_seconds: float | None = None) -> list[float]:
    async def _await_with_timeout() -> list[float]:
        if timeout_seconds is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await_with_timeout())

    result: list[float] | None = None
    error: BaseException | None = None

    def _target() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(_await_with_timeout())
        except BaseException as exc:
            error = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        raise TimeoutError("reranker timed out")
    if error is not None:
        raise error
    return list(result or [])


def _run_callable_with_timeout(fn: Callable[[], list[float]], *, timeout_seconds: float) -> list[float]:
    result: list[float] | None = None
    error: BaseException | None = None

    def _target() -> None:
        nonlocal result, error
        try:
            result = fn()
        except BaseException as exc:
            error = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        raise TimeoutError("reranker timed out")
    if error is not None:
        raise error
    return list(result or [])


def _score_from_response(response: Any) -> float:
    choices = list(getattr(response, "choices", []) or [])
    if not choices:
        return 0.0
    choice = choices[0]
    top_logprobs = _top_logprobs(choice)
    if top_logprobs:
        first = top_logprobs[0]
        token = str(getattr(first, "token", "") or "").strip().split(" ")[0].lower()
        logprob = float(getattr(first, "logprob", 0.0) or 0.0)
        probability = math.exp(logprob)
        return probability if token == "true" else 1.0 - probability
    content = str(getattr(getattr(choice, "message", None), "content", "") or "").strip().lower()
    return 1.0 if content.startswith("true") else 0.0


def _top_logprobs(choice: Any) -> list[Any]:
    logprobs = getattr(choice, "logprobs", None)
    if logprobs is None:
        return []
    content = getattr(logprobs, "content", None)
    if not content:
        return []
    return list(getattr(content[0], "top_logprobs", []) or [])


__all__ = [
    "CandidateReranker",
    "FunctionRerankerClient",
    "GraphitiCrossEncoderAdapter",
    "OpenAICompatibleRerankerClient",
    "RankFn",
    "RerankerClient",
    "build_candidate_reranker",
    "build_reranker_client",
    "resolve_reranker_config",
]

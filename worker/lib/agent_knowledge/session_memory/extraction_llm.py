"""Instruction-following extraction LLM for the autopilot miner.

The RAGFlow chat assistant is a conversational/RAG surface (it answers prose, not strict
JSON) so it is unsuitable for structured extraction. This module provides an OpenAI-compatible
completion_fn — by default the keyless local vertex-wrapper (per ~/.graphify/providers.json,
the project's headless extraction backend) — used as the envelope miner's completion_fn.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable

DEFAULT_PROVIDERS_PATH = "~/.graphify/providers.json"


def _urllib_post(url: str, *, headers: dict, body: str) -> str:
    request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8")


def build_openai_compat_completion_fn(
    base_url: str,
    model: str,
    *,
    api_key: str = "",
    temperature: float = 0.0,
    post_fn: Callable[..., str] = _urllib_post,
) -> Callable[[list[dict]], str]:
    """An OpenAI-compatible /chat/completions completion_fn: messages -> assistant content."""

    url = base_url.rstrip("/") + "/chat/completions"

    def completion_fn(messages: list[dict]) -> str:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = json.dumps({"model": model, "messages": list(messages), "temperature": temperature})
        try:
            raw = post_fn(url, headers=headers, body=body)
            data = json.loads(raw)
            return str(data["choices"][0]["message"]["content"] or "")
        except Exception:
            return ""

    return completion_fn


def build_openai_compat_embedding_fn(
    base_url: str,
    model: str,
    *,
    api_key: str = "",
    post_fn: Callable[..., str] = _urllib_post,
) -> Callable[[str], list[float]]:
    """An OpenAI-compatible /embeddings embed_fn: text -> embedding vector."""

    url = base_url.rstrip("/") + "/embeddings"

    def embed_fn(text: str) -> list[float]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = json.dumps({"model": model, "input": text})
        try:
            data = json.loads(post_fn(url, headers=headers, body=body))
            return [float(x) for x in data["data"][0]["embedding"]]
        except Exception:
            return []

    return embed_fn


def build_vertex_embedding_fn(
    *,
    providers_path: str = DEFAULT_PROVIDERS_PATH,
    model: str = "gemini-embedding-001",
    post_fn: Callable[..., str] = _urllib_post,
) -> Callable[[str], list[float]]:
    with open(os.path.expanduser(providers_path), encoding="utf-8") as handle:
        config = json.load(handle)
    providers = config if isinstance(config, list) else config.get("providers", config)
    vw = providers.get("vertex-wrapper") if isinstance(providers, dict) else next(
        (p for p in providers if p.get("name") == "vertex-wrapper"), None
    )
    if not vw:
        raise ValueError("vertex-wrapper provider not found in providers.json")
    env_key = str(vw.get("env_key") or "")
    api_key = os.environ.get(env_key, "") if env_key else ""
    return build_openai_compat_embedding_fn(str(vw["base_url"]), model, api_key=api_key, post_fn=post_fn)


def build_vertex_wrapper_completion_fn(
    *,
    providers_path: str = DEFAULT_PROVIDERS_PATH,
    post_fn: Callable[..., str] = _urllib_post,
) -> Callable[[list[dict]], str]:
    """Load the vertex-wrapper provider (base_url/default_model) and build its completion_fn.

    Keyless by default; if the provider declares an env_key and that env var is set, it is used.
    """
    with open(os.path.expanduser(providers_path), encoding="utf-8") as handle:
        config = json.load(handle)
    providers = config if isinstance(config, list) else config.get("providers", config)
    if isinstance(providers, dict):
        vw = providers.get("vertex-wrapper")
    else:
        vw = next((p for p in providers if p.get("name") == "vertex-wrapper"), None)
    if not vw:
        raise ValueError("vertex-wrapper provider not found in providers.json")
    env_key = str(vw.get("env_key") or "")
    api_key = os.environ.get(env_key, "") if env_key else ""
    return build_openai_compat_completion_fn(
        str(vw["base_url"]),
        str(vw.get("default_model") or ""),
        api_key=api_key,
        temperature=float(vw.get("temperature") or 0.0),
        post_fn=post_fn,
    )

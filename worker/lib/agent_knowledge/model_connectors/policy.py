from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PolicyViolation(ValueError):
    """Raised when a model provider is not allowed for a capability."""


class ModelConnectorConfigError(ValueError):
    """Raised when a live model connector is missing required non-secret config."""


@dataclass(frozen=True)
class ModelPolicy:
    """Capability-level model policy with provider-specific guardrails."""

    denied_chat_providers: frozenset[str] = frozenset({"gemini", "google-gemini"})
    chat_providers: frozenset[str] = frozenset(
        {"openai", "openai-compatible", "openai_compatible", "ollama", "gemma4-maas"}
    )
    embedding_providers: frozenset[str] = frozenset(
        {"openai", "openai-compatible", "openai_compatible", "ollama", "gemma4-maas"}
    )

    def validate(self, spec: Any, *, capability: str) -> None:
        provider = _provider(spec)
        capability_name = str(capability or "").strip().lower().replace("-", "_")
        if provider in self.denied_chat_providers and capability_name in {
            "chat",
            "structured_extraction",
            "rerank",
        }:
            raise PolicyViolation(f"provider not allowed for {capability_name}")
        if capability_name in {"chat", "structured_extraction", "rerank"}:
            if provider not in self.chat_providers:
                raise PolicyViolation(f"provider not allowed for {capability_name}")
        elif capability_name == "embedding" and provider:
            if provider not in self.embedding_providers:
                raise PolicyViolation("provider not allowed for embedding")


def _provider(spec: Any) -> str:
    provider = getattr(spec, "provider", None)
    if provider is None and isinstance(spec, dict):
        provider = spec.get("provider")
    return str(provider or "").strip().lower()

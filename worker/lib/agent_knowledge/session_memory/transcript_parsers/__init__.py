from __future__ import annotations

"""Provider transcript parsers — public facade.

Native provider implementations live in sibling modules; this package root owns
dispatch registries and re-exports the stable import path
``agent_knowledge.session_memory.transcript_parsers``.
"""

from pathlib import Path

from ..transcript_model import canonicalize_provider
from .providers.antigravity import extract_antigravity_tool_evidence, _parse_antigravity_native_jsonl
from .providers.claude import extract_claude_tool_evidence, _parse_claude_native_jsonl
from .providers.codex import extract_codex_tool_evidence, _parse_codex_native_jsonl
from .common import (
    GROK_PARSER_VERSION,
    PARSER_VERSION,
    TOOL_EVIDENCE_EXTRACTOR_VERSION,
    ParsedTranscript,
)
from .providers.fixture import _parse_provider_fixture
from .providers.gemini import extract_gemini_tool_evidence, _parse_gemini_native_jsonl
from .common import _load_json_source
from .providers.grok import extract_grok_tool_evidence, _parse_grok_native_jsonl

_NATIVE_PARSERS = {
    "claude": _parse_claude_native_jsonl,
    "gemini": _parse_gemini_native_jsonl,
    "codex": _parse_codex_native_jsonl,
    "antigravity": _parse_antigravity_native_jsonl,
    "grok": _parse_grok_native_jsonl,
}

_TOOL_EVIDENCE_EXTRACTORS = {
    "codex": extract_codex_tool_evidence,
    "claude": extract_claude_tool_evidence,
    "gemini": extract_gemini_tool_evidence,
    "antigravity": extract_antigravity_tool_evidence,
    "grok": extract_grok_tool_evidence,
}

_SUPPORTED_PROVIDERS = {"claude", "gemini", "codex", "antigravity", "hermes", "grok"}


def parse_transcript_source(
    provider: str,
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
) -> ParsedTranscript:
    provider = canonicalize_provider(provider)
    # hermes has no native transcript format yet; it ingests via the generic
    # provider_transcript_fixture.v1 path below, like any non-native provider.
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider}")
    path = Path(source_path)
    native = _NATIVE_PARSERS.get(provider)
    if native is not None and path.suffix.lower() == ".jsonl":
        return native(path, project=project, source_locator_hash=source_locator_hash)
    payload = _load_json_source(path)
    if canonicalize_provider(payload.get("provider")) != provider:
        raise ValueError("source_parse_failed: provider mismatch")
    if payload.get("schema_version") != "provider_transcript_fixture.v1":
        raise ValueError("source_parse_failed: unsupported fixture schema")
    return _parse_provider_fixture(provider, payload, project=project, source_locator_hash=source_locator_hash)


def extract_tool_evidence(
    provider: str,
    source_path: Path | str,
    *,
    project: str,
    source_locator_hash: str,
):
    """Dispatch tool-evidence extraction by provider."""
    provider = canonicalize_provider(provider)
    extractor = _TOOL_EVIDENCE_EXTRACTORS.get(provider)
    if extractor is None:
        raise ValueError(f"unsupported provider: {provider}")
    return extractor(source_path, project=project, source_locator_hash=source_locator_hash)


__all__ = [
    "PARSER_VERSION",
    "TOOL_EVIDENCE_EXTRACTOR_VERSION",
    "GROK_PARSER_VERSION",
    "ParsedTranscript",
    "parse_transcript_source",
    "extract_tool_evidence",
    "extract_codex_tool_evidence",
    "extract_claude_tool_evidence",
    "extract_gemini_tool_evidence",
    "extract_antigravity_tool_evidence",
    "extract_grok_tool_evidence",
]

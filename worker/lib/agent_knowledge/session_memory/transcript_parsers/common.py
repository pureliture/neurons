from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..transcript_model import (
    TranscriptSession,
    TranscriptToolEvent,
    TranscriptTurn,
)

PARSER_VERSION = "provider-transcript-parser.v1"
TOOL_EVIDENCE_EXTRACTOR_VERSION = "codex-tool-evidence-extractor.v1"
GROK_PARSER_VERSION = "grok-updates-jsonl-parser.v1"


@dataclass(frozen=True)
class ParsedTranscript:
    session: TranscriptSession
    turns: list[TranscriptTurn]
    tool_events: list[TranscriptToolEvent] = field(default_factory=list)
    parser_warnings: list[str] = field(default_factory=list)
    source_status: str = "source_locator_private_spool_only"


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_json_source(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError("source_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("source_parse_failed: invalid json") from exc
    if not isinstance(payload, dict):
        raise ValueError("source_parse_failed: source root must be an object")
    return payload


def _load_jsonl_source(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("source_parse_failed: jsonl record must be an object")
                records.append(record)
    except FileNotFoundError as exc:
        raise ValueError("source_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("source_parse_failed: invalid jsonl") from exc
    if not records:
        raise ValueError("source_parse_failed: empty jsonl")
    return records


def _extract_message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _extract_claude_message_text(content) -> str:
    return _extract_message_text(content)


def _normalize_role(role) -> str:
    role_text = str(role or "").lower()
    if role_text in {"assistant", "model"}:
        return "assistant"
    if role_text == "user":
        return "user"
    if role_text.startswith("tool"):
        return "tool_summary"
    return "system_observed"

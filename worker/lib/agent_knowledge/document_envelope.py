from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


DOCUMENT_SCHEMA_VERSION = "agent_knowledge_document.v2"
LEDGER_CONTRACT_VERSION = "agent_knowledge_ledger.v3"


@dataclass(frozen=True)
class ConversationEnvelopeInput:
    result_type: str
    knowledge_id: str
    provider: str
    project: str
    agent_id: str
    session_id_hash: str
    source_locator_hash: str
    chunk_id: str
    turn_start_index: int
    turn_end_index: int
    observed_at_start: str
    observed_at_end: str
    privacy_level: str
    redaction_version: str
    parser_version: str
    source_status: str
    capture_request_id: str = ""
    part_index: int = 1
    part_count: int = 1
    char_start: int = 0
    char_end: int = 0


def build_conversation_document_metadata(envelope: ConversationEnvelopeInput) -> dict:
    return {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "result_type": envelope.result_type,
        "knowledge_id": envelope.knowledge_id,
        "provider": envelope.provider,
        "project": envelope.project,
        "agent_id": envelope.agent_id,
        "session_id_hash": envelope.session_id_hash,
        "source_locator_hash": envelope.source_locator_hash,
        "chunk_id": envelope.chunk_id,
        "turn_start_index": envelope.turn_start_index,
        "turn_end_index": envelope.turn_end_index,
        "part_index": envelope.part_index,
        "part_count": envelope.part_count,
        "char_start": envelope.char_start,
        "char_end": envelope.char_end,
        "observed_at_start": envelope.observed_at_start,
        "observed_at_end": envelope.observed_at_end,
        "privacy_level": envelope.privacy_level,
        "redaction_version": envelope.redaction_version,
        "parser_version": envelope.parser_version,
        "source_status": envelope.source_status,
        "domain": "agent_memory",
        "type": "conversation_chunk",
        "provenance": {
            "capture_request_id": envelope.capture_request_id,
            "provider_source_contract": f"{envelope.provider}-transcript-source.v1",
            "ledger_contract": LEDGER_CONTRACT_VERSION,
        },
        "retrieval_hints": {
            "questions": [],
            "tags": [
                envelope.provider,
                envelope.project,
                "conversation_chunk",
            ],
        },
        "retention": {
            "policy": "private_indefinite_until_disabled",
            "supersedes": "",
        },
    }


def build_agent_id(*, provider: str, producer: str) -> str:
    return f"{_slug(provider)}-{_slug(producer)}"


def build_index_meta_fields(metadata: dict) -> dict:
    provenance = metadata.get("provenance") or {}
    retention = metadata.get("retention") or {}
    meta_fields = {
        "schema_version": metadata["schema_version"],
        "result_type": metadata["result_type"],
        "knowledge_id": metadata["knowledge_id"],
        "provider": metadata["provider"],
        "project": metadata["project"],
        "agent_id": metadata["agent_id"],
        "session_id_hash": metadata.get("session_id_hash", ""),
        "source_locator_hash": metadata["source_locator_hash"],
        "chunk_id": metadata["chunk_id"],
        "turn_start_index": metadata["turn_start_index"],
        "turn_end_index": metadata["turn_end_index"],
        "part_index": metadata.get("part_index", 1),
        "part_count": metadata.get("part_count", 1),
        "char_start": metadata.get("char_start", 0),
        "char_end": metadata.get("char_end", 0),
        "observed_at_start": metadata["observed_at_start"],
        "observed_at_end": metadata["observed_at_end"],
        "privacy_level": metadata["privacy_level"],
        "redaction_version": metadata["redaction_version"],
        "parser_version": metadata["parser_version"],
        "source_status": metadata["source_status"],
        "domain": metadata["domain"],
        "type": metadata["type"],
        "capture_request_id": provenance.get("capture_request_id", ""),
        "provider_source_contract": provenance.get("provider_source_contract", ""),
        "ledger_contract": provenance.get("ledger_contract", ""),
        "retention_policy": retention.get("policy", ""),
        "supersedes": retention.get("supersedes", ""),
    }
    for date_field in ("observed_at_start", "observed_at_end"):
        if not meta_fields.get(date_field):
            meta_fields.pop(date_field, None)
    return meta_fields


def build_document_filename(
    *,
    kind: str,
    provider: str,
    project: str,
    session_id_hash: str,
    turn_start_index: int,
    turn_end_index: int,
    observed_at_start: str,
    content: str,
) -> str:
    short_kind = {"conversation_chunk": "conv"}.get(kind, _slug(kind))
    session_hash12 = _hash_fragment(session_id_hash, 12)
    content_hash12 = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    observed_utc = _compact_utc_timestamp(observed_at_start)
    if kind == "project_context_snapshot":
        return f"{_slug(project)}.md"
    if kind == "runtime_evidence":
        return (
            f"ak-runtime-evidence-{_slug(project)}-{_slug(provider)}-{session_hash12}"
            f"-t{turn_start_index:04d}-{turn_end_index:04d}-{observed_utc}-{content_hash12}.md"
        )
    return (
        f"ak-{short_kind}-{_slug(provider)}-{_slug(project)}-{session_hash12}"
        f"-t{turn_start_index:04d}-{turn_end_index:04d}-{observed_utc}-{content_hash12}.md"
    )


def render_markdown_document(metadata: dict, body_lines: Iterable[str], *, max_chars: int | None = None) -> str:
    body = "\n".join(body_lines).rstrip() + "\n"
    if max_chars is not None:
        body = _bound_body_for_envelope(metadata, body, max_chars)
    return f"---\n{_render_yaml(metadata)}---\n{body}"


def _render_yaml(value: dict, *, indent: int = 0) -> str:
    lines = []
    prefix = " " * indent
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append(f"{prefix}{key}:")
            lines.append(_render_yaml(item, indent=indent + 2).rstrip())
        elif isinstance(item, list):
            if not item:
                lines.append(f"{prefix}{key}: []")
            else:
                lines.append(f"{prefix}{key}:")
                for entry in item:
                    if isinstance(entry, dict):
                        lines.append(f"{prefix}  -")
                        lines.append(_render_yaml(entry, indent=indent + 4).rstrip())
                    else:
                        lines.append(f"{prefix}  - {_yaml_scalar(entry)}")
        else:
            lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
    return "\n".join(lines) + "\n"


def _bound_body_for_envelope(metadata: dict, body: str, max_chars: int) -> str:
    prefix = f"---\n{_render_yaml(metadata)}---\n"
    if len(prefix) >= max_chars:
        raise ValueError("document envelope metadata exceeds maximum document size")
    available = max_chars - len(prefix)
    if len(body) <= available:
        return body
    marker = "\n[truncated]\n"
    if available <= len(marker):
        return body[:available]
    return body[: available - len(marker)] + marker


def _yaml_scalar(value) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text:
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_.:/@+-]+", text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def _hash_fragment(value: str, length: int) -> str:
    if ":" in value:
        value = value.split(":", 1)[1]
    value = re.sub(r"[^a-fA-F0-9]", "", value)
    if len(value) >= length:
        return value[:length].lower()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _compact_utc_timestamp(value: str) -> str:
    if not value:
        return "unknown-time"
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return _slug(value)[:32] or "unknown-time"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

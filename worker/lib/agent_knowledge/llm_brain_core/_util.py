from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from agent_knowledge.redaction import redact_public_ingress_text
from agent_knowledge.session_memory.transcript_model import bound_text


SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_:-]{2,160}$")
PRIVATE_OUTPUT_RE = re.compile(
    r"(/Users/|~/|/private/|/Volumes/|\bBearer\s+|\braw[_ -]?transcript\b)",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"\b[A-Z0-9_]*(TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)\b\s*[:=]",
    re.IGNORECASE,
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_payload(value: Any) -> str:
    return sha256_text(stable_json(value))


def short_hash(value: Any, *, length: int = 16) -> str:
    return hash_payload(value).split(":", 1)[1][:length]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def require_non_empty(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def require_sha256(value: str, field: str) -> str:
    text = require_non_empty(value, field)
    if not SHA256_RE.fullmatch(text):
        raise ValueError(f"{field} must be sha256:<64 hex>")
    return text


def require_opaque_id(value: str, field: str) -> str:
    text = require_non_empty(value, field)
    if not OPAQUE_ID_RE.fullmatch(text):
        raise ValueError(f"{field} must be an opaque id")
    ensure_public_safe(text, field)
    return text


def public_safe_text(value: str, *, max_chars: int = 2048) -> str:
    normalized = " ".join(str(value or "").split())
    return bound_text(redact_public_ingress_text(normalized), max_chars)


def ensure_public_safe(value: Any, field: str = "value") -> None:
    text = stable_json(value) if isinstance(value, (dict, list, tuple)) else str(value or "")
    if PRIVATE_OUTPUT_RE.search(text) or SECRET_ASSIGNMENT_RE.search(text):
        raise ValueError(f"{field} contains private or raw content")


def public_dict(value: dict[str, Any], *, field: str = "value") -> dict[str, Any]:
    ensure_public_safe(value, field)
    return value

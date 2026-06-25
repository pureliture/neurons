"""Delivery-safe public-safe / hashing / validation helpers.

These were originally housed in ``llm_brain_core._util``, but importing them from
there triggers the heavy ``llm_brain_core`` package ``__init__`` (graphiti, neo4j,
ontology, ...), which is NOT present in the ingress/delivery worker's vendored lib
subset. The searchable-mirror adapter (``rag_ingress.qdrant_docling_mirror``) needs
these helpers in that delivery subset, so they live here at the top level where the
only dependencies are ``agent_knowledge.redaction`` and
``agent_knowledge.session_memory.transcript_model`` (both already vendored in the
worker; ``session_memory`` uses lazy exports so importing ``transcript_model`` does
not drag heavy brain modules).

``llm_brain_core._util`` re-exports from here for backwards compatibility.
"""

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
    r"(/Users/|~/|/private/|/Volumes/|[A-Za-z]:\\|\\\\[A-Za-z0-9_.-]+|\bBearer\s+|\braw[_ -]?transcript\b)",
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


def list_or_empty(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


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


def _scan_public_safe_text(text: str, field: str) -> None:
    if PRIVATE_OUTPUT_RE.search(text) or SECRET_ASSIGNMENT_RE.search(text):
        raise ValueError(f"{field} contains private or raw content")


def ensure_public_safe(value: Any, field: str = "value") -> None:
    """Reject private/raw content in ``value`` (a leak guard for stored/emitted data).

    For containers, every key and leaf string is scanned with its RAW string form
    (recursively) -- NOT the JSON-serialized blob. Scanning the JSON blob caused
    false positives: ``json.dumps`` escapes real newlines as ``\\n`` and other control
    chars with backslashes, so a benign body line ending in ``word:`` followed by a
    newline serialized to ``word:\\n`` and matched the Windows-path alternative
    ``[A-Za-z]:\\`` in ``PRIVATE_OUTPUT_RE``. Per-value raw scanning still catches
    real ``/Users/`` / ``Bearer`` / ``C:\\path`` / UNC ``\\\\host`` / ``API_KEY:``
    content (and now scans dict keys too), while dropping escape-artifact matches.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_public_safe_text(str(key), field)
            ensure_public_safe(item, field)
    elif isinstance(value, (list, tuple)):
        for item in value:
            ensure_public_safe(item, field)
    else:
        # str(value) (not ``value or ""``) so falsy scalars like 0/False are scanned
        # as their real text rather than collapsed to "".
        _scan_public_safe_text(str(value) if value is not None else "", field)


def public_dict(value: dict[str, Any], *, field: str = "value") -> dict[str, Any]:
    ensure_public_safe(value, field)
    return value

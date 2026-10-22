"""Backend-neutral document index boundary.

The generic ingress bus hands a :class:`RagReadyDocument` to an
:class:`IndexAdapter`. The adapter owns *all* backend specifics:

- logical ``target_profile`` -> physical dataset id resolution,
- the concrete client call shape (upload/parse/status),
- the backend status vocabulary, which the adapter translates into the
  generic :class:`IndexStatus`.

This module carries no vendor wire protocol. Every transport lives in the
concrete adapter: the CouchDB adapter is
``couchdb_index_adapter.py``, the Qdrant adapter is
``qdrant_docling_mirror.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from .rag_ready_document import RagReadyDocument


class IndexStatus:
    """Backend-neutral lifecycle status. Never uses a backend's own vocabulary."""

    PENDING = "pending"
    INDEXING = "indexing"
    INDEXED = "indexed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BackendDocumentHandle:
    """Opaque reference to a submitted document in some backend."""

    dataset_ref: str
    document_ref: str


@dataclass(frozen=True)
class BackendSubmitResult:
    dataset_ref: str
    document_ref: str
    status: str


@dataclass(frozen=True)
class BackendStatusDetail:
    """Detailed, backend-neutral status.

    ``status`` is the generic :class:`IndexStatus`. ``progress`` is a generic
    0..1 float. ``backend_raw_status`` is an *opaque* pass-through string that the
    generic layer never interprets -- the adapter fills it with whatever the
    backend reported so a caller that owns a legacy ledger column can persist it
    without the generic layer learning the backend's words.
    """

    status: str
    progress: float = 0.0
    backend_raw_status: str = ""


# Optional per-step hook for callers that need granular lifecycle observability
# (e.g. recording ledger marks between upload/metadata/parse). The generic layer
# never requires it; it stays None for the simple submit path.
StepHook = Optional[Callable[..., None]]
NATURAL_KEY_PAGE_SIZE = 100
NATURAL_KEY_BROAD_SCAN_MAX_PAGES = 0


@runtime_checkable
class IndexAdapter(Protocol):
    def submit_document(
        self, document: RagReadyDocument, *, on_step_complete: StepHook = None
    ) -> BackendSubmitResult: ...

    def find_by_natural_key(
        self, *, target_profile: str, idempotency_key: str, payload_hash: str
    ) -> BackendDocumentHandle | None: ...

    def document_status(self, handle: BackendDocumentHandle) -> str: ...

    def document_status_detail(self, handle: BackendDocumentHandle) -> BackendStatusDetail: ...


def notify_step(hook: StepHook, step: str, **fields) -> None:
    """Invoke an optional per-step observer. No-op when no hook is supplied."""

    if hook is not None:
        hook(step, **fields)


def _natural_key_keywords(payload_hash: str) -> list[str]:
    keywords: list[str] = []
    for candidate in (payload_hash, _content_hash_fragment(payload_hash)):
        if candidate and candidate not in keywords:
            keywords.append(candidate)
    return keywords


def _content_hash_fragment(payload_hash: str) -> str:
    if payload_hash.startswith("sha256:") and len(payload_hash) >= 19:
        return payload_hash[7:19]
    return ""


def _document_matches_natural_key(doc: dict, *, idempotency_key: str, payload_hash: str) -> bool:
    metadata = doc.get("meta_fields") or doc.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    content_hash = str(
        metadata.get("content_hash")
        or metadata.get("contentHash")
        or doc.get("content_hash")
        or doc.get("contentHash")
        or ""
    )
    if content_hash != payload_hash:
        return False
    # Fail-safe: require the idempotency_key to be present AND equal. content_hash
    # is a function of body only (build_content_hash), so two documents with the
    # same body but different source_namespace share a content_hash while having
    # distinct idempotency_keys. Matching on content_hash alone (a doc whose
    # metadata lacks idempotency_key -- e.g. legacy/manual uploads) would
    # false-dedup a distinct document and silently drop its upload. The worker
    # injects idempotency_key on every upload (submit_document), so its own
    # documents always carry it; anything without it is conservatively re-uploaded.
    recorded_key = str(metadata.get("idempotency_key") or metadata.get("idempotencyKey") or "")
    return bool(recorded_key) and recorded_key == idempotency_key

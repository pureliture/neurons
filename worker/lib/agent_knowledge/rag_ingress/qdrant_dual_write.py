"""M6 dual-write shadow seam: primary IndexBackendAdapter + best-effort mirror.

The primary backend (RAGFlow today, CouchDB alternative) stays the authority of
record. The Qdrant mirror submit is best-effort: a mirror failure is captured as an
outcome and NEVER breaks or alters the primary submit result. find/status delegate
to the primary so the mirror cannot influence dedup or status.

This is the code-only seam for Stage 2 (M6). It is NOT wired into the live
``shadow_worker`` entrypoint here; activation (an env branch that constructs this
backend) is added at deploy time once a Qdrant instance exists. With no Qdrant
deployed and the flag off, the existing RAGFlow/CouchDB delivery is byte-identical.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .index_backend import (
    BackendDocumentHandle,
    BackendStatusDetail,
    BackendSubmitResult,
    StepHook,
)
from .rag_ready_document import RagReadyDocument

MirrorOutcomeHook = Optional[Callable[["MirrorWriteOutcome"], None]]


@dataclass(frozen=True)
class MirrorWriteOutcome:
    """Best-effort mirror write result, observed alongside the authoritative write.

    ``status`` is ``mirrored`` (mirror upsert succeeded), ``mirror_skipped`` (no
    mirror configured), or ``mirror_error`` (mirror raised; primary unaffected).
    ``error_class`` is the exception class name only -- never a message/payload.
    """

    status: str
    document_ref: str = ""
    error_class: str = ""


class MirrorDualWriteBackend:
    """``IndexBackendAdapter`` wrapping a primary + an optional best-effort mirror."""

    def __init__(
        self,
        *,
        primary: Any,
        mirror: Any | None = None,
        on_mirror_outcome: MirrorOutcomeHook = None,
    ) -> None:
        self._primary = primary
        self._mirror = mirror
        self._on_mirror_outcome = on_mirror_outcome

    def submit_document(
        self, document: RagReadyDocument, *, on_step_complete: StepHook = None
    ) -> BackendSubmitResult:
        # Authoritative write first. If the primary raises, propagate immediately
        # and do NOT touch the mirror (no partial/uncertain mirror state on a
        # failed primary).
        result = self._primary.submit_document(document, on_step_complete=on_step_complete)
        outcome = self._mirror_submit(document)
        if self._on_mirror_outcome is not None:
            self._on_mirror_outcome(outcome)
        return result

    def _mirror_submit(self, document: RagReadyDocument) -> MirrorWriteOutcome:
        if self._mirror is None:
            return MirrorWriteOutcome(status="mirror_skipped")
        try:
            mirror_result = self._mirror.submit_document(document)
        except Exception as exc:  # best-effort: never break the authoritative write
            return MirrorWriteOutcome(status="mirror_error", error_class=exc.__class__.__name__)
        return MirrorWriteOutcome(
            status="mirrored",
            document_ref=str(getattr(mirror_result, "document_ref", "") or ""),
        )

    # find/status are authoritative -> primary only. The mirror never influences
    # dedup or lifecycle status.
    def find_by_natural_key(
        self, *, target_profile: str, idempotency_key: str, payload_hash: str
    ) -> BackendDocumentHandle | None:
        return self._primary.find_by_natural_key(
            target_profile=target_profile,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )

    def document_status(self, handle: BackendDocumentHandle) -> str:
        return self._primary.document_status(handle)

    def document_status_detail(self, handle: BackendDocumentHandle) -> BackendStatusDetail:
        return self._primary.document_status_detail(handle)


def build_qdrant_mirror_from_env(environ: Any) -> Any | None:
    """Build the Qdrant mirror adapter from env, or None when not configured.

    Requires ``QDRANT_URL``; reuses the OpenAI-compatible embedding provider
    (``LLM_BRAIN_EMBEDDING_*``) and a passthrough normalizer (bodies are already
    redacted markdown). Returns None when ``QDRANT_URL`` is unset so the caller
    fails safe to primary-only delivery. The qdrant-client import is lazy (live
    path only); tests inject a mirror builder instead.
    """

    url = str(environ.get("QDRANT_URL") or "").strip()
    if not url:
        return None
    from .qdrant_docling_mirror import (
        DEFAULT_COLLECTION_NAME,
        PassthroughMarkdownNormalizer,
        build_remote_qdrant_docling_mirror_adapter,
    )
    from .qdrant_embedding import build_openai_embedding_provider

    collection = str(environ.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION_NAME).strip()
    return build_remote_qdrant_docling_mirror_adapter(
        url=url,
        collection_name=collection,
        embedding_provider=build_openai_embedding_provider(environ=environ),
        normalizer=PassthroughMarkdownNormalizer(),
    )


def _default_mirror_outcome_logger(outcome: "MirrorWriteOutcome") -> None:
    # Redaction-safe operability signal: status + error_class only (never a
    # message/payload). Logged for non-success so a silently-failing mirror is
    # observable. Without this the live worker would be blind to mirror_error.
    if outcome.status != "mirrored":
        print(
            json.dumps(
                {
                    "event": "qdrant_mirror_write",
                    "status": outcome.status,
                    "error_class": outcome.error_class,
                }
            ),
            flush=True,
        )


def maybe_wrap_dual_write(
    primary: Any,
    *,
    environ: Any,
    mirror_builder: Callable[[Any], Any | None] | None = None,
    on_mirror_outcome: MirrorOutcomeHook = None,
) -> Any:
    """Wrap ``primary`` with a best-effort Qdrant mirror when dual-write is enabled.

    Off by default: returns ``primary`` unchanged unless ``MIRROR_DUAL_WRITE=1``.
    Fail-safe at BUILD time too: if the flag is on but the mirror cannot be
    constructed (no ``QDRANT_URL``, missing embedding model, optional dep absent,
    bad URL), returns ``primary`` so a mirror misconfig can never block the
    authoritative live delivery worker from starting. This is the only
    shadow_worker activation hook; it performs no live mutation by itself.
    """

    if primary is None:
        return None
    if str(environ.get("MIRROR_DUAL_WRITE") or "").strip() != "1":
        return primary
    builder = mirror_builder or build_qdrant_mirror_from_env
    try:
        mirror = builder(environ)
    except Exception as exc:
        # Mirror construction failure must NOT crash the authoritative worker.
        _default_mirror_outcome_logger(
            MirrorWriteOutcome(status="mirror_build_error", error_class=exc.__class__.__name__)
        )
        return primary
    if mirror is None:
        return primary
    return MirrorDualWriteBackend(
        primary=primary,
        mirror=mirror,
        on_mirror_outcome=on_mirror_outcome or _default_mirror_outcome_logger,
    )


__all__ = [
    "MirrorDualWriteBackend",
    "MirrorWriteOutcome",
    "build_qdrant_mirror_from_env",
    "maybe_wrap_dual_write",
]

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


__all__ = ["MirrorDualWriteBackend", "MirrorWriteOutcome"]

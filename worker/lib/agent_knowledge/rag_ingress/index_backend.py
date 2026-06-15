"""Document index backend boundary.

The generic ingress bus hands a :class:`RagReadyDocument` to an
:class:`IndexBackendAdapter`. The adapter owns *all* backend specifics:

- logical ``target_profile`` -> physical dataset id resolution,
- the concrete client call shape (upload/parse/status),
- the backend status vocabulary (RAGFlow ``DONE/FAIL/RUNNING/UNSTART``) which is
  translated into the generic :class:`IndexStatus`.

Today the only adapter is :class:`RAGFlowIndexBackendAdapter`. A future Qdrant /
OpenSearch / LanceDB adapter would implement the same protocol without changing
the generic layer.
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
    backend reported (e.g. RAGFlow ``run``) so a caller that owns a legacy ledger
    column can persist it without the generic layer learning the backend's words.
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
class IndexBackendAdapter(Protocol):
    def submit_document(
        self, document: RagReadyDocument, *, on_step_complete: StepHook = None
    ) -> BackendSubmitResult: ...

    def find_by_natural_key(
        self, *, target_profile: str, idempotency_key: str, payload_hash: str
    ) -> BackendDocumentHandle | None: ...

    def document_status(self, handle: BackendDocumentHandle) -> str: ...

    def document_status_detail(self, handle: BackendDocumentHandle) -> BackendStatusDetail: ...


# RAGFlow ``run`` vocabulary -> generic IndexStatus. This table is the single
# place the backend status words appear in the ingress bus.
_RAGFLOW_RUN_TO_STATUS = {
    "DONE": IndexStatus.INDEXED,
    "FAIL": IndexStatus.FAILED,
    "RUNNING": IndexStatus.INDEXING,
    "UNSTART": IndexStatus.PENDING,
}


class RAGFlowIndexBackendAdapter:
    """Adapter that maps the generic document model onto RAGFlow.

    ``client`` is a ``RagflowHttpClient``-shaped object. ``resolve_dataset_id``
    maps a logical ``target_profile`` to a physical RAGFlow dataset id (in
    production this reads the ledger ``ragflow_datasets`` mapping). The generic
    document already carries flat metadata, so it is forwarded as RAGFlow
    ``meta_fields`` without further RAGFlow-shaped flattening here.
    """

    def __init__(self, *, client, resolve_dataset_id: Callable[[str], str], broad_scan_pages: int = 0):
        self._client = client
        self._resolve_dataset_id = resolve_dataset_id
        self._broad_scan_pages = max(int(broad_scan_pages), 0)

    def submit_document(
        self, document: RagReadyDocument, *, on_step_complete: StepHook = None
    ) -> BackendSubmitResult:
        dataset_id = self._resolve_dataset_id(document.target_profile)
        upload = self._client.upload_document(
            dataset_id, document.body, filename=document.filename
        )
        document_id = upload["document_id"]
        upload_run = str(upload.get("run", "UNSTART"))
        _notify_step(on_step_complete, "upload", document_ref=document_id, backend_raw_status=upload_run)
        # Persist the natural-key (content_hash + idempotency_key) into the
        # backend metadata so a later ``find_by_natural_key`` can recover this
        # document and dedup a redelivery instead of uploading a duplicate.
        # ``setdefault`` never overwrites producer-supplied values, and neither
        # key is secret-like. This restores the dedup-by-identity the retired
        # Java worker had (RagFlowTargetAdapter contentHash recording).
        metadata = dict(document.metadata)
        metadata.setdefault("content_hash", document.content_hash)
        metadata.setdefault("idempotency_key", document.idempotency_key)
        self._client.update_metadata(dataset_id, document_id, metadata)
        _notify_step(on_step_complete, "metadata", document_ref=document_id)
        self._client.request_parse(dataset_id, [document_id])
        _notify_step(on_step_complete, "parse", document_ref=document_id)
        return BackendSubmitResult(
            dataset_ref=dataset_id,
            document_ref=document_id,
            status=self._map_run(upload_run),
        )

    def find_by_natural_key(
        self, *, target_profile: str, idempotency_key: str, payload_hash: str
    ) -> BackendDocumentHandle | None:
        # Fail-closed: an empty natural key cannot identify a document. Without
        # this guard an empty payload_hash would scan every doc and match the
        # first one (catastrophic false-dedup). The live path is already gated by
        # validate_ingress_payload (sha256 contentHash + non-empty idempotencyKey);
        # this is defence-in-depth so the adapter is safe for any caller.
        if not payload_hash or not idempotency_key:
            return None
        dataset_id = self._resolve_dataset_id(target_profile)
        for doc in _iter_natural_key_candidates(
            self._client,
            dataset_id=dataset_id,
            payload_hash=payload_hash,
            broad_scan_pages=self._broad_scan_pages,
        ):
            if _document_matches_natural_key(doc, idempotency_key=idempotency_key, payload_hash=payload_hash):
                document_ref = str(doc.get("id") or doc.get("document_id") or "")
                if document_ref:
                    return BackendDocumentHandle(dataset_ref=dataset_id, document_ref=document_ref)
        return None

    def document_status(self, handle: BackendDocumentHandle) -> str:
        status = self._client.get_document_status(handle.dataset_ref, handle.document_ref)
        return self._map_run(status.get("run", ""))

    def document_status_detail(self, handle: BackendDocumentHandle) -> BackendStatusDetail:
        status = self._client.get_document_status(handle.dataset_ref, handle.document_ref)
        run = str(status.get("run", ""))
        return BackendStatusDetail(
            status=self._map_run(run),
            progress=float(status.get("progress", 0.0) or 0.0),
            backend_raw_status=run,
        )

    @staticmethod
    def _map_run(run: str) -> str:
        return _RAGFLOW_RUN_TO_STATUS.get(str(run), IndexStatus.UNKNOWN)


def _notify_step(hook: StepHook, step: str, **fields) -> None:
    if hook is not None:
        hook(step, **fields)


def _iter_natural_key_candidates(
    client,
    *,
    dataset_id: str,
    payload_hash: str,
    broad_scan_pages: int = NATURAL_KEY_BROAD_SCAN_MAX_PAGES,
):
    seen: set[str] = set()
    for keywords in _natural_key_keywords(payload_hash):
        for doc in client.list_documents(
            dataset_id,
            page=1,
            page_size=NATURAL_KEY_PAGE_SIZE,
            keywords=keywords,
        ):
            doc_id = str(doc.get("id") or doc.get("document_id") or "")
            if doc_id and doc_id in seen:
                continue
            if doc_id:
                seen.add(doc_id)
            yield doc
    for page in range(1, max(int(broad_scan_pages), 0) + 1):
        docs = client.list_documents(
            dataset_id,
            page=page,
            page_size=NATURAL_KEY_PAGE_SIZE,
            keywords="",
        )
        if not docs:
            break
        for doc in docs:
            doc_id = str(doc.get("id") or doc.get("document_id") or "")
            if doc_id and doc_id in seen:
                continue
            if doc_id:
                seen.add(doc_id)
            yield doc


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
    # meta_fields lacks idempotency_key — e.g. legacy/manual uploads) would
    # false-dedup a distinct document and silently drop its upload. The worker
    # injects idempotency_key on every upload (submit_document), so its own
    # documents always carry it; anything without it is conservatively re-uploaded.
    recorded_key = str(metadata.get("idempotency_key") or metadata.get("idempotencyKey") or "")
    return bool(recorded_key) and recorded_key == idempotency_key

"""Real RAGFlow projector for CouchDB-sourced session-memory documents.

Implements :class:`SessionMemoryProjector` by writing a session-memory card to
RAGFlow using the same write path (upload_document + update_metadata +
request_parse) as the existing session-memory pipeline.

Import discipline: this module imports ``RagflowHttpClient`` only at class
construction time (inside __init__), keeping ``session_memory_materializer``
import-light at module load.
"""

from __future__ import annotations

import hashlib

from .document_model import assert_ragflow_target_allowed


def _session_memory_filename(session_id_hash: str, content_hash: str) -> str:
    """Deterministic filename for the RAGFlow session-memory document.

    Mirrors the pattern used in the existing write path: a stable, content-
    addressed name so repeated uploads of the same session always produce the
    same filename (enabling idempotency via list_documents keyword lookup).
    Raw hash hex is truncated but still unique enough for de-dup lookup.
    """
    hash_hex = session_id_hash.split(":", 1)[-1][:24]
    content_hex = content_hash.split(":", 1)[-1][:12]
    return f"ak-session-memory-couchdb-{hash_hex}-{content_hex}.md"


def _find_existing_document(ragflow, dataset_id: str, filename: str) -> str:
    """Return the existing RAGFlow document id for *filename*, or '' if absent."""
    try:
        docs = ragflow.list_documents(dataset_id, keywords=filename, page=1, page_size=20)
    except Exception:
        return ""
    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        name = str(doc.get("name") or doc.get("filename") or doc.get("file_name") or "")
        if name == filename:
            return str(doc.get("id") or doc.get("document_id") or "")
    return ""


def _render_session_memory_document(document: dict) -> str:
    """Render the session-memory body as a plain-text document for RAGFlow."""
    provider = str(document.get("provider") or "")
    project = str(document.get("project") or "")
    session_id_hash = str(document.get("session_id_hash") or "")
    content_hash = str(document.get("content_hash") or "")
    body = str(document.get("body") or "")
    conversation_chunk_count = int(document.get("conversation_chunk_count") or 0)
    tool_evidence_bundle_count = int(document.get("tool_evidence_bundle_count") or 0)

    # The body from to_projection_document() already contains the full
    # materialized session-memory markdown. Prepend a metadata header so RAGFlow
    # indexing captures the session identity fields.
    lines = [
        f"# session-memory {provider} {project}",
        f"- session_id_hash: {session_id_hash}",
        f"- content_hash: {content_hash}",
        f"- conversation_chunk_count: {conversation_chunk_count}",
        f"- tool_evidence_bundle_count: {tool_evidence_bundle_count}",
        "",
        body.strip(),
        "",
    ]
    return "\n".join(lines)


def _projection_metadata(document: dict, *, idempotency_key: str) -> dict:
    """Metadata attached to the RAGFlow document for downstream filtering."""
    return {
        "result_type": "session_memory",
        "provider": str(document.get("provider") or ""),
        "project": str(document.get("project") or ""),
        "target_profile": str(document.get("target_profile") or "session-memory"),
        "session_id_hash": str(document.get("session_id_hash") or ""),
        "content_hash": str(document.get("content_hash") or ""),
        "conversation_chunk_count": int(document.get("conversation_chunk_count") or 0),
        "tool_evidence_bundle_count": int(document.get("tool_evidence_bundle_count") or 0),
        "idempotency_key": idempotency_key,
    }


class RagflowSessionMemoryProjector:
    """Writes a materialized CouchDB session-memory card to RAGFlow.

    Dataset resolution: ``list_datasets(name=dataset_name)`` on every call so
    the dataset_id is never hardcoded. One exact match is required; any other
    count raises ValueError (fail-closed).

    Idempotency: the filename is deterministic from (session_id_hash,
    content_hash). If a document with that filename already exists in the
    dataset, the upload is skipped and the existing document_id is returned.
    This mirrors the pattern in
    :func:`session_memory.ragflow_projection._find_existing_projection_document`.

    Write sequence (mirrors :class:`RagflowMemoryCardProjectionClient.upsert_memory_card`):
    1. upload_document  -> document_id
    2. update_metadata  (meta_fields for downstream filtering)
    3. request_parse    (kick off RAGFlow indexing)
    """

    def __init__(
        self,
        *,
        ragflow_url: str,
        bearer_token: str,
        dataset_name: str = "session-memory",
        request_timeout_seconds: float = 45,
    ) -> None:
        # Import deferred to module-load time of THIS class, not of materializer.
        from ..ragflow_client import RagflowHttpClient

        self._ragflow = RagflowHttpClient(
            base_url=ragflow_url,
            bearer_token=bearer_token,
            request_timeout_seconds=request_timeout_seconds,
        )
        self._dataset_name = dataset_name
        self._dataset_id: str = ""  # resolved lazily and cached per instance

    def _resolve_dataset_id(self) -> str:
        if self._dataset_id:
            return self._dataset_id
        datasets = self._ragflow.list_datasets(name=self._dataset_name)
        exact = [
            item
            for item in datasets
            if str(item.get("name") or "") == self._dataset_name and item.get("id")
        ]
        if len(exact) != 1:
            raise ValueError(
                f"expected exactly one RAGFlow dataset named {self._dataset_name!r}, "
                f"got {len(exact)}"
            )
        self._dataset_id = str(exact[0]["id"])
        return self._dataset_id

    def project(self, *, target_profile: str, document: dict) -> str:
        """Upload session-memory to RAGFlow; return the RAGFlow document id (ref string)."""
        assert_ragflow_target_allowed(target_profile)

        dataset_id = self._resolve_dataset_id()
        session_id_hash = str(document.get("session_id_hash") or "")
        content_hash = str(document.get("content_hash") or "")

        # Idempotency key: stable over (session_id_hash, content_hash)
        idempotency_seed = f"couchdb_session_memory:{session_id_hash}:{content_hash}"
        idempotency_key = "couchdb_sm:" + hashlib.sha256(
            idempotency_seed.encode("utf-8")
        ).hexdigest()

        filename = _session_memory_filename(session_id_hash, content_hash)

        # Idempotency check: if the document already exists, skip the write.
        existing_id = _find_existing_document(self._ragflow, dataset_id, filename)
        if existing_id:
            return existing_id

        # Upload, attach metadata, trigger parse -- same three-step sequence as
        # RagflowMemoryCardProjectionClient.upsert_memory_card.
        content = _render_session_memory_document(document)
        uploaded = self._ragflow.upload_document(dataset_id, content, filename=filename)
        document_id = str(uploaded.get("document_id") or "")
        if not document_id:
            raise ValueError("RAGFlow upload did not return document_id")

        self._ragflow.update_metadata(
            dataset_id,
            document_id,
            _projection_metadata(document, idempotency_key=idempotency_key),
        )
        self._ragflow.request_parse(dataset_id, [document_id])
        return document_id


__all__ = [
    "RagflowSessionMemoryProjector",
]

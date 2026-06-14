"""Real-file ingest lane — raw document upload to RAGFlow native parsing.

This is intentionally SEPARATE from the AI-tool session/transcript text pipeline
(`server_runtime` + `rag_ready_document` + redaction). Real project files
(PDF/PPTX/DOCX/...) are uploaded to RAGFlow as their original bytes and parsed
natively (DeepDOC layout, tables, RAPTOR, GraphRAG) — extracting/flattening to
text first would throw away exactly what RAGFlow is for.

No redaction runs here by design: these files are user-curated uploads, not
auto-captured session content, so the text-only redaction gate of the session
lane does not apply. Privacy for this lane is the uploader's curation, not a
denylist pass.
"""

from __future__ import annotations

# Extension -> MIME for the document types RAGFlow parses natively. Unknown
# extensions fall back to a generic binary type (RAGFlow still dispatches by the
# filename extension, so the real extension is what matters).
_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
    "htm": "text/html",
    "csv": "text/csv",
}


def content_type_for_filename(filename: str) -> str:
    _, _, ext = str(filename).rpartition(".")
    return _CONTENT_TYPES.get(ext.lower(), "application/octet-stream")


def submit_file(
    *,
    client,
    dataset_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str | None = None,
) -> dict:
    """Upload a raw file to a RAGFlow dataset and trigger native parsing.

    ``client`` is a ``RagflowHttpClient``-shaped object exposing ``upload_file``
    and ``request_parse``. The file bytes are forwarded unchanged — no text
    extraction and no redaction. Returns ``{dataset_id, document_id}``.
    """
    if not file_bytes:
        raise ValueError("file_bytes is required")
    if not str(filename).strip():
        raise ValueError("filename is required")
    if not str(dataset_id).strip():
        raise ValueError("dataset_id is required")
    ctype = content_type or content_type_for_filename(filename)
    upload = client.upload_file(dataset_id, file_bytes, filename=filename, content_type=ctype)
    document_id = upload["document_id"]
    client.request_parse(dataset_id, [document_id])
    return {"dataset_id": str(dataset_id), "document_id": document_id}

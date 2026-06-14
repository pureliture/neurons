from __future__ import annotations

import pytest


class FakeClient:
    """Records calls; mimics the RagflowHttpClient surface file_ingest needs."""

    def __init__(self):
        self.calls = []

    def upload_file(self, dataset_id, file_bytes, *, filename, content_type):
        self.calls.append(("upload", dataset_id, file_bytes, filename, content_type))
        return {"document_id": "doc_x"}

    def request_parse(self, dataset_id, document_ids):
        self.calls.append(("parse", dataset_id, tuple(document_ids)))


def test_content_type_is_inferred_from_filename():
    from agent_knowledge.rag_ingress.file_ingest import content_type_for_filename

    assert content_type_for_filename("report.pdf") == "application/pdf"
    assert content_type_for_filename("deck.pptx") == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert content_type_for_filename("notes.txt") == "text/plain"
    assert content_type_for_filename("weird.bin") == "application/octet-stream"


def test_submit_file_uploads_raw_bytes_unchanged_and_triggers_parse():
    from agent_knowledge.rag_ingress.file_ingest import submit_file

    fc = FakeClient()
    pdf = b"%PDF-1.4\nfake pdf bytes\n%%EOF"
    res = submit_file(client=fc, dataset_id="ds1", file_bytes=pdf, filename="report.pdf")

    assert res == {"dataset_id": "ds1", "document_id": "doc_x"}
    # raw file bytes are forwarded to RAGFlow unchanged — no extraction, no redaction
    assert ("upload", "ds1", pdf, "report.pdf", "application/pdf") in fc.calls
    # native parse is triggered for the uploaded document
    assert ("parse", "ds1", ("doc_x",)) in fc.calls


def test_submit_file_rejects_empty_bytes_or_filename():
    from agent_knowledge.rag_ingress.file_ingest import submit_file

    fc = FakeClient()
    with pytest.raises(ValueError):
        submit_file(client=fc, dataset_id="ds1", file_bytes=b"", filename="report.pdf")
    with pytest.raises(ValueError):
        submit_file(client=fc, dataset_id="ds1", file_bytes=b"x", filename="")

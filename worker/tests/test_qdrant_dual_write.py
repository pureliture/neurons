"""M6: dual-write shadow seam — primary authoritative, mirror best-effort.

A mirror failure never breaks the authoritative write; find/status never consult
the mirror. No live route (code-only seam).
"""

from __future__ import annotations

import pytest

from agent_knowledge.rag_ingress.index_backend import (
    BackendDocumentHandle,
    BackendStatusDetail,
    BackendSubmitResult,
    IndexStatus,
)
from agent_knowledge.rag_ingress.qdrant_dual_write import (
    MirrorDualWriteBackend,
    MirrorWriteOutcome,
)
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document


def _doc():
    return build_rag_ready_document(
        target_profile="derived-memory-items",
        document_kind="approved_memory_card",
        source_namespace="workspace-neurons",
        source_alias="cards/x.md",
        privacy_class="private",
        body="decision body",
        filename="x.md",
        metadata={"project": "neurons"},
    )


class _PrimaryOK:
    def __init__(self):
        self.calls = 0
        self.steps = []

    def submit_document(self, document, *, on_step_complete=None):
        self.calls += 1
        if on_step_complete:
            on_step_complete("primary_step", document_ref="p1")
        return BackendSubmitResult(dataset_ref="primary:ds", document_ref="p1", status=IndexStatus.INDEXED)

    def find_by_natural_key(self, *, target_profile, idempotency_key, payload_hash):
        return BackendDocumentHandle(dataset_ref="primary:ds", document_ref="p1")

    def document_status(self, handle):
        return IndexStatus.INDEXED

    def document_status_detail(self, handle):
        return BackendStatusDetail(status=IndexStatus.INDEXED, progress=1.0, backend_raw_status="DONE")


class _MirrorOK:
    def __init__(self):
        self.calls = 0

    def submit_document(self, document, *, on_step_complete=None):
        self.calls += 1
        return BackendSubmitResult(dataset_ref="qdrant:c", document_ref="m1", status=IndexStatus.INDEXED)


class _MirrorBoom:
    def submit_document(self, document, *, on_step_complete=None):
        raise RuntimeError("qdrant down")


def test_primary_result_is_authoritative_and_mirror_is_written():
    primary, mirror = _PrimaryOK(), _MirrorOK()
    outcomes = []
    backend = MirrorDualWriteBackend(primary=primary, mirror=mirror, on_mirror_outcome=outcomes.append)
    result = backend.submit_document(_doc())
    assert result.document_ref == "p1" and result.status == IndexStatus.INDEXED
    assert mirror.calls == 1
    assert outcomes[-1].status == "mirrored" and outcomes[-1].document_ref == "m1"


def test_mirror_failure_never_breaks_authoritative_write():
    primary = _PrimaryOK()
    outcomes = []
    backend = MirrorDualWriteBackend(primary=primary, mirror=_MirrorBoom(), on_mirror_outcome=outcomes.append)
    result = backend.submit_document(_doc())  # must NOT raise
    assert result.document_ref == "p1"
    assert outcomes[-1].status == "mirror_error"
    assert outcomes[-1].error_class == "RuntimeError"


def test_primary_failure_propagates_and_mirror_not_touched():
    class _PrimaryBoom:
        def submit_document(self, document, *, on_step_complete=None):
            raise ValueError("primary failed")

    mirror = _MirrorOK()
    backend = MirrorDualWriteBackend(primary=_PrimaryBoom(), mirror=mirror)
    with pytest.raises(ValueError):
        backend.submit_document(_doc())
    assert mirror.calls == 0  # no partial mirror state on failed primary


def test_no_mirror_configured_is_skipped():
    outcomes = []
    backend = MirrorDualWriteBackend(primary=_PrimaryOK(), mirror=None, on_mirror_outcome=outcomes.append)
    backend.submit_document(_doc())
    assert outcomes[-1].status == "mirror_skipped"


def test_find_and_status_delegate_to_primary_only():
    primary, mirror = _PrimaryOK(), _MirrorOK()
    backend = MirrorDualWriteBackend(primary=primary, mirror=mirror)
    handle = backend.find_by_natural_key(target_profile="t", idempotency_key="i", payload_hash="h")
    assert handle.document_ref == "p1"
    assert backend.document_status(handle) == IndexStatus.INDEXED
    assert backend.document_status_detail(handle).backend_raw_status == "DONE"
    assert mirror.calls == 0  # mirror never consulted for find/status


def test_step_hook_passes_through_to_primary():
    steps = []
    backend = MirrorDualWriteBackend(primary=_PrimaryOK(), mirror=_MirrorOK())
    backend.submit_document(_doc(), on_step_complete=lambda step, **kw: steps.append(step))
    assert "primary_step" in steps

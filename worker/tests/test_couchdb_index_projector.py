"""Unit tests for RetiredIndexBridgeSessionMemoryProjector.

Uses a FakeRetiredIndexBridgeClient injected via monkeypatching so no live RetiredIndexBridge calls
are made. Verifies:
- transcript-memory target_profile is rejected
- dataset is resolved by name via list_datasets
- write methods are called in the correct three-step sequence
- idempotency: re-projecting the same (session_id_hash, content_hash) skips the upload
- the RetiredIndexBridge document id (ref string) is returned
"""
from __future__ import annotations

import pytest

from agent_knowledge.couchdb_source.document_model import (
    RETIRED_RETIRED_INDEX_BRIDGE_PROFILE,
    RETIRED_INDEX_BRIDGE_RECALL_PROFILE,
    OwnershipViolation,
    build_session_id_hash,
    sha256_hash,
)
from agent_knowledge.couchdb_source.index_projector import (
    RetiredIndexBridgeSessionMemoryProjector,
    _session_memory_filename,
)


# ---------------------------------------------------------------------------
# FakeRetiredIndexBridgeClient
# ---------------------------------------------------------------------------

class FakeRetiredIndexBridgeClient:
    """Simulates RetiredIndexBridgeHttpClient with minimal method stubs.

    Tracks every call for assertion in tests.
    """

    def __init__(self, *, dataset_name: str = "session-memory", dataset_id: str = "ds-001") -> None:
        self._dataset_name = dataset_name
        self._dataset_id = dataset_id
        # call log
        self.list_datasets_calls: list[dict] = []
        self.list_documents_calls: list[dict] = []
        self.upload_document_calls: list[dict] = []
        self.update_metadata_calls: list[dict] = []
        self.request_parse_calls: list[dict] = []
        # state
        self._existing_docs: dict[str, dict] = {}  # filename -> doc dict
        self._next_doc_id = "index-doc-001"

    def list_datasets(self, *, name: str = "", dataset_id: str = "", include_parsing_status: bool = False) -> list[dict]:
        self.list_datasets_calls.append({"name": name, "dataset_id": dataset_id})
        if name == self._dataset_name:
            return [{"id": self._dataset_id, "name": self._dataset_name}]
        return []

    def list_documents(self, dataset_id: str, *, page: int = 1, page_size: int = 100, keywords: str = "") -> list[dict]:
        self.list_documents_calls.append({"dataset_id": dataset_id, "keywords": keywords})
        if keywords and keywords in self._existing_docs:
            doc = self._existing_docs[keywords]
            return [doc]
        return []

    def upload_document(self, dataset_id: str, content: str, *, filename: str = "agent-knowledge.md") -> dict:
        self.upload_document_calls.append({"dataset_id": dataset_id, "filename": filename, "content_len": len(content)})
        doc_id = self._next_doc_id
        # register the doc as existing so subsequent list_documents picks it up
        self._existing_docs[filename] = {"id": doc_id, "name": filename}
        return {"document_id": doc_id}

    def update_metadata(self, dataset_id: str, document_id: str, metadata: dict) -> None:
        self.update_metadata_calls.append({"dataset_id": dataset_id, "document_id": document_id, "metadata": metadata})

    def request_parse(self, dataset_id: str, document_ids: list[str]) -> None:
        self.request_parse_calls.append({"dataset_id": dataset_id, "document_ids": document_ids})


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_projector(fake_client: FakeRetiredIndexBridgeClient, dataset_name: str = "session-memory") -> RetiredIndexBridgeSessionMemoryProjector:
    """Create a projector with the fake client injected (bypasses __init__ HTTP construction)."""
    projector = object.__new__(RetiredIndexBridgeSessionMemoryProjector)
    projector._retired_index_bridge = fake_client
    projector._dataset_name = dataset_name
    projector._dataset_id = ""  # unresolved
    return projector


def _projection_doc(
    *,
    session_id_hash: str,
    content_hash: str,
    target_profile: str = RETIRED_INDEX_BRIDGE_RECALL_PROFILE,
    provider: str = "claude",
    project: str = "neurons",
    body: str = "session text",
    conversation_chunk_count: int = 1,
    tool_evidence_bundle_count: int = 0,
) -> dict:
    return {
        "target_profile": target_profile,
        "session_id_hash": session_id_hash,
        "provider": provider,
        "project": project,
        "body": body,
        "content_hash": content_hash,
        "conversation_chunk_count": conversation_chunk_count,
        "tool_evidence_bundle_count": tool_evidence_bundle_count,
    }


def _sid() -> str:
    return build_session_id_hash("claude", "test-session-1")


def _chash(body: str = "session text") -> str:
    return sha256_hash(body)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTranscriptMemoryRejected:
    """assert_index_target_allowed must reject retired profile before any I/O."""

    def test_retired_profile_raises_ownership_violation(self) -> None:
        fake = FakeRetiredIndexBridgeClient()
        projector = _make_projector(fake)
        doc = _projection_doc(
            session_id_hash=_sid(),
            content_hash=_chash(),
            target_profile=RETIRED_RETIRED_INDEX_BRIDGE_PROFILE,
        )
        with pytest.raises(OwnershipViolation, match="transcript-memory"):
            projector.project(target_profile=RETIRED_RETIRED_INDEX_BRIDGE_PROFILE, document=doc)

    def test_unknown_profile_raises_ownership_violation(self) -> None:
        fake = FakeRetiredIndexBridgeClient()
        projector = _make_projector(fake)
        doc = _projection_doc(session_id_hash=_sid(), content_hash=_chash())
        with pytest.raises(OwnershipViolation):
            projector.project(target_profile="unknown-profile", document=doc)

    def test_no_index_calls_when_profile_rejected(self) -> None:
        fake = FakeRetiredIndexBridgeClient()
        projector = _make_projector(fake)
        doc = _projection_doc(session_id_hash=_sid(), content_hash=_chash(), target_profile=RETIRED_RETIRED_INDEX_BRIDGE_PROFILE)
        try:
            projector.project(target_profile=RETIRED_RETIRED_INDEX_BRIDGE_PROFILE, document=doc)
        except OwnershipViolation:
            pass
        assert fake.list_datasets_calls == []
        assert fake.upload_document_calls == []


class TestDatasetResolutionByName:
    """dataset_id must be resolved by name via list_datasets, never hardcoded."""

    def test_list_datasets_called_with_correct_name(self) -> None:
        fake = FakeRetiredIndexBridgeClient(dataset_name="session-memory", dataset_id="ds-abc")
        projector = _make_projector(fake, dataset_name="session-memory")
        doc = _projection_doc(session_id_hash=_sid(), content_hash=_chash())

        projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)

        assert len(fake.list_datasets_calls) >= 1
        assert fake.list_datasets_calls[0]["name"] == "session-memory"

    def test_dataset_id_cached_after_first_call(self) -> None:
        fake = FakeRetiredIndexBridgeClient()
        projector = _make_projector(fake)

        sid1 = build_session_id_hash("claude", "sess-a")
        sid2 = build_session_id_hash("claude", "sess-b")
        fake._next_doc_id = "doc-1"
        doc1 = _projection_doc(session_id_hash=sid1, content_hash=sha256_hash("body a"))
        projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc1)

        fake._next_doc_id = "doc-2"
        doc2 = _projection_doc(session_id_hash=sid2, content_hash=sha256_hash("body b"))
        projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc2)

        # list_datasets called once; subsequent calls reuse cached dataset_id
        assert len(fake.list_datasets_calls) == 1

    def test_multiple_matching_datasets_raises(self) -> None:
        class _DualDatasetClient(FakeRetiredIndexBridgeClient):
            def list_datasets(self, *, name="", dataset_id="", include_parsing_status=False):
                self.list_datasets_calls.append({"name": name})
                return [
                    {"id": "ds-1", "name": "session-memory"},
                    {"id": "ds-2", "name": "session-memory"},
                ]

        fake = _DualDatasetClient()
        projector = _make_projector(fake)
        doc = _projection_doc(session_id_hash=_sid(), content_hash=_chash())
        with pytest.raises(ValueError, match="expected exactly one"):
            projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)

    def test_no_matching_dataset_raises(self) -> None:
        class _EmptyClient(FakeRetiredIndexBridgeClient):
            def list_datasets(self, *, name="", dataset_id="", include_parsing_status=False):
                self.list_datasets_calls.append({"name": name})
                return []

        fake = _EmptyClient()
        projector = _make_projector(fake)
        doc = _projection_doc(session_id_hash=_sid(), content_hash=_chash())
        with pytest.raises(ValueError, match="expected exactly one"):
            projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)


class TestWriteSequence:
    """The three-step write: upload_document -> update_metadata -> request_parse."""

    def test_three_step_write_called_in_order(self) -> None:
        call_order: list[str] = []
        fake = FakeRetiredIndexBridgeClient()
        original_upload = fake.upload_document
        original_update = fake.update_metadata
        original_parse = fake.request_parse

        def _track_upload(*a, **kw):
            call_order.append("upload")
            return original_upload(*a, **kw)

        def _track_update(*a, **kw):
            call_order.append("update_metadata")
            return original_update(*a, **kw)

        def _track_parse(*a, **kw):
            call_order.append("request_parse")
            return original_parse(*a, **kw)

        fake.upload_document = _track_upload
        fake.update_metadata = _track_update
        fake.request_parse = _track_parse

        projector = _make_projector(fake)
        doc = _projection_doc(session_id_hash=_sid(), content_hash=_chash())
        projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)

        assert call_order == ["upload", "update_metadata", "request_parse"]

    def test_write_uses_correct_dataset_id(self) -> None:
        fake = FakeRetiredIndexBridgeClient(dataset_id="ds-correct")
        projector = _make_projector(fake)
        doc = _projection_doc(session_id_hash=_sid(), content_hash=_chash())
        projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)

        assert fake.upload_document_calls[0]["dataset_id"] == "ds-correct"
        assert fake.update_metadata_calls[0]["dataset_id"] == "ds-correct"
        assert fake.request_parse_calls[0]["dataset_id"] == "ds-correct"

    def test_update_metadata_receives_identity_fields(self) -> None:
        sid = _sid()
        chash = _chash("body with unique content")
        fake = FakeRetiredIndexBridgeClient()
        projector = _make_projector(fake)
        doc = _projection_doc(session_id_hash=sid, content_hash=chash, body="body with unique content")
        projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)

        meta = fake.update_metadata_calls[0]["metadata"]
        assert meta["result_type"] == "session_memory"
        assert meta["session_id_hash"] == sid
        assert meta["content_hash"] == chash
        assert "idempotency_key" in meta

    def test_request_parse_receives_document_id(self) -> None:
        fake = FakeRetiredIndexBridgeClient()
        fake._next_doc_id = "my-doc-id-xyz"
        projector = _make_projector(fake)
        doc = _projection_doc(session_id_hash=_sid(), content_hash=_chash())
        ref = projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)

        assert ref == "my-doc-id-xyz"
        assert fake.request_parse_calls[0]["document_ids"] == ["my-doc-id-xyz"]

    def test_ref_string_returned(self) -> None:
        fake = FakeRetiredIndexBridgeClient()
        fake._next_doc_id = "returned-ref-id"
        projector = _make_projector(fake)
        doc = _projection_doc(session_id_hash=_sid(), content_hash=_chash())
        ref = projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)
        assert ref == "returned-ref-id"


class TestIdempotency:
    """Re-projecting the same (session_id_hash, content_hash) skips upload."""

    def test_second_call_same_content_skips_write(self) -> None:
        fake = FakeRetiredIndexBridgeClient()
        projector = _make_projector(fake)
        sid = _sid()
        chash = _chash()
        doc = _projection_doc(session_id_hash=sid, content_hash=chash)

        # First call: writes
        ref1 = projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)
        assert len(fake.upload_document_calls) == 1

        # Second call: same content -> list_documents returns the existing doc, no upload
        ref2 = projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc)
        assert len(fake.upload_document_calls) == 1  # still only 1
        assert ref1 == ref2

    def test_different_content_hash_triggers_new_upload(self) -> None:
        fake = FakeRetiredIndexBridgeClient()
        projector = _make_projector(fake)
        sid = _sid()

        doc1 = _projection_doc(session_id_hash=sid, content_hash=sha256_hash("body version 1"), body="body version 1")
        fake._next_doc_id = "doc-v1"
        projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc1)

        doc2 = _projection_doc(session_id_hash=sid, content_hash=sha256_hash("body version 2"), body="body version 2")
        fake._next_doc_id = "doc-v2"
        ref2 = projector.project(target_profile=RETIRED_INDEX_BRIDGE_RECALL_PROFILE, document=doc2)

        assert len(fake.upload_document_calls) == 2
        assert ref2 == "doc-v2"

    def test_filename_is_deterministic(self) -> None:
        sid = _sid()
        chash = _chash()
        fname1 = _session_memory_filename(sid, chash)
        fname2 = _session_memory_filename(sid, chash)
        assert fname1 == fname2
        assert fname1.startswith("ak-session-memory-couchdb-")
        assert fname1.endswith(".md")

    def test_filename_differs_for_different_content(self) -> None:
        sid = _sid()
        fname1 = _session_memory_filename(sid, sha256_hash("body v1"))
        fname2 = _session_memory_filename(sid, sha256_hash("body v2"))
        assert fname1 != fname2

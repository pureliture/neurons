"""Selection uses verified pointer hashes instead of re-reading every member body."""

from __future__ import annotations

from typing import Any

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_revision import (
    activate_source_revision,
    resolve_source_hash_for_currentness,
    source_hash_from_active_pointer,
)
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core.couchdb_projection_cli import _iter_session_revisions
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


def _session_store(*, activate: bool = True):
    session_id_hash = dm.build_session_id_hash("codex", "fast-selection-hash")
    store = InMemoryCouchDBSourceStore()
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=session_id_hash,
                provider="codex",
                project="neurons",
                started_at="2026-11-01T00:00:00Z",
                ended_at="2026-11-01T00:01:00Z",
            )
        )
    )
    for index in range(2):
        store.put(
            dm.build_conversation_chunk_document(
                chunk=TranscriptChunk.from_text(
                    chunk_id=f"fast-hash-{index}",
                    session_id_hash=session_id_hash,
                    provider="codex",
                    project="neurons",
                    turn_start_index=index,
                    turn_end_index=index,
                    text=f"selection hash body {index}",
                )
            )
        )
    if activate:
        activate_source_revision(store=store, session_id_hash=session_id_hash)
    return store, session_id_hash


class _CountingStore:
    def __init__(self, inner: InMemoryCouchDBSourceStore) -> None:
        self.inner = inner
        self.reads: list[str] = []
        self.iter_types: list[str] = []

    def get(self, doc_id: str):  # type: ignore[no-untyped-def]
        self.reads.append(doc_id)
        return self.inner.get(doc_id)

    def get_bounded(self, doc_id: str, *, max_response_bytes: int):  # type: ignore[no-untyped-def]
        self.reads.append(doc_id)
        return self.inner.get_bounded(doc_id, max_response_bytes=max_response_bytes)

    def iter_by_type(self, doc_type: str, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.iter_types.append(str(doc_type))
        return self.inner.iter_by_type(doc_type, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_active_pointer_hash_matches_full_currentness_hash() -> None:
    store, session_id_hash = _session_store()
    expected = resolve_source_hash_for_currentness(session_id_hash=session_id_hash, store=store)

    assert source_hash_from_active_pointer(session_id_hash=session_id_hash, store=store) == expected


def test_selection_does_not_re_read_member_bodies() -> None:
    inner, session_id_hash = _session_store()
    expected = resolve_source_hash_for_currentness(session_id_hash=session_id_hash, store=inner)
    store = _CountingStore(inner)

    sessions = list(_iter_session_revisions(store, project="", provider=""))  # type: ignore[arg-type]

    assert len(sessions) == 1
    assert sessions[0]["source_hash"] == expected
    assert dm.SourceDocType.ACTIVE_SOURCE_REVISION in store.iter_types
    assert len(store.reads) <= 4  # only snapshot probes / control docs
    assert all(
        doc_id.startswith(("active_source_revision:", "source_revision_manifest:"))
        for doc_id in store.reads
    )


def test_selection_falls_back_to_full_currentness_without_active_pointer() -> None:
    inner, session_id_hash = _session_store(activate=False)
    expected = resolve_source_hash_for_currentness(session_id_hash=session_id_hash, store=inner)
    assert source_hash_from_active_pointer(session_id_hash=session_id_hash, store=inner) is None

    sessions = list(_iter_session_revisions(inner, project="", provider=""))

    assert sessions[0]["source_hash"] == expected


def test_missing_manifest_is_not_trusted_as_current() -> None:
    inner, _session_id_hash = _session_store()

    class _MissingManifestStore(_CountingStore):
        def iter_by_type(self, doc_type: str, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            if doc_type == dm.SourceDocType.SOURCE_REVISION_MANIFEST:
                return iter([])
            return super().iter_by_type(doc_type, *args, **kwargs)

    from agent_knowledge.couchdb_source.source_revision import iter_active_source_hashes

    assert iter_active_source_hashes(_MissingManifestStore(inner))[0] == {}

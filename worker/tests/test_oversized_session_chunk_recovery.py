from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.local_embedding.canonical import split_utf8_chunks_v1
from agent_knowledge.model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from agent_knowledge.postgres_store.oversized_session_chunk_recovery import (
    _derived_chunk_id,
    recover_oversized_session_memory_chunks,
    main,
)


@dataclass
class FakeChunk:
    chunk_id: str
    session_id_hash: str = "s1"
    project: str = "p"
    provider: str = "prov"
    chunk_index: int = 0
    content_markdown: str = ""
    token_count: int = 0
    content_hash: str = ""
    embedding_model: str = "m"
    embedding_state: str = "pending"
    embedding_revision: int = 1
    embedding: object = None
    created_at: object = None
    updated_at: object = None
    observed_at: object = None


@dataclass
class FakeMeta:
    chunk_id: str
    content_bytes: int
    session_id_hash: str = "s1"
    project: str = "p"
    provider: str = "prov"
    content_hash: str = ""


class FakeStore:
    def __init__(self, *, outbox_profiles=None, raise_on_iter=False):
        self.chunks: dict[str, FakeChunk] = {}
        self.insert_calls: list[tuple[FakeChunk, str]] = []
        self.outbox_profiles = dict(outbox_profiles or {})
        self.raise_on_iter = raise_on_iter
        self.iter_called = False

    def add(self, chunk: FakeChunk):
        self.chunks[chunk.chunk_id] = chunk

    def iter_session_chunk_meta_batches(self, *, batch_size=100, limit_sessions=20000):
        if self.raise_on_iter:
            self.iter_called = True
            raise AssertionError("full scan must not be called")
        self.iter_called = True
        metas = [
            FakeMeta(chunk_id=c.chunk_id, content_bytes=len(c.content_markdown.encode("utf-8")), content_hash=c.content_hash)
            for c in self.chunks.values()
        ]
        yield "s1", metas

    def get_chunk(self, chunk_id):
        return self.chunks.get(chunk_id)

    def get_local_outbox_profile_id(self, *, target_type="session_chunk", target_id, conn=None):
        return self.outbox_profiles.get(target_id, LOCAL_EMBEDDING_PROFILE_ID)

    def insert_chunk_with_local_outbox(self, chunk, *, profile_id="local", insert_only=False):
        if insert_only and chunk.chunk_id in self.chunks:
            return chunk.chunk_id, 0
        self.chunks[chunk.chunk_id] = chunk
        self.insert_calls.append((chunk, profile_id))
        return chunk.chunk_id, 1


def test_dry_run_does_not_insert():
    store = FakeStore()
    big = "x" * 600000
    store.add(FakeChunk(chunk_id="orig", content_markdown=big, content_hash=sha256_hash(big)))
    report = recover_oversized_session_memory_chunks(store, dry_run=True)
    assert report["oversized_rows"] == 1
    assert report["planned_splits"] > 1
    assert report["inserted_chunks"] == 0
    assert "orig__split_0000" not in store.chunks


def test_execute_splits_and_idempotent():
    store = FakeStore()
    big = "y" * 600000
    store.add(FakeChunk(chunk_id="orig2", content_markdown=big, content_hash=sha256_hash(big)))
    report = recover_oversized_session_memory_chunks(store, dry_run=False)
    assert report["inserted_chunks"] == len(list(split_utf8_chunks_v1(big, max_chunk_bytes=524288)))
    assert "orig2__split_0000" in store.chunks
    # idempotent
    report2 = recover_oversized_session_memory_chunks(store, dry_run=False)
    assert report2["inserted_chunks"] == 0
    assert report2["skipped_existing"] > 0


def test_target_restricted_skips_full_scan():
    store = FakeStore(raise_on_iter=True, outbox_profiles={"t1": "prof-target"})
    big = "z" * 600000
    store.add(FakeChunk(chunk_id="t1", content_markdown=big, content_hash=sha256_hash(big)))
    report = recover_oversized_session_memory_chunks(store, target_chunk_ids=["t1"], dry_run=True)
    assert store.iter_called is False
    assert report["oversized_rows"] == 1
    assert report["planned_splits"] > 1


def test_profile_comes_from_outbox():
    custom = "custom-profile-xyz"
    store = FakeStore(outbox_profiles={"t2": custom})
    big = "w" * 600000
    store.add(FakeChunk(chunk_id="t2", content_markdown=big, content_hash=sha256_hash(big)))
    report = recover_oversized_session_memory_chunks(store, target_chunk_ids=["t2"], dry_run=False)
    assert report["inserted_chunks"] > 0
    assert all(pid == custom for (_c, pid) in store.insert_calls)


def test_all_required_for_full_scan_cli(capsys):
    # no targets, no --all -> status
    rc = main([])
    assert rc == 2
    out = capsys.readouterr().out
    assert "targets_or_all_required" in out


def test_derived_chunk_id_length_stability_uniqueness():
    original = "a" * 64
    first = _derived_chunk_id(original, 0)
    assert len(first) <= 64
    assert first == _derived_chunk_id(original, 0)
    derived = [_derived_chunk_id(original, i) for i in range(8)]
    assert all(len(x) <= 64 for x in derived)
    assert len(set(derived)) == len(derived)
    assert _derived_chunk_id(original, 0) != _derived_chunk_id("b" * 64, 0)
    # short ids keep readable scheme
    assert _derived_chunk_id("orig", 0) == "orig__split_0000"

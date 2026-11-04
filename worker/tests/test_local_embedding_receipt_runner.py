from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_knowledge.couchdb_source.source_store import SourceStoreConflict
from agent_knowledge.local_embedding.profile import LOCAL_EMBEDDING_PROFILE_ID


class _CasCouchStore:
    def __init__(self, document):
        self.document = deepcopy(document)
        self.put_calls = []
        self.before_put = None
        self.fail_readback = False

    def get(self, doc_id):
        if self.fail_readback and self.put_calls:
            raise RuntimeError("private read detail")
        if self.document is None or self.document.get("_id") != doc_id:
            return None
        return deepcopy(self.document)

    def put_if_revision(self, document, *, expected_rev):
        self.put_calls.append((deepcopy(document), expected_rev))
        if self.before_put is not None:
            callback, self.before_put = self.before_put, None
            callback(self)
        if str((self.document or {}).get("_rev") or "") != expected_rev:
            raise SourceStoreConflict("synthetic revision conflict")
        updated = deepcopy(document)
        updated["_rev"] = f"rev-{len(self.put_calls) + 1}"
        self.document = updated
        return SimpleNamespace(outcome="accepted", rev=updated["_rev"])


def _receipt(profile_id: str, value: str = "new") -> dict:
    return {
        "receipt_version": 3,
        "embedding_profile": profile_id,
        "provenance_digest": f"sha256:{value}",
        "value": value,
    }


def _cas_helper():
    from agent_knowledge.local_embedding.transition import _cas_merge_projection_state_receipt

    return _cas_merge_projection_state_receipt


def test_receipt_cas_re_reads_and_preserves_concurrent_unrelated_receipts():
    profile = LOCAL_EMBEDDING_PROFILE_ID
    store = _CasCouchStore(
        {
            "_id": "projection_state:fixture",
            "_rev": "rev-1",
            "backend_receipts": {
                "other_backend": {"keep": {"state": "ready"}},
                "postgres_pgvector_local": {"other-profile": {"receipt_version": 3}},
            },
        }
    )

    def concurrent_write(current):
        current.document["backend_receipts"]["concurrent_backend"] = {"keep": True}
        current.document["_rev"] = "rev-concurrent"

    store.before_put = concurrent_write
    result = _cas_helper()(
        couchdb_store=store,
        doc_id="projection_state:fixture",
        profile_id=profile,
        receipt=_receipt(profile),
        validator=lambda value: value == _receipt(profile),
        max_attempts=3,
    )

    assert result.status == "created"
    assert len(store.put_calls) == 2
    receipts = store.document["backend_receipts"]
    assert receipts["other_backend"] == {"keep": {"state": "ready"}}
    assert receipts["postgres_pgvector_local"]["other-profile"] == {"receipt_version": 3}
    assert receipts["concurrent_backend"] == {"keep": True}
    assert receipts["postgres_pgvector_local"][profile] == _receipt(profile)


def test_receipt_cas_unknown_write_is_resolved_by_readback_without_rewrite():
    profile = LOCAL_EMBEDDING_PROFILE_ID
    store = _CasCouchStore({"_id": "projection_state:fixture", "_rev": "rev-1"})
    original_put = store.put_if_revision

    def write_then_lose_response(document, *, expected_rev):
        original_put(document, expected_rev=expected_rev)
        raise RuntimeError("private transport body")

    store.put_if_revision = write_then_lose_response
    result = _cas_helper()(
        couchdb_store=store,
        doc_id="projection_state:fixture",
        profile_id=profile,
        receipt=_receipt(profile),
        validator=lambda value: value == _receipt(profile),
        max_attempts=3,
    )

    assert result.status == "already_valid"
    assert result.reason_code == "readback_valid_after_uncertain_write"
    assert len(store.put_calls) == 1


def test_receipt_cas_reports_mutation_unknown_when_write_and_readback_are_uncertain():
    profile = LOCAL_EMBEDDING_PROFILE_ID
    store = _CasCouchStore({"_id": "projection_state:fixture", "_rev": "rev-1"})
    original_put = store.put_if_revision

    def lose_response(document, *, expected_rev):
        original_put(document, expected_rev=expected_rev)
        store.fail_readback = True
        raise RuntimeError("private transport body")

    store.put_if_revision = lose_response
    result = _cas_helper()(
        couchdb_store=store,
        doc_id="projection_state:fixture",
        profile_id=profile,
        receipt=_receipt(profile),
        validator=lambda value: value == _receipt(profile),
        max_attempts=2,
    )

    assert result.status == "mutation_unknown"
    assert result.reason_code == "readback_unavailable"
    assert len(store.put_calls) == 1


def test_receipt_pages_are_bounded_and_checkpoint_resumes_without_raw_ids_or_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_knowledge.local_embedding import transition

    rows = [
        {
            "session_id_hash": f"private-session-{index}",
            "chunk_id": f"private-chunk-{index}",
            "project": "neurons",
            "provider": "codex",
            "content_hash": f"sha256:content-{index}",
            "input_hash": f"sha256:input-{index}",
            "content_markdown": f"private transcript body {index}",
        }
        for index in range(5)
    ]

    class _PagedPgStore:
        def __init__(self):
            self.requested_limits = []

        def iter_local_embedding_receipt_rows(self, *, after_key, limit, profile_id):
            self.requested_limits.append(limit)
            assert profile_id == LOCAL_EMBEDDING_PROFILE_ID
            assert 0 < limit <= 2
            ordered = sorted(rows, key=lambda row: (row["session_id_hash"], row["chunk_id"]))
            if after_key is not None:
                ordered = [
                    row
                    for row in ordered
                    if (row["session_id_hash"], row["chunk_id"]) > after_key
                ]
            return ordered[:limit]

    monkeypatch.setattr(
        transition,
        "_process_pg_local_receipt_row",
        lambda **_kwargs: ("created", "receipt_created"),
    )
    store = _PagedPgStore()
    checkpoint = tmp_path / "receipt-cursor.json"

    first = transition.process_pg_local_embedding_receipts(
        store,
        _CasCouchStore(None),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        checkpoint_path=checkpoint,
        page_size=2,
        max_pages=1,
        max_rows=2,
        max_seconds=30,
    )
    second = transition.process_pg_local_embedding_receipts(
        store,
        _CasCouchStore(None),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        checkpoint_path=checkpoint,
        page_size=2,
        max_pages=1,
        max_rows=2,
        max_seconds=30,
    )
    third = transition.process_pg_local_embedding_receipts(
        store,
        _CasCouchStore(None),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        checkpoint_path=checkpoint,
        page_size=2,
        max_pages=1,
        max_rows=2,
        max_seconds=30,
    )

    assert [first.rows_processed, second.rows_processed, third.rows_processed] == [2, 4, 5]
    assert [first.complete, second.complete, third.complete] == [False, False, True]
    assert max(store.requested_limits) <= 2
    manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    assert manifest["profile_id"] == LOCAL_EMBEDDING_PROFILE_ID
    assert manifest["progress"]["rows_processed"] == 5
    assert manifest["run_id"] == first.run_id == second.run_id == third.run_id
    serialized = json.dumps(manifest)
    for row in rows:
        assert row["session_id_hash"] not in serialized
        assert row["chunk_id"] not in serialized
        assert row["content_markdown"] not in serialized


def test_receipt_page_failure_does_not_advance_checkpoint(tmp_path: Path, monkeypatch):
    from agent_knowledge.local_embedding import transition

    class _PagedPgStore:
        rows = [
            {
                "session_id_hash": "private-session",
                "chunk_id": "private-chunk",
                "project": "neurons",
                "provider": "codex",
                "content_hash": "sha256:content",
                "input_hash": "sha256:input",
            }
        ]

        def iter_local_embedding_receipt_rows(self, *, after_key, limit, profile_id):
            _ = profile_id
            return [row for row in self.rows if after_key is None][:limit]

    monkeypatch.setattr(
        transition,
        "_process_pg_local_receipt_row",
        lambda **_kwargs: ("failed", "materialization_failed"),
    )
    checkpoint = tmp_path / "receipt-cursor.json"
    result = transition.process_pg_local_embedding_receipts(
        _PagedPgStore(),
        _CasCouchStore(None),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        checkpoint_path=checkpoint,
        page_size=1,
        max_pages=1,
        max_rows=1,
        max_seconds=30,
    )

    assert result.failed == 1
    assert result.rows_processed == 0
    manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert manifest["progress"]["rows_processed"] == 0
    assert "private-session" not in checkpoint.read_text(encoding="utf-8")
    assert "private-chunk" not in checkpoint.read_text(encoding="utf-8")


def test_vector_addition_after_approved_receipt_snapshot_requires_new_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_knowledge.local_embedding import transition

    original_identity = "sha256:" + "1" * 64
    expanded_identity = "sha256:" + "2" * 64
    current = {"identity": original_identity}
    row = {
        "session_id_hash": "private-session",
        "chunk_id": "private-chunk",
        "project": "neurons",
        "provider": "codex",
        "content_hash": "sha256:content",
        "input_hash": "sha256:input",
    }

    class _PagedPgStore:
        def iter_local_embedding_receipt_rows(self, *, after_key, limit, profile_id):
            assert profile_id == LOCAL_EMBEDDING_PROFILE_ID
            return [row] if after_key is None and limit else []

    store = _PagedPgStore()
    monkeypatch.setattr(
        transition,
        "_receipt_source_input_identity",
        lambda *_args, **_kwargs: (current["identity"], 1),
    )

    def add_vector_after_receipt_write(**_kwargs):
        current["identity"] = expanded_identity
        return "created", "receipt_created"

    monkeypatch.setattr(
        transition,
        "_process_pg_local_receipt_row",
        add_vector_after_receipt_write,
    )
    checkpoint = tmp_path / "approved-epoch-one.json"
    first = transition.process_pg_local_embedding_receipts(
        store,
        _CasCouchStore(None),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        checkpoint_path=checkpoint,
        page_size=1,
        max_pages=1,
        max_rows=1,
        max_seconds=30,
        run_id="receipt-run-one",
        expected_source_identity=original_identity,
        target_fingerprint="target-epoch-one",
    )
    checkpoint_after_drift = checkpoint.read_bytes()
    manifest_after_drift = json.loads(checkpoint_after_drift)

    assert first.complete is False
    assert first.rows_processed == 0
    assert first.reason_counts["source_identity_changed"] == 1
    assert manifest_after_drift["run_id"] == "receipt-run-one"
    assert manifest_after_drift["source_input_identity"] == original_identity
    assert manifest_after_drift["progress"]["rows_processed"] == 0

    retry = transition.process_pg_local_embedding_receipts(
        store,
        _CasCouchStore(None),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        checkpoint_path=checkpoint,
        page_size=1,
        max_pages=1,
        max_rows=1,
        max_seconds=30,
        run_id="receipt-run-one",
        expected_source_identity=original_identity,
        target_fingerprint="target-epoch-one",
    )
    assert retry.complete is False
    assert retry.reason_counts["approved_source_identity_mismatch"] == 1
    assert checkpoint.read_bytes() == checkpoint_after_drift

    monkeypatch.setattr(
        transition,
        "_process_pg_local_receipt_row",
        lambda **_kwargs: ("already_valid", "receipt_already_valid"),
    )
    new_checkpoint = tmp_path / "approved-epoch-two.json"
    next_epoch = transition.process_pg_local_embedding_receipts(
        store,
        _CasCouchStore(None),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        checkpoint_path=new_checkpoint,
        page_size=1,
        max_pages=1,
        max_rows=1,
        max_seconds=30,
        run_id="receipt-run-two",
        expected_source_identity=expanded_identity,
        target_fingerprint="target-epoch-two",
    )

    assert next_epoch.complete is True
    assert next_epoch.rows_processed == 1
    next_manifest = json.loads(new_checkpoint.read_text(encoding="utf-8"))
    assert next_manifest["run_id"] == "receipt-run-two"
    assert next_manifest["source_input_identity"] == expanded_identity


def test_receipt_source_drift_does_not_advance_checkpoint(tmp_path: Path, monkeypatch):
    from agent_knowledge.local_embedding import transition

    class _PagedPgStore:
        def iter_local_embedding_receipt_rows(self, *, after_key, limit, profile_id):
            _ = (after_key, profile_id)
            row = {
                "session_id_hash": "private-session",
                "chunk_id": "private-chunk",
                "project": "neurons",
                "provider": "codex",
                "content_hash": "sha256:content",
                "input_hash": "sha256:input",
            }
            if after_key is not None:
                return []
            return [row][:limit]

    monkeypatch.setattr(
        transition,
        "_process_pg_local_receipt_row",
        lambda **_kwargs: ("stale_skipped", "inactive_session_chunk"),
    )
    checkpoint = tmp_path / "receipt-cursor.json"
    result = transition.process_pg_local_embedding_receipts(
        _PagedPgStore(),
        _CasCouchStore(None),
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        checkpoint_path=checkpoint,
        page_size=1,
        max_pages=1,
        max_rows=1,
        max_seconds=30,
    )

    manifest = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert result.stale_skipped == 1
    assert result.rows_processed == 0
    assert result.complete is False
    assert manifest["progress"]["rows_processed"] == 0


def test_receipt_cas_writer_uses_canonical_projection_state_id(monkeypatch):
    from agent_knowledge.couchdb_source import session_memory_materializer
    from agent_knowledge.couchdb_source.document_model import projection_state_doc_id
    from agent_knowledge.local_embedding import transition
    from agent_knowledge.rag_ingress import pg_representation

    session_hash = "sha256:" + "c" * 64
    content_hash = "sha256:" + "a" * 64
    input_hash = "sha256:" + "b" * 64
    current_materialized = SimpleNamespace(
        fully_materialized=True,
        content_hash=content_hash,
        source_hash="sha256:" + "c" * 64,
    )
    monkeypatch.setattr(
        session_memory_materializer,
        "materialize_session_memory",
        lambda **_kwargs: current_materialized,
    )
    monkeypatch.setattr(
        pg_representation,
        "build_local_representation_receipt_v3",
        lambda **_kwargs: {"receipt_version": 3},
    )
    monkeypatch.setattr(
        pg_representation,
        "valid_local_representation_receipt_v3",
        lambda *_args, **_kwargs: True,
    )

    cas_doc_ids = []

    def capture_cas(*, doc_id, **_kwargs):
        cas_doc_ids.append(doc_id)
        return SimpleNamespace(status="created", reason_code="receipt_created")

    monkeypatch.setattr(transition, "_cas_merge_projection_state_receipt", capture_cas)
    result = transition._process_pg_local_receipt_row(
        store=object(),
        couchdb_store=object(),
        row={
            "chunk_id": "synthetic-chunk-id",
            "project": "neurons",
            "provider": "codex",
            "session_id_hash": session_hash,
            "content_hash": content_hash,
            "input_hash": input_hash,
        },
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
    )

    expected_doc_id = projection_state_doc_id(session_hash)
    assert result == ("created", "receipt_created")
    assert cas_doc_ids == [expected_doc_id]
    assert f"projection_state:{session_hash}" not in cas_doc_ids


def test_receipt_verifier_reads_canonical_projection_state_id(monkeypatch):
    from agent_knowledge.couchdb_source import session_memory_materializer
    from agent_knowledge.couchdb_source.document_model import projection_state_doc_id
    from agent_knowledge.local_embedding import transition
    from agent_knowledge.rag_ingress import pg_representation

    session_hash = "sha256:" + "c" * 64
    receipt = {"receipt_version": 3}
    current_materialized = SimpleNamespace(
        fully_materialized=True,
        content_hash="sha256:" + "a" * 64,
        source_hash="sha256:" + "b" * 64,
    )
    monkeypatch.setattr(
        session_memory_materializer,
        "materialize_session_memory",
        lambda **_kwargs: current_materialized,
    )
    monkeypatch.setattr(
        pg_representation,
        "valid_local_pg_receipt_metadata",
        lambda *_args, **_kwargs: True,
    )

    class _ReceiptPgStore:
        def iter_local_session_receipt_session_ids(self, *, after_session_hash, limit):
            assert limit > 0
            return [session_hash] if not after_session_hash else []

    class _RecordingSourceStore:
        def __init__(self):
            self.get_doc_ids = []

        def get(self, doc_id):
            self.get_doc_ids.append(doc_id)
            return {
                "backend_receipts": {
                    "postgres_pgvector_local": {LOCAL_EMBEDDING_PROFILE_ID: receipt}
                }
            }

    source_store = _RecordingSourceStore()
    result = transition._inspect_pg_session_receipts(
        _ReceiptPgStore(),
        source_store,
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        eligible=1,
    )

    expected_doc_id = projection_state_doc_id(session_hash)
    assert result.checked == 1
    assert result.valid == 1
    assert source_store.get_doc_ids == [expected_doc_id]
    assert f"projection_state:{session_hash}" not in source_store.get_doc_ids

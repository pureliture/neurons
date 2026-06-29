import json
import os
import stat

import pytest

from agent_knowledge.session_memory.gc_backup import (
    GC_BACKUP_SCHEMA_VERSION,
    list_gc_backups,
    read_gc_backup,
    restore_gc_backup,
    write_gc_backup,
)


class _FakeRestoreClient:
    def __init__(self):
        self.uploaded: list = []
        self.parsed: list = []

    def upload_document(self, dataset_id, content, *, filename="x.md"):
        self.uploaded.append((dataset_id, content, filename))
        return {"document_id": "new-doc-123", "run": "UNSTART"}

    def request_parse(self, dataset_id, document_ids):
        self.parsed.append((dataset_id, tuple(document_ids)))


def _write(tmp_path, **over):
    kw = dict(
        kind="session_memory",
        knowledge_id="kn_sm_rev4",
        content_hash="sha256:abc",
        session_id_hash="sha256:sess",
        provider="codex",
        project="workspace-index-advisor",
        dataset_id="ds_session_memory",
        index_document_id="index-doc-RAW-12345",
        body="# session memory rev4\n\nredacted body.",
        replacement_knowledge_id="kn_sm_rev5",
    )
    kw.update(over)
    return write_gc_backup(tmp_path / "gc-backup", **kw)


def test_write_then_read_roundtrip_preserves_body_and_meta(tmp_path):
    path = _write(tmp_path)
    rec = read_gc_backup(path)
    assert rec["schema_version"] == GC_BACKUP_SCHEMA_VERSION
    assert rec["kind"] == "session_memory"
    assert rec["knowledge_id"] == "kn_sm_rev4"
    assert rec["replacement_knowledge_id"] == "kn_sm_rev5"
    assert rec["body"] == "# session memory rev4\n\nredacted body."
    assert rec["backed_up_at"]


def test_raw_document_id_never_stored_only_hash(tmp_path):
    path = _write(tmp_path)
    raw = "index-doc-RAW-12345"
    on_disk = path.read_text(encoding="utf-8")
    assert raw not in on_disk  # raw id must not be persisted
    rec = read_gc_backup(path)
    assert "index_document_id" not in rec
    assert rec["index_document_id_hash"] != raw and len(rec["index_document_id_hash"]) == 64


def test_backup_dir_is_private_0700(tmp_path):
    path = _write(tmp_path)
    root = tmp_path / "gc-backup"
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "session_memory").st_mode) == 0o700


def test_list_filters_by_kind(tmp_path):
    _write(tmp_path, kind="session_memory", knowledge_id="kn_a")
    _write(tmp_path, kind="transcript_memory", knowledge_id="kn_b")
    sm = list_gc_backups(tmp_path / "gc-backup", kind="session_memory")
    tm = list_gc_backups(tmp_path / "gc-backup", kind="transcript_memory")
    allb = list_gc_backups(tmp_path / "gc-backup")
    assert len(sm) == 1 and len(tm) == 1 and len(allb) == 2


def test_unsupported_kind_rejected(tmp_path):
    with pytest.raises(ValueError):
        _write(tmp_path, kind="bogus")


def test_restore_reuploads_body_and_reembeds(tmp_path):
    rec = read_gc_backup(_write(tmp_path, body="restore me", dataset_id="ds_x"))
    fake = _FakeRestoreClient()
    out = restore_gc_backup(fake, rec)
    assert out["restored"] is True and out["parsed"] is True
    assert out["new_document_id"] == "new-doc-123"
    assert fake.uploaded[0][0] == "ds_x" and fake.uploaded[0][1] == "restore me"
    assert fake.parsed == [("ds_x", ("new-doc-123",))]


def test_restore_no_parse_skips_reembed(tmp_path):
    rec = read_gc_backup(_write(tmp_path, dataset_id="ds_x"))
    fake = _FakeRestoreClient()
    out = restore_gc_backup(fake, rec, parse=False)
    assert out["restored"] is True and out["parsed"] is False
    assert fake.parsed == []


def test_restore_rejects_non_backup_record():
    with pytest.raises(ValueError):
        restore_gc_backup(_FakeRestoreClient(), {"schema_version": "nope"})

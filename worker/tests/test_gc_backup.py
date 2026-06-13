import os
import stat

import pytest

from agent_knowledge.session_memory.gc_backup import (
    GC_BACKUP_SCHEMA_VERSION,
    list_gc_backups,
    read_gc_backup,
    write_gc_backup,
)


def _write(tmp_path, **over):
    payload = dict(
        kind="session_memory",
        knowledge_id="kn_sm_rev4",
        content_hash="sha256:abc",
        session_id_hash="sha256:sess",
        provider="codex",
        project="workspace-ragflow-advisor",
        dataset_id="ds_session_memory",
        ragflow_document_id="ragflow-doc-RAW-12345",
        body="# session memory rev4\n\nredacted body.",
        replacement_knowledge_id="kn_sm_rev5",
    )
    payload.update(over)
    return write_gc_backup(tmp_path / "gc-backup", **payload)


def test_write_then_read_roundtrip_preserves_body_and_meta(tmp_path):
    path = _write(tmp_path)
    record = read_gc_backup(path)
    assert record["schema_version"] == GC_BACKUP_SCHEMA_VERSION
    assert record["kind"] == "session_memory"
    assert record["knowledge_id"] == "kn_sm_rev4"
    assert record["replacement_knowledge_id"] == "kn_sm_rev5"
    assert record["body"] == "# session memory rev4\n\nredacted body."
    assert record["backed_up_at"]


def test_raw_document_id_never_stored_only_hash(tmp_path):
    path = _write(tmp_path)
    raw_document_id = "ragflow-doc-RAW-12345"
    on_disk = path.read_text(encoding="utf-8")
    assert raw_document_id not in on_disk
    record = read_gc_backup(path)
    assert "ragflow_document_id" not in record
    assert record["ragflow_document_id_hash"] != raw_document_id
    assert len(record["ragflow_document_id_hash"]) == 64


def test_backup_dir_and_file_are_private(tmp_path):
    path = _write(tmp_path)
    root = tmp_path / "gc-backup"
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(root / "session_memory").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_list_filters_by_kind(tmp_path):
    _write(tmp_path, kind="session_memory", knowledge_id="kn_a")
    _write(tmp_path, kind="transcript_memory", knowledge_id="kn_b")
    session_memory = list_gc_backups(tmp_path / "gc-backup", kind="session_memory")
    transcript_memory = list_gc_backups(tmp_path / "gc-backup", kind="transcript_memory")
    all_backups = list_gc_backups(tmp_path / "gc-backup")
    assert len(session_memory) == 1
    assert len(transcript_memory) == 1
    assert len(all_backups) == 2


def test_unsupported_kind_rejected(tmp_path):
    with pytest.raises(ValueError):
        _write(tmp_path, kind="bogus")


def test_invalid_backup_record_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version":"nope"}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_gc_backup(path)

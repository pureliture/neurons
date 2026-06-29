import hashlib

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.session_memory.terminal_skipped_quarantine import (
    TERMINAL_SKIPPED_QUARANTINE_MARKER,
    TerminalSkippedQuarantineConfig,
    TerminalSkippedQuarantineRunner,
)


PROJECT = "workspace-index-advisor"


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _edge_manifest_hash(pairs: list[tuple[str, str]]) -> str:
    material = "\n".join("|".join(pair) for pair in sorted(pairs))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _seed_skipped(ledger: Ledger, session_id_hash: str, reason: str) -> None:
    ledger.mark_session_memory_dirty(
        session_id_hash=session_id_hash,
        provider="codex",
        project=PROJECT,
        reason="test-dirty",
    )
    ledger.mark_dirty_session_memory_skipped(session_id_hash=session_id_hash, reason=reason)


def _seed_active_session_memory(ledger: Ledger, session_id_hash: str) -> None:
    source_content_hash = _sha(f"{session_id_hash}:source")
    source_window_hash = _sha(f"{session_id_hash}:window")
    item = ledger.upsert_session_memory(
        knowledge_id=f"kn_{session_id_hash.split(':')[-1]}",
        content_hash=_sha(f"{session_id_hash}:content"),
        provider="codex",
        project=PROJECT,
        session_id_hash=session_id_hash,
        title="active session memory",
        summary="active session memory",
        evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        coverage_status="complete",
        source_manifest_hash=_edge_manifest_hash([(source_content_hash, source_window_hash)]),
        source_chunk_count=1,
    )
    ledger.record_session_memory_coverage(
        active_knowledge_id=item["knowledge_id"],
        source_content_hash=source_content_hash,
        source_window_hash=source_window_hash,
        derived_content_hash=item["content_hash"],
        redaction_version="redaction.v2",
        turn_start_index=1,
        turn_end_index=1,
    )
    ledger.mark_uploaded(item["knowledge_id"], dataset_id="ds_session_memory", document_id=f"doc_{item['knowledge_id']}", run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")
    ledger.promote_session_memory(item["knowledge_id"])


def test_terminal_skipped_quarantine_dry_run_does_not_mutate(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    _seed_skipped(ledger, "sha256:terminal-skip-a", "coverage_incomplete_before_upload")
    _seed_skipped(ledger, "sha256:terminal-skip-b", "source_session_unresolved")

    report = TerminalSkippedQuarantineRunner(
        config=TerminalSkippedQuarantineConfig(ledger_path=ledger_path, execute=False)
    ).run()

    assert report["eligible_count"] == 2
    assert report["selected_count"] == 2
    assert report["quarantined_count"] == 0
    assert report["blocking_missing_count_before"] == 2
    assert report["blocking_missing_count_after"] == 0
    assert report["mutation_performed"] is False
    assert report["raw_ids_printed"] is False
    assert Ledger(ledger_path).get_dirty_session_memory("sha256:terminal-skip-a")["status"] == "skipped"


def test_terminal_skipped_quarantine_execute_marks_audited_terminal_state(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    _seed_skipped(ledger, "sha256:terminal-skip-a", "coverage_incomplete_before_upload")
    _seed_skipped(ledger, "sha256:terminal-skip-b", "source_session_unresolved")

    report = TerminalSkippedQuarantineRunner(
        config=TerminalSkippedQuarantineConfig(ledger_path=ledger_path, execute=True)
    ).run()

    dirty = Ledger(ledger_path).get_dirty_session_memory("sha256:terminal-skip-a")
    with Ledger(ledger_path)._connect() as connection:
        audit_count = connection.execute("SELECT count(*) FROM session_memory_terminal_skipped_audit").fetchone()[0]
        categories = {
            row["category"]
            for row in connection.execute("SELECT category FROM session_memory_terminal_skipped_audit").fetchall()
        }

    assert report["eligible_count"] == 2
    assert report["quarantined_count"] == 2
    assert report["blocking_missing_count_after"] == 0
    assert report["audit_count"] == 2
    assert dirty["status"] == "quarantined"
    assert dirty["last_error_class"] == TERMINAL_SKIPPED_QUARANTINE_MARKER
    assert audit_count == 2
    assert categories == {"source_coverage_guard", "source_unresolved"}


def test_terminal_skipped_quarantine_keeps_unknown_skips_blocking(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    _seed_skipped(ledger, "sha256:terminal-skip-a", "coverage_incomplete_before_upload")
    _seed_skipped(ledger, "sha256:terminal-skip-unknown", "unexpected_terminal_reason")

    report = TerminalSkippedQuarantineRunner(
        config=TerminalSkippedQuarantineConfig(ledger_path=ledger_path, execute=True)
    ).run()

    assert report["eligible_count"] == 1
    assert report["quarantined_count"] == 1
    assert report["unknown_skipped_count"] == 1
    assert report["blocking_missing_count_after"] == 1
    assert Ledger(ledger_path).get_dirty_session_memory("sha256:terminal-skip-unknown")["status"] == "skipped"


def test_terminal_skipped_quarantine_ignores_skipped_session_with_active_memory(tmp_path):
    ledger_path = tmp_path / "ledger.sqlite"
    ledger = Ledger(ledger_path)
    session_id_hash = "sha256:terminal-skip-active"
    _seed_skipped(ledger, session_id_hash, "coverage_incomplete_before_upload")
    _seed_active_session_memory(ledger, session_id_hash)

    report = TerminalSkippedQuarantineRunner(
        config=TerminalSkippedQuarantineConfig(ledger_path=ledger_path, execute=True)
    ).run()

    assert report["eligible_count"] == 0
    assert report["blocking_missing_count_before"] == 0
    assert Ledger(ledger_path).get_dirty_session_memory(session_id_hash)["status"] == "skipped"

import hashlib
import json
from datetime import datetime, timedelta, timezone

from agent_knowledge.ledger import Ledger, SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS
from agent_knowledge.session_memory.transcript_volume_gc import (
    MIN_ACTIVE_AGE_FLOOR_SECONDS,
    TranscriptVolumeGcConfig,
    TranscriptVolumeGcRunner,
    main,
)


PROJECT = "workspace-ragflow-advisor"
SESSION = "sha256:vol-sess"
SM_DS = "ds_session_memory"
TX_DS = "ds_transcript_memory"


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _manifest(pairs):
    return _sha("\n".join("|".join(pair) for pair in sorted(pairs)))


def _active_sm_covering(ledger, *, kid, doc, source_hash, aged=True):
    swd = _sha("win:" + source_hash)
    item = ledger.upsert_session_memory(
        knowledge_id=kid,
        content_hash=_sha(kid),
        provider="codex",
        project=PROJECT,
        session_id_hash=SESSION,
        title=kid,
        summary=kid,
        evidence_status=SESSION_MEMORY_REGENERATION_EVIDENCE_STATUS,
        coverage_status="complete",
        source_manifest_hash=_manifest([(source_hash, swd)]),
        source_chunk_count=1,
    )
    ledger.record_session_memory_coverage(
        active_knowledge_id=item["knowledge_id"],
        source_content_hash=source_hash,
        source_window_hash=swd,
        derived_content_hash=item["content_hash"],
        redaction_version="redaction.v2",
        turn_start_index=1,
        turn_end_index=1,
    )
    ledger.mark_uploaded(item["knowledge_id"], dataset_id=SM_DS, document_id=doc, run="DONE")
    ledger.mark_indexed(item["knowledge_id"], run="DONE")
    ledger.promote_session_memory(item["knowledge_id"])
    if aged:
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=2 * MIN_ACTIVE_AGE_FLOOR_SECONDS)).isoformat()
        with ledger._connect() as c:
            c.execute(
                "UPDATE session_memory_active_snapshots SET updated_at=?, activated_at=? WHERE active_knowledge_id=?",
                (stamp, stamp, item["knowledge_id"]),
            )
    return item


def _cfg(tmp_path, ledger_path, *, execute=False, backup=True):
    return TranscriptVolumeGcConfig(
        ledger_path=ledger_path,
        transcript_dataset_id=TX_DS,
        ragflow_url="http://localhost:9380",
        backup_dir=str(tmp_path / "gc-backup") if backup else "",
        execute=execute,
    )


def test_dryrun_lists_covered_source_hash(tmp_path):
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=True)

    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=False), token="t").run()

    assert report["eligible_count"] == 1
    assert report["mode"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_execute_is_blocked_in_worker_slice(tmp_path):
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=True)

    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=True), token="t").run()

    assert report["status"] == "blocked_live_execution"
    assert report["eligible_count"] == 1
    assert report["deleted_count"] == 0
    assert report["hard_delete_performed"] is False
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_fresh_active_below_floor_not_eligible(tmp_path):
    src = _sha("covered-source-1")
    ledger_path = tmp_path / "l.sqlite"
    ledger = Ledger(ledger_path)
    _active_sm_covering(ledger, kid="kn_sm5", doc="doc_sm5", source_hash=src, aged=False)

    report = TranscriptVolumeGcRunner(config=_cfg(tmp_path, ledger_path, execute=False), token="t").run()

    assert report["eligible_count"] == 0
    assert report["deleted_count"] == 0


def test_transcript_volume_gc_cli_dry_run_reports_json_without_network(tmp_path, capsys):
    ledger_path = tmp_path / "l.sqlite"
    Ledger(ledger_path)

    exit_code = main([
        "--ledger",
        str(ledger_path),
        "--transcript-dataset-id",
        TX_DS,
        "--ragflow-url",
        "http://localhost:9380",
    ])

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["mode"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_transcript_volume_gc_cli_execute_is_fail_closed(tmp_path, capsys):
    ledger_path = tmp_path / "l.sqlite"
    Ledger(ledger_path)

    exit_code = main([
        "--ledger",
        str(ledger_path),
        "--transcript-dataset-id",
        TX_DS,
        "--ragflow-url",
        "http://localhost:9380",
        "--execute",
    ])

    assert exit_code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "blocked_live_execution"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False

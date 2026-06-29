import json

from agent_knowledge.ledger import Ledger
from agent_knowledge.rag_ingress.replay_delivery import (
    IngressEnqueueRejected,
    IngressEnqueueUnreachable,
    reconstruct_replay_payload,
    replay_deliver_dispositions,
    select_replay_rows,
    validate_replay_payload,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk

PROJECT = "workspace-index-advisor"
DEFAULT_TRANSCRIPT_TARGET_PROFILE = "index-transcript-memory"


def _replay_requested_chunk(
    ledger,
    *,
    knowledge_id,
    chunk_id,
    session_id_hash,
    provider="codex",
    text="replay chunk redacted text",
    job_id="job_original_1",
):
    chunk = TranscriptChunk(
        chunk_id=chunk_id,
        session_id_hash=session_id_hash,
        provider=provider,
        project=PROJECT,
        turn_start_index=1,
        turn_end_index=2,
        redacted_text=text,
        content_hash="sha256:caller_ignored",
    )
    item = ledger.upsert_transcript_chunk(knowledge_id=knowledge_id, chunk=chunk)
    ledger.mark_enqueued(
        item["knowledge_id"],
        target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        job_id=job_id,
    )
    projected = ledger.list_queued_documents(
        document_type="conversation_chunk",
        target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        limit=50,
    )
    row = next(r for r in projected if r["knowledge_id"] == item["knowledge_id"])
    ledger.mark_replay_requested_if_queued(
        item["knowledge_id"],
        reason="m5_replay_missing_packet",
        expected_target_profile=row["target_profile"],
        expected_ingress_job_id=row["ingress_job_id"],
        expected_updated_at=row["updated_at"],
    )
    return item


class _FakeIngress:
    def __init__(self, *, job_id="job_replay_new", raise_with=None):
        self._job_id = job_id
        self._raise_with = raise_with
        self.calls = []

    def enqueue_document_payload(self, payload):
        validate_replay_payload(payload)
        self.calls.append(payload)
        if self._raise_with is not None:
            raise self._raise_with
        return {"job_id": self._job_id, "status": "queued"}


def _approval(path, *, operation, argv, candidate_set_digest):
    path.write_text(
        json.dumps(
            {
                "schema_version": "agent_knowledge_live_approval.v1",
                "operation": operation,
                "target": {"candidate_set_digest": candidate_set_digest},
                "command": {"argv": ["agent-knowledge", *argv]},
                "timeout_seconds": 300,
                "redaction_required": True,
                "rollback_or_abort_criteria": ["timeout expires", "digest mismatch"],
                "operator_approval": {"approved": True, "approved_by": "ddalkak"},
            }
        ),
        encoding="utf-8",
    )
    return path


# ---- reconstruction ----

def test_reconstruct_replay_payload_carries_convergence_metadata(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(
        ledger,
        knowledge_id="kn_recon",
        chunk_id="chunk_recon",
        session_id_hash="sha256:sess_recon",
    )
    selected = select_replay_rows(ledger, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE, limit=50)
    row, chunk = selected[0]

    payload = reconstruct_replay_payload(row=row, chunk=chunk, attempt=1)

    # convergence-critical natural key is exact
    meta = payload["payload"]["document"]["metadata"]
    assert meta["type"] == "conversation_chunk"
    assert meta["knowledge_id"] == item["knowledge_id"]
    assert meta["chunk_id"] == "chunk_recon"
    assert meta["provider"] == "codex"
    assert meta["project"] == PROJECT
    assert meta["session_id_hash"] == "sha256:sess_recon"
    assert meta["m6_replay_reconstructed"] == "true"
    # salted key + valid wire payload
    assert payload["idempotencyKey"] == f"{item['knowledge_id']}:replay.1"
    assert payload["contentHash"].startswith("sha256:")
    assert payload["schemaVersion"] == "rag_ingress_enqueue.v1"
    assert "replay chunk redacted text" in payload["payload"]["document"]["body"]
    validate_replay_payload(payload)  # must not raise


def test_select_replay_rows_ignores_plain_queued_rows(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    _replay_requested_chunk(
        ledger, knowledge_id="kn_sel_replay", chunk_id="chunk_sel_replay",
        session_id_hash="sha256:sess_sel_replay", text="replay one",
    )
    # a plain queued chunk (not replay_requested)
    plain = TranscriptChunk(
        chunk_id="chunk_plain", session_id_hash="sha256:sess_plain", provider="codex",
        project=PROJECT, turn_start_index=1, turn_end_index=1, redacted_text="plain text",
        content_hash="sha256:caller_ignored",
    )
    item = ledger.upsert_transcript_chunk(knowledge_id="kn_plain", chunk=plain)
    ledger.mark_enqueued(item["knowledge_id"], target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE, job_id="job_plain")

    selected = select_replay_rows(ledger, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE, limit=50)
    assert len(selected) == 1
    assert selected[0][0]["knowledge_id"] == "kn_sel_replay"


# ---- dry-run / probe ----

def test_replay_deliver_dry_run_no_mutation_no_network(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(
        ledger, knowledge_id="kn_dry", chunk_id="chunk_dry", session_id_hash="sha256:sess_dry",
    )
    client = _FakeIngress()

    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", limit=50, dry_run=True,
    )

    assert result["selected_count"] == 1
    assert result["delivered_count"] == 0
    assert result["execution_status"] == "dry_run"
    assert result["mutation_performed"] is False
    assert result["network_used"] is False
    assert result["remote_enqueue_count"] == 0
    assert client.calls == []
    row = ledger.get_by_knowledge_id(item["knowledge_id"])
    assert row["status"] == "queued"
    assert json.loads(row["metadata_json"])["m5_disposition_status"] == "replay_requested"


def test_replay_deliver_probe_limits_to_one(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    _replay_requested_chunk(ledger, knowledge_id="kn_p1", chunk_id="chunk_p1", session_id_hash="sha256:p1", text="p1 text")
    _replay_requested_chunk(ledger, knowledge_id="kn_p2", chunk_id="chunk_p2", session_id_hash="sha256:p2", text="p2 text")

    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=_FakeIngress(), target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", limit=50, probe=True, dry_run=True,
    )
    assert result["selected_count"] == 1
    assert result["probe"] is True


# ---- apply ----

def test_replay_deliver_apply_reenqueues_and_marks_delivered(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(
        ledger, knowledge_id="kn_apply", chunk_id="chunk_apply", session_id_hash="sha256:sess_apply",
    )
    client = _FakeIngress(job_id="job_replay_new_apply")
    dry = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", limit=50, dry_run=True,
    )

    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", limit=50, dry_run=False,
        expected_candidate_set_digest=dry["candidate_set_digest"],
    )

    assert result["delivered_count"] == 1
    assert result["execution_status"] == "executed"
    assert result["mutation_performed"] is True
    assert result["network_used"] is True
    # the row was genuinely re-enqueued with the new job id, still queued for the worker
    row = ledger.get_by_knowledge_id(item["knowledge_id"])
    assert row["status"] == "queued"
    assert row["ingress_job_id"] == "job_replay_new_apply"
    assert json.loads(row["metadata_json"])["m5_disposition_status"] == "replay_delivered"
    # the posted payload used a salted key distinct from the original
    assert len(client.calls) == 1
    assert client.calls[0]["idempotencyKey"] == f"{item['knowledge_id']}:replay.1"


def test_replay_deliver_apply_is_idempotent_on_rerun(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    _replay_requested_chunk(ledger, knowledge_id="kn_idem", chunk_id="chunk_idem", session_id_hash="sha256:idem")
    client = _FakeIngress(job_id="job_idem_new")
    dry = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=True,
    )
    replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=False,
        expected_candidate_set_digest=dry["candidate_set_digest"],
    )
    # second run: the delivered row is no longer replay_requested, so nothing is selected
    rerun = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=True,
    )
    assert rerun["selected_count"] == 0


# ---- fail-closed / race / unreachable ----

def test_replay_deliver_requires_digest_for_apply(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(ledger, knowledge_id="kn_nodig", chunk_id="chunk_nodig", session_id_hash="sha256:nodig")
    client = _FakeIngress()

    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=False,
    )
    assert result["delivered_count"] == 0
    assert result["blockers"] == ["candidate_set_digest_required"]
    assert result["execution_status"] == "blocked"
    assert client.calls == []
    assert ledger.get_by_knowledge_id(item["knowledge_id"])["status"] == "queued"


def test_replay_deliver_rejects_stale_digest(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(ledger, knowledge_id="kn_stale", chunk_id="chunk_stale", session_id_hash="sha256:stale")
    client = _FakeIngress()

    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=False,
        expected_candidate_set_digest="sha256:not_current",
    )
    assert result["delivered_count"] == 0
    assert result["blockers"] == ["candidate_set_digest_mismatch"]
    assert result["execution_status"] == "blocked"
    assert client.calls == []
    assert json.loads(ledger.get_by_knowledge_id(item["knowledge_id"])["metadata_json"])["m5_disposition_status"] == "replay_requested"


def test_replay_deliver_cas_race_reports_remote_mutation(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    _replay_requested_chunk(ledger, knowledge_id="kn_race", chunk_id="chunk_race", session_id_hash="sha256:race")
    client = _FakeIngress(job_id="job_race_new")
    dry = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=True,
    )
    ledger.mark_replay_delivered_if_queued = lambda *a, **k: False

    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=False,
        expected_candidate_set_digest=dry["candidate_set_digest"],
    )
    assert result["delivered_count"] == 0
    assert result["race_skipped_count"] == 1
    # the remote POST happened BEFORE the ledger CAS failed: the report must not
    # under-state that a remote queue job was already created
    assert len(client.calls) == 1
    assert result["remote_enqueue_count"] == 1
    assert result["mutation_performed"] is True
    assert result["blockers"] == ["remote_enqueued_ledger_cas_failed"]
    assert result["execution_status"] == "partial_failure"
    assert result["resume_required"] is True


def test_replay_deliver_clean_apply_has_no_cas_failure_blocker(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    _replay_requested_chunk(ledger, knowledge_id="kn_clean", chunk_id="chunk_clean", session_id_hash="sha256:clean")
    client = _FakeIngress(job_id="job_clean_new")
    dry = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=True,
    )

    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=False,
        expected_candidate_set_digest=dry["candidate_set_digest"],
    )
    assert result["delivered_count"] == 1
    assert result["remote_enqueue_count"] == 1
    assert result["blockers"] == []
    assert result["execution_status"] == "executed"


def test_replay_deliver_unreachable_stops_without_mutation(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(ledger, knowledge_id="kn_unreach", chunk_id="chunk_unreach", session_id_hash="sha256:unreach")
    client = _FakeIngress(raise_with=IngressEnqueueUnreachable("down"))
    dry = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=True,
    )

    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=False,
        expected_candidate_set_digest=dry["candidate_set_digest"],
    )
    assert result["delivered_count"] == 0
    assert result["unreachable_stop"] is True
    assert result["execution_status"] == "blocked_unreachable"
    assert result["mutation_performed"] is False
    assert result["remote_enqueue_count"] == 0
    assert ledger.get_by_knowledge_id(item["knowledge_id"])["status"] == "queued"


def test_replay_deliver_rejected_payload_is_counted_not_mutated(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(ledger, knowledge_id="kn_rej", chunk_id="chunk_rej", session_id_hash="sha256:rej")
    client = _FakeIngress(raise_with=IngressEnqueueRejected("rejected"))
    dry = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=True,
    )

    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=False,
        expected_candidate_set_digest=dry["candidate_set_digest"],
    )
    assert result["rejected_count"] == 1
    assert result["delivered_count"] == 0
    # a rejected POST created no remote queue job
    assert result["remote_enqueue_count"] == 0
    assert result["mutation_performed"] is False
    assert ledger.get_by_knowledge_id(item["knowledge_id"])["status"] == "queued"


# ---- redaction ----

def test_replay_deliver_report_is_redacted(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    _replay_requested_chunk(
        ledger, knowledge_id="kn_secret_redact", chunk_id="chunk_secret_redact",
        session_id_hash="sha256:sess_secret_redact", text="redact me body text", job_id="job_secret_redact",
    )
    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=_FakeIngress(), target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m6_replay_delivery_packet", dry_run=True,
    )
    blob = json.dumps(result)
    assert "kn_secret_redact" not in blob
    assert "chunk_secret_redact" not in blob
    assert "job_secret_redact" not in blob
    assert "redact me body text" not in blob


# ---- structural blockers (key/source completeness) ----

def test_replay_key_incomplete_helper():
    from agent_knowledge.rag_ingress.replay_delivery import _replay_key_incomplete
    complete = ({"knowledge_id": "k", "session_id_hash": "s", "metadata": {"chunk_id": "c"}}, {})
    assert _replay_key_incomplete(*complete) is False
    assert _replay_key_incomplete({"knowledge_id": "", "metadata": {"chunk_id": "c"}, "session_id_hash": "s"}, {}) is True
    assert _replay_key_incomplete({"knowledge_id": "k", "metadata": {}, "session_id_hash": "s"}, {}) is True


class _StubLedger:
    def __init__(self, rows, chunks):
        self._rows = rows
        self._chunks = chunks

    def list_queued_documents(self, *, document_type, target_profile, limit):
        return self._rows

    def get_transcript_chunk_by_knowledge_id(self, knowledge_id):
        return self._chunks.get(knowledge_id)


def test_replay_deliver_blocks_on_incomplete_key_and_missing_text():
    row = {
        "knowledge_id": "",
        "metadata": {"m5_disposition_status": "replay_requested", "chunk_id": ""},
        "session_id_hash": "",
        "target_profile": "index-transcript-memory",
        "ingress_job_id": "j",
        "updated_at": "t",
    }
    ledger = _StubLedger([row], {})
    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=_FakeIngress(), target_profile="index-transcript-memory",
        reason="m6_replay_delivery_packet", dry_run=True,
    )
    assert "replay_source_text_missing" in result["blockers"]
    assert "replay_key_incomplete" in result["blockers"]
    assert result["incomplete_key_count"] == 1

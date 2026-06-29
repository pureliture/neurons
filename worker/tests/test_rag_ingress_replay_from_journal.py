"""M7.3: replay-deliver --from-journal (byte-faithful replay with fallback).

Contract under test:
- journal hit (keyed by the replay row's knowledge_id) -> the re-POSTed document
  body/metadata/contentHash are BYTE-FAITHFUL to the originally acked wire payload;
  only the idempotencyKey is salted with the replay attempt (transport dedupe).
- journal miss -> M6 best-effort reconstruct fallback, unchanged.
- the report separates journal_hit_count / reconstructed_count.
- a journal hit neutralizes the chunk-text/key blockers for that row (its body does
  not come from the ledger chunk).
"""

import hashlib
import json

from agent_knowledge.ledger import Ledger
from agent_knowledge.rag_ingress.ingress_journal import IngressJournal
from agent_knowledge.rag_ingress.replay_delivery import replay_deliver_dispositions, validate_replay_payload
from agent_knowledge.session_memory.transcript_model import TranscriptChunk

PROJECT = "workspace-index-advisor"
DEFAULT_TRANSCRIPT_TARGET_PROFILE = "index-transcript-memory"


def _replay_requested_chunk(ledger, *, knowledge_id, chunk_id, session_id_hash, text="replay chunk text"):
    chunk = TranscriptChunk(
        chunk_id=chunk_id,
        session_id_hash=session_id_hash,
        provider="codex",
        project=PROJECT,
        turn_start_index=1,
        turn_end_index=2,
        redacted_text=text,
        content_hash="sha256:caller_ignored",
    )
    item = ledger.upsert_transcript_chunk(knowledge_id=knowledge_id, chunk=chunk)
    ledger.mark_enqueued(item["knowledge_id"], target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE, job_id="job_orig")
    row = next(
        r
        for r in ledger.list_queued_documents(
            document_type="conversation_chunk", target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE, limit=50
        )
        if r["knowledge_id"] == item["knowledge_id"]
    )
    ledger.mark_replay_requested_if_queued(
        item["knowledge_id"],
        reason="m7_replay_from_journal",
        expected_target_profile=row["target_profile"],
        expected_ingress_job_id=row["ingress_job_id"],
        expected_updated_at=row["updated_at"],
    )
    return item


def _original_wire_payload(*, knowledge_id, chunk_id, session_id_hash, body):
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {"host": "mac_mini", "producer": "session-compactor", "provider": "codex", "project": PROJECT},
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "filename": f"ak-conv-{chunk_id}.md",
                "contentType": "text/markdown",
                "body": body,
                "metadata": {
                    "type": "conversation_chunk",
                    "knowledge_id": knowledge_id,
                    "chunk_id": chunk_id,
                    "provider": "codex",
                    "project": PROJECT,
                    "session_id_hash": session_id_hash,
                },
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "targetProfile": DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        "kind": "conversation_chunk",
        "idempotencyKey": f"original-key-{knowledge_id}",
    }


class _FakeIngress:
    def __init__(self):
        self.calls = []

    def enqueue_document_payload(self, payload):
        validate_replay_payload(payload)
        self.calls.append(payload)
        return {"job_id": "job_replay_new", "status": "queued"}


def _run(ledger, client, *, journal, dry_run):
    kwargs = {}
    if not dry_run:
        dry = replay_deliver_dispositions(
            ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
            reason="m7_replay_from_journal", dry_run=True, journal=journal,
        )
        kwargs["expected_candidate_set_digest"] = dry["candidate_set_digest"]
    return replay_deliver_dispositions(
        ledger=ledger, ingress_client=client, target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m7_replay_from_journal", dry_run=dry_run, journal=journal, **kwargs,
    )


# ---- journal hit: byte-faithful ----

def test_journal_hit_reflushes_byte_faithful_document(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(
        ledger, knowledge_id="kn_j1", chunk_id="chunk_j1", session_id_hash="sha256:sess_j1"
    )
    original = _original_wire_payload(
        knowledge_id=item["knowledge_id"], chunk_id="chunk_j1",
        session_id_hash="sha256:sess_j1", body="ORIGINAL WIRE BODY exact bytes\n",
    )
    journal = IngressJournal(tmp_path / "private" / "journal")
    assert journal.record(original) is True
    client = _FakeIngress()

    result = _run(ledger, client, journal=journal, dry_run=False)

    assert result["journal_hit_count"] == 1
    assert result["reconstructed_count"] == 0
    assert result["delivered_count"] == 1
    posted = client.calls[0]
    # body + metadata + contentHash are the original bytes, not a reconstruction
    assert posted["payload"]["document"]["body"] == "ORIGINAL WIRE BODY exact bytes\n"
    assert posted["payload"]["document"]["metadata"] == original["payload"]["document"]["metadata"]
    assert posted["contentHash"] == original["contentHash"]
    assert posted["payload"]["document"]["filename"] == original["payload"]["document"]["filename"]
    # only the transport idempotencyKey is salted with the replay attempt
    assert posted["idempotencyKey"] == f"{item['knowledge_id']}:replay.1"
    assert result["reconstruction_fidelity"] == "byte_faithful_journal_with_reconstruct_fallback"


def test_journal_replay_does_not_mutate_stored_entry(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(
        ledger, knowledge_id="kn_j2", chunk_id="chunk_j2", session_id_hash="sha256:sess_j2"
    )
    original = _original_wire_payload(
        knowledge_id=item["knowledge_id"], chunk_id="chunk_j2",
        session_id_hash="sha256:sess_j2", body="immutable body\n",
    )
    journal = IngressJournal(tmp_path / "private" / "journal")
    journal.record(original)

    _run(ledger, _FakeIngress(), journal=journal, dry_run=False)

    # the journal still holds the ORIGINAL idempotencyKey, not the salted one
    assert journal.get(item["knowledge_id"]) == original


def test_journal_overwrite_replays_latest_acked_bytes(tmp_path):
    # latest-wins semantics: re-recording the same knowledge_id overwrites, so a
    # replay re-flushes the MOST RECENT acked wire bytes (codearch follow-up 5a)
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(
        ledger, knowledge_id="kn_j3", chunk_id="chunk_j3", session_id_hash="sha256:sess_j3"
    )
    journal = IngressJournal(tmp_path / "private" / "journal")
    journal.record(
        _original_wire_payload(
            knowledge_id=item["knowledge_id"], chunk_id="chunk_j3",
            session_id_hash="sha256:sess_j3", body="first acked body\n",
        )
    )
    latest = _original_wire_payload(
        knowledge_id=item["knowledge_id"], chunk_id="chunk_j3",
        session_id_hash="sha256:sess_j3", body="second acked body\n",
    )
    journal.record(latest)
    client = _FakeIngress()

    _run(ledger, client, journal=journal, dry_run=False)

    posted = client.calls[0]
    assert posted["payload"]["document"]["body"] == "second acked body\n"
    assert posted["contentHash"] == latest["contentHash"]


# ---- journal miss: fallback ----

def test_journal_miss_falls_back_to_reconstruct(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    _replay_requested_chunk(
        ledger, knowledge_id="kn_miss", chunk_id="chunk_miss",
        session_id_hash="sha256:sess_miss", text="fallback chunk text",
    )
    journal = IngressJournal(tmp_path / "private" / "journal")  # empty
    client = _FakeIngress()

    result = _run(ledger, client, journal=journal, dry_run=False)

    assert result["journal_hit_count"] == 0
    assert result["reconstructed_count"] == 1
    assert result["delivered_count"] == 1
    posted = client.calls[0]
    assert posted["payload"]["document"]["metadata"]["m6_replay_reconstructed"] == "true"
    assert "fallback chunk text" in posted["payload"]["document"]["body"]


def test_mixed_hit_and_miss_are_counted_separately(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    hit = _replay_requested_chunk(
        ledger, knowledge_id="kn_mix_hit", chunk_id="chunk_mix_hit",
        session_id_hash="sha256:mix_hit", text="mix hit chunk text",
    )
    _replay_requested_chunk(
        ledger, knowledge_id="kn_mix_miss", chunk_id="chunk_mix_miss",
        session_id_hash="sha256:mix_miss", text="mix miss chunk text",
    )
    journal = IngressJournal(tmp_path / "private" / "journal")
    journal.record(
        _original_wire_payload(
            knowledge_id=hit["knowledge_id"], chunk_id="chunk_mix_hit",
            session_id_hash="sha256:mix_hit", body="mix hit original\n",
        )
    )
    client = _FakeIngress()

    result = _run(ledger, client, journal=journal, dry_run=False)

    assert result["selected_count"] == 2
    assert result["journal_hit_count"] == 1
    assert result["reconstructed_count"] == 1
    assert result["delivered_count"] == 2


# ---- dry-run / no journal ----

def test_dry_run_reports_journal_hits_without_post(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    item = _replay_requested_chunk(
        ledger, knowledge_id="kn_dryj", chunk_id="chunk_dryj", session_id_hash="sha256:dryj"
    )
    journal = IngressJournal(tmp_path / "private" / "journal")
    journal.record(
        _original_wire_payload(
            knowledge_id=item["knowledge_id"], chunk_id="chunk_dryj",
            session_id_hash="sha256:dryj", body="dry body\n",
        )
    )
    client = _FakeIngress()

    result = _run(ledger, client, journal=journal, dry_run=True)

    assert result["journal_hit_count"] == 1
    assert result["reconstructed_count"] == 0
    assert result["execution_status"] == "dry_run"
    assert client.calls == []


def test_without_journal_report_counts_all_as_reconstructed(tmp_path):
    ledger = Ledger(tmp_path / "private" / "ledger.sqlite")
    _replay_requested_chunk(
        ledger, knowledge_id="kn_noj", chunk_id="chunk_noj", session_id_hash="sha256:noj"
    )
    result = replay_deliver_dispositions(
        ledger=ledger, ingress_client=_FakeIngress(), target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m7_replay_from_journal", dry_run=True,
    )
    assert result["journal_hit_count"] == 0
    assert result["reconstructed_count"] == 1
    assert result["reconstruction_fidelity"] == "convergence_faithful_body_best_effort"


# ---- blocker scoping ----

class _StubLedger:
    def __init__(self, rows, chunks):
        self._rows = rows
        self._chunks = chunks

    def list_queued_documents(self, *, document_type, target_profile, limit):
        return self._rows

    def get_transcript_chunk_by_knowledge_id(self, knowledge_id):
        return self._chunks.get(knowledge_id)


def _stub_row(knowledge_id):
    return {
        "knowledge_id": knowledge_id,
        "metadata": {"m5_disposition_status": "replay_requested", "chunk_id": ""},
        "session_id_hash": "",
        "target_profile": DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        "ingress_job_id": "j",
        "updated_at": "t",
        "provider": "codex",
        "project": PROJECT,
    }


def test_journal_hit_neutralizes_chunk_blockers_for_that_row(tmp_path):
    # row with no chunk text and incomplete chunk-side key, but a journal hit
    journal = IngressJournal(tmp_path / "private" / "journal")
    journal.record(
        _original_wire_payload(
            knowledge_id="kn_blocked", chunk_id="chunk_b",
            session_id_hash="sha256:b", body="journal covers this row\n",
        )
    )
    ledger = _StubLedger([_stub_row("kn_blocked")], {})

    with_journal = replay_deliver_dispositions(
        ledger=ledger, ingress_client=_FakeIngress(), target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m7_replay_from_journal", dry_run=True, journal=journal,
    )
    assert with_journal["blockers"] == []
    assert with_journal["missing_source_text_count"] == 0
    assert with_journal["incomplete_key_count"] == 0
    assert with_journal["journal_hit_count"] == 1

    without_journal = replay_deliver_dispositions(
        ledger=ledger, ingress_client=_FakeIngress(), target_profile=DEFAULT_TRANSCRIPT_TARGET_PROFILE,
        reason="m7_replay_from_journal", dry_run=True,
    )
    assert "replay_source_text_missing" in without_journal["blockers"]
    assert "replay_key_incomplete" in without_journal["blockers"]


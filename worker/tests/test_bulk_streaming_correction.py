"""TDD for independent-review corrections: memory, receipts, privacy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_full_input_pg_state import PgChunkState
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    BulkSemanticWriteReport,
    DeterministicGraphitiSemanticWriter,
    OpenAICompatibleBulkSemanticExtractor,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

pytestmark = pytest.mark.skipif(not os.environ.get("TRIAL_PG_DSN"), reason="no local PG")
P = "synthetic-provider"
PFX = uuid.uuid4().hex[:8] + "-"


class _Extractor:
    def __init__(self, secrets=()):
        self.texts = []
        self.secrets = tuple(secrets)
        self.model = "free-model"
        self.provider = P
        self.endpoint_class = "approved_free"
        self.price_status = "free_confirmed"

    def extract(self, batch):
        self.texts.extend(item.text for item in batch)
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=PFX + "E", type="Concept", summary="s"),),
            )
            for item in batch
        ))


class _SelectiveWriter:
    """Verifies only explicitly keyed receipts, including out-of-order success."""

    def __init__(self, good_suffixes):
        self.good_suffixes = tuple(good_suffixes)

    def write_batch(self, inputs, extraction, *, allow_empty_sessions=False):
        receipts = {
            item.session_key: "receipt:" + item.session_key
            for item in inputs
            if item.session_key.endswith(self.good_suffixes)
        }
        return BulkSemanticWriteReport(
            projected=len(receipts), entities_written=len(receipts), relations_written=0,
            chunk_receipts=receipts,
        )


def _seed(store, project, bodies, *, evidence="", secret_marker="", omit_provenance=False):
    sid = dm.build_session_id_hash(P, project + "-session")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=project,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for i, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(chunk_id=f"chunk_{i}", session_id_hash=sid,
                                          provider=P, project=project, turn_start_index=i,
                                          turn_end_index=i, text=body + secret_marker)
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        if omit_provenance:
            doc.pop("redaction_version", None)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    ec = 0
    if evidence:
        ev = dm.build_tool_evidence_bundle_document(
            session_id_hash=sid, provider=P, project=project, part_index=0, part_count=1,
            evidence_index_start=0, evidence_index_end=1, record_content_hashes=[],
            body=evidence + secret_marker)
        if omit_provenance:
            ev.pop("redaction_version", None)
        store.put(ev)
        ec = 1
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=project,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=ec,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True}))
    return sid


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    store = InMemoryCouchDBSourceStore()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kw: store)
    args = ["--ledger", str(tmp_path / "ledger.db"), "--runtime-dir", str(tmp_path / "runtime"),
            "--full-input-policy-version", PFX + "v1", "--limit", "1"]
    return dict(store=store, args=args, tmp=tmp_path, monkeypatch=monkeypatch)


def _run(env):
    rc = cli.main(list(env["args"]))
    return rc, json.loads(__import__("io").StringIO()) if False else rc


def _run_report(env, capsys):
    rc = cli.main(list(env["args"]))
    out = capsys.readouterr()
    return rc, json.loads(out.out or out.err)


def test_per_chunk_receipts_allow_out_of_order_success_only(env, capsys):
    project = PFX + "receipts"
    sid = _seed(env["store"], project, ["front", "middle", "back"], evidence=False)
    extractor = _Extractor()
    env["monkeypatch"].setattr(OpenAICompatibleBulkSemanticExtractor, "from_env",
                               classmethod(lambda cls, **kw: extractor))
    # Only back chunk succeeds: prefix-based logic would wrongly mark chunk 0.
    env["monkeypatch"].setattr(DeterministicGraphitiSemanticWriter, "from_env",
                               classmethod(lambda cls, **kw: _SelectiveWriter(("-c2",))))
    rc, report = _run_report(env, capsys)
    assert rc == 0, json.dumps(report, sort_keys=True)
    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    completed = state.completed_chunks(project, report["full_input_policy_version"])
    assert len(completed) == 1, (os.environ["TRIAL_PG_DSN"][-8:], completed)
    assert completed[0].endswith(":sha256:") or "-c2" not in completed[0]
    assert all("-c0" not in key for key in completed)


def test_privacy_evidence_and_exception_leak_are_fail_closed(env, capsys):
    project = PFX + "privacy"
    sid = _seed(env["store"], project, ["public"], evidence="evidence public",
                secret_marker=" SUPERSECRET_SYNTHETIC_MARKER")
    extractor = _Extractor()
    env["monkeypatch"].setattr(OpenAICompatibleBulkSemanticExtractor, "from_env",
                               classmethod(lambda cls, **kw: extractor))
    env["args"].extend(["--privacy-forbidden-marker", "SUPERSECRET_SYNTHETIC_MARKER"])
    rc, report = _run_report(env, capsys)
    assert rc == 1
    assert extractor.texts == []
    assert "SUPERSECRET_SYNTHETIC_MARKER" not in json.dumps(report)
    assert report.get("error_reason") in {"source_invalid", "privacy_violation"} or \
        report["projection"]["failure_reasons"]


def test_missing_redaction_provenance_is_fail_closed(env, capsys):
    project = PFX + "provenance"
    _seed(env["store"], project, ["body"], evidence="evidence", omit_provenance=True)
    env["monkeypatch"].setattr(OpenAICompatibleBulkSemanticExtractor, "from_env",
                               classmethod(lambda cls, **kw: _Extractor()))
    rc, report = _run_report(env, capsys)
    assert rc == 1, json.dumps(report, sort_keys=True)
    assert report["full_input_source_invalid"] >= 1


def _make_source_file(path: Path, total_bytes: int, chunk_bytes: int):
    body = ("한글경계" + "A" * (chunk_bytes - 12)).encode("utf-8")[:chunk_bytes]
    written = 0
    with path.open("w", encoding="utf-8") as fh:
        index = 0
        while written < total_bytes:
            payload = body.decode("utf-8", errors="ignore")
            doc = {"doc_type": "conversation_chunk", "body": payload,
                   "redaction_version": "synthetic-redaction.v1", "turn_start_index": index,
                   "turn_end_index": index, "part_index": 0, "char_start": 0}
            fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
            written += len(payload.encode("utf-8"))
            index += 1
    return written

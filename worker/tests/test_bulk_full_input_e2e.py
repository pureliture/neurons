"""End-to-end trial: product bulk CLI -> fake free extractor -> real PG/Neo4j.

Synthetic data only. Disposable local containers only. No API calls.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_full_input_pg_state import PgChunkState
from agent_knowledge.llm_brain_core.bulk_full_input_reader import (
    FullInputReader,
    PgAuthorityCardStore,
)
from agent_knowledge.llm_brain_core.bulk_full_input_neo4j_writer import Neo4jShadowWriter
from agent_knowledge.llm_brain_core.bulk_semantic import (
    BulkSemanticEntity,
    BulkSemanticExtractionResult,
    BulkSemanticSessionResult,
    OpenAICompatibleBulkSemanticExtractor,
)
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

pytestmark = pytest.mark.skipif(
    not (os.environ.get("TRIAL_PG_DSN") and os.environ.get("TRIAL_NEO4J_URI")),
    reason="no isolated local PG/Neo4j",
)
P = "synthetic-provider"
POLICY = "full-input.v1"
PREFIX = uuid.uuid4().hex[:8] + "-"


class _FreeExtractor:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_calls = 0
        self.model = "free-model"
        self.provider = P
        self.endpoint_class = "approved_free"
        self.price_status = "free_confirmed"

    def extract(self, batch):  # type: ignore[no-untyped-def]
        if self.fail_calls:
            self.fail_calls -= 1
            raise ValueError("synthetic interruption")
        self.calls.append([item.text for item in batch])
        return BulkSemanticExtractionResult(tuple(
            BulkSemanticSessionResult(
                session_key=item.session_key,
                entities=(BulkSemanticEntity(name=PREFIX + "Entity", type="Concept",
                                             summary="synthetic"),),
                relations=(),
            )
            for item in batch
        ))


def _seed(store, project, bodies, *, evidence=False, marker=""):
    sid = dm.build_session_id_hash(P, project + "-session")
    store.put(dm.build_transcript_session_document(
        session=TranscriptSession(session_id_hash=sid, provider=P, project=project,
                                  started_at="2026-01-01T00:00:00Z")))
    hashes = []
    for index, body in enumerate(bodies):
        chunk = TranscriptChunk.from_text(chunk_id=f"chunk_{index}", session_id_hash=sid,
                                          provider=P, project=project,
                                          turn_start_index=index, turn_end_index=index,
                                          text=body + marker)
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        hashes.append(str(doc["content_hash"]))
        store.put(doc)
    evidence_count = 0
    if evidence:
        ev = dm.build_tool_evidence_bundle_document(
            session_id_hash=sid, provider=P, project=project, part_index=0,
            part_count=1, evidence_index_start=0, evidence_index_end=1,
            record_content_hashes=[], body="synthetic evidence body",
        )
        store.put(ev)
        evidence_count = 1
    store.put(dm.build_coverage_manifest_document(
        session_id_hash=sid, provider=P, project=project,
        conversation_chunk_count=len(bodies), tool_evidence_bundle_count=evidence_count,
        conversation_content_hashes=hashes, tool_evidence_coverage_hashes=[],
        project_authority={"project": project, "ambiguous": False,
                           "eligible_for_retirement": True},
    ))
    return sid


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
    monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "free-model")
    store = InMemoryCouchDBSourceStore()
    extractor = _FreeExtractor()
    monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
    monkeypatch.setattr(
        OpenAICompatibleBulkSemanticExtractor, "from_env",
        classmethod(lambda cls, **kwargs: extractor),
    )
    args = [
        "--ledger", str(tmp_path / "ledger.db"),
        "--runtime-dir", str(tmp_path / "runtime"),
        "--full-input-policy-version", POLICY,
        "--trial-neo4j-writer",
        "--privacy-forbidden-marker", "SYNTHETIC_PRIVATE_MARKER",
        "--limit", "1",
    ]
    return dict(store=store, extractor=extractor, args=args, tmp=tmp_path)


def _run(env, capsys):
    rc = cli.main(list(env["args"]))
    captured = capsys.readouterr()
    return rc, json.loads(captured.out or captured.err)


def test_e2e_full_input_real_pg_real_neo4j_and_real_reader(env, capsys):
    project = PREFIX + "e2e"
    sid = _seed(env["store"], project, ["FRONT" + "A" * 4000, "MIDDLE" + "B" * 4000,
                                         "C" * 4000 + "END"], evidence=True)
    rc, report = _run(env, capsys)
    assert rc == 0, json.dumps(report, sort_keys=True)
    assert report["full_input_chunks"] == 4
    assert report["full_input_shadow_groups"]
    assert len(env["extractor"].calls[0]) == 4

    state = PgChunkState(os.environ["TRIAL_PG_DSN"])
    assert len(state.completed_chunks(project, POLICY)) == 4
    writer = Neo4jShadowWriter(os.environ["TRIAL_NEO4J_URI"], "neo4j",
                               os.environ["TRIAL_NEO4J_PASSWORD"])
    try:
        assert writer.count_nodes(chunk_key=f"{sid}-c0") >= 1
        assert writer.verify_receipt(chunk_key=f"{sid}-c0",
                                     expected_hash="invalid") is False
    finally:
        writer.close()

    cards = PgAuthorityCardStore(os.environ["TRIAL_PG_DSN"])
    cards.upsert_card(memory_id=PREFIX + "card", project=project,
                      content_hash="sha256:" + "e" * 64, provenance="shadow")
    from agent_knowledge.llm_brain_core.bulk_full_input import build_session_grounding_fact
    fact = build_session_grounding_fact(
        project=project, memory_id=PREFIX + "card",
        content_hash="sha256:" + "e" * 64,
        shadow_group=report["full_input_shadow_groups"][0], chunk_key="s1-c0",
    )
    result = FullInputReader(os.environ["TRIAL_PG_DSN"]).resolve(
        project, (fact,), eligible_shadow_groups=report["full_input_shadow_groups"]
    )
    assert result["status"] == "verified"
    assert len(result["cards"]) == 1


def test_e2e_interruption_recovers_without_duplicate_writes(env, capsys):
    project = PREFIX + "interrupt"
    sid = _seed(env["store"], project, ["alpha", "beta", "gamma"])
    env["extractor"].fail_calls = 4
    rc1, report1 = _run(env, capsys)
    assert rc1 == 1
    assert report1["projection"]["failed"] >= 1
    rc2, report2 = _run(env, capsys)
    assert rc2 == 0, report2
    assert report2["full_input_chunks"] == 3
    writer = Neo4jShadowWriter(os.environ["TRIAL_NEO4J_URI"], "neo4j",
                               os.environ["TRIAL_NEO4J_PASSWORD"])
    try:
        for ordinal in range(3):
            assert writer.count_nodes(chunk_key=f"{sid}-c{ordinal}") == 1
    finally:
        writer.close()


def test_e2e_source_revision_change_reprocesses(env, capsys):
    project = PREFIX + "sourcechange"
    sid = _seed(env["store"], project, ["alpha", "beta"])
    assert _run(env, capsys)[0] == 0, capsys.readouterr().out
    before = len(env["extractor"].calls)
    docs = env["store"].find_by_session(session_id_hash=sid)
    old = next(doc for doc in docs if doc.get("doc_type") == dm.SourceDocType.CONVERSATION_CHUNK)
    changed = TranscriptChunk.from_text(chunk_id="chunk_0", session_id_hash=sid,
                                        provider=P, project=project, turn_start_index=0,
                                        turn_end_index=0, text="alpha CHANGED")
    env["store"].put({**dm.build_conversation_chunk_document(chunk=changed),
                      "_rev": old.get("_rev")})
    rc, report = _run(env, capsys)
    assert rc == 0, report
    assert report["full_input_chunks"] == 2
    assert len(env["extractor"].calls) > before


def test_e2e_privacy_marker_fails_closed(env, capsys):
    project = PREFIX + "privacy"
    _seed(env["store"], project, ["alpha", "beta"], marker=" SYNTHETIC_PRIVATE_MARKER")
    rc, report = _run(env, capsys)
    assert rc == 1
    assert report["full_input_source_invalid"] >= 1
    assert env["extractor"].calls == []


def test_e2e_selector_fairness_across_projects(env, capsys):
    for index in range(3):
        _seed(env["store"], PREFIX + f"fair{index}", ["alpha"])
    env["args"][env["args"].index("--limit") + 1] = "3"
    rc, report = _run(env, capsys)
    assert rc == 0, report
    assert report["project_count"] == 3


def test_e2e_trial_mode_required_in_production(env, capsys, monkeypatch):
    monkeypatch.delenv("NEURON_TRIAL_MODE")
    _seed(env["store"], PREFIX + "guard", ["alpha"])
    rc, report = _run(env, capsys)
    assert rc == 1
    assert report["status"] == "failed"

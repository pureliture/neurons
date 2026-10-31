"""native identity가 기존 Couch source와 materialization에서 사라지지 않는지 검증한다."""
from dataclasses import replace
import json
import os
import re
import uuid

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.couchdb_source.source_revision import resolve_active_source_revision
from agent_knowledge.couchdb_source.tool_evidence_bundler import build_tool_evidence_bundle_documents, store_tool_evidence_bundles
from agent_knowledge.session_memory.observation_refs import identity, validate
from agent_knowledge.session_memory.transcript_model import ToolEvidenceSummaryRecord
from agent_knowledge.session_memory.transcript_parsers import extract_codex_tool_evidence
from test_active_source_revision_tool_evidence_bundler import _seed_unpinned_source, _session_id_hash, PROJECT


def record(**overrides):
    values = dict(session_id_hash=_session_id_hash(), provider="codex", project=PROJECT,
        category="test_result", outcome="pass", tool_name="exec_command", command_summary="uv run pytest -q",
        redacted_summary="3 passed", observed_at="2026-10-26T01:00:00Z",
        source_event_id_hash=dm.sha256_hash("native-call"), source_content_hash=dm.sha256_hash("native-output"),
        extractor_version="codex-tool-evidence-extractor.v2")
    return ToolEvidenceSummaryRecord(**{**values, **overrides})


def native_chunk(records=None):
    source = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(source)
    store_tool_evidence_bundles(records or [record()], store=source, full_session_generation=True)
    materialized = materialize_session_memory(session_id_hash=_session_id_hash(), store=source)
    return {**materialized.to_projection_document(), "metadata": {"knowledge_id": "native-test"}}


@pytest.mark.parametrize("batched", [False, True])
def test_miner_preserves_only_model_selected_native_evidence(batched):
    from agent_knowledge.session_memory.llm_brain_miner import LlmBrainEnvelopeMiner
    from agent_knowledge.session_memory.batch_miner import BatchedEnvelopeMiner
    from test_batch_miner import _decision_item
    a, b = record(), replace(record(), source_event_id_hash=dm.sha256_hash("second-call"), evidence_index=1)
    chunk = native_chunk([a, b])
    def complete(messages):
        assert a.evidence_id_hash in messages[-1]["content"]
        assert b.evidence_id_hash in messages[-1]["content"]
        item = {**_decision_item(), "evidence_id_hashes": [a.evidence_id_hash]}
        if batched:
            key = re.search(r"SOURCE_KEY: (\S+)", messages[-1]["content"])[1]
            return json.dumps([{"source_key": key, "candidate": item}])
        return json.dumps([item])
    if batched:
        cards, _ = BatchedEnvelopeMiner(completion_fn=complete).mine_batches([chunk], refresh_watermark="native-v1")
    else:
        cards = LlmBrainEnvelopeMiner(completion_fn=complete).mine_chunk(chunk, refresh_watermark="native-v1")
    assert len(cards) == 1
    ref = cards[0]["source_refs"][0]
    assert ref["source_revision_hash"] == chunk["source_hash"]
    assert [entry["evidence_id_hash"] for entry in ref["native_observation_refs"]] == [a.evidence_id_hash]


def test_batch_rejects_cross_source_or_invisible_evidence_and_corrupt_body_before_call():
    from agent_knowledge.session_memory.batch_miner import BatchedEnvelopeMiner, BatchExtractionError
    from test_batch_miner import _decision_item
    a = record()
    chunk = native_chunk([a])
    calls = []
    def complete(messages):
        calls.append(messages)
        key = re.search(r"SOURCE_KEY: (\S+)", messages[-1]["content"])[1]
        return json.dumps([{"source_key": key, "candidate": {
            **_decision_item(), "evidence_id_hashes": [dm.sha256_hash("different-source")]}}])
    miner = BatchedEnvelopeMiner(completion_fn=complete)
    with pytest.raises(BatchExtractionError, match="not shown"):
        miner.mine_batches([chunk], refresh_watermark="native-v1")
    calls.clear()
    with pytest.raises(BatchExtractionError, match="source is invalid"):
        miner.mine_batches([{**chunk, "body": chunk["body"] + "tampered"}], refresh_watermark="native-v1")
    assert calls == []


def test_native_prompt_excludes_truncated_spans_and_remains_bounded():
    from agent_knowledge.session_memory.llm_brain_miner import _bounded_chunk_prompt
    records = [replace(record(), source_event_id_hash=dm.sha256_hash(f"call-{i}"), evidence_index=i,
        redacted_summary=f"result-{i}: " + "x" * 700) for i in range(10)]
    chunk = native_chunk(records)
    text, shown = _bounded_chunk_prompt(chunk, max_chars=4000, include_ids=True)
    assert len(text) <= 4000 and records[4].evidence_id_hash not in shown
    assert records[0].evidence_id_hash in shown
    assert records[0].evidence_id_hash in text and records[4].evidence_id_hash not in text


@pytest.fixture
def actual_couch_source():
    import base64
    from urllib.parse import urlsplit
    from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
    url = os.environ.get("LBRAIN_TEST_COUCHDB_URL", "")
    if not url:
        pytest.skip("LBRAIN_TEST_COUCHDB_URL 미설정 (전용 live Couch integration gate)")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}
    auth = base64.b64encode((os.environ["COUCHDB_USER"] + ":" + os.environ["COUCHDB_PASSWORD"]).encode()).decode()
    store = CouchDBHttpSourceStore(base_url=url, db="native_obs_" + uuid.uuid4().hex,
        auth_header="Basic " + auth, request_timeout_seconds=8)
    try:
        store.ensure_database()
    except Exception:
        pytest.fail("task Couch HTTP source unavailable (credentials withheld)", pytrace=False)
    return store


def test_actual_couch_pg_intake_reuses_vector_but_fences_active_interpretation(actual_couch_source, isolated_pg_store):
    from agent_knowledge.couchdb_source.observation_catalog import CouchObservationCatalog, ObservationUnavailable
    from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector
    from agent_knowledge.session_memory.candidate_intake_cli import load_chunks_from_pg
    from agent_knowledge.session_memory.batch_miner import _batch_source_key
    from test_pg_backfill import CountingEmbedProvider
    source = actual_couch_source
    _seed_unpinned_source(source)
    a = record()
    store_tool_evidence_bundles([a], store=source, full_session_generation=True)
    original = resolve_active_source_revision(store=source, session_id_hash=a.session_id_hash)
    embedder = CountingEmbedProvider()
    projector = PgSessionMemoryProjector(isolated_pg_store, embedder)
    materialized = materialize_session_memory(session_id_hash=a.session_id_hash, store=source)
    chunk_id = projector.project(target_profile=materialized.target_profile, document=materialized.to_projection_document())
    catalog = CouchObservationCatalog(source, extractor_versions={"codex": a.extractor_version})
    [first] = load_chunks_from_pg(isolated_pg_store.dsn, project=PROJECT, limit=20, observation_catalog=catalog)
    assert first["source_hash"] == original.source_hash
    old_key = _batch_source_key(first, scope="project")
    b = replace(a, extractor_version="codex-tool-evidence-extractor.v3")
    store_tool_evidence_bundles([b], store=source, full_session_generation=True, expected_predecessor=original)
    latest = materialize_session_memory(session_id_hash=a.session_id_hash, store=source)
    assert latest.body == materialized.body and latest.source_hash != materialized.source_hash
    updated = CouchObservationCatalog(source, extractor_versions={"codex": b.extractor_version})
    with pytest.raises(ObservationUnavailable, match="projection_source_missing"):
        load_chunks_from_pg(isolated_pg_store.dsn, project=PROJECT, limit=20, observation_catalog=updated)
    assert projector.project(target_profile=latest.target_profile, document=latest.to_projection_document()) == chunk_id
    with pytest.raises(ObservationUnavailable, match="extractor_policy_mismatch"):
        load_chunks_from_pg(isolated_pg_store.dsn, project=PROJECT, limit=20, observation_catalog=catalog)
    [second] = load_chunks_from_pg(isolated_pg_store.dsn, project=PROJECT, limit=20, observation_catalog=updated)
    assert second["content"] == first["content"] and _batch_source_key(second, scope="project") != old_key
    assert second["observation_refs"][0]["observation_id_hash"] == a.observation_id_hash
    assert second["observation_refs"][0]["evidence_id_hash"] == b.evidence_id_hash
    assert embedder.calls == 1
    assert source.get(original.tool_evidence_bundles[0]["_id"])["observation_refs"][0]["evidence_id_hash"] == a.evidence_id_hash


def test_actual_codex_parser_keeps_native_anchor_in_bundle(tmp_path):
    path = tmp_path / "synthetic-session.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": "provenance-fixture"}},
        {"type": "response_item", "timestamp": "2026-10-26T01:00:00Z", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "native-one",
            "arguments": json.dumps({"cmd": "uv run pytest -q"})}},
        {"type": "response_item", "timestamp": "2026-10-26T01:00:01Z", "payload": {
            "type": "function_call_output", "call_id": "native-one", "output": "3 passed in 0.1s"}}]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    parsed = extract_codex_tool_evidence(path, project=PROJECT, source_locator_hash=dm.sha256_hash("synthetic-locator"))
    assert len(parsed) == 1 and parsed[0].observation_id_hash
    [bundle] = build_tool_evidence_bundle_documents(parsed)
    [ref] = bundle["observation_refs"]
    assert ref["observation_id_hash"] == parsed[0].observation_id_hash
    assert ref["evidence_id_hash"] == parsed[0].evidence_id_hash
    assert bundle["body"][ref["span_start"]:ref["span_end"]] == parsed[0].redacted_summary.rstrip()
    assert "native-one" not in str(bundle) and str(path) not in str(bundle)


def test_rechunking_preserves_observation_and_interpretation_identity():
    a = record()
    b = replace(a, evidence_index=1, source_event_id_hash=dm.sha256_hash("independent-native-call"))
    whole = build_tool_evidence_bundle_documents([a, b])[0]
    split = build_tool_evidence_bundle_documents([a, b], max_chars=140)
    assert len(split) > 1
    assert [identity(ref) for part in split for ref in part["observation_refs"]] == [identity(ref) for ref in whole["observation_refs"]]
    assert a.observation_id_hash != b.observation_id_hash


def test_same_rendered_body_with_new_extractor_keeps_distinct_source_revision():
    a = record()
    b = replace(a, extractor_version="codex-tool-evidence-extractor.v3")
    first, second = [build_tool_evidence_bundle_documents([item])[0] for item in (a, b)]
    assert first["body"] == second["body"] and first["content_hash"] == second["content_hash"]
    assert first["coverage_hash"] != second["coverage_hash"]
    assert first["observation_refs"][0]["observation_id_hash"] == second["observation_refs"][0]["observation_id_hash"]
    assert first["observation_refs"][0]["evidence_id_hash"] != second["observation_refs"][0]["evidence_id_hash"]
    store = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(store)
    store_tool_evidence_bundles([a], store=store, full_session_generation=True)
    before = resolve_active_source_revision(store=store, session_id_hash=a.session_id_hash)
    store_tool_evidence_bundles([b], store=store, full_session_generation=True, expected_predecessor=before)
    after = resolve_active_source_revision(store=store, session_id_hash=a.session_id_hash)
    assert before.source_hash != after.source_hash
    assert after.tool_evidence_bundles[0]["observation_refs"][0]["extractor_version"].endswith("v3")
    assert store.get(before.tool_evidence_bundles[0]["_id"])["observation_refs"][0]["extractor_version"].endswith("v2")


def test_active_source_materialization_keeps_grounded_observation_spans():
    store = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(store)
    a = record()
    store_tool_evidence_bundles([a], store=store, full_session_generation=True)
    materialized = materialize_session_memory(session_id_hash=a.session_id_hash, store=store)
    assert materialized.fully_materialized
    [ref] = validate(materialized.observation_refs, body=materialized.body, session_id_hash=a.session_id_hash,
        provider=a.provider, project=a.project)
    assert materialized.body[ref["span_start"]:ref["span_end"]] == a.redacted_summary
    assert materialized.to_projection_document()["observation_refs"] == [ref]


@pytest.mark.parametrize("field,value", [("project", "other-project"), ("span_start", 0),
    ("span_content_hash", "sha256:" + "0" * 64), ("observation_id_hash", "sha256:" + "1" * 64)])
def test_corrupt_or_cross_scope_reference_is_not_materialized(field, value):
    [bundle] = build_tool_evidence_bundle_documents([record()])
    ref = {**bundle["observation_refs"][0], field: value}
    with pytest.raises(ValueError):
        validate([ref], body=bundle["body"], session_id_hash=bundle["session_id_hash"],
            project=bundle["project"], provider=bundle["provider"])


def test_actual_pg_projection_reuse_retains_each_source_and_native_version(isolated_pg_store):
    from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector
    from test_pg_backfill import SyntheticEmbedProvider
    class CountingEmbedder(SyntheticEmbedProvider):
        calls = 0
        def embed(self, text):
            self.calls += 1
            return super().embed(text)
    source = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(source)
    first = record()
    store_tool_evidence_bundles([first], store=source, full_session_generation=True)
    materialized = materialize_session_memory(session_id_hash=first.session_id_hash, store=source)
    embedder = CountingEmbedder()
    projector = PgSessionMemoryProjector(isolated_pg_store, embedder)
    chunk_id = projector.project(target_profile=materialized.target_profile, document=materialized.to_projection_document())
    previous = resolve_active_source_revision(store=source, session_id_hash=first.session_id_hash)
    store_tool_evidence_bundles([replace(first, extractor_version="codex-tool-evidence-extractor.v3")],
        store=source, full_session_generation=True, expected_predecessor=previous)
    newer = materialize_session_memory(session_id_hash=first.session_id_hash, store=source)
    assert materialized.body == newer.body and materialized.source_hash != newer.source_hash
    reused = projector.project(target_profile=newer.target_profile, document=newer.to_projection_document())
    assert reused == chunk_id and embedder.calls == 1
    refs = isolated_pg_store.get_chunk_source_provenance(project=PROJECT, chunk_ids=[chunk_id])
    assert len(refs) == 2
    assert {ref["source_hash"] for ref in refs} == {materialized.source_hash, newer.source_hash}
    assert {ref["observation_refs"][0]["observation_id_hash"] for ref in refs} == {first.observation_id_hash}
    assert {ref["observation_refs"][0]["extractor_version"] for ref in refs} == {
        "codex-tool-evidence-extractor.v2", "codex-tool-evidence-extractor.v3"}
    assert isolated_pg_store.get_chunk_source_provenance(project="other-project", chunk_ids=[chunk_id]) == []


def test_actual_pg_chunk_and_provenance_roll_back_together(isolated_pg_store, monkeypatch):
    from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector
    from test_pg_backfill import SyntheticEmbedProvider
    source = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(source)
    first = record()
    store_tool_evidence_bundles([first], store=source, full_session_generation=True)
    materialized = materialize_session_memory(session_id_hash=first.session_id_hash, store=source)
    original = isolated_pg_store.put_chunk_source_provenance
    def interrupted(**kwargs):
        original(**kwargs)
        raise RuntimeError("injected failure after provenance insert")
    monkeypatch.setattr(isolated_pg_store, "put_chunk_source_provenance", interrupted)
    with pytest.raises(RuntimeError, match="injected failure"):
        PgSessionMemoryProjector(isolated_pg_store, SyntheticEmbedProvider()).project(
            target_profile=materialized.target_profile, document=materialized.to_projection_document())
    with isolated_pg_store._scope() as db:
        assert db.execute("SELECT count(*) AS n FROM session_memory_chunks").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM session_memory_chunk_sources").fetchone()["n"] == 0


def test_native_catalog_admission_is_bound_to_active_revision_and_extractor_policy():
    from agent_knowledge.couchdb_source.observation_catalog import CouchObservationCatalog, ObservationUnavailable
    source = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(source)
    a = record()
    store_tool_evidence_bundles([a], store=source, full_session_generation=True)
    current = resolve_active_source_revision(store=source, session_id_hash=a.session_id_hash)
    catalog = CouchObservationCatalog(source, extractor_versions={"codex": a.extractor_version})
    args = dict(project=PROJECT, provider="codex", session_id_hash=a.session_id_hash,
        source_hash=current.source_hash, evidence_id_hashes=[a.evidence_id_hash])
    admitted = catalog.read_many(**args)[a.evidence_id_hash]
    assert admitted.redacted_text == a.redacted_summary
    catalog.assert_current(admitted)
    modified = admitted.reference
    modified["observation_id_hash"] = dm.sha256_hash("forged")
    assert admitted.reference["observation_id_hash"] == a.observation_id_hash
    with pytest.raises(ObservationUnavailable, match="scope_mismatch"):
        catalog.read_many(**{**args, "project": "other-project"})
    b = replace(a, extractor_version="codex-tool-evidence-extractor.v3")
    store_tool_evidence_bundles([b], store=source, full_session_generation=True, expected_predecessor=current)
    with pytest.raises(ObservationUnavailable, match="revision_conflict"):
        catalog.assert_current(admitted)
    newer = resolve_active_source_revision(store=source, session_id_hash=a.session_id_hash)
    updated_args = {**args, "source_hash": newer.source_hash, "evidence_id_hashes": [b.evidence_id_hash]}
    with pytest.raises(ObservationUnavailable, match="extractor_policy_mismatch"):
        catalog.read_many(**updated_args)
    current_catalog = CouchObservationCatalog(source, extractor_versions={"codex": b.extractor_version})
    reread = current_catalog.read_many(**updated_args)[b.evidence_id_hash]
    assert reread.reference["observation_id_hash"] == a.observation_id_hash
    assert reread.reference["evidence_id_hash"] != admitted.reference["evidence_id_hash"]


def test_native_catalog_does_not_certify_legacy_unpinned_source():
    from agent_knowledge.couchdb_source.observation_catalog import CouchObservationCatalog, ObservationUnavailable
    source = InMemoryCouchDBSourceStore()
    _seed_unpinned_source(source)
    a = record()
    store_tool_evidence_bundles([a], store=source)
    current = resolve_active_source_revision(store=source, session_id_hash=a.session_id_hash)
    assert current.is_legacy_unpinned
    with pytest.raises(ObservationUnavailable, match="source_unpinned"):
        CouchObservationCatalog(source, extractor_versions={"codex": a.extractor_version}).read_many(
            project=PROJECT, provider="codex", session_id_hash=a.session_id_hash, source_hash=current.source_hash,
            evidence_id_hashes=[a.evidence_id_hash])

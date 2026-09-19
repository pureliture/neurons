"""CLI -> materializer -> disposable SQL -> brain.query; synthetic I/O boundaries only."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from agent_knowledge.couchdb_source import build_cli, document_model as dm
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.knowledge_search_service import KnowledgeSearchService
from agent_knowledge.ledger import Ledger
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
from agent_knowledge.rag_ingress.pg_recall import build_pg_brain_query_search_from_env
from test_couchdb_build_cli import _build_synthetic_session, _mark_projected, _write_approval
from test_pg_backfill import SyntheticEmbedProvider
from agent_knowledge.rag_ingress.pg_backfill import _derive_pg_chunk_id_impl, PgSessionMemoryProjector


PG_DSN = os.environ.get("LBRAIN_TEST_PG_DSN", "")


@pytest.fixture
def lane(monkeypatch, tmp_path, capsys):
    if not PG_DSN:
        pytest.skip("LBRAIN_TEST_PG_DSN 미설정 (전용 live PostgreSQL integration gate)")
    dsn = PG_DSN
    sql = PgVectorStore(dsn=dsn)
    sql.execute_ddl()
    couch = InMemoryCouchDBSourceStore()
    project = "pg-e2e-" + uuid.uuid4().hex
    sid = _build_synthetic_session(couch, provider="codex", project=project, raw_id=project)
    _mark_projected(couch, sid, "codex", project)
    legacy = couch.get(dm.projection_state_doc_id(sid))
    embeds = []

    def embedding_factory(*, environ):
        provider = SyntheticEmbedProvider()
        embeds.append(provider)
        return provider

    monkeypatch.setattr("agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore", lambda **kw: couch)
    monkeypatch.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", embedding_factory)
    monkeypatch.setenv("SESSION_MEMORY_PROJECTION_BACKEND", "postgres_pgvector")
    monkeypatch.setenv("NEURON_LBRAIN_PGVECTOR_DSN", dsn)
    monkeypatch.setenv("COUCHDB_URL", "http://synthetic.invalid")
    argv = ["--project", project, "--approval", str(tmp_path / "approval.json")]
    _write_approval(tmp_path, argv=argv)

    def run():
        rc = build_cli.main(argv)
        return rc, json.loads(capsys.readouterr().out)

    import argparse
    from agent_knowledge import cli
    ledger_path = str(tmp_path / "ledger.sqlite")
    Ledger(ledger_path)
    parser = argparse.ArgumentParser()
    cli._add_recall_service_arguments(parser)
    args = parser.parse_args(["--ledger", ledger_path])
    def query():
        service = cli._build_recall_service(args)
        assert isinstance(service, KnowledgeSearchService)
        return service.brain_query(brain_id="/project/" + project, query="session context")

    yield dict(sql=sql, couch=couch, sid=sid, project=project, run=run, query=query, legacy=legacy, embeds=embeds)
    with sql.transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM embedding_outbox WHERE target_id IN (SELECT chunk_id FROM session_memory_chunks WHERE project = %s)", (project,))
            cur.execute("DELETE FROM session_memory_chunks WHERE project = %s", (project,))


def test_cli_sql_archive_preserves_legacy_receipt_and_is_idempotent(lane):
    rc, report = lane["run"]()
    assert rc == 0 and report["projected"] == 1
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    for key in ("session_memory_knowledge_id", "active_content_hash", "projected_source_hash", "projection_status"):
        assert state[key] == lane["legacy"][key]
    receipt = state["backend_receipts"]["postgres_pgvector"]
    assert receipt["projection_status"] == "projected"
    assert receipt["projected_source_hash"] != receipt["active_content_hash"]
    stored = lane["sql"].get_chunk(receipt["session_memory_knowledge_id"])
    assert stored.embedding_state == "ready"
    assert stored.content_hash == receipt["active_content_hash"]
    response = lane["query"]()
    assert [hit["memory_id"] for hit in response["archive"]] == [stored.chunk_id]
    rc, report = lane["run"]()
    assert rc == 0 and report["selected"] == 0




@pytest.mark.parametrize("model,size", [("other-model", 3072), ("gemini-embedding-2", 768)])
def test_projector_rejects_unvalidated_profile_before_sql(lane, model, size):
    with pytest.raises(ValueError, match="unsupported PG embedding profile"):
        PgSessionMemoryProjector(store=lane["sql"], embed_provider=SyntheticEmbedProvider(model=model, size=size))


@pytest.mark.parametrize("field,value", [("provider", "wrong"), ("content_markdown", "tampered"), ("embedding_model", "wrong")])
def test_projector_refuses_corrupt_existing_row(lane, field, value):
    from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
    assert lane["run"]()[0] == 0
    doc = materialize_session_memory(session_id_hash=lane["sid"], store=lane["couch"]).to_projection_document()
    receipt = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))["backend_receipts"]["postgres_pgvector"]
    row = lane["sql"].get_chunk(receipt["session_memory_knowledge_id"])
    setattr(row, field, value)
    lane["sql"].insert_chunk(row)
    projector = PgSessionMemoryProjector(store=lane["sql"], embed_provider=SyntheticEmbedProvider())
    with pytest.raises(ValueError, match="PG chunk identity mismatch"):
        projector.project(target_profile="session-memory", document=doc)
    assert getattr(lane["sql"].get_chunk(row.chunk_id), field) == value


@pytest.mark.parametrize("field,value", [("embedding_state", "failed"), ("embedding_model", "wrong"), ("content_markdown", "tampered"), ("provider", "wrong"), ("project", "wrong")])
def test_recall_rejects_nonready_or_mislabeled_sql_rows(lane, field, value):
    assert lane["run"]()[0] == 0
    receipt = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))["backend_receipts"]["postgres_pgvector"]
    row = lane["sql"].get_chunk(receipt["session_memory_knowledge_id"])
    original = getattr(row, field)
    try:
        setattr(row, field, value)
        lane["sql"].insert_chunk(row)
        assert lane["query"]()["archive"] == []
    finally:
        setattr(row, field, original)
        lane["sql"].insert_chunk(row)


def _receipt(lane):
    return (lane["couch"].get(dm.projection_state_doc_id(lane["sid"])).get("backend_receipts") or {}).get("postgres_pgvector", {})


def _change_source(lane):
    docs = lane["couch"].find_by_session(session_id_hash=lane["sid"])
    chunk = next(doc for doc in docs if doc["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
    chunk["body"] += " revised"
    chunk["content_hash"] = dm.sha256_hash(chunk["body"])
    lane["couch"].put(chunk)


def test_pg_commit_couch_crash_retry_reuses_ready_vector(lane, monkeypatch):
    original = lane["couch"].put_if_revision
    def crash(doc, **kwargs):
        if "backend_receipts" in doc:
            raise RuntimeError("synthetic CouchDB crash")
        return original(doc, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(lane["couch"], "put_if_revision", crash)
        assert lane["run"]()[0] == 1
    assert not _receipt(lane)
    assert lane["query"]()["archive"] == []
    rows = lane["sql"].search_session_chunks(SyntheticEmbedProvider().embed("x"), project=lane["project"])
    assert len(rows) == 1
    before = lane["sql"].get_chunk(rows[0]["chunk_id"])
    def no_embed(self, text):
        raise AssertionError("ready retry must not re-embed")
    with monkeypatch.context() as m:
        m.setattr(SyntheticEmbedProvider, "embed", no_embed)
        assert lane["run"]()[0] == 0
    after = lane["sql"].get_chunk(before.chunk_id)
    assert after.embedding == before.embedding and after.updated_at == before.updated_at
    assert len(lane["query"]()["archive"]) == 1


@pytest.mark.parametrize("failure", ["embedding", "pg"])
def test_failure_has_no_ready_receipt_then_retry(lane, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure")
    with monkeypatch.context() as m:
        if failure == "embedding":
            m.setattr(SyntheticEmbedProvider, "embed", fail)
        from psycopg import sql
        constraint = sql.Identifier("test_failure_" + uuid.uuid4().hex)
        if failure == "pg":
            with lane["sql"].transaction() as conn:
                conn.execute(sql.SQL("ALTER TABLE session_memory_chunks ADD CONSTRAINT {} CHECK (project <> {}) NOT VALID").format(constraint, sql.Literal(lane["project"])))
        try:
            assert lane["run"]()[0] == 1
        finally:
            if failure == "pg":
                with lane["sql"].transaction() as conn:
                    conn.execute(sql.SQL("ALTER TABLE session_memory_chunks DROP CONSTRAINT {}").format(constraint))
    assert _receipt(lane)["projection_status"] == "failed"
    assert _receipt(lane)["session_memory_knowledge_id"] == ""
    assert lane["query"]()["archive"] == []
    assert lane["run"]()[0] == 0
    assert len(lane["query"]()["archive"]) == 1


def test_source_change_drops_old_hit_and_rebuilds(lane):
    assert lane["run"]()[0] == 0
    old = _receipt(lane)["session_memory_knowledge_id"]
    _change_source(lane)
    assert lane["query"]()["archive"] == []
    assert lane["run"]()[0] == 0
    new = _receipt(lane)["session_memory_knowledge_id"]
    assert new != old
    assert [h["memory_id"] for h in lane["query"]()["archive"]] == [new]
    assert lane["sql"].get_chunk(old).embedding_state == "ready"


def test_source_change_during_receipt_commit_fails_closed(lane, monkeypatch):
    original = lane["couch"].put_if_revision
    def race(doc, **kwargs):
        result = original(doc, **kwargs)
        if "backend_receipts" in doc:
            _change_source(lane)
        return result
    with monkeypatch.context() as m:
        m.setattr(lane["couch"], "put_if_revision", race)
        assert lane["run"]()[0] == 1
    assert lane["query"]()["archive"] == []
    assert lane["run"]()[0] == 0
    assert len(lane["query"]()["archive"]) == 1


def test_configured_projection_factory_error_is_redacted(lane, monkeypatch):
    def fail(*, environ):
        raise ValueError("private-secret-must-not-escape")
    monkeypatch.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", fail)
    rc, report = lane["run"]()
    assert rc == 2
    assert report["error"] == "pg_projector_unavailable"
    assert "private-secret" not in json.dumps(report)
    assert not _receipt(lane)


def test_recall_factory_rejects_wrong_profile(lane, monkeypatch):
    monkeypatch.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: SyntheticEmbedProvider(model="wrong"))
    with pytest.raises(RuntimeError, match="^PG recall construction failed$"):
        build_pg_brain_query_search_from_env(os.environ)


def test_qdrant_receipt_alone_never_authorizes_pg_hit(lane):
    assert lane["run"]()[0] == 0
    state = lane["couch"].get(dm.projection_state_doc_id(lane["sid"]))
    receipt = state.pop("backend_receipts")["postgres_pgvector"]
    state.update(receipt)
    lane["couch"].put(state)
    assert lane["query"]()["archive"] == []
    assert lane["run"]()[1]["selected"] == 1
    assert len(lane["query"]()["archive"]) == 1


def test_canary_is_excluded_after_authority_join(lane):
    sid = _build_synthetic_session(lane["couch"], provider="lbrain-temporal-canary", project=lane["project"], raw_id="canary")
    assert lane["run"]()[1]["projected"] == 2
    canary = lane["couch"].get(dm.projection_state_doc_id(sid))["backend_receipts"]["postgres_pgvector"]["session_memory_knowledge_id"]
    assert lane["sql"].get_chunk(canary).embedding_state == "ready"
    assert [hit["memory_id"] for hit in lane["query"]()["archive"]] == [_receipt(lane)["session_memory_knowledge_id"]]


def test_short_embedding_has_no_receipt(lane, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(SyntheticEmbedProvider, "embed", lambda self, text: [1.0])
        assert lane["run"]()[0] == 1
    assert _receipt(lane)["projection_status"] == "failed"
    assert lane["query"]()["archive"] == []
    assert lane["run"]()[0] == 0
    assert lane["embeds"][0]._closed


def test_source_race_during_embedding_cannot_commit_stale_receipt(lane, monkeypatch):
    original = SyntheticEmbedProvider.embed
    def race(self, text):
        _change_source(lane)
        return original(self, text)
    with monkeypatch.context() as m:
        m.setattr(SyntheticEmbedProvider, "embed", race)
        assert lane["run"]()[0] == 1
    assert not _receipt(lane)
    assert lane["query"]()["archive"] == []
    assert lane["run"]()[0] == 0
    assert len(lane["query"]()["archive"]) == 1


def test_concurrent_retry_preserves_first_committed_ready_vector(lane):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
    doc = materialize_session_memory(session_id_hash=lane["sid"], store=lane["couch"]).to_projection_document()
    started, release = Event(), Event()
    class SlowEmbed(SyntheticEmbedProvider):
        def embed(self, text):
            started.set()
            assert release.wait(10)
            return [0.25] * self.size
    slow = PgSessionMemoryProjector(store=lane["sql"], embed_provider=SlowEmbed())
    fast = PgSessionMemoryProjector(store=lane["sql"], embed_provider=SyntheticEmbedProvider())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(slow.project, target_profile="session-memory", document=doc)
        try:
            assert started.wait(10)
            chunk_id = fast.project(target_profile="session-memory", document=doc)
            before = lane["sql"].get_chunk(chunk_id)
        finally:
            release.set()
        assert future.result(timeout=10) == chunk_id
    after = lane["sql"].get_chunk(chunk_id)
    assert after.embedding == before.embedding
    assert after.updated_at == before.updated_at


def test_projector_rejects_forged_body_hash_before_sql(lane):
    from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
    doc = materialize_session_memory(session_id_hash=lane["sid"], store=lane["couch"]).to_projection_document()
    doc["body"] += " tampered"
    projector = PgSessionMemoryProjector(store=lane["sql"], embed_provider=SyntheticEmbedProvider())
    with pytest.raises(ValueError, match="PG content hash mismatch"):
        projector.project(target_profile="session-memory", document=doc)
    assert lane["sql"].search_session_chunks(SyntheticEmbedProvider().embed("x"), project=lane["project"]) == []


def test_deleted_source_revokes_archive(lane):
    assert lane["run"]()[0] == 0
    for doc in lane["couch"].find_by_session(session_id_hash=lane["sid"]):
        if doc["doc_type"] in (dm.SourceDocType.TRANSCRIPT_SESSION, dm.SourceDocType.CONVERSATION_CHUNK):
            lane["couch"].delete(doc["_id"])
    assert lane["query"]()["archive"] == []


def test_recall_embedding_failure_does_not_fallback(lane, monkeypatch):
    assert lane["run"]()[0] == 0
    def fail(self, text):
        raise RuntimeError("synthetic embedding outage")
    monkeypatch.setattr(SyntheticEmbedProvider, "embed", fail)
    assert lane["query"]()["archive"] == []


def test_legacy_projection_update_preserves_pg_receipt(lane):
    from agent_knowledge.couchdb_source.session_memory_materializer import materialize_and_project, RecordingSessionMemoryProjector
    assert lane["run"]()[0] == 0
    previous = dict(_receipt(lane))
    _change_source(lane)
    result = materialize_and_project(session_id_hash=lane["sid"], store=lane["couch"], projector=RecordingSessionMemoryProjector(), backend="qdrant")
    assert result["projection"]["status"] == "projected"
    assert _receipt(lane) == previous
    assert lane["query"]()["archive"] == []
    assert lane["run"]()[0] == 0
    assert len(lane["query"]()["archive"]) == 1


def test_scoped_id_has_no_delimiter_alias():
    common = dict(session_id_hash="sid", source_hash="source", content_hash="body")
    assert _derive_pg_chunk_id_impl(project="a|b", provider="c", **common) != _derive_pg_chunk_id_impl(project="a", provider="b|c", **common)


# C2: real SQL ranking, only embedding and CouchDB I/O are synthetic.
@pytest.fixture
def c2_lane(isolated_pg_store, monkeypatch, tmp_path):
    import argparse
    from agent_knowledge import cli
    from agent_knowledge.couchdb_source.session_memory_materializer import materialize_and_project

    couch = InMemoryCouchDBSourceStore()
    project = "c2-recall"
    vector = [1.0, 0.0] + [0.0] * 3070

    class AxisEmbedding(SyntheticEmbedProvider):
        def embed(self, text):
            return list(vector)

    monkeypatch.setattr("agent_knowledge.couchdb_source.couchdb_http_store.CouchDBHttpSourceStore", lambda **kw: couch)
    monkeypatch.setattr("agent_knowledge.rag_ingress.qdrant_embedding.build_openai_embedding_provider", lambda **kw: AxisEmbedding())
    monkeypatch.setenv("NEURON_LBRAIN_PGVECTOR_DSN", isolated_pg_store.dsn)
    monkeypatch.setenv("COUCHDB_URL", "http://synthetic.invalid")
    ledger_path = str(tmp_path / "c2-ledger.sqlite")
    Ledger(ledger_path)
    parser = argparse.ArgumentParser()
    cli._add_recall_service_arguments(parser)
    args = parser.parse_args(["--ledger", ledger_path])

    def seed(raw_id="current", provider="codex", target_project=project):
        sid = _build_synthetic_session(couch, provider=provider, project=target_project, raw_id=raw_id)
        result = materialize_and_project(
            session_id_hash=sid, store=couch, backend="postgres_pgvector",
            projector=PgSessionMemoryProjector(store=isolated_pg_store, embed_provider=AxisEmbedding()),
        )
        assert result["projection"]["status"] == "projected"
        state = couch.get(dm.projection_state_doc_id(sid))
        assert state is not None
        receipt = state["backend_receipts"]["postgres_pgvector"]
        row = isolated_pg_store.get_chunk(receipt["session_memory_knowledge_id"])
        assert row is not None
        return row

    def query():
        return cli._build_recall_service(args).brain_query(brain_id="/project/" + project, query="session context")

    return dict(sql=isolated_pg_store, couch=couch, project=project, seed=seed, query=query, vector=vector, embed_class=AxisEmbedding)


def _c2_stale_rows(lane, current, count):
    from dataclasses import replace
    # Old derived IDs/bodies cannot match the current backend receipt.
    with lane["sql"].transaction() as conn:
        for index in range(count):
            body = f"synthetic stale revision {index}"
            lane["sql"].insert_chunk(replace(
                current, chunk_id=f"000-stale-{index:04d}",
                content_markdown=body, content_hash=dm.sha256_hash(body),
                embedding=list(lane["vector"]),
            ), conn=conn)


def test_c2_public_archive_survives_six_higher_ranked_stale_rows(c2_lane):
    current = c2_lane["seed"]()
    current.embedding = [0.0, 1.0] + [0.0] * 3070
    c2_lane["sql"].insert_chunk(current)
    _c2_stale_rows(c2_lane, current, 6)
    top = c2_lane["sql"].search_session_chunks(c2_lane["vector"], project=c2_lane["project"])
    assert len(top) == 5
    assert all(row["chunk_id"].startswith("000-stale-") and row["distance"] == 0 for row in top)
    response = c2_lane["query"]()
    assert [hit["memory_id"] for hit in response["archive"]] == [current.chunk_id]
    assert response["projection_state"]["status"] == "fresh"
    assert response["archive"][0]["retrieval_lane"] == "pg_semantic"
    assert response["archive"][0]["summary"] == current.content_markdown


@pytest.mark.parametrize("valid_count,stale_count,expected_status", [
    (1, 99, "fresh"),       # exactly 100 rows, fully exhausted, one valid
    (1, 100, "unavailable"),  # the valid row lies beyond the budget
    (2, 99, "unavailable"),   # partial valid results cannot hide saturation
    (7, 0, "fresh"),        # preserve the public top-five contract
    (5, 95, "fresh"),       # five authorized rows at the budget boundary
])
def test_c2_authority_budget_and_public_top_five(c2_lane, valid_count, stale_count, expected_status):
    rows = [c2_lane["seed"](raw_id=f"valid-{i}") for i in range(valid_count)]
    _c2_stale_rows(c2_lane, rows[0], stale_count)
    response = c2_lane["query"]()
    assert response["projection_state"]["status"] == expected_status
    if expected_status == "unavailable":
        assert response["archive"] == []
        search = build_pg_brain_query_search_from_env(os.environ)
        assert search is not None
        with pytest.raises(RuntimeError, match="^PG recall candidate limit exhausted$"):
            search("session context", "/project/" + c2_lane["project"])
    else:
        expected = sorted(row.chunk_id for row in rows)[:5]
        assert [hit["memory_id"] for hit in response["archive"]] == expected


@pytest.mark.parametrize("brain_id", ["", "/global", "/project/", "c2-recall", None])
def test_c2_invalid_internal_scope_fails_closed(c2_lane, brain_id):
    c2_lane["seed"]()
    search = build_pg_brain_query_search_from_env(os.environ)
    assert search is not None
    with pytest.raises(RuntimeError, match="^PG recall requires project scope$"):
        search("session context", brain_id)


@pytest.mark.parametrize("count,expected_status", [(6, "fresh"), (100, "fresh"), (101, "unavailable"), (150, "unavailable")])
def test_c2_all_stale_bounded_termination(c2_lane, count, expected_status):
    import sys

    current = c2_lane["seed"]()
    _c2_stale_rows(c2_lane, current, count)
    with c2_lane["sql"].transaction() as conn:
        conn.execute("DELETE FROM session_memory_chunks WHERE chunk_id = %s", (current.chunk_id,))
    observed = {"search": [], "get": 0}
    search_code = PgVectorStore.search_session_chunks.__code__
    get_code = PgVectorStore.get_chunk.__code__

    def trace(frame, event, arg):
        if frame.f_code is search_code and event == "return":
            observed["search"].append((frame.f_locals["limit"], len(arg)))
        if frame.f_code is get_code and event == "call":
            observed["get"] += 1

    # Observe real calls, without replacing the SQL adapter or its connection.
    previous = sys.getprofile()
    try:
        sys.setprofile(trace)
        response = c2_lane["query"]()
    finally:
        sys.setprofile(previous)
    assert response["archive"] == []
    assert response["projection_state"]["status"] == expected_status
    assert observed == {"search": [(101, min(count, 101))], "get": min(count, 100)}


def test_c2_equal_distance_order_is_stable_after_stale_saturation(c2_lane):
    rows = [c2_lane["seed"](raw_id=f"tie-{i}") for i in range(7)]
    _c2_stale_rows(c2_lane, rows[0], 8)
    # Reverse the heap insertion order so the expected sort is not incidental.
    with c2_lane["sql"].transaction() as conn:
        for row in sorted(rows, key=lambda row: row.chunk_id, reverse=True):
            conn.execute("DELETE FROM session_memory_chunks WHERE chunk_id = %s", (row.chunk_id,))
            c2_lane["sql"].insert_chunk(row, conn=conn)
    expected = sorted(row.chunk_id for row in rows)[:5]
    for _ in range(3):
        assert [h["memory_id"] for h in c2_lane["query"]()["archive"]] == expected


def test_c2_unknown_project_does_not_search_other_projects(c2_lane):
    c2_lane["seed"](target_project="other-project")
    response = c2_lane["query"]()
    assert response["archive"] == []
    assert response["projection_state"]["status"] == "fresh"
    search = build_pg_brain_query_search_from_env(os.environ)
    assert search is not None
    assert search("session context", "/project/unknown") == []


@pytest.mark.parametrize("failure", ["couchdb", "sql", "embedding"])
def test_c2_errors_are_unavailable_not_empty_success(c2_lane, monkeypatch, failure):
    from psycopg.errors import UndefinedColumn

    c2_lane["seed"]()
    search = build_pg_brain_query_search_from_env(os.environ)
    assert search is not None

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-private-error")

    if failure == "couchdb":
        monkeypatch.setattr(c2_lane["couch"], "get", fail)
    elif failure == "embedding":
        monkeypatch.setattr(c2_lane["embed_class"], "embed", fail)
    else:
        with c2_lane["sql"].transaction() as conn:
            conn.execute("ALTER TABLE session_memory_chunks RENAME COLUMN embedding TO c2_unavailable_embedding")
    try:
        with pytest.raises(UndefinedColumn if failure == "sql" else RuntimeError):
            search("session context", "/project/" + c2_lane["project"])
        response = c2_lane["query"]()
        assert response["archive"] == []
        assert response["projection_state"]["status"] == "unavailable"
        assert "synthetic-private-error" not in json.dumps(response)
        assert "c2_unavailable_embedding" not in json.dumps(response)
    finally:
        if failure == "sql":
            with c2_lane["sql"].transaction() as conn:
                conn.execute("ALTER TABLE session_memory_chunks RENAME COLUMN c2_unavailable_embedding TO embedding")


@pytest.mark.parametrize("rejection", ["canary", "source", "body", "profile", "ready", "scope", "receipt"])
def test_c2_expanded_candidates_preserve_authority_guards(c2_lane, rejection):
    current = c2_lane["seed"]()
    current.embedding = [0.0, 1.0] + [0.0] * 3070
    c2_lane["sql"].insert_chunk(current)
    for index in range(6):
        row = c2_lane["seed"](raw_id=f"rejected-{index}", provider="lbrain-temporal-canary" if rejection == "canary" else "codex")
        if rejection == "source":
            _change_source(dict(couch=c2_lane["couch"], sid=row.session_id_hash))
        elif rejection in {"body", "profile", "ready"}:
            field, value = {"body": ("content_markdown", "tampered"), "profile": ("embedding_model", "wrong"), "ready": ("embedding_state", "failed")}[rejection]
            setattr(row, field, value)
            c2_lane["sql"].insert_chunk(row)
        elif rejection in {"scope", "receipt"}:
            state = c2_lane["couch"].get(dm.projection_state_doc_id(row.session_id_hash))
            assert state is not None
            if rejection == "scope":
                state["backend_receipts"]["postgres_pgvector"]["project"] = "other-project"
            else:
                state.pop("backend_receipts")
            c2_lane["couch"].put(state)
    response = c2_lane["query"]()
    assert [h["memory_id"] for h in response["archive"]] == [current.chunk_id]
    assert response["projection_state"]["status"] == "fresh"


"""Private-tailnet policy boundary tests; not SQL integration evidence.

All inputs and provider vectors are synthetic. No external services are used.
The superseded public-output guard's ten deny tests intentionally no longer
apply: internal paths/terms are allowed; credential checks belong at egress.
"""
from unittest.mock import MagicMock

import pytest

from agent_knowledge.couchdb_source.document_model import sha256_hash
from agent_knowledge.postgres_store.pgvector_store import PgVectorStore, SessionChunk
from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector


ALLOWED_TEXT = [
    "Synthetic /Users/synthetic/private-file\n/private/test ~/test /Volumes/test",
    r"Synthetic \\synthetic-host\share C:\Users\synthetic\file",
    "Synthetic raw_transcript dataset_id document_id private_locator",
    "API_KEY password passwd secret token Bearer Authorization Basic",
    "Bearer authentication and password rotation documentation",
]


def projection_boundary(body, reuse="none"):
    store = MagicMock(spec=PgVectorStore)
    provider = MagicMock()
    provider.model = "gemini-embedding-2"
    provider.size = 3072
    provider.embed.return_value = [0.01] * 3072
    doc = dict(body=body, content_hash=sha256_hash(body),
               session_id_hash="sha256:" + "a" * 64,
               source_hash="sha256:" + "b" * 64,
               project="privacy-test", provider="synthetic")
    ready = SessionChunk(
        chunk_id="legacy-privacy-test", session_id_hash=doc["session_id_hash"],
        project=doc["project"], provider=doc["provider"], content_markdown=body,
        content_hash=doc["content_hash"], embedding_state="ready",
        embedding_model=provider.model, embedding=provider.embed.return_value,
    )
    rows = {}
    store.get_chunk.side_effect = lambda key, **kw: ready if reuse == "id" else rows.get(key)
    store.find_ready_chunk_by_identity.return_value = ready if reuse == "identity" else None
    store.insert_chunk.side_effect = lambda chunk, **kw: rows.setdefault(chunk.chunk_id, chunk)
    return PgSessionMemoryProjector(store, provider), store, provider, doc, ready


@pytest.mark.parametrize("reuse", ["none", "id", "identity"])
@pytest.mark.parametrize("body", ALLOWED_TEXT)
def test_internal_text_preserved_without_public_output_policy(body, reuse):
    projector, store, provider, doc, ready = projection_boundary(body, reuse)
    result = projector.project(target_profile="session-memory", document=doc)
    if reuse != "none":
        assert result == ready.chunk_id
        provider.embed.assert_not_called()
        store.insert_chunk.assert_not_called()
    else:
        provider.embed.assert_called_once_with(body)
        saved = store.insert_chunk.call_args.args[0]
        assert saved.content_markdown.encode("utf-8") == body.encode("utf-8")
        assert saved.content_hash == sha256_hash(body) == doc["content_hash"]
        assert result == saved.chunk_id


ASSIGNED_CREDENTIALS = [
    'API_KEY=synthetic-not-a-real-key',
    'export DATABASE_PASSWORD=synthetic-password',
    'password=x',
    '{"api_key": "synthetic-key"}',
    "{'password': 'synthetic password with spaces'}",
    '{"accessToken":"synthetic-access-token"}',
    '{"clientSecret": "synthetic-client-secret"}',
    '{"refresh_token": "synthetic-refresh-token"}',
    'X-Api-Key: synthetic-key',
    '{"password":\n "synthetic-password"}',
    'api_key:\n synthetic-key',
    'password="synthetic\\\"password"',
    'password=<redacted:secret>synthetic-tail',
]
REDACTED_ASSIGNMENTS = [
    'API_KEY=<redacted:secret>',
    'password=[REDACTED]',
    '{"api_key":"<redacted_secret>"}',
    "{'password': '***'}",
    'token=<REDACTED>',
    'password=""',
    'API_KEY=',
]


AUTH_CREDENTIALS = [
    'Authorization: Bearer synthetic-token',
    'authorization: basic c3ludGhldGljOnBhc3N3b3Jk',
    '{"Authorization": "Bearer synthetic-token"}',
    "{'Authorization': 'Basic c3ludGhldGljOnBhc3N3b3Jk'}",
    'Authorization: Bearer x',
    'Authorization: synthetic-token',
    '{"Authorization":\n "Bearer synthetic-token"}',
    '{"Authorization":\n "Bearer x"}',
    'Synthetic sk-proj-' + 'aB3x' * 10,
    'Synthetic ghp_' + 'aB3x' * 9,
    'Synthetic github_pat_' + 'aB3x' * 20,
    'Bearer synthetic-not-a-real-token',
    'Basic c3ludGhldGljOnBhc3N3b3Jk',
    'postgresql://synthetic:synthetic-password@localhost/test',
    'https://synthetic:synthetic%40password@example.invalid/',
    '-----BEGIN PRIVATE KEY-----\nc3ludGhldGlj\n-----END PRIVATE KEY-----',
    '-----BEGIN RSA PRIVATE KEY-----\nc3ludGhldGlj\n-----END RSA PRIVATE KEY-----',
    '-----BEGIN OPENSSH PRIVATE KEY-----\nc3ludGhldGlj\n-----END OPENSSH PRIVATE KEY-----',
    '-----BEGIN EC PRIVATE KEY-----\nc3ludGhldGlj',
    '-----BEGIN ENCRYPTED PRIVATE KEY-----\nc3ludGhldGlj\n-----END ENCRYPTED PRIVATE KEY-----',
]
REDACTED_AUTH = [
    'Authorization: Bearer <redacted:secret>',
    '{"Authorization": "Basic [REDACTED]"}',
    'Authorization: <redacted_secret>',
    'Bearer <redacted:secret>',
    'Basic [REDACTED]',
    'https://synthetic:<redacted:secret>@example.invalid/',
    '-----BEGIN PRIVATE KEY-----\n[REDACTED]\n-----END PRIVATE KEY-----',
    'PRIVATE KEY rotation; Basic authentication; Bearer authentication',
]


@pytest.mark.parametrize("body", AUTH_CREDENTIALS)
def test_auth_credentials_rejected_before_egress(body):
    projector, store, provider, doc, _ = projection_boundary(body)
    with pytest.raises(ValueError, match="^PG embedding input rejected by secret egress policy$"):
        projector.project(target_profile="session-memory", document=doc)
    provider.embed.assert_not_called()
    store.insert_chunk.assert_not_called()


@pytest.mark.parametrize("body", REDACTED_AUTH)
def test_redacted_auth_preserved(body):
    projector, store, provider, doc, _ = projection_boundary(body)
    projector.project(target_profile="session-memory", document=doc)
    provider.embed.assert_called_once_with(body)


@pytest.mark.parametrize("body", ASSIGNED_CREDENTIALS)
def test_credential_assignment_rejected_before_egress(body):
    projector, store, provider, doc, _ = projection_boundary(body)
    with pytest.raises(ValueError, match="^PG embedding input rejected by secret egress policy$") as exc:
        projector.project(target_profile="session-memory", document=doc)
    assert body not in str(exc.value)
    assert exc.value.__cause__ is None
    provider.embed.assert_not_called()
    store.insert_chunk.assert_not_called()
    store.transaction.assert_not_called()
    assert doc["body"] == body
    assert doc["content_hash"] == sha256_hash(body)


@pytest.mark.parametrize("body", REDACTED_ASSIGNMENTS)
def test_redacted_assignment_preserved(body):
    projector, store, provider, doc, _ = projection_boundary(body)
    projector.project(target_profile="session-memory", document=doc)
    provider.embed.assert_called_once_with(body)
    assert store.insert_chunk.call_args.args[0].content_markdown == body


@pytest.mark.parametrize("reuse", ["id", "identity"])
@pytest.mark.parametrize("body", ASSIGNED_CREDENTIALS)
def test_ready_credential_content_reuses_without_external_egress(body, reuse):
    # Existing private ready data is not being newly published or embedded.
    # This is not a claim that its previous embedding had safe provenance.
    projector, store, provider, doc, ready = projection_boundary(body, reuse)
    assert projector.project(target_profile="session-memory", document=doc) == ready.chunk_id
    provider.embed.assert_not_called()
    store.insert_chunk.assert_not_called()


def recall_boundary(monkeypatch):
    from agent_knowledge.rag_ingress import pg_recall
    from agent_knowledge.rag_ingress import qdrant_embedding
    from agent_knowledge.couchdb_source import couchdb_http_store

    pg_store = MagicMock(spec=PgVectorStore)
    pg_store.search_session_chunks.return_value = []
    source = MagicMock()
    _, _, provider, _, _ = projection_boundary("synthetic")
    monkeypatch.setattr(pg_recall, "PgVectorStore", lambda **kw: pg_store)
    monkeypatch.setattr(couchdb_http_store, "CouchDBHttpSourceStore", lambda **kw: source)
    monkeypatch.setattr(qdrant_embedding, "build_openai_embedding_provider", lambda **kw: provider)
    search = pg_recall.build_pg_brain_query_search_from_env({
        "NEURON_LBRAIN_PGVECTOR_DSN": "synthetic-not-a-dsn",
        "COUCHDB_URL": "https://example.invalid",
    })
    assert search is not None
    return search, pg_store, source, provider


@pytest.mark.parametrize("query", ASSIGNED_CREDENTIALS + AUTH_CREDENTIALS)
def test_recall_query_credentials_rejected_before_embedding(monkeypatch, query):
    search, store, source, provider = recall_boundary(monkeypatch)
    with pytest.raises(ValueError, match="^PG embedding input rejected by secret egress policy$"):
        search(query, "/project/privacy-test")
    provider.embed.assert_not_called()
    assert store.mock_calls == []
    assert source.mock_calls == []


@pytest.mark.parametrize("query", ALLOWED_TEXT + REDACTED_ASSIGNMENTS + REDACTED_AUTH)
def test_recall_internal_query_preserved(monkeypatch, query):
    search, store, _, provider = recall_boundary(monkeypatch)
    assert search(query, "/project/privacy-test") == []
    provider.embed.assert_called_once_with(query)
    assert store.search_session_chunks.call_args.kwargs["project"] == "privacy-test"


@pytest.mark.parametrize("reuse", ["none", "id", "identity"])
def test_hash_mismatch_still_rejected_before_any_boundary(reuse):
    projector, store, provider, doc, _ = projection_boundary(ALLOWED_TEXT[0], reuse)
    doc["content_hash"] = sha256_hash("different synthetic representation")
    with pytest.raises(ValueError, match="^PG content hash mismatch$"):
        projector.project(target_profile="session-memory", document=doc)
    assert store.mock_calls == []
    provider.embed.assert_not_called()


@pytest.mark.parametrize("reuse", ["id", "identity"])
@pytest.mark.parametrize("field,value", [
    ("session_id_hash", "wrong-session"), ("project", "wrong-project"),
    ("provider", "wrong-provider"), ("content_hash", sha256_hash("wrong")),
    ("content_markdown", "wrong body"), ("embedding_model", "wrong-model"),
    ("embedding", [0.01]),
])
def test_ready_reuse_still_requires_exact_identity_and_vector(reuse, field, value):
    from dataclasses import replace
    projector, store, provider, doc, ready = projection_boundary(ALLOWED_TEXT[0], reuse)
    invalid = replace(ready, **{field: value})
    if reuse == "id":
        store.get_chunk.side_effect = None
        store.get_chunk.return_value = invalid
    else:
        store.find_ready_chunk_by_identity.return_value = invalid
    with pytest.raises(ValueError, match="^PG (chunk identity|ready chunk vector) mismatch$"):
        projector.project(target_profile="session-memory", document=doc)
    provider.embed.assert_not_called()
    store.insert_chunk.assert_not_called()


def test_recall_scope_required_before_embedding(monkeypatch):
    search, store, source, provider = recall_boundary(monkeypatch)
    with pytest.raises(RuntimeError, match="^PG recall requires project scope$"):
        search(ALLOWED_TEXT[0], "/global")
    provider.embed.assert_not_called()
    assert store.mock_calls == []
    assert source.mock_calls == []

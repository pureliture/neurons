"""Backfill CLI 안전성(CouchDB-native): 서버측 collection의 우발적 생성이 없고
(run/rollback은 --create-collection이 없는 한 없는 collection에서 fail-closed),
COUCHDB_URL이 없으면 CouchDB store가 fail-closed되며, dry-run/verify는 서버를 절대
건드리지 않는다. Qdrant 구성은 in-memory fake로 monkeypatch된다 -- 네트워크 없음.
"""

from __future__ import annotations

import pytest

import agent_knowledge.rag_ingress.qdrant_docling_mirror as mirror_mod
import agent_knowledge.rag_ingress.qdrant_embedding as embedding_mod
import agent_knowledge.rag_ingress.qdrant_backfill_cli as cli_mod
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    FOUNDATION_DIRECT_WRITE_CONTRACT,
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
)
from agent_knowledge.qdrant_write_gateway_runtime import QdrantMutationSource
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient

VECTOR_SIZE = 64


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _args(**overrides):
    base = {
        "collection": "",
        "checkpoint": "",
        "submitted": "",
        "embedding_concurrency": 1,
        "qdrant_url": "http://qdrant.local",
        "cohort": "",
        "limit": None,
        "create_collection": False,
    }
    base.update(overrides)
    return _NS(**base)


@pytest.fixture
def shared_client():
    return InMemoryQdrantClient()


@pytest.fixture
def patched_qdrant(monkeypatch, shared_client):
    """remote adapter builder + embedding provider를 in-memory fake로 patch한다.

    fake builder는 실제 builder와 동일하게 ``ensure_collection``을 존중하므로
    fail-closed / opt-in-create 동작이 충실히 검증된다.
    """

    monkeypatch.setenv("QDRANT_WRITE_ACTIVATION", "remote_gateway")
    monkeypatch.setenv(
        "QDRANT_WRITE_GATEWAY_ENDPOINT", "https://gateway.invalid:8443"
    )
    monkeypatch.setenv("QDRANT_WRITE_GATEWAY_GENERATION", "7")
    monkeypatch.setenv("QDRANT_WRITE_GATEWAY_TOKEN_FILE", "/fixed/token")
    monkeypatch.setenv("QDRANT_WRITE_GATEWAY_CA_FILE", "/fixed/ca.pem")
    monkeypatch.setenv("QDRANT_READ_API_KEY_FILE", "/fixed/read-api-key")

    def _fake_builder(
        *,
        read_url,
        read_api_key_path,
        gateway_endpoint,
        gateway_token_path,
        gateway_ca_path,
        gateway_generation,
        collection_name,
        source,
        embedding_provider=None,
        normalizer=None,
    ):
        return QdrantDoclingMirrorAdapter(
            client=shared_client,
            collection_name=collection_name,
            normalizer=PassthroughMarkdownNormalizer(),
            embedding_provider=HashEmbeddingProvider(size=VECTOR_SIZE),
            direct_write_contract=FOUNDATION_DIRECT_WRITE_CONTRACT,
        )

    monkeypatch.setattr(
        mirror_mod,
        "build_remote_qdrant_docling_sidecar_adapter",
        _fake_builder,
    )
    monkeypatch.setattr(
        embedding_mod,
        "build_openai_embedding_provider",
        lambda *, environ=None, embed_fn=None: HashEmbeddingProvider(size=VECTOR_SIZE),
    )
    return shared_client


# ------------------------------------------- FIX 1: collection의 우발적 생성 없음

def test_run_absent_collection_without_create_fails_closed(patched_qdrant):
    client = patched_qdrant
    assert not client.collection_exists("typo_collection")
    with pytest.raises(SystemExit) as exc:
        cli_mod._build_adapter(
            _args(),
            collection_name="typo_collection",
            source=QdrantMutationSource.BACKFILL,
            ensure_collection=False,
        )
    assert "typo_collection" in str(exc.value)
    assert not client.collection_exists("typo_collection")


def test_run_create_collection_flag_is_operator_only(patched_qdrant):
    client = patched_qdrant
    assert not client.collection_exists("staging_mirror")
    with pytest.raises(SystemExit, match="operator-only"):
        cli_mod._build_adapter(
            _args(),
            collection_name="staging_mirror",
            source=QdrantMutationSource.BACKFILL,
            ensure_collection=True,
        )
    assert not client.collection_exists("staging_mirror")


def test_existing_collection_no_create_succeeds(patched_qdrant):
    client = patched_qdrant
    client.create_collection("live_mirror", vectors_config={"size": VECTOR_SIZE, "distance": "Cosine"})
    adapter = cli_mod._build_adapter(
        _args(),
        collection_name="live_mirror",
        source=QdrantMutationSource.BACKFILL,
        ensure_collection=False,
    )
    assert adapter.collection_name == "live_mirror"
    assert client.point_count("live_mirror") == 0


def test_rollback_absent_collection_fails_closed(patched_qdrant, tmp_path):
    client = patched_qdrant
    manifest = tmp_path / "submitted.jsonl"
    manifest.write_text(
        '{"target_profile": "session-memory", "idempotency_key": "claude:session_memory:sha256:x", "content_hash": "sha256:x"}\n',
        encoding="utf-8",
    )
    args = _args(collection="typo_rollback", submitted=str(manifest))
    with pytest.raises(SystemExit) as exc:
        cli_mod._cmd_rollback(args)
    assert "typo_rollback" in str(exc.value)
    assert not client.collection_exists("typo_rollback")


def test_dry_run_creates_no_collection(patched_qdrant, monkeypatch):
    client = patched_qdrant
    # dry-run은 _build_adapter가 아니라 _DryRunAdapter를 쓴다; CouchDB store는 읽는다.
    monkeypatch.setattr(cli_mod, "_build_store", lambda args: InMemoryCouchDBSourceStore())
    cli_mod._cmd_dry_run(_args(collection="should_not_be_created"))
    assert client._collections == {}  # 어디에도 생성되지 않음


def test_verify_creates_no_collection(patched_qdrant, monkeypatch, capsys):
    client = patched_qdrant
    monkeypatch.setattr(cli_mod, "_build_store", lambda args: InMemoryCouchDBSourceStore())
    cli_mod._cmd_verify(_args())
    assert client._collections == {}
    out = capsys.readouterr().out
    assert '"command": "verify"' in out


# --------------------------------- FIX 3: url이 없으면 CouchDB store fail-closed

def test_missing_couchdb_url_fails_closed(monkeypatch):
    monkeypatch.delenv("COUCHDB_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        cli_mod._build_store(_args())
    assert "COUCHDB_URL" in str(exc.value)


def test_build_store_with_couchdb_url_succeeds(monkeypatch):
    monkeypatch.setenv("COUCHDB_URL", "http://couchdb.local")
    monkeypatch.setenv("COUCHDB_USER", "neuron")
    monkeypatch.setenv("COUCHDB_PASSWORD", "x")
    store = cli_mod._build_store(_args())
    assert store is not None


# ------------------------------------------------------------------- jsonl 관용성

def test_load_submitted_jsonl_skips_corrupt_line(tmp_path):
    manifest = tmp_path / "submitted.jsonl"
    manifest.write_text(
        '{"target_profile": "session-memory", "idempotency_key": "k1", "content_hash": "h1"}\n'
        "this is not json\n"
        '{"target_profile": "session-memory", "idempotency_key": "k2", "content_hash": "h2"}\n',
        encoding="utf-8",
    )
    records = cli_mod._load_submitted_jsonl(str(manifest))
    assert len(records) == 2
    assert {r["content_hash"] for r in records} == {"h1", "h2"}

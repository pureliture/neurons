from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_knowledge.couchdb_source import build_cli
from agent_knowledge.qdrant_write_gateway_runtime import (
    QdrantMutationSource,
    QdrantWriteActivation,
    qdrant_write_activation_from_environment,
)
from agent_knowledge.rag_ingress import qdrant_backfill_cli, qdrant_dual_write
from agent_knowledge.rag_ingress import qdrant_docling_mirror as mirror_mod
from agent_knowledge.rag_ingress import qdrant_embedding as embedding_mod


def test_live_callers_own_fixed_non_string_source_constants() -> None:
    assert qdrant_dual_write.QDRANT_MUTATION_SOURCE is QdrantMutationSource.NORMAL_INGEST
    assert build_cli.QDRANT_MUTATION_SOURCE is QdrantMutationSource.PROJECTION
    assert qdrant_backfill_cli.RUN_QDRANT_MUTATION_SOURCE is QdrantMutationSource.BACKFILL
    assert qdrant_backfill_cli.ROLLBACK_QDRANT_MUTATION_SOURCE is QdrantMutationSource.REPAIR


def test_backfill_cannot_activate_operator_provisioning() -> None:
    class Args:
        qdrant_url = "http://qdrant.invalid"
        create_collection = True

    with pytest.raises(SystemExit, match="operator-only"):
        qdrant_backfill_cli._build_adapter(
            Args(),
            collection_name="mirror",
            source=QdrantMutationSource.BACKFILL,
            ensure_collection=True,
        )


def test_normal_ingest_and_projection_builders_pass_owned_source(monkeypatch) -> None:
    captured: list[QdrantMutationSource] = []
    sentinel = object()

    def fake_builder(**kwargs):
        captured.append(kwargs["source"])
        return sentinel

    monkeypatch.setattr(
        mirror_mod,
        "build_remote_qdrant_docling_sidecar_adapter",
        fake_builder,
    )
    monkeypatch.setattr(
        embedding_mod,
        "build_openai_embedding_provider",
        lambda *, environ=None, embed_fn=None: object(),
    )
    environ = {
        "QDRANT_URL": "http://qdrant.invalid",
        "QDRANT_COLLECTION": "mirror",
        "QDRANT_WRITE_ACTIVATION": "remote_gateway",
        "QDRANT_WRITE_GATEWAY_ENDPOINT": "https://gateway.invalid:8443",
        "QDRANT_WRITE_GATEWAY_GENERATION": "7",
        "QDRANT_WRITE_GATEWAY_TOKEN_FILE": "/fixed/token",
        "QDRANT_WRITE_GATEWAY_CA_FILE": "/fixed/ca.pem",
        "QDRANT_READ_API_KEY_FILE": "/fixed/read-api-key",
        "MIRROR_DUAL_WRITE": "1",
    }

    assert qdrant_dual_write.build_qdrant_mirror_from_env(environ) is sentinel
    assert build_cli._build_forward_mirror_sink(environ) is not None
    assert build_cli._build_qdrant_projector(environ) is not None
    assert captured == [
        QdrantMutationSource.NORMAL_INGEST,
        QdrantMutationSource.PROJECTION,
        QdrantMutationSource.PROJECTION,
    ]


def test_foundation_default_is_inactive_even_when_qdrant_url_exists() -> None:
    assert (
        qdrant_dual_write.build_qdrant_mirror_from_env(
            {"QDRANT_URL": "https://qdrant.invalid"}
        )
        is None
    )


def test_foundation_direct_is_typed_pr_c_only_and_audit_coverage_pending() -> None:
    contract = mirror_mod.FOUNDATION_DIRECT_WRITE_CONTRACT

    assert (
        qdrant_write_activation_from_environment(
            {"QDRANT_WRITE_ACTIVATION": "foundation_direct"}
        )
        is QdrantWriteActivation.FOUNDATION_DIRECT
    )
    assert contract.activation is QdrantWriteActivation.FOUNDATION_DIRECT
    assert contract.phase == "pr_c_foundation_compatibility"
    assert contract.audit_status == "pending"
    assert contract.coverage_status == "pending"

    with pytest.raises(TypeError):
        mirror_mod.QdrantDoclingMirrorAdapter(
            client=object(),
            direct_write_compatibility=True,
        )


def test_foundation_direct_preserves_normal_projection_backfill_and_repair_builders(
    monkeypatch,
) -> None:
    direct_calls = []
    remote_calls = []

    class Adapter:
        def collection_exists(self):
            return True

    def direct_builder(**kwargs):
        direct_calls.append(kwargs)
        return Adapter()

    monkeypatch.setattr(
        mirror_mod,
        "build_remote_qdrant_docling_mirror_adapter",
        direct_builder,
    )
    monkeypatch.setattr(
        mirror_mod,
        "build_remote_qdrant_docling_sidecar_adapter",
        lambda **kwargs: remote_calls.append(kwargs) or Adapter(),
    )
    monkeypatch.setattr(
        embedding_mod,
        "build_openai_embedding_provider",
        lambda *, environ=None, embed_fn=None: object(),
    )
    environ = {
        "QDRANT_URL": "http://qdrant.invalid",
        "QDRANT_COLLECTION": "mirror",
        "QDRANT_WRITE_ACTIVATION": "foundation_direct",
        "MIRROR_DUAL_WRITE": "1",
    }

    assert qdrant_dual_write.build_qdrant_mirror_from_env(environ) is not None
    assert build_cli._build_forward_mirror_sink(environ) is not None
    assert build_cli._build_qdrant_projector(environ) is not None

    monkeypatch.setenv("QDRANT_WRITE_ACTIVATION", "foundation_direct")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid")

    class Args:
        qdrant_url = "http://qdrant.invalid"
        create_collection = False

    for source in (
        QdrantMutationSource.BACKFILL,
        QdrantMutationSource.REPAIR,
    ):
        assert qdrant_backfill_cli._build_adapter(
            Args(),
            collection_name="mirror",
            source=source,
        ) is not None

    assert len(direct_calls) == 5
    assert remote_calls == []
    assert all(
        call["direct_write_contract"]
        is mirror_mod.FOUNDATION_DIRECT_WRITE_CONTRACT
        for call in direct_calls
    )
    assert all("read_api_key_path" not in call for call in direct_calls)


def test_remote_writer_builder_requires_one_explicit_read_only_credential(
    monkeypatch,
    tmp_path,
) -> None:
    import qdrant_client
    import agent_knowledge.qdrant_write_gateway_http as gateway_http

    qdrant_calls = []
    transport_calls = []

    class ReadClient:
        pass

    class WriteTransport:
        def __init__(self, **kwargs):
            transport_calls.append(kwargs)

    monkeypatch.setattr(
        qdrant_client,
        "QdrantClient",
        lambda **kwargs: qdrant_calls.append(kwargs) or ReadClient(),
    )
    monkeypatch.setattr(gateway_http, "RemoteQdrantGatewayTransport", WriteTransport)
    read_key = tmp_path / "read-api-key"
    read_key.write_text("read-only-secret", encoding="utf-8")
    read_key.chmod(0o600)

    adapter = mirror_mod.build_remote_qdrant_docling_sidecar_adapter(
        read_url="https://qdrant.invalid:6333",
        read_api_key_path=read_key,
        gateway_endpoint="https://gateway.invalid:8443",
        gateway_token_path="/fixed/token",
        gateway_ca_path="/fixed/ca.pem",
        gateway_generation=7,
        collection_name="mirror",
        source=QdrantMutationSource.NORMAL_INGEST,
    )

    assert adapter is not None
    assert qdrant_calls == [
        {
            "url": "https://qdrant.invalid:6333",
            "api_key": "read-only-secret",
            "timeout": 5,
            "prefer_grpc": False,
            "trust_env": False,
            "follow_redirects": False,
        }
    ]
    assert len(transport_calls) == 1


@pytest.mark.parametrize(
    "read_url",
    (
        "http://qdrant.invalid",
        "https://user:secret@qdrant.invalid",
        "https://qdrant.invalid/collections",
        "https://qdrant.invalid?target=other",
        "https://qdrant.invalid#fragment",
        "https://qdrant.invalid",
        "https://qdrant.invalid:8443",
        "https://qdrant.invalid:not-a-port",
        "https://qdrant.invalid\\@other.invalid",
        "https://qdrant.invalid%2f.other.invalid",
    ),
)
def test_remote_writer_rejects_noncanonical_read_authority_before_secret_use(
    monkeypatch,
    tmp_path,
    read_url,
) -> None:
    import qdrant_client
    import agent_knowledge.qdrant_write_gateway_http as gateway_http

    qdrant_calls = []
    secret_reads = []
    monkeypatch.setattr(
        qdrant_client,
        "QdrantClient",
        lambda **kwargs: qdrant_calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        gateway_http,
        "read_projected_qdrant_api_key",
        lambda path: secret_reads.append(path) or "read-only-secret",
    )
    monkeypatch.setattr(
        gateway_http,
        "RemoteQdrantGatewayTransport",
        lambda **kwargs: object(),
    )

    with pytest.raises(
        mirror_mod.SearchableMirrorUnavailable,
        match="qdrant_read_url_invalid",
    ) as caught:
        mirror_mod.build_remote_qdrant_docling_sidecar_adapter(
            read_url=read_url,
            read_api_key_path=tmp_path / "read-api-key",
            gateway_endpoint="https://gateway.invalid:8443",
            gateway_token_path="/fixed/token",
            gateway_ca_path="/fixed/ca.pem",
            gateway_generation=7,
            collection_name="mirror",
            source=QdrantMutationSource.NORMAL_INGEST,
        )

    assert qdrant_calls == []
    assert secret_reads == []
    assert "secret" not in str(caught.value)


def test_normal_writer_modules_have_zero_direct_qdrant_mutation_or_marker_builders() -> None:
    package = Path(__file__).parents[1] / "lib" / "agent_knowledge"
    relative_paths = (
        "rag_ingress/qdrant_dual_write.py",
        "rag_ingress/qdrant_backfill_cli.py",
        "couchdb_source/build_cli.py",
    )
    forbidden_names = {
        "build_qdrant_gateway_transport",
        "QdrantMutationMarkerStore",
        "DirectQdrantWriteTransport",
        "reconcile_qdrant_marker_metadata",
    }
    violations = []
    for relative in relative_paths:
        path = package / relative
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        if forbidden_names.intersection(
            {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        ):
            violations.append(relative)
        if "QDRANT_API_KEY" in text or "QDRANT_MARKER_COLLECTION" in text:
            violations.append(relative)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"upsert", "delete"}
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert violations == []

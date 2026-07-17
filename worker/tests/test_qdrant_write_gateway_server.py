from __future__ import annotations

import json
import hashlib
import inspect
from pathlib import Path

import pytest

from agent_knowledge.qdrant_write_gateway_runtime import (
    QDRANT_SOURCE_REGISTRY,
    QdrantMutationSource,
    RenderedQdrantWriter,
    build_qdrant_coverage_activation_anchor,
)
from agent_knowledge.qdrant_write_gateway_sidecar import (
    build_qdrant_gateway_route_set_hash,
)


def _write(path: Path, value: str) -> str:
    path.write_text(value, encoding="utf-8")
    return str(path)


def _environment(tmp_path: Path) -> dict[str, str]:
    active_sources = tuple(
        source
        for source in QdrantMutationSource
        if source.value in {"normal_ingest", "projection", "backfill", "repair"}
    )
    route_set_hash = build_qdrant_gateway_route_set_hash(active_sources)
    workload_ref_hash = hashlib.sha256(b"workload:active-writer").hexdigest()
    bindings = [
        {
            "subject": "system:serviceaccount:neurons:active-writer",
            "sources": [source.value for source in active_sources],
            "workload_ref_hash": workload_ref_hash,
            "route_set_hash": route_set_hash,
        }
    ]
    inventory = tuple(
        RenderedQdrantWriter(
            source=source,
            route=binding.route,
            writer_ref_hash=binding.writer_ref_hash,
            active_caller=binding.active_caller,
            workload_ref_hash=workload_ref_hash if binding.active_caller else None,
            image_ref_hash=(
                hashlib.sha256(f"image:{source.value}".encode()).hexdigest()
                if binding.active_caller
                else None
            ),
            network_policy_ref_hash=(
                hashlib.sha256(f"network:{source.value}".encode()).hexdigest()
                if binding.active_caller
                else None
            ),
            route_set_hash=route_set_hash if binding.active_caller else None,
        )
        for source, binding in QDRANT_SOURCE_REGISTRY.items()
    )
    activation = build_qdrant_coverage_activation_anchor(
        generation=7,
        marker_collection="mutation_markers",
        rendered_inventory=inventory,
        previous_generation_hash=hashlib.sha256(b"generation:6").hexdigest(),
        auth_boundary_status="validated",
        network_policy_status="validated",
        direct_write_credentials_zero=True,
        read_endpoint_write_denied_status="validated",
    )
    activation_payload = {
        "generation": activation.generation,
        "marker_collection": activation.marker_collection,
        "rendered_inventory": [
            {
                "source": item.source.value,
                "route": item.route.value,
                "writer_ref_hash": item.writer_ref_hash,
                "active_caller": item.active_caller,
                "workload_ref_hash": item.workload_ref_hash,
                "image_ref_hash": item.image_ref_hash,
                "network_policy_ref_hash": item.network_policy_ref_hash,
                "route_set_hash": item.route_set_hash,
            }
            for item in inventory
        ],
        "previous_generation_hash": activation.previous_generation_hash,
        "auth_boundary_status": activation.auth_boundary_status,
        "network_policy_status": activation.network_policy_status,
        "direct_write_credentials_zero": activation.direct_write_credentials_zero,
        "read_endpoint_write_denied_status": activation.read_endpoint_write_denied_status,
        "activation_hash": activation.activation_hash,
    }
    return {
        "NEURONS_QDRANT_GATEWAY_GENERATION": "7",
        "NEURONS_QDRANT_GATEWAY_PRODUCT_COLLECTIONS": json.dumps(["mirror"]),
        "NEURONS_QDRANT_GATEWAY_MARKER_COLLECTION": "mutation_markers",
        "NEURONS_QDRANT_GATEWAY_MARKER_METADATA_POINT_ID": "00000000-0000-4000-8000-000000000001",
        "NEURONS_QDRANT_GATEWAY_ACTIVATION_ANCHOR": json.dumps(activation_payload),
        "NEURONS_QDRANT_GATEWAY_PRODUCT_COLLECTION_SCHEMA_HASHES": json.dumps(
            [{"collection": "mirror", "schema_hash": "d" * 64}]
        ),
        "NEURONS_QDRANT_GATEWAY_SUBJECT_BINDINGS": json.dumps(bindings),
        "NEURONS_QDRANT_GATEWAY_PRODUCT_URL": "https://qdrant.invalid",
        "NEURONS_QDRANT_GATEWAY_MARKER_URL": "https://qdrant.invalid",
        "NEURONS_QDRANT_GATEWAY_PRODUCT_API_KEY_FILE": _write(
            tmp_path / "product-key", "product-secret"
        ),
        "NEURONS_QDRANT_GATEWAY_MARKER_API_KEY_FILE": _write(
            tmp_path / "marker-key", "marker-secret"
        ),
        "NEURONS_QDRANT_GATEWAY_KUBE_API_SERVER": "https://kubernetes.default.svc",
        "NEURONS_QDRANT_GATEWAY_KUBE_TOKEN_FILE": _write(
            tmp_path / "kube-token", "kube-secret"
        ),
        "NEURONS_QDRANT_GATEWAY_KUBE_CA_FILE": _write(
            tmp_path / "kube-ca", "fixture-ca"
        ),
        "NEURONS_QDRANT_GATEWAY_TLS_CERT_FILE": _write(
            tmp_path / "tls.crt", "fixture-cert"
        ),
        "NEURONS_QDRANT_GATEWAY_TLS_KEY_FILE": _write(
            tmp_path / "tls.key", "fixture-key"
        ),
    }


def test_server_builder_owns_separate_proxy_disabled_product_and_marker_clients(
    tmp_path: Path,
) -> None:
    from agent_knowledge.qdrant_write_gateway_server import (
        build_gateway_application_from_environment,
    )

    calls: list[dict[str, object]] = []

    class Client:
        pass

    def client_factory(**kwargs):
        calls.append(kwargs)
        return Client()

    app = build_gateway_application_from_environment(
        _environment(tmp_path),
        qdrant_client_factory=client_factory,
        token_review_transport=object(),
    )

    assert app is not None
    assert len(calls) == 2
    assert calls[0]["api_key"] == "product-secret"
    assert calls[1]["api_key"] == "marker-secret"
    assert all(call["trust_env"] is False for call in calls)
    assert all(call["follow_redirects"] is False for call in calls)
    assert all(call["prefer_grpc"] is False for call in calls)
    assert all(call["timeout"] == 5 for call in calls)


def test_server_rejects_subject_workload_not_bound_to_external_activation_anchor(
    tmp_path: Path,
) -> None:
    from agent_knowledge.qdrant_write_gateway_server import (
        QdrantGatewayServerConfigurationError,
        build_gateway_application_from_environment,
    )

    environ = _environment(tmp_path)
    bindings = json.loads(environ["NEURONS_QDRANT_GATEWAY_SUBJECT_BINDINGS"])
    bindings[0]["workload_ref_hash"] = "f" * 64
    environ["NEURONS_QDRANT_GATEWAY_SUBJECT_BINDINGS"] = json.dumps(bindings)

    with pytest.raises(QdrantGatewayServerConfigurationError):
        build_gateway_application_from_environment(
            environ,
            qdrant_client_factory=lambda **_kwargs: object(),
            token_review_transport=object(),
        )


def test_entrypoint_script_smoke_dispatches_tls_server_without_live_network(
    tmp_path: Path,
) -> None:
    import agent_knowledge.qdrant_write_gateway_server as server

    calls = []
    sentinel = object()

    result = server.main(
        [],
        environ=_environment(tmp_path),
        application_builder=lambda environ: sentinel,
        server_runner=lambda **kwargs: calls.append(kwargs),
    )

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["application"] is sentinel
    assert calls[0]["bind_host"] == "127.0.0.1"
    assert calls[0]["port"] == 8443
    assert calls[0]["cert_path"].endswith("tls.crt")
    assert calls[0]["key_path"].endswith("tls.key")


def test_pyproject_exposes_only_dedicated_gateway_entrypoint() -> None:
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert (
        'neurons-qdrant-write-gateway = '
        '"agent_knowledge.qdrant_write_gateway_server:main"'
    ) in pyproject


def test_ops_handoff_constants_fix_command_port_readiness_and_six_sources() -> None:
    from agent_knowledge.qdrant_write_gateway_server import (
        QDRANT_GATEWAY_BIND_HOST,
        QDRANT_GATEWAY_COMMAND,
        QDRANT_GATEWAY_PORT,
        QDRANT_GATEWAY_READINESS_PATH,
        QDRANT_GATEWAY_REQUIRED_ENV,
        QDRANT_GATEWAY_SOURCE_VALUES,
    )

    assert QDRANT_GATEWAY_COMMAND == ("neurons-qdrant-write-gateway",)
    assert QDRANT_GATEWAY_BIND_HOST == "127.0.0.1"
    assert QDRANT_GATEWAY_PORT == 8443
    assert QDRANT_GATEWAY_READINESS_PATH == "/readyz"
    assert set(QDRANT_GATEWAY_SOURCE_VALUES) == {
        "normal_ingest",
        "projection",
        "backfill",
        "repair",
    }
    assert {
        "NEURONS_QDRANT_GATEWAY_TLS_CERT_FILE",
        "NEURONS_QDRANT_GATEWAY_TLS_KEY_FILE",
        "NEURONS_QDRANT_GATEWAY_PRODUCT_API_KEY_FILE",
        "NEURONS_QDRANT_GATEWAY_MARKER_API_KEY_FILE",
        "NEURONS_QDRANT_GATEWAY_KUBE_TOKEN_FILE",
        "NEURONS_QDRANT_GATEWAY_KUBE_CA_FILE",
        "NEURONS_QDRANT_GATEWAY_MARKER_METADATA_POINT_ID",
        "NEURONS_QDRANT_GATEWAY_ACTIVATION_ANCHOR",
        "NEURONS_QDRANT_GATEWAY_PRODUCT_COLLECTION_SCHEMA_HASHES",
    }.issubset(QDRANT_GATEWAY_REQUIRED_ENV)


def test_readyz_delegates_to_read_only_application_preflight() -> None:
    from agent_knowledge.qdrant_write_gateway_server import serve_qdrant_gateway

    source = inspect.getsource(serve_qdrant_gateway)

    assert "application.readiness()" in source
    assert '_fixed_response(200, "ready")' not in source

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from agent_knowledge.qdrant_write_gateway_http import (
    QDRANT_TOKEN_REVIEW_PATH,
    GatewayTlsTransportError,
    KubernetesTokenReviewClient,
    RemoteQdrantGatewayTransport,
    StrictTlsJsonTransport,
    build_qdrant_gateway_tls_context,
    read_projected_qdrant_api_key,
)
from agent_knowledge.qdrant_write_gateway_runtime import QdrantMutationSource
from agent_knowledge.qdrant_write_gateway_sidecar import (
    QDRANT_WRITE_GATEWAY_AUDIENCE,
)


class _Transport:
    def __init__(self, response: dict[str, object] | None = None) -> None:
        self.response = response or {
            "schema_version": "qdrant_write_gateway_response.v1",
            "status": "succeeded",
            "operation_ref": "a" * 64,
        }
        self.calls: list[dict[str, object]] = []

    def post_json(self, **kwargs: object) -> bytes:
        self.calls.append(dict(kwargs))
        return json.dumps(self.response).encode("utf-8")


def _secret_file(tmp_path: Path, name: str, value: str) -> Path:
    path = tmp_path / name
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_tokenreview_client_posts_once_with_fixed_audience_and_no_retry(tmp_path) -> None:
    caller_token = _secret_file(tmp_path, "reviewer-token", "reviewer-secret")
    ca_file = _secret_file(tmp_path, "kube-ca", "fixture-ca")
    transport = _Transport(
        {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "status": {"authenticated": False},
        }
    )
    client = KubernetesTokenReviewClient(
        api_server="https://kubernetes.default.svc",
        caller_token_path=caller_token,
        ca_path=ca_file,
        transport=transport,
    )

    response = client.review(
        token="writer-projected-token",
        audience=QDRANT_WRITE_GATEWAY_AUDIENCE,
    )

    assert response["kind"] == "TokenReview"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://kubernetes.default.svc" + QDRANT_TOKEN_REVIEW_PATH
    assert call["headers"] == {
        "accept": "application/json",
        "authorization": "Bearer reviewer-secret",
        "content-type": "application/json",
    }
    assert json.loads(call["body"]) == {
        "apiVersion": "authentication.k8s.io/v1",
        "kind": "TokenReview",
        "spec": {
            "audiences": [QDRANT_WRITE_GATEWAY_AUDIENCE],
            "token": "writer-projected-token",
        },
    }
    assert "secret" not in repr(client)


def test_remote_transport_uses_only_fixed_https_route_and_projected_token(tmp_path) -> None:
    token_path = _secret_file(tmp_path, "writer-token", "writer-secret")
    ca_path = _secret_file(tmp_path, "gateway-ca", "fixture-ca")
    transport = _Transport()
    client = RemoteQdrantGatewayTransport(
        endpoint="https://neurons-qdrant-write-gateway:8443",
        source=QdrantMutationSource.NORMAL_INGEST,
        generation=7,
        collection_name="mirror",
        token_path=token_path,
        ca_path=ca_path,
        transport=transport,
    )

    receipt = client.upsert_points(
        points=[{"id": "raw-point", "vector": [0.1], "payload": {"safe": True}}]
    )

    assert receipt.operation_ref == "a" * 64
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == (
        "https://neurons-qdrant-write-gateway:8443/v1/points/upsert"
    )
    assert call["headers"] == {
        "authorization": "Bearer writer-secret",
        "content-type": "application/json",
    }
    assert json.loads(call["body"])["source"] == "normal_ingest"
    assert json.loads(call["body"])["generation"] == 7
    assert "raw-point" not in repr(client)
    assert "writer-secret" not in repr(client)

    client.delete_points(points_selector=["raw-point"], item_count=1)
    assert len(transport.calls) == 2
    assert transport.calls[1]["url"].endswith("/v1/points/delete")


def test_projected_volume_secret_symlinks_resolve_to_bounded_regular_files(
    tmp_path: Path,
) -> None:
    token_target = _secret_file(tmp_path, "..token-data", "writer-secret")
    ca_target = _secret_file(tmp_path, "..ca-data", "fixture-ca")
    token_path = tmp_path / "token"
    ca_path = tmp_path / "ca.crt"
    token_path.symlink_to(token_target.name)
    ca_path.symlink_to(ca_target.name)
    transport = _Transport()
    client = RemoteQdrantGatewayTransport(
        endpoint="https://gateway.invalid:8443",
        source=QdrantMutationSource.NORMAL_INGEST,
        generation=7,
        collection_name="mirror",
        token_path=token_path,
        ca_path=ca_path,
        transport=transport,
    )

    client.upsert_points(
        points=[{"id": "point", "vector": [0.1], "payload": {"safe": True}}]
    )

    assert transport.calls[0]["ca_path"] == ca_path
    assert transport.calls[0]["headers"]["authorization"] == "Bearer writer-secret"


def test_projected_secret_symlink_swap_reads_one_open_inode_atomically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original = _secret_file(tmp_path, "..data-original", "original-secret")
    replacement = _secret_file(tmp_path, "..data-replacement", "replacement-secret")
    projected = tmp_path / "api-key"
    projected.symlink_to(original.name)
    real_open = __import__("os").open
    open_count = 0

    def swapping_open(path, flags):
        nonlocal open_count
        open_count += 1
        fd = real_open(path, flags)
        projected.unlink()
        projected.symlink_to(replacement.name)
        return fd

    import agent_knowledge.qdrant_write_gateway_http as gateway_http

    monkeypatch.setattr(gateway_http.os, "open", swapping_open)

    assert read_projected_qdrant_api_key(projected) == "original-secret"
    assert projected.read_text(encoding="utf-8") == "replacement-secret"
    assert open_count == 1


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://gateway.invalid",
        "https://user:secret@gateway.invalid",
        "https://gateway.invalid/path",
        "https://gateway.invalid?target=other",
        "https://gateway.invalid#fragment",
        "https://gateway.invalid",
        "https://gateway.invalid:443",
        "https://gateway.invalid:8444",
    ),
)
def test_remote_gateway_endpoint_is_tls_only_and_not_a_generic_proxy(
    endpoint: str, tmp_path: Path
) -> None:
    token_path = _secret_file(tmp_path, "writer-token", "writer-secret")
    ca_path = _secret_file(tmp_path, "gateway-ca", "fixture-ca")
    with pytest.raises(ValueError, match="gateway_endpoint_invalid") as caught:
        RemoteQdrantGatewayTransport(
            endpoint=endpoint,
            source=QdrantMutationSource.NORMAL_INGEST,
            generation=7,
            collection_name="mirror",
            token_path=token_path,
            ca_path=ca_path,
            transport=_Transport(),
        )
    assert "secret" not in str(caught.value)


def test_strict_tls_transport_disables_proxy_redirect_and_sanitizes_failure() -> None:
    captured: dict[str, object] = {}

    class Opener:
        def open(self, request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "raw-private-redirect",
                {"Location": "https://attacker.invalid"},
                None,
            )

    def opener_factory(*handlers):
        captured["handlers"] = handlers
        return Opener()

    transport = StrictTlsJsonTransport(
        ssl_context_factory=lambda cafile: object(),
        opener_factory=opener_factory,
    )

    with pytest.raises(GatewayTlsTransportError, match="gateway_tls_request_failed") as caught:
        transport.post_json(
            url="https://gateway.invalid/v1/points/upsert",
            headers={"authorization": "Bearer raw-private-token"},
            body=b'{"raw":"private"}',
            ca_path=Path("/fixed/ca.pem"),
            timeout_seconds=5,
        )

    handlers = captured["handlers"]
    proxy_handlers = [handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert any(type(handler).__name__ == "_RejectRedirectHandler" for handler in handlers)
    assert "raw-private" not in str(caught.value)


def test_server_tls_context_requires_cert_key_and_tls12(tmp_path: Path) -> None:
    cert = _secret_file(tmp_path, "tls.crt", "fixture-cert")
    key = _secret_file(tmp_path, "tls.key", "fixture-key")

    class Context:
        minimum_version = None

        def load_cert_chain(self, *, certfile: str, keyfile: str) -> None:
            self.loaded = (certfile, keyfile)

    context = Context()
    result = build_qdrant_gateway_tls_context(
        cert_path=cert,
        key_path=key,
        context_factory=lambda purpose: context,
    )

    assert result is context
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
    assert context.loaded == (str(cert), str(key))

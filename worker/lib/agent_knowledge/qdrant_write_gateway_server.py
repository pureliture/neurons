"""Dedicated TLS sidecar entrypoint for ``neurons-qdrant-write-gateway``."""

from __future__ import annotations

import json
import os
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .qdrant_write_gateway_http import (
    KubernetesTokenReviewClient,
    build_qdrant_gateway_tls_context,
    read_projected_qdrant_api_key,
)
from .qdrant_write_gateway_runtime import (
    ACTIVE_QDRANT_MUTATION_SOURCES,
    QdrantCollectionPolicy,
    QdrantMutationSource,
    RenderedQdrantWriter,
    build_qdrant_coverage_activation_anchor,
    build_qdrant_coverage_manifest_from_activation_anchor,
)
from .qdrant_write_gateway import QdrantMutationRoute
from .qdrant_write_gateway_sidecar import (
    MAX_GATEWAY_REQUEST_BYTES,
    GatewayHttpResponse,
    KubernetesBoundTokenVerifier,
    QdrantGatewayApplication,
    QdrantGatewayReadinessContract,
    QdrantGatewayService,
    QdrantGatewaySubjectPolicy,
    QdrantGatewayWorkloadBinding,
)


QDRANT_GATEWAY_COMMAND = ("neurons-qdrant-write-gateway",)
QDRANT_GATEWAY_BIND_HOST = "127.0.0.1"
QDRANT_GATEWAY_PORT = 8443
QDRANT_GATEWAY_READINESS_PATH = "/readyz"
QDRANT_GATEWAY_SOURCE_VALUES = tuple(
    source.value for source in ACTIVE_QDRANT_MUTATION_SOURCES
)
QDRANT_GATEWAY_REQUIRED_ENV = (
    "NEURONS_QDRANT_GATEWAY_GENERATION",
    "NEURONS_QDRANT_GATEWAY_PRODUCT_COLLECTIONS",
    "NEURONS_QDRANT_GATEWAY_MARKER_COLLECTION",
    "NEURONS_QDRANT_GATEWAY_MARKER_METADATA_POINT_ID",
    "NEURONS_QDRANT_GATEWAY_ACTIVATION_ANCHOR",
    "NEURONS_QDRANT_GATEWAY_PRODUCT_COLLECTION_SCHEMA_HASHES",
    "NEURONS_QDRANT_GATEWAY_SUBJECT_BINDINGS",
    "NEURONS_QDRANT_GATEWAY_PRODUCT_URL",
    "NEURONS_QDRANT_GATEWAY_MARKER_URL",
    "NEURONS_QDRANT_GATEWAY_PRODUCT_API_KEY_FILE",
    "NEURONS_QDRANT_GATEWAY_MARKER_API_KEY_FILE",
    "NEURONS_QDRANT_GATEWAY_KUBE_API_SERVER",
    "NEURONS_QDRANT_GATEWAY_KUBE_TOKEN_FILE",
    "NEURONS_QDRANT_GATEWAY_KUBE_CA_FILE",
    "NEURONS_QDRANT_GATEWAY_TLS_CERT_FILE",
    "NEURONS_QDRANT_GATEWAY_TLS_KEY_FILE",
)


class QdrantGatewayServerConfigurationError(RuntimeError):
    """Public-safe startup failure; configuration values are never included."""


def build_gateway_application_from_environment(
    environ: Mapping[str, str],
    *,
    qdrant_client_factory: Callable[..., object] | None = None,
    token_review_transport: object | None = None,
) -> QdrantGatewayApplication:
    try:
        generation = _positive_integer(
            environ.get("NEURONS_QDRANT_GATEWAY_GENERATION")
        )
        product_collections = _string_list(
            environ.get("NEURONS_QDRANT_GATEWAY_PRODUCT_COLLECTIONS")
        )
        marker_collection = _bounded_text(
            environ.get("NEURONS_QDRANT_GATEWAY_MARKER_COLLECTION")
        )
        activation_anchor = _activation_anchor(
            environ.get("NEURONS_QDRANT_GATEWAY_ACTIVATION_ANCHOR")
        )
        if (
            activation_anchor.generation != generation
            or activation_anchor.marker_collection != marker_collection
        ):
            raise ValueError
        coverage = build_qdrant_coverage_manifest_from_activation_anchor(
            activation_anchor
        )
        readiness_contract = QdrantGatewayReadinessContract(
            metadata_point_id=_bounded_text(
                environ.get("NEURONS_QDRANT_GATEWAY_MARKER_METADATA_POINT_ID")
            ),
            activation_hash=activation_anchor.activation_hash,
            coverage_hash=coverage.coverage_hash,
            previous_generation_hash=activation_anchor.previous_generation_hash,
            product_collection_schema_hashes=_product_schema_hashes(
                environ.get(
                    "NEURONS_QDRANT_GATEWAY_PRODUCT_COLLECTION_SCHEMA_HASHES"
                ),
                product_collections=product_collections,
            ),
        )
        subject_policy = _subject_policy(
            environ.get("NEURONS_QDRANT_GATEWAY_SUBJECT_BINDINGS")
        )
        _validate_subject_policy_activation(subject_policy, activation_anchor)
        product_url = _https_base_url(
            environ.get("NEURONS_QDRANT_GATEWAY_PRODUCT_URL")
        )
        marker_url = _https_base_url(
            environ.get("NEURONS_QDRANT_GATEWAY_MARKER_URL")
        )
        product_key = _read_secret(
            environ.get("NEURONS_QDRANT_GATEWAY_PRODUCT_API_KEY_FILE")
        )
        marker_key = _read_secret(
            environ.get("NEURONS_QDRANT_GATEWAY_MARKER_API_KEY_FILE")
        )
        if product_key == marker_key:
            raise ValueError
        if qdrant_client_factory is None:
            from qdrant_client import QdrantClient

            qdrant_client_factory = QdrantClient
        product_client = qdrant_client_factory(
            url=product_url,
            api_key=product_key,
            timeout=5,
            prefer_grpc=False,
            trust_env=False,
            follow_redirects=False,
        )
        marker_client = qdrant_client_factory(
            url=marker_url,
            api_key=marker_key,
            timeout=5,
            prefer_grpc=False,
            trust_env=False,
            follow_redirects=False,
        )
        if product_client is marker_client:
            raise ValueError
        reviewer = KubernetesTokenReviewClient(
            api_server=_https_base_url(
                environ.get("NEURONS_QDRANT_GATEWAY_KUBE_API_SERVER")
            ),
            caller_token_path=_required_path(
                environ.get("NEURONS_QDRANT_GATEWAY_KUBE_TOKEN_FILE")
            ),
            ca_path=_required_path(
                environ.get("NEURONS_QDRANT_GATEWAY_KUBE_CA_FILE")
            ),
            transport=token_review_transport,  # type: ignore[arg-type]
        )
        policy = QdrantCollectionPolicy(
            product_collections=product_collections,
            marker_collection=marker_collection,
        )
        return QdrantGatewayApplication(
            service=QdrantGatewayService(
                product_client=product_client,
                marker_client=marker_client,
                policy=policy,
                generation=generation,
                readiness_contract=readiness_contract,
            ),
            token_verifier=KubernetesBoundTokenVerifier(token_reviewer=reviewer),
            subject_policy=subject_policy,
        )
    except Exception:
        raise QdrantGatewayServerConfigurationError(
            "qdrant gateway configuration invalid"
        ) from None


def serve_qdrant_gateway(
    *,
    application: QdrantGatewayApplication,
    bind_host: str,
    port: int,
    cert_path: str | Path,
    key_path: str | Path,
    server_factory: Callable[..., Any] = ThreadingHTTPServer,
) -> None:
    """Serve only bounded POST requests over TLS; logs contain no request data."""

    if bind_host != QDRANT_GATEWAY_BIND_HOST or port != QDRANT_GATEWAY_PORT:
        raise QdrantGatewayServerConfigurationError(
            "qdrant gateway listen address invalid"
        )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            try:
                lengths = self.headers.get_all("Content-Length", failobj=[])
                if len(lengths) != 1:
                    raise ValueError
                length = int(lengths[0])
                if not 0 <= length <= MAX_GATEWAY_REQUEST_BYTES:
                    response = _fixed_response(413, "request_too_large")
                else:
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError
                    headers: dict[str, str] = {}
                    for name in ("authorization", "content-type"):
                        values = self.headers.get_all(name, failobj=[])
                        if len(values) == 1:
                            headers[name] = values[0]
                    response = application.handle(
                        path=self.path,
                        headers=headers,
                        body=body,
                    )
            except Exception:
                response = _fixed_response(400, "request_invalid")
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self) -> None:  # noqa: N802
            response = (
                application.readiness()
                if self.path == QDRANT_GATEWAY_READINESS_PATH
                else _fixed_response(404, "route_not_found")
            )
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, format: str, *args: object) -> None:
            return

    context = build_qdrant_gateway_tls_context(
        cert_path=cert_path,
        key_path=key_path,
    )
    server = server_factory((bind_host, port), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    application_builder: Callable[[Mapping[str, str]], object] = (
        build_gateway_application_from_environment
    ),
    server_runner: Callable[..., None] = serve_qdrant_gateway,
) -> int:
    if list(argv or ()):
        raise QdrantGatewayServerConfigurationError(
            "qdrant gateway arguments invalid"
        )
    selected = os.environ if environ is None else environ
    application = application_builder(selected)
    server_runner(
        application=application,
        bind_host=QDRANT_GATEWAY_BIND_HOST,
        port=QDRANT_GATEWAY_PORT,
        cert_path=_required_path(
            selected.get("NEURONS_QDRANT_GATEWAY_TLS_CERT_FILE")
        ),
        key_path=_required_path(
            selected.get("NEURONS_QDRANT_GATEWAY_TLS_KEY_FILE")
        ),
    )
    return 0


def _subject_policy(raw: object) -> QdrantGatewaySubjectPolicy:
    value = json.loads(_bounded_text(raw))
    if not isinstance(value, list) or not value:
        raise ValueError
    bindings: list[QdrantGatewayWorkloadBinding] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "subject",
            "sources",
            "workload_ref_hash",
            "route_set_hash",
        }:
            raise ValueError
        bindings.append(
            QdrantGatewayWorkloadBinding(
                subject=_bounded_text(item["subject"]),
                sources=tuple(
                    QdrantMutationSource(source)
                    for source in item["sources"]
                ),
                workload_ref_hash=_bounded_text(item["workload_ref_hash"]),
                route_set_hash=_bounded_text(item["route_set_hash"]),
            )
        )
    if {
        source for binding in bindings for source in binding.sources
    } != set(ACTIVE_QDRANT_MUTATION_SOURCES):
        raise ValueError
    return QdrantGatewaySubjectPolicy(bindings=tuple(bindings))


def _activation_anchor(raw: object):
    value = json.loads(_bounded_text(raw))
    expected_keys = {
        "generation",
        "marker_collection",
        "rendered_inventory",
        "previous_generation_hash",
        "auth_boundary_status",
        "network_policy_status",
        "direct_write_credentials_zero",
        "read_endpoint_write_denied_status",
        "activation_hash",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError
    inventory = value["rendered_inventory"]
    if not isinstance(inventory, list):
        raise ValueError
    item_keys = {
        "source",
        "route",
        "writer_ref_hash",
        "active_caller",
        "workload_ref_hash",
        "image_ref_hash",
        "network_policy_ref_hash",
        "route_set_hash",
    }
    rendered: list[RenderedQdrantWriter] = []
    for item in inventory:
        if not isinstance(item, dict) or set(item) != item_keys:
            raise ValueError
        rendered.append(
            RenderedQdrantWriter(
                source=QdrantMutationSource(item["source"]),
                route=QdrantMutationRoute(item["route"]),
                writer_ref_hash=item["writer_ref_hash"],
                active_caller=item["active_caller"],
                workload_ref_hash=item["workload_ref_hash"],
                image_ref_hash=item["image_ref_hash"],
                network_policy_ref_hash=item["network_policy_ref_hash"],
                route_set_hash=item["route_set_hash"],
            )
        )
    return build_qdrant_coverage_activation_anchor(
        generation=value["generation"],
        marker_collection=value["marker_collection"],
        rendered_inventory=tuple(rendered),
        previous_generation_hash=value["previous_generation_hash"],
        auth_boundary_status=value["auth_boundary_status"],
        network_policy_status=value["network_policy_status"],
        direct_write_credentials_zero=value["direct_write_credentials_zero"],
        read_endpoint_write_denied_status=value[
            "read_endpoint_write_denied_status"
        ],
        activation_hash=value["activation_hash"],
    )


def _validate_subject_policy_activation(
    policy: QdrantGatewaySubjectPolicy,
    activation_anchor: object,
) -> None:
    inventory = getattr(activation_anchor, "rendered_inventory", None)
    if not isinstance(inventory, tuple):
        raise ValueError
    active = {item.source: item for item in inventory if item.active_caller}
    workload_subjects: dict[str, str] = {}
    for binding in policy.bindings:
        prior_subject = workload_subjects.setdefault(
            binding.workload_ref_hash,
            binding.subject,
        )
        if prior_subject != binding.subject:
            raise ValueError
        for source in binding.sources:
            rendered = active.get(source)
            if (
                rendered is None
                or rendered.workload_ref_hash != binding.workload_ref_hash
                or rendered.route_set_hash != binding.route_set_hash
            ):
                raise ValueError


def _string_list(raw: object) -> tuple[str, ...]:
    value = json.loads(_bounded_text(raw))
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 64
        or len(value) != len(set(value))
    ):
        raise ValueError
    return tuple(_bounded_text(item) for item in value)


def _product_schema_hashes(
    raw: object,
    *,
    product_collections: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    value = json.loads(_bounded_text(raw))
    if not isinstance(value, list) or len(value) != len(product_collections):
        raise ValueError
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"collection", "schema_hash"}:
            raise ValueError
        result.append(
            (_bounded_text(item["collection"]), _sha256(item["schema_hash"]))
        )
    if tuple(name for name, _ in result) != product_collections:
        raise ValueError
    return tuple(result)


def _positive_integer(value: object) -> int:
    parsed = int(value)  # type: ignore[arg-type]
    if str(parsed) != value or not 0 < parsed < 2**63:
        raise ValueError
    return parsed


def _https_base_url(value: object) -> str:
    raw = _bounded_text(value)
    parsed = urllib.parse.urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError
    return raw.rstrip("/")


def _bounded_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16_384
        or any(character < " " or character == "\x7f" for character in value)
    ):
        raise ValueError
    return value


def _sha256(value: object) -> str:
    parsed = _bounded_text(value)
    if len(parsed) != 64 or any(
        character not in "0123456789abcdef" for character in parsed
    ):
        raise ValueError
    return parsed


def _required_path(value: object) -> str:
    return _bounded_text(value)


def _read_secret(value: object) -> str:
    return read_projected_qdrant_api_key(_required_path(value))


def _fixed_response(status_code: int, status: str) -> GatewayHttpResponse:
    return GatewayHttpResponse(
        status_code=status_code,
        body=json.dumps(
            {
                "schema_version": "qdrant_write_gateway_response.v1",
                "status": status,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "QDRANT_GATEWAY_BIND_HOST",
    "QDRANT_GATEWAY_COMMAND",
    "QDRANT_GATEWAY_PORT",
    "QDRANT_GATEWAY_READINESS_PATH",
    "QDRANT_GATEWAY_REQUIRED_ENV",
    "QDRANT_GATEWAY_SOURCE_VALUES",
    "QdrantGatewayServerConfigurationError",
    "build_gateway_application_from_environment",
    "main",
    "serve_qdrant_gateway",
]

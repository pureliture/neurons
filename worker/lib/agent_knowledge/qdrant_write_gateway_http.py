"""Fail-closed TLS transports for the Qdrant gateway and Kubernetes TokenReview."""

from __future__ import annotations

import json
import os
import re
import ssl
import stat
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .qdrant_write_gateway_runtime import (
    ACTIVE_QDRANT_MUTATION_SOURCES,
    QdrantMutationSource,
)
from .qdrant_write_gateway_sidecar import (
    MAX_GATEWAY_REQUEST_BYTES,
    MAX_GATEWAY_TOKEN_BYTES,
    QDRANT_WRITE_GATEWAY_AUDIENCE,
)


QDRANT_TOKEN_REVIEW_PATH = "/apis/authentication.k8s.io/v1/tokenreviews"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COLLECTION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_HTTPS_DNS_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_MAX_RESPONSE_BYTES = 131_072
QDRANT_GATEWAY_HTTPS_PORT = 8443
QDRANT_READ_HTTPS_PORT = 6333


class GatewayTlsTransportError(RuntimeError):
    """Fixed public-safe transport failure with no URL/token/body detail."""


class JsonTlsTransport(Protocol):
    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        ca_path: Path,
        timeout_seconds: int,
    ) -> bytes: ...


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class StrictTlsJsonTransport:
    """One-attempt HTTPS POST with no env proxy and no redirect following."""

    def __init__(
        self,
        *,
        ssl_context_factory: Callable[..., object] = ssl.create_default_context,
        opener_factory: Callable[..., object] = urllib.request.build_opener,
    ) -> None:
        self._ssl_context_factory = ssl_context_factory
        self._opener_factory = opener_factory

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        ca_path: Path,
        timeout_seconds: int,
    ) -> bytes:
        try:
            _validated_https_url(url, allow_path=True)
            if (
                not isinstance(body, bytes)
                or len(body) > MAX_GATEWAY_REQUEST_BYTES
                or type(timeout_seconds) is not int
                or not 1 <= timeout_seconds <= 30
            ):
                raise ValueError
            context = self._ssl_context_factory(cafile=str(ca_path))
            opener = self._opener_factory(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                _RejectRedirectHandler(),
            )
            request = urllib.request.Request(
                url,
                data=body,
                headers=dict(headers),
                method="POST",
            )
            response = opener.open(request, timeout=timeout_seconds)
            try:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError
            return raw
        except Exception:
            raise GatewayTlsTransportError("gateway_tls_request_failed") from None


class KubernetesTokenReviewClient:
    """TokenReview client authenticated by the sidecar's projected token."""

    def __init__(
        self,
        *,
        api_server: str,
        caller_token_path: str | Path,
        ca_path: str | Path,
        transport: JsonTlsTransport | None = None,
    ) -> None:
        self._api_server = _validated_https_url(api_server, allow_path=False)
        self._caller_token_path = Path(caller_token_path)
        self._ca_path = Path(ca_path)
        self._transport = transport or StrictTlsJsonTransport()

    def review(self, *, token: str, audience: str) -> Mapping[str, object]:
        if audience != QDRANT_WRITE_GATEWAY_AUDIENCE:
            raise GatewayTlsTransportError("tokenreview_audience_invalid")
        _validated_token(token)
        caller_token = _read_secret(self._caller_token_path)
        body = json.dumps(
            {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {
                    "audiences": [QDRANT_WRITE_GATEWAY_AUDIENCE],
                    "token": token,
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            raw = self._transport.post_json(
                url=self._api_server + QDRANT_TOKEN_REVIEW_PATH,
                headers={
                    "accept": "application/json",
                    "authorization": f"Bearer {caller_token}",
                    "content-type": "application/json",
                },
                body=body,
                ca_path=self._ca_path,
                timeout_seconds=5,
            )
            value = json.loads(raw, object_pairs_hook=_strict_object)
            if not isinstance(value, Mapping):
                raise ValueError
            return value
        except Exception:
            raise GatewayTlsTransportError("tokenreview_failed") from None


@dataclass(frozen=True)
class RemoteGatewayWriteReceipt:
    operation_ref: str
    status: str = "succeeded"

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.operation_ref) or self.status != "succeeded":
            raise ValueError("remote_gateway_receipt_invalid")


class RemoteQdrantGatewayTransport:
    """Writer-side transport with no Qdrant write client or Qdrant credential."""

    def __init__(
        self,
        *,
        endpoint: str,
        source: QdrantMutationSource,
        generation: int,
        collection_name: str,
        token_path: str | Path,
        ca_path: str | Path,
        transport: JsonTlsTransport | None = None,
    ) -> None:
        try:
            self._endpoint = _validated_https_url(
                endpoint,
                allow_path=False,
                allowed_ports=frozenset({QDRANT_GATEWAY_HTTPS_PORT}),
            )
        except Exception:
            raise ValueError("gateway_endpoint_invalid") from None
        if source not in ACTIVE_QDRANT_MUTATION_SOURCES:
            raise ValueError("gateway_source_invalid")
        if type(generation) is not int or not 0 < generation < 2**63:
            raise ValueError("gateway_generation_invalid")
        if not isinstance(collection_name, str) or not _COLLECTION_RE.fullmatch(
            collection_name
        ):
            raise ValueError("gateway_collection_invalid")
        self._source = source
        self._generation = generation
        self._collection_name = collection_name
        self._token_path = Path(token_path)
        self._ca_path = Path(ca_path)
        self._transport = transport or StrictTlsJsonTransport()

    @property
    def source(self) -> QdrantMutationSource:
        return self._source

    def upsert_points(self, *, points: Sequence[Any]) -> RemoteGatewayWriteReceipt:
        if isinstance(points, (str, bytes, bytearray)) or not isinstance(
            points, Sequence
        ):
            raise ValueError("gateway_points_invalid")
        serialized = [_serialize_point(point) for point in points]
        if not 1 <= len(serialized) <= 256:
            raise ValueError("gateway_points_invalid")
        return self._mutate(
            path="/v1/points/upsert",
            payload={
                "schema_version": "qdrant_write_gateway_upsert.v1",
                "generation": self._generation,
                "source": self._source.value,
                "collection": self._collection_name,
                "points": serialized,
            },
        )

    def delete_points(
        self, *, points_selector: Any, item_count: int
    ) -> RemoteGatewayWriteReceipt:
        point_ids = _selector_ids(points_selector)
        if type(item_count) is not int or item_count != len(point_ids):
            raise ValueError("gateway_point_selector_invalid")
        if not 1 <= item_count <= 256:
            raise ValueError("gateway_point_selector_invalid")
        return self._mutate(
            path="/v1/points/delete",
            payload={
                "schema_version": "qdrant_write_gateway_delete.v1",
                "generation": self._generation,
                "source": self._source.value,
                "collection": self._collection_name,
                "point_ids": point_ids,
            },
        )

    def _mutate(
        self, *, path: str, payload: Mapping[str, object]
    ) -> RemoteGatewayWriteReceipt:
        try:
            token = _read_secret(self._token_path)
            body = json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            raw = self._transport.post_json(
                url=self._endpoint + path,
                headers={
                    "authorization": f"Bearer {token}",
                    "content-type": "application/json",
                },
                body=body,
                ca_path=self._ca_path,
                timeout_seconds=5,
            )
            value = json.loads(raw, object_pairs_hook=_strict_object)
            if (
                not isinstance(value, dict)
                or set(value) != {"schema_version", "status", "operation_ref"}
                or value["schema_version"] != "qdrant_write_gateway_response.v1"
                or value["status"] != "succeeded"
                or not isinstance(value["operation_ref"], str)
            ):
                raise ValueError
            return RemoteGatewayWriteReceipt(operation_ref=value["operation_ref"])
        except Exception:
            raise GatewayTlsTransportError("remote_gateway_mutation_failed") from None


def build_qdrant_gateway_tls_context(
    *,
    cert_path: str | Path,
    key_path: str | Path,
    context_factory: Callable[..., object] = ssl.SSLContext,
) -> object:
    cert = _validated_regular_file(Path(cert_path))
    key = _validated_regular_file(Path(key_path))
    try:
        context = context_factory(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        return context
    except Exception:
        raise GatewayTlsTransportError("gateway_tls_configuration_invalid") from None


def validate_qdrant_read_base_url(value: object) -> str:
    """Return one canonical HTTPS read authority on the fixed TLS port."""

    return _validated_https_url(
        value,
        allow_path=False,
        allowed_ports=frozenset({QDRANT_READ_HTTPS_PORT}),
    )


def _validated_https_url(
    value: object,
    *,
    allow_path: bool,
    allowed_ports: frozenset[int | None] | None = None,
) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2_048:
        raise ValueError
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        raise ValueError
    if "\\" in value:
        raise ValueError
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError from None
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (not allow_path and parsed.path not in {"", "/"})
        or "%" in parsed.netloc
        or hostname.endswith(".")
        or any(
            not _HTTPS_DNS_LABEL_RE.fullmatch(label)
            for label in hostname.split(".")
        )
        or (allowed_ports is not None and port not in allowed_ports)
    ):
        raise ValueError
    canonical_authority = hostname if port is None else f"{hostname}:{port}"
    if parsed.netloc != canonical_authority:
        raise ValueError
    return value.rstrip("/")


def _validated_regular_file(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError
    return resolved


def _read_secret(path: Path) -> str:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError
        if not 0 < metadata.st_size <= MAX_GATEWAY_TOKEN_BYTES:
            raise ValueError
        chunks: list[bytes] = []
        length = 0
        while True:
            chunk = os.read(descriptor, MAX_GATEWAY_TOKEN_BYTES + 1 - length)
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > MAX_GATEWAY_TOKEN_BYTES:
                raise ValueError
        value = b"".join(chunks).decode("utf-8").strip()
        return _validated_token(value)
    except Exception:
        raise GatewayTlsTransportError("gateway_credential_unavailable") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def read_projected_qdrant_api_key(path: str | Path) -> str:
    """Read one bounded projected Qdrant key without exposing path or value."""

    return _read_secret(Path(path))


def _validated_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode("utf-8")) <= MAX_GATEWAY_TOKEN_BYTES
        or any(character <= " " or character == "\x7f" for character in value)
    ):
        raise ValueError
    return value


def _serialize_point(point: object) -> dict[str, object]:
    if isinstance(point, Mapping):
        value = dict(point)
    else:
        dump = getattr(point, "model_dump", None)
        if callable(dump):
            value = dump(mode="json", exclude_none=True)
        else:
            value = {
                "id": getattr(point, "id", None),
                "vector": getattr(point, "vector", None),
                "payload": getattr(point, "payload", None),
            }
    if set(value) != {"id", "vector", "payload"}:
        raise ValueError("gateway_point_schema_invalid")
    return value


def _selector_ids(value: object) -> list[str | int]:
    if isinstance(value, (str, bytes, bytearray)):
        raise ValueError("gateway_point_selector_invalid")
    if isinstance(value, Sequence):
        point_ids = list(value)
    else:
        point_ids = list(getattr(value, "points", []))
    if any(
        isinstance(point_id, bool)
        or not isinstance(point_id, (str, int))
        or point_id == ""
        for point_id in point_ids
    ):
        raise ValueError("gateway_point_selector_invalid")
    return point_ids


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


__all__ = [
    "QDRANT_TOKEN_REVIEW_PATH",
    "GatewayTlsTransportError",
    "KubernetesTokenReviewClient",
    "RemoteGatewayWriteReceipt",
    "RemoteQdrantGatewayTransport",
    "StrictTlsJsonTransport",
    "validate_qdrant_read_base_url",
    "build_qdrant_gateway_tls_context",
    "read_projected_qdrant_api_key",
]

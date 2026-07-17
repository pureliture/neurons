"""Strict in-pod Qdrant write-gateway application and TokenReview boundary.

Writer containers send one of two typed HTTPS commands to this sidecar.  Only
the sidecar owns Qdrant write clients.  Parsing completes before TokenReview,
and TokenReview completes before any marker or product mutation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .qdrant_write_gateway import (
    QdrantGatewayContractError,
    QdrantGatewayMarkerError,
    QdrantGatewayProductError,
)
from .qdrant_write_gateway_runtime import (
    ACTIVE_QDRANT_MUTATION_SOURCES,
    AuthenticatedQdrantSubject,
    ExactRouteAuthorizer,
    QDRANT_EXACT_MARKER_METADATA_KEYS,
    QDRANT_EXACT_MARKER_METADATA_SCHEMA,
    QdrantCollectionPolicy,
    QdrantMutationMarkerStore,
    QdrantMutationSource,
    build_qdrant_gateway_transport,
    validate_qdrant_marker_collection_topology,
)


QDRANT_WRITE_GATEWAY_AUDIENCE = "neurons-qdrant-write-gateway"
MAX_GATEWAY_REQUEST_BYTES = 1_048_576
MAX_GATEWAY_TOKEN_BYTES = 16_384
_COLLECTION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class QdrantGatewayTokenReviewer(Protocol):
    def review(self, *, token: str, audience: str) -> Mapping[str, object]: ...


class _RequestContractError(ValueError):
    def __init__(self, status_code: int, status: str) -> None:
        super().__init__(status)
        self.status_code = status_code
        self.status = status


class QdrantGatewayActivationError(RuntimeError):
    """Fixed public-safe failure when the exact activation state is not ready."""


@dataclass(frozen=True)
class GatewayUpsertCommand:
    generation: int
    source: QdrantMutationSource
    collection: str
    points: tuple[Mapping[str, object], ...] = field(repr=False)


@dataclass(frozen=True)
class GatewayDeleteCommand:
    generation: int
    source: QdrantMutationSource
    collection: str
    point_ids: tuple[str | int, ...] = field(repr=False)


GatewayCommand = GatewayUpsertCommand | GatewayDeleteCommand


@dataclass(frozen=True)
class AuthenticatedGatewayIdentity:
    subject_ref_hash: str
    pod_ref_hash: str
    workload_ref_hash: str
    route_set_hash: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.subject_ref_hash) or not _SHA256_RE.fullmatch(
            self.pod_ref_hash
        ) or not _SHA256_RE.fullmatch(
            self.workload_ref_hash
        ) or not _SHA256_RE.fullmatch(
            self.route_set_hash
        ):
            raise ValueError("gateway_identity_invalid")


@dataclass(frozen=True)
class GatewayHttpResponse:
    status_code: int
    body: bytes = field(repr=False)
    content_type: str = "application/json"


def build_qdrant_gateway_collection_schema_hash(
    collection_info: object,
) -> str:
    try:
        config = _required_member(collection_info, "config")
        params = _required_member(config, "params")
        vectors = _required_member(params, "vectors")
        size = _point_member(vectors, "size")
        distance = _point_member(vectors, "distance")
        normalized_distance = str(
            getattr(distance, "value", distance)
        ).casefold().rsplit(".", 1)[-1]
        if (
            type(size) is not int
            or size < 1
            or normalized_distance not in {"cosine", "dot", "euclid", "manhattan"}
        ):
            raise ValueError
        fixed_params: dict[str, int] = {}
        for field_name in (
            "shard_number",
            "replication_factor",
            "write_consistency_factor",
        ):
            value = _point_member(params, field_name)
            if type(value) is not int or value < 1:
                raise ValueError
            fixed_params[field_name] = value
        payload_schema = _required_member(collection_info, "payload_schema")
        normalized_schema: dict[str, str] = {}
        for field_name, field_config in payload_schema.items():
            if not isinstance(field_name, str) or not field_name:
                raise ValueError
            data_type = _point_member(field_config, "data_type")
            normalized_type = str(
                getattr(data_type, "value", data_type)
            ).casefold().rsplit(".", 1)[-1]
            if not normalized_type:
                raise ValueError
            normalized_schema[field_name] = normalized_type
        encoded = json.dumps(
            {
                "params": fixed_params,
                "payload_schema": normalized_schema,
                "schema_version": "qdrant_collection_schema_anchor.v1",
                "vectors": {"distance": normalized_distance, "size": size},
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except Exception:
        raise ValueError("gateway_collection_schema_invalid") from None
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class QdrantGatewayReadinessContract:
    metadata_point_id: str
    activation_hash: str
    coverage_hash: str
    previous_generation_hash: str
    product_collection_schema_hashes: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.metadata_point_id)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("gateway_readiness_metadata_point_invalid") from None
        if str(parsed) != self.metadata_point_id:
            raise ValueError("gateway_readiness_metadata_point_invalid")
        if any(
            not _SHA256_RE.fullmatch(value)
            for value in (
                self.activation_hash,
                self.coverage_hash,
                self.previous_generation_hash,
            )
        ):
            raise ValueError("gateway_readiness_hash_invalid")
        if (
            type(self.product_collection_schema_hashes) is not tuple
            or not self.product_collection_schema_hashes
            or len({name for name, _ in self.product_collection_schema_hashes})
            != len(self.product_collection_schema_hashes)
        ):
            raise ValueError("gateway_readiness_product_schema_invalid")
        for collection_name, schema_hash in self.product_collection_schema_hashes:
            if (
                not isinstance(collection_name, str)
                or not _COLLECTION_RE.fullmatch(collection_name)
                or not _SHA256_RE.fullmatch(schema_hash)
            ):
                raise ValueError("gateway_readiness_product_schema_invalid")


def build_qdrant_gateway_route_set_hash(
    sources: tuple[QdrantMutationSource, ...],
) -> str:
    if (
        type(sources) is not tuple
        or not sources
        or len(set(sources)) != len(sources)
        or any(source not in ACTIVE_QDRANT_MUTATION_SOURCES for source in sources)
    ):
        raise ValueError("gateway_route_set_invalid")
    encoded = json.dumps(
        {
            "schema_version": "qdrant_gateway_route_set.v1",
            "sources": sorted(source.value for source in sources),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class QdrantGatewayWorkloadBinding:
    subject: str = field(repr=False)
    sources: tuple[QdrantMutationSource, ...]
    workload_ref_hash: str
    route_set_hash: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.subject, str)
            or not self.subject.startswith("system:serviceaccount:")
            or len(self.subject) > 512
            or any(
                character <= " " or character == "\x7f"
                for character in self.subject
            )
        ):
            raise ValueError("gateway_workload_subject_invalid")
        expected_route_set_hash = build_qdrant_gateway_route_set_hash(self.sources)
        if (
            not _SHA256_RE.fullmatch(self.workload_ref_hash)
            or self.route_set_hash != expected_route_set_hash
        ):
            raise ValueError("gateway_workload_binding_invalid")


@dataclass(frozen=True)
class QdrantGatewaySubjectPolicy:
    bindings: tuple[QdrantGatewayWorkloadBinding, ...]

    def __post_init__(self) -> None:
        if type(self.bindings) is not tuple or not self.bindings:
            raise ValueError("gateway_subject_policy_missing")
        sources: set[QdrantMutationSource] = set()
        subjects: set[str] = set()
        for binding in self.bindings:
            if not isinstance(binding, QdrantGatewayWorkloadBinding):
                raise ValueError("gateway_subject_policy_invalid")
            if binding.subject in subjects or sources.intersection(binding.sources):
                raise ValueError("gateway_subject_policy_duplicate")
            subjects.add(binding.subject)
            sources.update(binding.sources)

    def binding_for_source(
        self, source: QdrantMutationSource
    ) -> QdrantGatewayWorkloadBinding:
        for binding in self.bindings:
            if source in binding.sources:
                return binding
        raise ValueError("gateway_subject_not_allowed")


class KubernetesBoundTokenVerifier:
    """Trust TokenReview once, then bind its result to exp/aud/sub/Pod claims."""

    def __init__(
        self,
        *,
        token_reviewer: QdrantGatewayTokenReviewer,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not callable(getattr(token_reviewer, "review", None)) or not callable(now):
            raise ValueError("gateway_token_verifier_invalid")
        self._token_reviewer = token_reviewer
        self._now = now

    def verify(
        self,
        *,
        token: str,
        source: QdrantMutationSource,
        subject_policy: QdrantGatewaySubjectPolicy,
    ) -> AuthenticatedGatewayIdentity:
        try:
            binding = subject_policy.binding_for_source(source)
            expected_subject = binding.subject
            _validate_token_text(token)
            # Exactly one reviewer call is permitted for each parsed request.
            review = self._token_reviewer.review(
                token=token,
                audience=QDRANT_WRITE_GATEWAY_AUDIENCE,
            )
            status = _mapping_member(review, "status")
            if (
                review.get("apiVersion") != "authentication.k8s.io/v1"
                or review.get("kind") != "TokenReview"
                or status.get("authenticated") is not True
                or status.get("audiences") != [QDRANT_WRITE_GATEWAY_AUDIENCE]
            ):
                raise ValueError
            user = _mapping_member(status, "user")
            if user.get("username") != expected_subject:
                raise ValueError
            extra = _mapping_member(user, "extra")
            pod_name = _single_text(
                extra.get("authentication.kubernetes.io/pod-name")
            )
            pod_uid = _single_text(
                extra.get("authentication.kubernetes.io/pod-uid")
            )
            claims = _decode_jwt_claims(token)
            if (
                claims.get("sub") != expected_subject
                or claims.get("aud") != [QDRANT_WRITE_GATEWAY_AUDIENCE]
                or type(claims.get("exp")) is not int
                or int(claims["exp"]) <= self._now()
            ):
                raise ValueError
            kubernetes = _mapping_member(claims, "kubernetes.io")
            pod = _mapping_member(kubernetes, "pod")
            if pod.get("name") != pod_name or pod.get("uid") != pod_uid:
                raise ValueError
            pod_ref_hash = hashlib.sha256(
                f"{pod_name}\n{pod_uid}".encode("utf-8")
            ).hexdigest()
            return AuthenticatedGatewayIdentity(
                subject_ref_hash=hashlib.sha256(
                    expected_subject.encode("utf-8")
                ).hexdigest(),
                pod_ref_hash=pod_ref_hash,
                workload_ref_hash=binding.workload_ref_hash,
                route_set_hash=binding.route_set_hash,
            )
        except Exception:
            raise ValueError("gateway_token_invalid") from None


class QdrantGatewayService:
    """Central mutation service owning distinct product and marker clients."""

    def __init__(
        self,
        *,
        product_client: object,
        marker_client: object,
        policy: QdrantCollectionPolicy,
        generation: int,
        readiness_contract: QdrantGatewayReadinessContract | None = None,
    ) -> None:
        if product_client is marker_client:
            raise ValueError("gateway_qdrant_client_separation_required")
        if type(generation) is not int or generation < 1:
            raise ValueError("gateway_generation_invalid")
        self._product_client = product_client
        self._marker_client = marker_client
        self._policy = policy
        self._generation = generation
        if readiness_contract is not None and not isinstance(
            readiness_contract, QdrantGatewayReadinessContract
        ):
            raise ValueError("gateway_readiness_contract_invalid")
        if readiness_contract is not None and tuple(
            name for name, _ in readiness_contract.product_collection_schema_hashes
        ) != policy.product_collections:
            raise ValueError("gateway_readiness_product_schema_invalid")
        self._readiness_contract = readiness_contract

    @property
    def generation(self) -> int:
        return self._generation

    def mutate(
        self,
        command: GatewayCommand,
        identity: AuthenticatedGatewayIdentity,
    ) -> str:
        if not isinstance(identity, AuthenticatedGatewayIdentity):
            raise QdrantGatewayContractError("gateway_identity_required")
        try:
            self._require_activation_state(require_quiescent=False)
        except Exception:
            raise QdrantGatewayActivationError("activation_not_ready")
        source = command.source
        authorizer = ExactRouteAuthorizer(
            bindings=((identity.subject_ref_hash, source, command.collection),)
        )
        transport = build_qdrant_gateway_transport(
            client=self._product_client,
            collection_name=command.collection,
            source=source,
            subject=AuthenticatedQdrantSubject(identity.subject_ref_hash),
            authorizer=authorizer,
            policy=self._policy,
            marker_store=QdrantMutationMarkerStore(
                client=self._marker_client,
                policy=self._policy,
            ),
            generation=self._generation,
            pod_ref_hash=identity.pod_ref_hash,
            workload_ref_hash=identity.workload_ref_hash,
            route_set_hash=identity.route_set_hash,
        )
        models = _models()
        if isinstance(command, GatewayUpsertCommand):
            points = [models.PointStruct(**dict(point)) for point in command.points]
            return transport.mutate_upsert_points(points=points).operation_ref
        selector = models.PointIdsList(points=list(command.point_ids))
        return transport.mutate_delete_points(
            points_selector=selector,
            item_count=len(command.point_ids),
        ).operation_ref

    def _require_activation_state(self, *, require_quiescent: bool) -> None:
        """Validate the exact activation anchor shared by mutation and readiness."""

        if type(require_quiescent) is not bool:
            raise ValueError
        contract = self._readiness_contract
        if contract is None:
            raise ValueError
        models = _models()
        for collection_name, expected_schema_hash in (
            contract.product_collection_schema_hashes
        ):
            collection_info = self._product_client.get_collection(
                collection_name=collection_name
            )
            _require_green_collection(collection_info)
            if (
                build_qdrant_gateway_collection_schema_hash(collection_info)
                != expected_schema_hash
            ):
                raise ValueError

        marker_info = self._marker_client.get_collection(
            collection_name=self._policy.marker_collection
        )
        _require_green_collection(marker_info)
        validate_qdrant_marker_collection_topology(
            marker_info,
            require_indexes=True,
        )
        if require_quiescent:
            unresolved = self._marker_client.count(
                collection_name=self._policy.marker_collection,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="record_kind",
                            match=models.MatchValue(value="event"),
                        ),
                        models.FieldCondition(
                            key="generation",
                            match=models.MatchValue(value=self._generation),
                        ),
                        models.FieldCondition(
                            key="phase",
                            match=models.MatchValue(value="start"),
                        ),
                        models.FieldCondition(
                            key="unresolved",
                            match=models.MatchValue(value=True),
                        ),
                    ]
                ),
                exact=True,
            )
            if type(getattr(unresolved, "count", None)) is not int or unresolved.count != 0:
                raise ValueError
        metadata_points = self._marker_client.retrieve(
            collection_name=self._policy.marker_collection,
            ids=[contract.metadata_point_id],
            with_payload=True,
            with_vectors=False,
            consistency=models.ReadConsistencyType.ALL,
        )
        if not isinstance(metadata_points, list) or len(metadata_points) != 1:
            raise ValueError
        metadata_point = metadata_points[0]
        if _point_member(metadata_point, "id") != contract.metadata_point_id:
            raise ValueError
        metadata = _point_member(metadata_point, "payload")
        if (
            not isinstance(metadata, Mapping)
            or set(metadata) != QDRANT_EXACT_MARKER_METADATA_KEYS
            or metadata.get("schema_version")
            != QDRANT_EXACT_MARKER_METADATA_SCHEMA
            or metadata.get("generation") != self._generation
            or metadata.get("coverage_hash") != contract.coverage_hash
            or metadata.get("coverage_status") != "complete"
            or metadata.get("bypass_count") != 0
            or metadata.get("activation_hash") != contract.activation_hash
            or metadata.get("previous_generation_hash")
            != contract.previous_generation_hash
        ):
            raise ValueError

    def ready(self) -> bool:
        """Run bounded, strong-consistency reads only; never mutate readiness state."""

        try:
            self._require_activation_state(require_quiescent=True)
            return True
        except Exception:
            return False


class QdrantGatewayApplication:
    """Pure HTTP application; an HTTPS server adapter supplies transport only."""

    def __init__(
        self,
        *,
        service: QdrantGatewayService,
        token_verifier: KubernetesBoundTokenVerifier,
        subject_policy: QdrantGatewaySubjectPolicy,
    ) -> None:
        self._service = service
        self._token_verifier = token_verifier
        self._subject_policy = subject_policy

    def handle(
        self,
        *,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> GatewayHttpResponse:
        try:
            command = _parse_command(path=path, headers=headers, body=body)
        except _RequestContractError as exc:
            return _response(exc.status_code, exc.status)
        if command.generation != self._service.generation:
            return _response(409, "activation_generation_mismatch")
        try:
            token = _bearer_token(headers)
            identity = self._token_verifier.verify(
                token=token,
                source=command.source,
                subject_policy=self._subject_policy,
            )
        except Exception:
            return _response(401, "authentication_failed")
        try:
            operation_ref = self._service.mutate(command, identity)
            return _response(200, "succeeded", operation_ref=operation_ref)
        except QdrantGatewayActivationError:
            return _response(503, "activation_not_ready")
        except QdrantGatewayProductError:
            return _response(502, "product_mutation_failed")
        except QdrantGatewayMarkerError:
            return _response(503, "marker_mutation_failed")
        except (QdrantGatewayContractError, PermissionError, ValueError):
            return _response(403, "mutation_not_allowed")
        except Exception:
            return _response(503, "gateway_unavailable")

    def readiness(self) -> GatewayHttpResponse:
        return (
            _response(200, "ready")
            if self._service.ready()
            else _response(503, "not_ready")
        )


def _parse_command(
    *, path: str, headers: Mapping[str, str], body: bytes
) -> GatewayCommand:
    if path not in {"/v1/points/upsert", "/v1/points/delete"}:
        raise _RequestContractError(404, "route_not_found")
    content_type = _header(headers, "content-type")
    if content_type != "application/json":
        raise _RequestContractError(415, "content_type_invalid")
    if not isinstance(body, bytes):
        raise _RequestContractError(400, "request_invalid")
    if len(body) > MAX_GATEWAY_REQUEST_BYTES:
        raise _RequestContractError(413, "request_too_large")
    try:
        value = json.loads(body, object_pairs_hook=_strict_object)
    except Exception:
        raise _RequestContractError(400, "request_invalid") from None
    if not isinstance(value, dict):
        raise _RequestContractError(400, "request_invalid")
    if path.endswith("/upsert"):
        if set(value) != {
            "schema_version",
            "generation",
            "source",
            "collection",
            "points",
        }:
            raise _RequestContractError(400, "request_invalid")
        if value["schema_version"] != "qdrant_write_gateway_upsert.v1":
            raise _RequestContractError(400, "request_invalid")
        source = _source(value["source"])
        collection = _collection(value["collection"])
        points = _points(value["points"])
        return GatewayUpsertCommand(
            generation=_generation(value["generation"]),
            source=source,
            collection=collection,
            points=points,
        )
    if set(value) != {
        "schema_version",
        "generation",
        "source",
        "collection",
        "point_ids",
    }:
        raise _RequestContractError(400, "request_invalid")
    if value["schema_version"] != "qdrant_write_gateway_delete.v1":
        raise _RequestContractError(400, "request_invalid")
    return GatewayDeleteCommand(
        generation=_generation(value["generation"]),
        source=_source(value["source"]),
        collection=_collection(value["collection"]),
        point_ids=_point_ids(value["point_ids"]),
    )


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _source(value: object) -> QdrantMutationSource:
    try:
        source = QdrantMutationSource(value)
    except Exception:
        raise _RequestContractError(400, "request_invalid") from None
    if source not in ACTIVE_QDRANT_MUTATION_SOURCES:
        raise _RequestContractError(400, "request_invalid")
    return source


def _generation(value: object) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise _RequestContractError(400, "request_invalid")
    return value


def _collection(value: object) -> str:
    if not isinstance(value, str) or not _COLLECTION_RE.fullmatch(value):
        raise _RequestContractError(400, "request_invalid")
    return value


def _points(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 256:
        raise _RequestContractError(400, "request_invalid")
    result: list[Mapping[str, object]] = []
    for point in value:
        if not isinstance(point, dict) or set(point) != {"id", "vector", "payload"}:
            raise _RequestContractError(400, "request_invalid")
        point_id = point["id"]
        vector = point["vector"]
        payload = point["payload"]
        if (
            isinstance(point_id, bool)
            or not isinstance(point_id, (str, int))
            or point_id == ""
            or not isinstance(vector, list)
            or not 1 <= len(vector) <= 65_536
            or any(
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(float(number))
                for number in vector
            )
            or not isinstance(payload, dict)
            or any(not isinstance(key, str) or not key for key in payload)
        ):
            raise _RequestContractError(400, "request_invalid")
        try:
            json.dumps(payload, ensure_ascii=True, allow_nan=False)
        except Exception:
            raise _RequestContractError(400, "request_invalid") from None
        result.append(point)
    return tuple(result)


def _point_ids(value: object) -> tuple[str | int, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 256:
        raise _RequestContractError(400, "request_invalid")
    if any(
        isinstance(point_id, bool)
        or not isinstance(point_id, (str, int))
        or point_id == ""
        for point_id in value
    ):
        raise _RequestContractError(400, "request_invalid")
    return tuple(value)


def _bearer_token(headers: Mapping[str, str]) -> str:
    authorization = _header(headers, "authorization")
    if not authorization.startswith("Bearer "):
        raise ValueError("gateway_token_invalid")
    token = authorization[7:]
    _validate_token_text(token)
    return token


def _header(headers: Mapping[str, str], name: str) -> str:
    values = [value for key, value in headers.items() if key.lower() == name]
    if len(values) != 1 or not isinstance(values[0], str):
        raise _RequestContractError(400, "request_invalid")
    return values[0]


def _validate_token_text(token: object) -> str:
    if (
        not isinstance(token, str)
        or not 1 <= len(token.encode("utf-8")) <= MAX_GATEWAY_TOKEN_BYTES
        or any(character <= " " or character == "\x7f" for character in token)
    ):
        raise ValueError("gateway_token_invalid")
    return token


def _decode_jwt_claims(token: str) -> Mapping[str, object]:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError
    payload = parts[1]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    claims = json.loads(decoded, object_pairs_hook=_strict_object)
    if not isinstance(claims, Mapping):
        raise ValueError
    return claims


def _mapping_member(value: object, key: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError
    member = value.get(key)
    if not isinstance(member, Mapping):
        raise ValueError
    return member


def _single_text(value: object) -> str:
    if (
        not isinstance(value, list)
        or len(value) != 1
        or not isinstance(value[0], str)
        or not value[0]
        or len(value[0]) > 512
    ):
        raise ValueError
    return value[0]


def _models() -> Any:
    try:
        from qdrant_client import models
    except ImportError as exc:  # pragma: no cover - image dependency guard
        raise RuntimeError("qdrant_client_required_for_gateway") from exc
    return models


def _point_member(value: object, name: str) -> object:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _required_member(value: object, name: str) -> Mapping[str, object]:
    member = _point_member(value, name)
    if not isinstance(member, Mapping):
        raise ValueError
    return member


def _require_green_collection(value: object) -> None:
    status = _point_member(value, "status")
    normalized = str(getattr(status, "value", status)).casefold().rsplit(".", 1)[-1]
    if normalized != "green":
        raise ValueError("gateway_collection_not_green")


def _response(
    status_code: int,
    status: str,
    *,
    operation_ref: str | None = None,
) -> GatewayHttpResponse:
    value: dict[str, object] = {
        "schema_version": "qdrant_write_gateway_response.v1",
        "status": status,
    }
    if operation_ref is not None:
        value["operation_ref"] = operation_ref
    return GatewayHttpResponse(
        status_code=status_code,
        body=json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )


__all__ = [
    "MAX_GATEWAY_REQUEST_BYTES",
    "QDRANT_WRITE_GATEWAY_AUDIENCE",
    "AuthenticatedGatewayIdentity",
    "GatewayDeleteCommand",
    "GatewayHttpResponse",
    "GatewayUpsertCommand",
    "KubernetesBoundTokenVerifier",
    "QdrantGatewayApplication",
    "QdrantGatewayReadinessContract",
    "QdrantGatewayService",
    "QdrantGatewaySubjectPolicy",
    "QdrantGatewayTokenReviewer",
    "QdrantGatewayWorkloadBinding",
    "build_qdrant_gateway_route_set_hash",
    "build_qdrant_gateway_collection_schema_hash",
]

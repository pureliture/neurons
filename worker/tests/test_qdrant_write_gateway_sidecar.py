from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from agent_knowledge.qdrant_write_gateway_runtime import (
    QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS,
    QdrantCollectionPolicy,
    QdrantMutationSource,
)
from agent_knowledge.qdrant_write_gateway_sidecar import (
    MAX_GATEWAY_REQUEST_BYTES,
    QDRANT_WRITE_GATEWAY_AUDIENCE,
    KubernetesBoundTokenVerifier,
    QdrantGatewayApplication,
    QdrantGatewayReadinessContract,
    QdrantGatewayService,
    QdrantGatewaySubjectPolicy,
    QdrantGatewayWorkloadBinding,
    build_qdrant_gateway_route_set_hash,
    build_qdrant_gateway_collection_schema_hash,
)


def _b64(value: dict[str, object]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _token(
    *,
    subject: str,
    expires_at: int = 2_000,
    pod_name: str = "writer-0",
    pod_uid: str = "pod-uid-1",
) -> str:
    return ".".join(
        (
            _b64({"alg": "RS256", "typ": "JWT"}),
            _b64(
                {
                    "aud": [QDRANT_WRITE_GATEWAY_AUDIENCE],
                    "exp": expires_at,
                    "sub": subject,
                    "kubernetes.io": {
                        "namespace": "neurons",
                        "pod": {"name": pod_name, "uid": pod_uid},
                    },
                }
            ),
            "fixture-signature",
        )
    )


class _Reviewer:
    def __init__(
        self,
        *,
        subject: str,
        pod_name: str = "writer-0",
        pod_uid: str = "pod-uid-1",
    ) -> None:
        self.subject = subject
        self.pod_name = pod_name
        self.pod_uid = pod_uid
        self.calls: list[tuple[str, str]] = []

    def review(self, *, token: str, audience: str) -> dict[str, object]:
        self.calls.append((token, audience))
        return {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenReview",
            "status": {
                "authenticated": True,
                "audiences": [QDRANT_WRITE_GATEWAY_AUDIENCE],
                "user": {
                    "username": self.subject,
                    "extra": {
                        "authentication.kubernetes.io/pod-name": [self.pod_name],
                        "authentication.kubernetes.io/pod-uid": [self.pod_uid],
                    },
                },
            },
        }


@dataclass
class _UpdateResult:
    status: str
    operation_id: int


class _QdrantClient:
    def __init__(
        self,
        *,
        fail_product: bool = False,
        product_result: object | None = None,
        shared_order: list[str] | None = None,
        client_role: str = "client",
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.points: dict[str, Any] = {}
        self.position = 0
        self.fail_product = fail_product
        self.product_result = product_result
        self.shared_order = shared_order
        self.client_role = client_role
        self.collection_info = {
            "status": "green",
            "config": {
                "params": {
                    "vectors": {"size": 64, "distance": "cosine"},
                    "shard_number": 1,
                    "replication_factor": 1,
                    "write_consistency_factor": 1,
                }
            },
            "payload_schema": {"document_kind": {"data_type": "keyword"}},
        }
        self.unresolved_count = 0

    def upsert(self, **kwargs: Any) -> _UpdateResult:
        self.calls.append(("upsert", kwargs))
        if self.shared_order is not None:
            self.shared_order.append(f"{self.client_role}:upsert")
        if self.fail_product:
            raise RuntimeError("protected-product-value")
        if self.product_result is not None:
            return self.product_result  # type: ignore[return-value]
        self.position += 1
        for point in kwargs["points"]:
            self.points[str(point.id)] = point
        return _UpdateResult("completed", self.position)

    def delete(self, **kwargs: Any) -> _UpdateResult:
        self.calls.append(("delete", kwargs))
        if self.fail_product:
            raise RuntimeError("protected-product-value")
        self.position += 1
        return _UpdateResult("completed", self.position)

    def retrieve(self, *, ids: list[str], **kwargs: Any) -> list[Any]:
        self.calls.append(("retrieve", {"ids": ids, **kwargs}))
        return [self.points[point_id] for point_id in ids if point_id in self.points]

    def get_collection(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(("get_collection", kwargs))
        return self.collection_info

    def count(self, **kwargs: Any) -> Any:
        self.calls.append(("count", kwargs))
        return SimpleNamespace(count=self.unresolved_count)


def _body(*, extra: dict[str, object] | None = None) -> bytes:
    value: dict[str, object] = {
        "schema_version": "qdrant_write_gateway_upsert.v1",
        "generation": 7,
        "source": "normal_ingest",
        "collection": "mirror",
        "points": [
            {"id": "protected-point-id", "vector": [0.1], "payload": {"safe": True}}
        ],
    }
    value.update(extra or {})
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _application(
    *,
    fail_product: bool = False,
    product_result: object | None = None,
    activation_state: str = "complete",
):
    subject = "system:serviceaccount:neurons:normal-writer"
    reviewer = _Reviewer(subject=subject)
    order: list[str] = []
    product = _QdrantClient(
        fail_product=fail_product,
        product_result=product_result,
        shared_order=order,
        client_role="product",
    )
    marker = _QdrantClient(
        shared_order=order,
        client_role="marker",
    )
    marker.collection_info = {
        "status": "green",
        "config": {
            "params": {
                "vectors": {"size": 1, "distance": "cosine"},
                "shard_number": 1,
                "replication_factor": 1,
                "write_consistency_factor": 1,
            }
        },
        "payload_schema": {
            field_name: {
                "data_type": (
                    "bool" if field_name == "unresolved" else "keyword"
                )
            }
            for field_name in QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS
        },
    }
    readiness_contract = None
    if activation_state != "contract_absent":
        metadata_point_id = "00000000-0000-4000-8000-000000000001"
        readiness_contract = QdrantGatewayReadinessContract(
            metadata_point_id=metadata_point_id,
            activation_hash="a" * 64,
            coverage_hash="c" * 64,
            previous_generation_hash="b" * 64,
            product_collection_schema_hashes=((
                "mirror",
                build_qdrant_gateway_collection_schema_hash(
                    product.collection_info
                ),
            ),),
        )
        if activation_state != "metadata_absent":
            payload = {
                "schema_version": "qdrant_exact_marker_metadata.v2",
                "generation": 7,
                "coverage_hash": "c" * 64,
                "coverage_status": "complete",
                "bypass_count": 0,
                "activation_hash": "a" * 64,
                "previous_generation_hash": "b" * 64,
            }
            if activation_state == "pending":
                payload["coverage_status"] = "pending_cutover"
            elif activation_state == "bypass":
                payload["bypass_count"] = 1
            elif activation_state == "schema_mismatch":
                payload["schema_version"] = "qdrant_exact_marker_metadata.v1"
            marker.points[metadata_point_id] = SimpleNamespace(
                id=metadata_point_id,
                payload=payload,
            )
    service = QdrantGatewayService(
        product_client=product,
        marker_client=marker,
        policy=QdrantCollectionPolicy(
            product_collections=("mirror",),
            marker_collection="mutation_markers",
        ),
        generation=7,
        readiness_contract=readiness_contract,
    )
    sources = (QdrantMutationSource.NORMAL_INGEST,)
    app = QdrantGatewayApplication(
        service=service,
        token_verifier=KubernetesBoundTokenVerifier(
            token_reviewer=reviewer,
            now=lambda: 1_000,
        ),
        subject_policy=QdrantGatewaySubjectPolicy(
            bindings=(
                QdrantGatewayWorkloadBinding(
                    subject=subject,
                    sources=sources,
                    workload_ref_hash=hashlib.sha256(
                        b"workload:normal-writer"
                    ).hexdigest(),
                    route_set_hash=build_qdrant_gateway_route_set_hash(sources),
                ),
            )
        ),
    )
    return app, reviewer, product, marker, _token(subject=subject)


def _request(app: QdrantGatewayApplication, token: str, body: bytes):
    return app.handle(
        path="/v1/points/upsert",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
        body=body,
    )


def test_valid_request_tokenreviews_once_and_uses_separate_owned_clients() -> None:
    app, reviewer, product, marker, token = _application()

    response = _request(app, token, _body())

    assert response.status_code == 200
    assert set(json.loads(response.body)) == {"schema_version", "status", "operation_ref"}
    assert len(reviewer.calls) == 1
    assert reviewer.calls[0] == (token, QDRANT_WRITE_GATEWAY_AUDIENCE)
    assert [name for name, _ in product.calls] == ["get_collection", "upsert"]
    assert [name for name, _ in marker.calls] == [
        "get_collection",
        "retrieve",
        "upsert",
        "retrieve",
        "upsert",
        "retrieve",
        "retrieve",
        "retrieve",
        "upsert",
        "retrieve",
    ]
    assert product is not marker


def test_authenticated_mutation_requires_activation_contract_before_any_qdrant_write() -> None:
    app, reviewer, product, marker, token = _application(
        activation_state="contract_absent"
    )

    response = _request(app, token, _body())

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "schema_version": "qdrant_write_gateway_response.v1",
        "status": "activation_not_ready",
    }
    assert len(reviewer.calls) == 1
    assert [call for call in product.calls if call[0] in {"upsert", "delete"}] == []
    assert [call for call in marker.calls if call[0] in {"upsert", "delete"}] == []


@pytest.mark.parametrize(
    "activation_state",
    ("metadata_absent", "pending", "bypass", "schema_mismatch"),
)
def test_authenticated_mutation_fails_closed_on_incomplete_activation_metadata(
    activation_state: str,
) -> None:
    app, reviewer, product, marker, token = _application(
        activation_state=activation_state
    )

    response = _request(app, token, _body())

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "activation_not_ready"
    assert len(reviewer.calls) == 1
    assert [call for call in product.calls if call[0] in {"upsert", "delete"}] == []
    assert [call for call in marker.calls if call[0] in {"upsert", "delete"}] == []


def test_complete_activation_gate_does_not_use_unresolved_readiness_count() -> None:
    app, reviewer, product, marker, token = _application()
    marker.unresolved_count = 1

    response = _request(app, token, _body())

    assert response.status_code == 200
    assert len(reviewer.calls) == 1
    assert "count" not in [name for name, _ in marker.calls]
    assert [name for name, _ in product.calls][-1] == "upsert"


def test_mutation_gate_binds_exact_previous_generation_hash() -> None:
    app, reviewer, product, marker, token = _application()
    next(iter(marker.points.values())).payload["previous_generation_hash"] = "d" * 64

    response = _request(app, token, _body())

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "activation_not_ready"
    assert len(reviewer.calls) == 1
    assert [call for call in product.calls if call[0] in {"upsert", "delete"}] == []
    assert [call for call in marker.calls if call[0] in {"upsert", "delete"}] == []


@pytest.mark.parametrize(
    "drift",
    ("missing_index", "wrong_index_type", "wrong_vector_size"),
)
def test_mutation_gate_rejects_marker_collection_topology_drift(drift: str) -> None:
    app, reviewer, product, marker, token = _application()
    if drift == "missing_index":
        marker.collection_info["payload_schema"].pop("record_kind")
    elif drift == "wrong_index_type":
        marker.collection_info["payload_schema"]["unresolved"][
            "data_type"
        ] = "keyword"
    else:
        marker.collection_info["config"]["params"]["vectors"]["size"] = 2

    response = _request(app, token, _body())

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "activation_not_ready"
    assert len(reviewer.calls) == 1
    assert [call for call in product.calls if call[0] in {"upsert", "delete"}] == []
    assert [call for call in marker.calls if call[0] in {"upsert", "delete"}] == []


@pytest.mark.parametrize(
    ("body", "status_code"),
    (
        (b"{", 400),
        (
            b'{"schema_version":"qdrant_write_gateway_upsert.v1",'
            b'"source":"normal_ingest","source":"repair",'
            b'"collection":"mirror","points":[]}',
            400,
        ),
        (_body(extra={"unknown": "protected-value"}), 400),
        (b"x" * (MAX_GATEWAY_REQUEST_BYTES + 1), 413),
    ),
)
def test_invalid_request_is_rejected_before_auth_marker_or_product(
    body: bytes,
    status_code: int,
) -> None:
    app, reviewer, product, marker, token = _application()

    response = _request(app, token, body)

    assert response.status_code == status_code
    assert reviewer.calls == []
    assert product.calls == []
    assert marker.calls == []
    assert "protected" not in response.body.decode("utf-8")


def test_tokenreview_requires_exact_subject_expiry_audience_and_pod_binding() -> None:
    subject = "system:serviceaccount:neurons:normal-writer"
    reviewer = _Reviewer(subject=subject)
    verifier = KubernetesBoundTokenVerifier(token_reviewer=reviewer, now=lambda: 1_000)
    sources = (QdrantMutationSource.NORMAL_INGEST,)
    policy = QdrantGatewaySubjectPolicy(
        bindings=(
            QdrantGatewayWorkloadBinding(
                subject=subject,
                sources=sources,
                workload_ref_hash=hashlib.sha256(
                    b"workload:normal-writer"
                ).hexdigest(),
                route_set_hash=build_qdrant_gateway_route_set_hash(sources),
            ),
        )
    )

    identity = verifier.verify(
        token=_token(subject=subject),
        source=QdrantMutationSource.NORMAL_INGEST,
        subject_policy=policy,
    )

    assert identity.subject_ref_hash == hashlib.sha256(subject.encode()).hexdigest()
    assert identity.pod_ref_hash == hashlib.sha256(b"writer-0\npod-uid-1").hexdigest()
    assert identity.route_set_hash == build_qdrant_gateway_route_set_hash(sources)
    assert "system:serviceaccount" not in repr(identity)
    assert len(reviewer.calls) == 1

    with pytest.raises(ValueError, match="gateway_token_invalid"):
        verifier.verify(
            token=_token(subject=subject, expires_at=1_000),
            source=QdrantMutationSource.NORMAL_INGEST,
            subject_policy=policy,
        )
    assert len(reviewer.calls) == 2


@pytest.mark.parametrize(
    "mutation",
    ("audience", "subject", "pod_name", "pod_uid"),
)
def test_tokenreview_mismatch_fails_after_exactly_one_review(mutation: str) -> None:
    subject = "system:serviceaccount:neurons:normal-writer"
    reviewer = _Reviewer(subject=subject)
    original_review = reviewer.review

    def review(**kwargs):
        value = original_review(**kwargs)
        status = value["status"]
        if mutation == "audience":
            status["audiences"] = ["other-audience"]
        elif mutation == "subject":
            status["user"]["username"] = "system:serviceaccount:other:writer"
        elif mutation == "pod_name":
            status["user"]["extra"][
                "authentication.kubernetes.io/pod-name"
            ] = ["other-pod"]
        else:
            status["user"]["extra"][
                "authentication.kubernetes.io/pod-uid"
            ] = ["other-uid"]
        return value

    reviewer.review = review
    verifier = KubernetesBoundTokenVerifier(token_reviewer=reviewer, now=lambda: 1_000)

    with pytest.raises(ValueError, match="gateway_token_invalid"):
        verifier.verify(
            token=_token(subject=subject),
            source=QdrantMutationSource.NORMAL_INGEST,
            subject_policy=QdrantGatewaySubjectPolicy(
                bindings=(
                    QdrantGatewayWorkloadBinding(
                        subject=subject,
                        sources=(QdrantMutationSource.NORMAL_INGEST,),
                        workload_ref_hash=hashlib.sha256(
                            b"workload:normal-writer"
                        ).hexdigest(),
                        route_set_hash=build_qdrant_gateway_route_set_hash(
                            (QdrantMutationSource.NORMAL_INGEST,)
                        ),
                    ),
                )
            ),
        )
    assert len(reviewer.calls) == 1


def test_stale_generation_is_rejected_before_auth_marker_or_product() -> None:
    app, reviewer, product, marker, token = _application()

    response = _request(app, token, _body(extra={"generation": 6}))

    assert response.status_code == 409
    assert json.loads(response.body)["status"] == "activation_generation_mismatch"
    assert reviewer.calls == []
    assert product.calls == []
    assert marker.calls == []


def test_alternate_service_account_is_rejected_before_marker_or_product() -> None:
    app, original_reviewer, product, marker, _ = _application()
    subject = "system:serviceaccount:neurons:alternate-writer"
    reviewer = _Reviewer(
        subject=subject,
        pod_name="alternate-writer-0",
        pod_uid="alternate-pod-uid",
    )
    app._token_verifier = KubernetesBoundTokenVerifier(
        token_reviewer=reviewer,
        now=lambda: 1_000,
    )
    token = _token(
        subject=subject,
        pod_name="alternate-writer-0",
        pod_uid="alternate-pod-uid",
    )

    response = _request(app, token, _body())

    assert response.status_code == 401
    assert len(reviewer.calls) == 1
    assert original_reviewer.calls == []
    assert product.calls == []
    assert marker.calls == []


def test_same_approved_workload_accepts_new_pod_name_and_uid_after_rollout() -> None:
    app, original_reviewer, product, marker, _ = _application()
    subject = "system:serviceaccount:neurons:normal-writer"
    reviewer = _Reviewer(
        subject=subject,
        pod_name="writer-rollout-7fd8f9c6b4-k2lm9",
        pod_uid="pod-uid-after-restart",
    )
    app._token_verifier = KubernetesBoundTokenVerifier(
        token_reviewer=reviewer,
        now=lambda: 1_000,
    )

    response = _request(
        app,
        _token(
            subject=subject,
            pod_name="writer-rollout-7fd8f9c6b4-k2lm9",
            pod_uid="pod-uid-after-restart",
        ),
        _body(),
    )

    assert response.status_code == 200
    assert original_reviewer.calls == []
    assert len(reviewer.calls) == 1
    assert [name for name, _ in product.calls] == ["get_collection", "upsert"]
    marker_start = next(
        kwargs["points"][0].payload
        for name, kwargs in marker.calls
        if name == "upsert"
        and kwargs["points"][0].payload.get("phase") == "start"
    )
    assert marker_start["pod_ref_hash"] == hashlib.sha256(
        b"writer-rollout-7fd8f9c6b4-k2lm9\npod-uid-after-restart"
    ).hexdigest()


def test_deleted_bound_token_is_rejected_by_tokenreview_before_qdrant() -> None:
    app, reviewer, product, marker, token = _application()
    original_review = reviewer.review

    def deleted_review(**kwargs):
        value = original_review(**kwargs)
        value["status"] = {"authenticated": False}
        return value

    reviewer.review = deleted_review

    response = _request(app, token, _body())

    assert response.status_code == 401
    assert len(reviewer.calls) == 1
    assert product.calls == []
    assert marker.calls == []


def test_one_subject_may_own_one_exact_multi_route_workload_binding() -> None:
    subject = "system:serviceaccount:neurons:shared-writer"
    sources = (
        QdrantMutationSource.NORMAL_INGEST,
        QdrantMutationSource.PROJECTION,
    )
    policy = QdrantGatewaySubjectPolicy(
        bindings=(
            QdrantGatewayWorkloadBinding(
                subject=subject,
                sources=sources,
                workload_ref_hash=hashlib.sha256(b"workload:shared").hexdigest(),
                route_set_hash=build_qdrant_gateway_route_set_hash(sources),
            ),
        )
    )

    assert policy.binding_for_source(QdrantMutationSource.NORMAL_INGEST) is policy.bindings[0]
    assert policy.binding_for_source(QdrantMutationSource.PROJECTION) is policy.bindings[0]


def test_same_subject_in_multiple_workload_bindings_is_rejected() -> None:
    subject = "system:serviceaccount:neurons:shared-writer"

    with pytest.raises(ValueError, match="gateway_subject_policy_duplicate"):
        QdrantGatewaySubjectPolicy(
            bindings=tuple(
                QdrantGatewayWorkloadBinding(
                    subject=subject,
                    sources=(source,),
                    workload_ref_hash=hashlib.sha256(
                        f"workload:{source.value}".encode()
                    ).hexdigest(),
                    route_set_hash=build_qdrant_gateway_route_set_hash((source,)),
                )
                for source in (
                    QdrantMutationSource.NORMAL_INGEST,
                    QdrantMutationSource.PROJECTION,
                )
            )
        )


@pytest.mark.parametrize("inactive_source", ("gc_retention", "operator_maintenance"))
def test_inactive_source_has_no_http_route_tokenreview_or_qdrant_access(
    inactive_source: str,
) -> None:
    app, reviewer, product, marker, token = _application()

    response = _request(app, token, _body(extra={"source": inactive_source}))

    assert response.status_code == 400
    assert reviewer.calls == []
    assert product.calls == []
    assert marker.calls == []


def _readiness_application() -> tuple[
    QdrantGatewayApplication,
    _QdrantClient,
    _QdrantClient,
]:
    app, _, product, marker, _ = _application()
    marker.points["00000000-0000-4000-8000-000000000001"] = SimpleNamespace(
        id="00000000-0000-4000-8000-000000000001",
        payload={
            "schema_version": "qdrant_exact_marker_metadata.v2",
            "generation": 7,
            "coverage_hash": "c" * 64,
            "coverage_status": "complete",
            "bypass_count": 0,
            "activation_hash": "a" * 64,
            "previous_generation_hash": "b" * 64,
        },
    )
    app._service = QdrantGatewayService(
        product_client=product,
        marker_client=marker,
        policy=QdrantCollectionPolicy(
            product_collections=("mirror",),
            marker_collection="mutation_markers",
        ),
        generation=7,
        readiness_contract=QdrantGatewayReadinessContract(
            metadata_point_id="00000000-0000-4000-8000-000000000001",
            activation_hash="a" * 64,
            coverage_hash="c" * 64,
            previous_generation_hash="b" * 64,
            product_collection_schema_hashes=((
                "mirror",
                build_qdrant_gateway_collection_schema_hash(
                    product.collection_info
                ),
            ),),
        ),
    )
    product.calls.clear()
    marker.calls.clear()
    return app, product, marker


def test_readiness_runs_strict_read_only_product_and_marker_preflight() -> None:
    app, product, marker = _readiness_application()

    response = app.readiness()

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "ready"
    assert [name for name, _ in product.calls] == ["get_collection"]
    assert [name for name, _ in marker.calls] == [
        "get_collection",
        "count",
        "retrieve",
    ]
    assert all(
        name not in {"upsert", "delete"}
        for name, _ in product.calls + marker.calls
    )
    for name, kwargs in product.calls + marker.calls:
        if name == "retrieve":
            assert kwargs["with_payload"] is True
            assert kwargs["with_vectors"] is False
            assert str(kwargs["consistency"]).casefold().endswith("all")
            assert kwargs["ids"] == ["00000000-0000-4000-8000-000000000001"]
    assert not [call for call in product.calls if call[0] == "retrieve"]


@pytest.mark.parametrize(
    "failure",
    ("product_schema_mismatch", "pending_marker", "wrong_generation", "unresolved"),
)
def test_readiness_fails_closed_when_any_read_only_anchor_is_invalid(
    failure: str,
) -> None:
    app, product, marker = _readiness_application()
    if failure == "product_schema_mismatch":
        product.collection_info["payload_schema"] = {
            "unexpected": {"data_type": "keyword"}
        }
    elif failure == "pending_marker":
        next(iter(marker.points.values())).payload["coverage_status"] = "pending_cutover"
    else:
        if failure == "wrong_generation":
            next(iter(marker.points.values())).payload["generation"] = 6
        else:
            marker.unresolved_count = 1

    response = app.readiness()

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "not_ready"
    assert all(
        name not in {"upsert", "delete"}
        for name, _ in product.calls + marker.calls
    )


def test_product_failure_is_sanitized_terminalized_cleared_and_not_retried() -> None:
    app, reviewer, product, marker, token = _application(fail_product=True)

    response = _request(app, token, _body())

    assert response.status_code == 502
    assert json.loads(response.body) == {
        "schema_version": "qdrant_write_gateway_response.v1",
        "status": "product_mutation_failed",
    }
    assert len(reviewer.calls) == 1
    assert [name for name, _ in product.calls] == ["get_collection", "upsert"]
    marker_writes = [kwargs for name, kwargs in marker.calls if name == "upsert"]
    assert len(marker_writes) == 3
    assert any(point.payload.get("outcome") == "failed" for point in marker_writes[1]["points"])
    assert marker_writes[2]["points"][0].payload["unresolved"] is False
    assert "protected" not in response.body.decode("utf-8")


@pytest.mark.parametrize(
    "product_result",
    (
        _UpdateResult("acknowledged", 41),
        _UpdateResult("completed", 0),
        {"status": "completed"},
        object(),
    ),
)
def test_non_completed_or_malformed_product_ack_is_failed_terminalized_and_cleared(
    product_result: object,
) -> None:
    app, reviewer, product, marker, token = _application(
        product_result=product_result
    )

    response = _request(app, token, _body())

    assert response.status_code == 502
    assert json.loads(response.body)["status"] == "product_mutation_failed"
    assert len(reviewer.calls) == 1
    assert [name for name, _ in product.calls] == ["get_collection", "upsert"]
    marker_writes = [kwargs for name, kwargs in marker.calls if name == "upsert"]
    assert len(marker_writes) == 3
    assert marker_writes[0]["points"][0].payload["phase"] == "start"
    assert marker_writes[1]["points"][0].payload["outcome"] == "failed"
    assert marker_writes[2]["points"][0].payload["unresolved"] is False
    assert product.shared_order == [
        "marker:upsert",
        "product:upsert",
        "marker:upsert",
        "marker:upsert",
    ]

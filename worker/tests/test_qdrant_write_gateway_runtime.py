from __future__ import annotations

import hashlib
from dataclasses import replace
from dataclasses import dataclass
from typing import Any

import pytest

from agent_knowledge.qdrant_write_gateway import (
    GatewayMutationRequest,
    WriterCoverage,
    MarkerClearEvent,
    MarkerStartEvent,
    MarkerTerminalEvent,
    QdrantGatewayContractError,
    QdrantMutationKind,
    QdrantMutationRoute,
    build_gateway_coverage_manifest,
)
from agent_knowledge.qdrant_write_gateway_runtime import (
    ACTIVE_QDRANT_MUTATION_SOURCES,
    QDRANT_EXACT_MARKER_METADATA_KEYS,
    QDRANT_EXACT_MARKER_METADATA_SCHEMA,
    QDRANT_MARKER_EVENT_RECORD_KIND,
    QDRANT_MARKER_PHASE_START,
    QDRANT_MARKER_PHASE_TERMINAL,
    QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS,
    QdrantMarkerMetadataPhase,
    QdrantCoverageActivationAnchor,
    QdrantPendingCutoverAnchor,
    QDRANT_SOURCE_REGISTRY,
    AuthenticatedQdrantSubject,
    DirectQdrantWriteTransport,
    ExactRouteAuthorizer,
    QdrantCollectionPolicy,
    QdrantMutationMarkerStore,
    QdrantMutationSource,
    QdrantWriteGatewayTransport,
    RenderedQdrantWriter,
    assess_qdrant_source_coverage,
    activate_qdrant_marker_collection,
    build_qdrant_gateway_transport,
    build_qdrant_exact_marker_hash,
    build_qdrant_coverage_activation_anchor,
    build_qdrant_coverage_manifest_from_activation_anchor,
    build_qdrant_pending_cutover_anchor,
    reconcile_qdrant_marker_metadata,
    provision_qdrant_collection,
)
from agent_knowledge.rag_ingress.qdrant_docling_mirror import (
    HashEmbeddingProvider,
    PassthroughMarkdownNormalizer,
    QdrantDoclingMirrorAdapter,
    SearchableMirrorUnavailable,
)
from agent_knowledge.rag_ingress.qdrant_docling_testing import InMemoryQdrantClient
from agent_knowledge.rag_ingress.rag_ready_document import build_rag_ready_document


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class _UpdateResult:
    status: str
    operation_id: int


class _GatewayClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.points: dict[str, Any] = {}
        self.position = 0

    def upsert(self, **kwargs: Any) -> _UpdateResult:
        self.calls.append(("upsert", kwargs))
        self.position += 1
        for point in kwargs["points"]:
            point_id = str(getattr(point, "id", point["id"] if isinstance(point, dict) else ""))
            self.points[point_id] = point
        return _UpdateResult(status="completed", operation_id=self.position)

    def delete(self, **kwargs: Any) -> _UpdateResult:
        self.calls.append(("delete", kwargs))
        self.position += 1
        return _UpdateResult(status="completed", operation_id=self.position)

    def retrieve(self, *, collection_name: str, ids: list[str], **kwargs: Any) -> list[Any]:
        self.calls.append(("retrieve", {"collection_name": collection_name, "ids": ids, **kwargs}))
        return [self.points[point_id] for point_id in ids if point_id in self.points]

    def count(self, **kwargs: Any) -> Any:
        self.calls.append(("count", kwargs))

        @dataclass
        class CountResult:
            count: int

        return CountResult(count=len(self.points))


def _policy() -> QdrantCollectionPolicy:
    return QdrantCollectionPolicy(
        product_collections=("mirror",),
        marker_collection="mutation_markers",
        max_items_per_mutation=100,
    )


def _subject() -> AuthenticatedQdrantSubject:
    return AuthenticatedQdrantSubject(subject_ref_hash=_sha("runtime-subject"))


def _authorizer(source: QdrantMutationSource) -> ExactRouteAuthorizer:
    return ExactRouteAuthorizer(
        bindings=((_subject().subject_ref_hash, source, "mirror"),)
    )


def test_source_registry_has_fixed_routes_and_does_not_claim_inactive_callers() -> None:
    assert QDRANT_SOURCE_REGISTRY[QdrantMutationSource.NORMAL_INGEST].route is QdrantMutationRoute.NORMAL_INGEST
    assert QDRANT_SOURCE_REGISTRY[QdrantMutationSource.PROJECTION].route is QdrantMutationRoute.PROJECTION
    assert QDRANT_SOURCE_REGISTRY[QdrantMutationSource.BACKFILL].route is QdrantMutationRoute.BACKFILL
    assert QDRANT_SOURCE_REGISTRY[QdrantMutationSource.REPAIR].route is QdrantMutationRoute.REPAIR
    assert set(ACTIVE_QDRANT_MUTATION_SOURCES) == {
        QdrantMutationSource.NORMAL_INGEST,
        QdrantMutationSource.PROJECTION,
        QdrantMutationSource.BACKFILL,
        QdrantMutationSource.REPAIR,
    }

    source_only = assess_qdrant_source_coverage()

    assert source_only["coverage_status"] == "unverified"
    assert source_only["rendered_inventory_status"] == "missing"
    assert source_only["inactive_sources"] == ("gc_retention", "operator_maintenance")

    rendered = tuple(
        RenderedQdrantWriter(
            source=source,
            route=QDRANT_SOURCE_REGISTRY[source].route,
            writer_ref_hash=QDRANT_SOURCE_REGISTRY[source].writer_ref_hash,
            active_caller=QDRANT_SOURCE_REGISTRY[source].active_caller,
            workload_ref_hash=(
                _sha(f"workload:{source.value}")
                if QDRANT_SOURCE_REGISTRY[source].active_caller
                else None
            ),
            image_ref_hash=(
                _sha(f"image:{source.value}")
                if QDRANT_SOURCE_REGISTRY[source].active_caller
                else None
            ),
            network_policy_ref_hash=(
                _sha(f"network-policy:{source.value}")
                if QDRANT_SOURCE_REGISTRY[source].active_caller
                else None
            ),
            route_set_hash=(
                _sha(f"routes:{source.value}")
                if QDRANT_SOURCE_REGISTRY[source].active_caller
                else None
            ),
        )
        for source in QdrantMutationSource
    )
    source_validated_only = assess_qdrant_source_coverage(rendered)
    assert source_validated_only["coverage_status"] == "unverified"
    assert source_validated_only["rendered_inventory_status"] == "validated"
    assert source_validated_only["auth_boundary_status"] == "missing"
    assert source_validated_only["network_policy_status"] == "missing"
    assert source_validated_only["direct_write_credentials_status"] == "missing"

    validated = assess_qdrant_source_coverage(
        rendered,
        auth_boundary_status="validated",
        network_policy_status="validated",
        direct_write_credentials_zero=True,
        read_endpoint_write_denied_status="validated",
    )
    assert validated["coverage_status"] == "complete"
    assert validated["rendered_inventory_status"] == "validated"
    assert validated["inactive_sources"] == ("gc_retention", "operator_maintenance")


def _fixed_six_inventory() -> tuple[RenderedQdrantWriter, ...]:
    return tuple(
        RenderedQdrantWriter(
            source=source,
            route=binding.route,
            writer_ref_hash=binding.writer_ref_hash,
            active_caller=binding.active_caller,
            workload_ref_hash=(
                _sha(f"workload:{source.value}") if binding.active_caller else None
            ),
            image_ref_hash=(
                _sha(f"image:{source.value}") if binding.active_caller else None
            ),
            network_policy_ref_hash=(
                _sha(f"network-policy:{source.value}") if binding.active_caller else None
            ),
            route_set_hash=(
                _sha(f"routes:{source.value}") if binding.active_caller else None
            ),
        )
        for source, binding in QDRANT_SOURCE_REGISTRY.items()
    )


def _activation_anchor(generation: int = 7) -> QdrantCoverageActivationAnchor:
    return build_qdrant_coverage_activation_anchor(
        generation=generation,
        marker_collection="mutation_markers",
        rendered_inventory=_fixed_six_inventory(),
        previous_generation_hash=_sha(f"generation:{generation - 1}"),
        auth_boundary_status="validated",
        network_policy_status="validated",
        direct_write_credentials_zero=True,
        read_endpoint_write_denied_status="validated",
    )


def _pending_anchor(generation: int = 7) -> QdrantPendingCutoverAnchor:
    return build_qdrant_pending_cutover_anchor(
        generation=generation,
        marker_collection="mutation_markers",
        previous_generation_hash=_sha(f"generation:{generation - 1}"),
    )


def test_pending_cutover_anchor_is_source_typed_without_writer_coverage_claim() -> None:
    anchor = build_qdrant_pending_cutover_anchor(
        generation=7,
        marker_collection="mutation_markers",
        previous_generation_hash=_sha("generation:6"),
    )

    assert isinstance(anchor, QdrantPendingCutoverAnchor)
    assert vars(anchor) == {
        "generation": 7,
        "marker_collection": "mutation_markers",
        "previous_generation_hash": _sha("generation:6"),
        "actual_writer_coverage_status": "not_registered",
        "direct_writer_status": "foundation_direct_present",
        "auth_boundary_status": "unverified",
        "network_policy_status": "unverified",
        "read_endpoint_write_denied_status": "unverified",
        "bypass_count": 1,
        "coverage_hash": anchor.coverage_hash,
        "activation_hash": anchor.activation_hash,
    }
    assert anchor.coverage_hash == build_qdrant_pending_cutover_anchor(
        generation=7,
        marker_collection="mutation_markers",
        previous_generation_hash=_sha("generation:6"),
    ).coverage_hash
    assert anchor.activation_hash == build_qdrant_pending_cutover_anchor(
        generation=7,
        marker_collection="mutation_markers",
        previous_generation_hash=_sha("generation:6"),
    ).activation_hash
    assert not hasattr(anchor, "rendered_inventory")


def test_pending_reconcile_initializes_from_pending_anchor_insert_only() -> None:
    client = _GatewayClient()
    operator = _subject()
    authorizer = ExactRouteAuthorizer(
        bindings=(
            (
                operator.subject_ref_hash,
                QdrantMutationSource.OPERATOR_MAINTENANCE,
                "mutation_markers",
            ),
        )
    )
    anchor = _pending_anchor()
    point_id = "00000000-0000-4000-8000-000000000001"

    result = reconcile_qdrant_marker_metadata(
        client=client,
        metadata_point_id=point_id,
        activation_anchor=anchor,
        phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )

    assert result.status == "initialized"
    write = next(kwargs for name, kwargs in client.calls if name == "upsert")
    assert str(write["update_mode"]).lower().endswith("insert_only")
    payload = client.points[point_id].payload
    assert payload == {
        "schema_version": QDRANT_EXACT_MARKER_METADATA_SCHEMA,
        "generation": 7,
        "coverage_hash": anchor.coverage_hash,
        "coverage_status": "pending_cutover",
        "bypass_count": 1,
        "activation_hash": anchor.activation_hash,
        "previous_generation_hash": anchor.previous_generation_hash,
    }


def test_activation_anchor_is_source_fixed_and_rejects_self_declared_inventory() -> None:
    anchor = _activation_anchor()
    coverage = build_qdrant_coverage_manifest_from_activation_anchor(anchor)

    assert coverage.generation == 7
    assert len(coverage.writers) == 6
    assert anchor.activation_hash == _activation_anchor().activation_hash

    malformed = list(_fixed_six_inventory())
    malformed[0] = replace(malformed[0], writer_ref_hash=_sha("self-declared"))
    with pytest.raises(QdrantGatewayContractError, match="rendered_inventory_mismatch"):
        build_qdrant_coverage_activation_anchor(
            generation=7,
            marker_collection="mutation_markers",
            rendered_inventory=tuple(malformed),
            previous_generation_hash=_sha("generation:6"),
            auth_boundary_status="validated",
            network_policy_status="validated",
            direct_write_credentials_zero=True,
            read_endpoint_write_denied_status="validated",
        )

    with pytest.raises(QdrantGatewayContractError, match="activation_hash_mismatch"):
        replace(anchor, activation_hash=_sha("forged-anchor"))


def test_source_coverage_requires_external_read_endpoint_deny_canary() -> None:
    with pytest.raises(
        QdrantGatewayContractError,
        match="read_endpoint_write_deny_evidence_invalid",
    ):
        assess_qdrant_source_coverage(
            _fixed_six_inventory(),
            auth_boundary_status="validated",
            network_policy_status="validated",
            direct_write_credentials_zero=True,
            read_endpoint_write_denied_status="missing",
        )


def test_gateway_transport_is_source_bound_authorized_and_bounded() -> None:
    client = _GatewayClient()
    transport = build_qdrant_gateway_transport(
        client=client,
        collection_name="mirror",
        source=QdrantMutationSource.NORMAL_INGEST,
        subject=_subject(),
        authorizer=_authorizer(QdrantMutationSource.NORMAL_INGEST),
        policy=_policy(),
        marker_store=QdrantMutationMarkerStore(client=client, policy=_policy()),
        generation=1,
        pod_ref_hash=_sha("pod:normal_ingest"),
        workload_ref_hash=_sha("workload:normal_ingest"),
        route_set_hash=_sha("routes:normal_ingest"),
    )

    assert isinstance(transport, QdrantWriteGatewayTransport)
    transport.upsert_points(points=[{"id": "point-1", "vector": [0.1], "payload": {"safe": True}}])

    product_calls = [kwargs for name, kwargs in client.calls if name == "upsert" and kwargs["collection_name"] == "mirror"]
    assert len(product_calls) == 1
    assert product_calls[0]["wait"] is True
    assert str(product_calls[0]["ordering"]).lower().endswith("strong")

    with pytest.raises(ValueError, match="mutation_item_count_exceeded"):
        transport.upsert_points(points=[{}] * 101)
    with pytest.raises(ValueError, match="mutation_point_schema_invalid"):
        transport.upsert_points(points=[{}])
    with pytest.raises(ValueError, match="points_selector_invalid"):
        transport.delete_points(points_selector=object(), item_count=1)

    with pytest.raises(PermissionError, match="qdrant_route_unauthorized"):
        build_qdrant_gateway_transport(
            client=client,
            collection_name="mirror",
            source=QdrantMutationSource.BACKFILL,
            subject=_subject(),
            authorizer=_authorizer(QdrantMutationSource.NORMAL_INGEST),
            policy=_policy(),
            marker_store=QdrantMutationMarkerStore(client=client, policy=_policy()),
            generation=1,
            pod_ref_hash=_sha("pod:backfill"),
            workload_ref_hash=_sha("workload:backfill"),
            route_set_hash=_sha("routes:backfill"),
        )

    with pytest.raises(PermissionError, match="operator_source_not_product_route"):
        build_qdrant_gateway_transport(
            client=client,
            collection_name="mirror",
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=_subject(),
            authorizer=_authorizer(QdrantMutationSource.OPERATOR_MAINTENANCE),
            policy=_policy(),
            marker_store=QdrantMutationMarkerStore(client=client, policy=_policy()),
            generation=1,
            pod_ref_hash=_sha("pod:operator"),
            workload_ref_hash=_sha("workload:operator"),
            route_set_hash=_sha("routes:operator"),
        )


def test_marker_store_uses_fixed_modes_strong_wait_and_readback() -> None:
    client = _GatewayClient()
    store = QdrantMutationMarkerStore(client=client, policy=_policy())
    operation_ref = _sha("operation")

    start = store.append_start(
        MarkerStartEvent(
            operation_ref=operation_ref,
            route=QdrantMutationRoute.NORMAL_INGEST,
            kind=QdrantMutationKind.UPSERT_POINTS,
            item_count=1,
            generation=9,
            writer_ref_hash=_sha("writer:normal_ingest"),
            pod_ref_hash=_sha("pod:normal_ingest"),
            workload_ref_hash=_sha("workload:normal_ingest"),
            route_set_hash=_sha("routes:normal_ingest"),
        ),
        options=__import__("agent_knowledge.qdrant_write_gateway", fromlist=["INSERT_ONLY_OPTIONS"]).INSERT_ONLY_OPTIONS,
    )
    terminal = store.append_terminal(
        MarkerTerminalEvent(
            operation_ref=operation_ref,
            start_marker_hash=start.marker_hash,
            route=QdrantMutationRoute.NORMAL_INGEST,
            generation=9,
            writer_ref_hash=_sha("writer:normal_ingest"),
            pod_ref_hash=_sha("pod:normal_ingest"),
            workload_ref_hash=_sha("workload:normal_ingest"),
            route_set_hash=_sha("routes:normal_ingest"),
            outcome="succeeded",
        ),
        options=__import__("agent_knowledge.qdrant_write_gateway", fromlist=["INSERT_ONLY_OPTIONS"]).INSERT_ONLY_OPTIONS,
    )
    store.clear_unresolved(
        MarkerClearEvent(
            operation_ref=operation_ref,
            terminal_marker_hash=terminal.marker_hash,
        ),
        options=__import__("agent_knowledge.qdrant_write_gateway", fromlist=["UPDATE_ONLY_OPTIONS"]).UPDATE_ONLY_OPTIONS,
    )

    writes = [kwargs for name, kwargs in client.calls if name == "upsert"]
    assert len(writes) == 3
    assert all(write["wait"] is True for write in writes)
    assert all(str(write["ordering"]).lower().endswith("strong") for write in writes)
    assert str(writes[0]["update_mode"]).lower().endswith("insert_only")
    assert str(writes[1]["update_mode"]).lower().endswith("insert_only")
    assert str(writes[2]["update_mode"]).lower().endswith("update_only")
    assert writes[2]["update_filter"] is not None
    assert len([call for call in client.calls if call[0] == "retrieve"]) == 5
    assert all(
        str(kwargs["consistency"]).lower().endswith("all")
        for name, kwargs in client.calls
        if name == "retrieve"
    )
    assert "raw" not in repr(writes).lower()

    start_points = [
        point
        for point in client.points.values()
        if getattr(point, "payload", {}).get("operation_ref") == operation_ref
        and getattr(point, "payload", {}).get("unresolved") is False
    ]
    assert len(start_points) == 1
    cleared_payload = start_points[0].payload
    assert cleared_payload["record_kind"] == QDRANT_MARKER_EVENT_RECORD_KIND
    assert cleared_payload["phase"] == QDRANT_MARKER_PHASE_START
    assert cleared_payload["generation"] == 9
    assert cleared_payload["writer_ref_hash"] == _sha("writer:normal_ingest")
    assert cleared_payload["pod_ref_hash"] == _sha("pod:normal_ingest")
    assert cleared_payload["route_set_hash"] == _sha("routes:normal_ingest")
    assert cleared_payload["route"] == "normal_ingest"
    assert cleared_payload["kind"] == "upsert_points"
    assert cleared_payload["item_count"] == 1

    terminal_points = [
        point
        for point in client.points.values()
        if getattr(point, "payload", {}).get("operation_ref") == operation_ref
        and getattr(point, "payload", {}).get("phase")
        == QDRANT_MARKER_PHASE_TERMINAL
    ]
    assert len(terminal_points) == 1
    assert terminal_points[0].payload["record_kind"] == QDRANT_MARKER_EVENT_RECORD_KIND
    assert terminal_points[0].payload["generation"] == 9
    assert terminal_points[0].payload["writer_ref_hash"] == _sha(
        "writer:normal_ingest"
    )
    assert terminal_points[0].payload["pod_ref_hash"] == _sha(
        "pod:normal_ingest"
    )


def test_exact_marker_metadata_contract_has_one_canonical_hash() -> None:
    assert QDRANT_EXACT_MARKER_METADATA_SCHEMA == "qdrant_exact_marker_metadata.v2"
    assert QDRANT_EXACT_MARKER_METADATA_KEYS == frozenset(
        {
            "schema_version",
            "generation",
            "coverage_hash",
            "coverage_status",
            "bypass_count",
            "activation_hash",
            "previous_generation_hash",
        }
    )
    assert build_qdrant_exact_marker_hash(
        generation=7,
        event_position=24,
        in_flight_count=0,
        coverage_hash=_sha("coverage"),
    ) == build_qdrant_exact_marker_hash(
        generation=7,
        event_position=24,
        in_flight_count=0,
        coverage_hash=_sha("coverage"),
    )
    with pytest.raises(ValueError, match="qdrant_marker_generation_invalid"):
        build_qdrant_exact_marker_hash(
            generation=0,
            event_position=24,
            in_flight_count=0,
            coverage_hash=_sha("coverage"),
        )


def test_operator_metadata_reconcile_is_idempotent_and_mismatch_closed() -> None:
    client = _GatewayClient()
    pending_anchor = _pending_anchor()
    anchor = _activation_anchor()
    operator = _subject()
    authorizer = ExactRouteAuthorizer(
        bindings=(
            (
                operator.subject_ref_hash,
                QdrantMutationSource.OPERATOR_MAINTENANCE,
                "mutation_markers",
            ),
        )
    )
    point_id = "00000000-0000-4000-8000-000000000001"

    pending = reconcile_qdrant_marker_metadata(
        client=client,
        metadata_point_id=point_id,
        activation_anchor=pending_anchor,
        phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )
    repeated_pending = reconcile_qdrant_marker_metadata(
        client=client,
        metadata_point_id=point_id,
        activation_anchor=pending_anchor,
        phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )
    complete = reconcile_qdrant_marker_metadata(
        client=client,
        metadata_point_id=point_id,
        activation_anchor=anchor,
        phase=QdrantMarkerMetadataPhase.POST_RECONCILE,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )
    repeated_complete = reconcile_qdrant_marker_metadata(
        client=client,
        metadata_point_id=point_id,
        activation_anchor=anchor,
        phase=QdrantMarkerMetadataPhase.POST_RECONCILE,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )

    assert (pending.status, repeated_pending.status) == (
        "initialized",
        "already_current",
    )
    assert (complete.status, repeated_complete.status) == (
        "reconciled",
        "already_current",
    )
    writes = [kwargs for name, kwargs in client.calls if name == "upsert"]
    assert len(writes) == 2
    assert str(writes[0]["update_mode"]).lower().endswith("insert_only")
    assert str(writes[1]["update_mode"]).lower().endswith("update_only")
    assert {
        condition.key for condition in writes[1]["update_filter"].must
    } == QDRANT_EXACT_MARKER_METADATA_KEYS
    transition_filter = {
        condition.key: condition.match.value
        for condition in writes[1]["update_filter"].must
    }
    assert transition_filter["coverage_hash"] == pending_anchor.coverage_hash
    assert transition_filter["coverage_hash"] != build_qdrant_coverage_manifest_from_activation_anchor(
        anchor
    ).coverage_hash
    assert transition_filter["activation_hash"] == pending_anchor.activation_hash
    assert transition_filter["activation_hash"] != anchor.activation_hash
    assert transition_filter["previous_generation_hash"] == anchor.previous_generation_hash
    payload = client.points[point_id].payload
    assert set(payload) == QDRANT_EXACT_MARKER_METADATA_KEYS
    assert payload["coverage_status"] == "complete"
    assert payload["bypass_count"] == 0
    assert payload["activation_hash"] == anchor.activation_hash
    assert payload["previous_generation_hash"] == anchor.previous_generation_hash
    assert "writers" not in payload
    assert all(
        str(kwargs["consistency"]).lower().endswith("all")
        for name, kwargs in client.calls
        if name == "retrieve"
    )

    with pytest.raises(QdrantGatewayContractError, match="marker_metadata_mismatch"):
        reconcile_qdrant_marker_metadata(
            client=client,
            metadata_point_id=point_id,
            activation_anchor=_activation_anchor(generation=8),
            phase=QdrantMarkerMetadataPhase.POST_RECONCILE,
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=operator,
            authorizer=authorizer,
            policy=_policy(),
        )


def test_pending_to_complete_rejects_generation_continuity_break_before_write() -> None:
    client = _GatewayClient()
    operator = _subject()
    authorizer = ExactRouteAuthorizer(
        bindings=(
            (
                operator.subject_ref_hash,
                QdrantMutationSource.OPERATOR_MAINTENANCE,
                "mutation_markers",
            ),
        )
    )
    point_id = "00000000-0000-4000-8000-000000000001"
    reconcile_qdrant_marker_metadata(
        client=client,
        metadata_point_id=point_id,
        activation_anchor=_pending_anchor(),
        phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )
    client.points[point_id].payload["generation"] = 8
    writes_before = len([call for call in client.calls if call[0] == "upsert"])

    with pytest.raises(
        QdrantGatewayContractError,
        match="marker_generation_continuity_mismatch",
    ):
        reconcile_qdrant_marker_metadata(
            client=client,
            metadata_point_id=point_id,
            activation_anchor=_activation_anchor(),
            phase=QdrantMarkerMetadataPhase.POST_RECONCILE,
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=operator,
            authorizer=authorizer,
            policy=_policy(),
        )

    assert len([call for call in client.calls if call[0] == "upsert"]) == writes_before


def test_pending_to_complete_rejects_previous_hash_continuity_break_before_write() -> None:
    client = _GatewayClient()
    operator = _subject()
    authorizer = ExactRouteAuthorizer(
        bindings=(
            (
                operator.subject_ref_hash,
                QdrantMutationSource.OPERATOR_MAINTENANCE,
                "mutation_markers",
            ),
        )
    )
    point_id = "00000000-0000-4000-8000-000000000001"
    reconcile_qdrant_marker_metadata(
        client=client,
        metadata_point_id=point_id,
        activation_anchor=_pending_anchor(),
        phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )
    client.points[point_id].payload["previous_generation_hash"] = _sha(
        "forged-previous-generation"
    )
    writes_before = len([call for call in client.calls if call[0] == "upsert"])

    with pytest.raises(
        QdrantGatewayContractError,
        match="marker_previous_generation_continuity_mismatch",
    ):
        reconcile_qdrant_marker_metadata(
            client=client,
            metadata_point_id=point_id,
            activation_anchor=_activation_anchor(),
            phase=QdrantMarkerMetadataPhase.POST_RECONCILE,
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=operator,
            authorizer=authorizer,
            policy=_policy(),
        )

    assert len([call for call in client.calls if call[0] == "upsert"]) == writes_before


def test_pending_to_complete_rejects_activation_hash_continuity_break_before_write() -> None:
    client = _GatewayClient()
    operator = _subject()
    authorizer = ExactRouteAuthorizer(
        bindings=(
            (
                operator.subject_ref_hash,
                QdrantMutationSource.OPERATOR_MAINTENANCE,
                "mutation_markers",
            ),
        )
    )
    point_id = "00000000-0000-4000-8000-000000000001"
    reconcile_qdrant_marker_metadata(
        client=client,
        metadata_point_id=point_id,
        activation_anchor=_pending_anchor(),
        phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )
    client.points[point_id].payload["activation_hash"] = _sha(
        "forged-pending-activation"
    )
    writes_before = len([call for call in client.calls if call[0] == "upsert"])

    with pytest.raises(
        QdrantGatewayContractError,
        match="marker_activation_continuity_mismatch",
    ):
        reconcile_qdrant_marker_metadata(
            client=client,
            metadata_point_id=point_id,
            activation_anchor=_activation_anchor(),
            phase=QdrantMarkerMetadataPhase.POST_RECONCILE,
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=operator,
            authorizer=authorizer,
            policy=_policy(),
        )

    assert len([call for call in client.calls if call[0] == "upsert"]) == writes_before


def test_pending_to_complete_conditional_no_match_fails_closed_on_readback() -> None:
    class ConditionalNoMatchClient(_GatewayClient):
        def upsert(self, **kwargs: Any) -> _UpdateResult:
            if str(kwargs.get("update_mode")).lower().endswith("update_only"):
                self.calls.append(("upsert", kwargs))
                self.position += 1
                return _UpdateResult(status="completed", operation_id=self.position)
            return super().upsert(**kwargs)

    client = ConditionalNoMatchClient()
    operator = _subject()
    authorizer = ExactRouteAuthorizer(
        bindings=(
            (
                operator.subject_ref_hash,
                QdrantMutationSource.OPERATOR_MAINTENANCE,
                "mutation_markers",
            ),
        )
    )
    point_id = "00000000-0000-4000-8000-000000000001"
    reconcile_qdrant_marker_metadata(
        client=client,
        metadata_point_id=point_id,
        activation_anchor=_pending_anchor(),
        phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )

    with pytest.raises(
        QdrantGatewayContractError,
        match="marker_metadata_readback_mismatch",
    ):
        reconcile_qdrant_marker_metadata(
            client=client,
            metadata_point_id=point_id,
            activation_anchor=_activation_anchor(),
            phase=QdrantMarkerMetadataPhase.POST_RECONCILE,
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=operator,
            authorizer=authorizer,
            policy=_policy(),
        )

    assert client.points[point_id].payload["coverage_status"] == "pending_cutover"
    assert client.points[point_id].payload["bypass_count"] == 1


def test_pending_metadata_reconcile_requires_fresh_generation_collection() -> None:
    class NonEmptyClient(_GatewayClient):
        def count(self, **kwargs: Any) -> Any:
            self.calls.append(("count", kwargs))

            @dataclass
            class CountResult:
                count: int = 1

            return CountResult()

    client = NonEmptyClient()
    operator = _subject()
    authorizer = ExactRouteAuthorizer(
        bindings=((operator.subject_ref_hash, QdrantMutationSource.OPERATOR_MAINTENANCE, "mutation_markers"),)
    )

    with pytest.raises(
        QdrantGatewayContractError,
        match="marker_generation_collection_not_fresh",
    ):
        reconcile_qdrant_marker_metadata(
            client=client,
            metadata_point_id="00000000-0000-4000-8000-000000000001",
            activation_anchor=_pending_anchor(),
            phase=QdrantMarkerMetadataPhase.PENDING_CUTOVER,
            source=QdrantMutationSource.OPERATOR_MAINTENANCE,
            subject=operator,
            authorizer=authorizer,
            policy=_policy(),
        )

    assert not [call for call in client.calls if call[0] == "upsert"]


def test_operator_marker_collection_activation_is_fixed_single_shard_and_idempotent() -> None:
    calls = []

    class Client:
        def __init__(self):
            self.exists = False
            self.indexes = set()

        def collection_exists(self, collection_name):
            calls.append(("exists", collection_name))
            return self.exists

        def create_collection(self, **kwargs):
            calls.append(("create", kwargs))
            self.exists = True

        def create_payload_index(self, **kwargs):
            calls.append(("index", kwargs))
            self.indexes.add(kwargs["field_name"])

        def get_collection(self, collection_name):
            calls.append(("get", collection_name))
            return {
                "config": {
                    "params": {
                        "shard_number": 1,
                        "replication_factor": 1,
                        "write_consistency_factor": 1,
                        "vectors": {"size": 1, "distance": "Cosine"},
                    }
                },
                "payload_schema": {
                    name: {
                        "data_type": (
                            "bool" if name == "unresolved" else "keyword"
                        )
                    }
                    for name in self.indexes
                },
            }

    client = Client()
    operator = _subject()
    authorizer = ExactRouteAuthorizer(
        bindings=(
            (
                operator.subject_ref_hash,
                QdrantMutationSource.OPERATOR_MAINTENANCE,
                "mutation_markers",
            ),
        )
    )

    activate_qdrant_marker_collection(
        client=client,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )
    activate_qdrant_marker_collection(
        client=client,
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=operator,
        authorizer=authorizer,
        policy=_policy(),
    )

    create_calls = [value for name, value in calls if name == "create"]
    assert len(create_calls) == 1
    assert create_calls[0]["shard_number"] == 1
    assert create_calls[0]["replication_factor"] == 1
    assert create_calls[0]["write_consistency_factor"] == 1
    assert {
        value["field_name"] for name, value in calls if name == "index"
    } == set(QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS)


def test_direct_transport_is_explicit_and_operator_provisioning_is_separate() -> None:
    client = _GatewayClient()
    direct = DirectQdrantWriteTransport(
        client=client,
        collection_name="mirror",
        policy=_policy(),
    )
    direct.delete_points(points_selector=["point-1"], item_count=1)
    assert [name for name, _ in client.calls] == ["delete"]

    with pytest.raises(PermissionError, match="operator_source_required"):
        provision_qdrant_collection(
            client=client,
            collection_name="mirror",
            vector_size=64,
            payload_index_fields=("privacy_class",),
            source=QdrantMutationSource.NORMAL_INGEST,
            subject=_subject(),
            authorizer=_authorizer(QdrantMutationSource.NORMAL_INGEST),
            policy=_policy(),
        )

    operator_authorizer = _authorizer(QdrantMutationSource.OPERATOR_MAINTENANCE)
    provision_client = InMemoryQdrantClient()
    provision_qdrant_collection(
        client=provision_client,
        collection_name="mirror",
        vector_size=64,
        payload_index_fields=("privacy_class", "target_profile"),
        source=QdrantMutationSource.OPERATOR_MAINTENANCE,
        subject=_subject(),
        authorizer=operator_authorizer,
        policy=_policy(),
    )
    assert provision_client.collection_exists("mirror") is True
    assert set(provision_client.payload_indexes("mirror")) == {
        "privacy_class",
        "target_profile",
    }


def test_mirror_adapter_requires_explicit_write_transport_and_never_provisions() -> None:
    client = InMemoryQdrantClient()
    client.create_collection("mirror", vectors_config={"size": 8, "distance": "Cosine"})
    document = build_rag_ready_document(
        target_profile="derived-memory-items",
        document_kind="approved_memory_card",
        source_namespace="workspace-neurons",
        source_alias="cards/example.md",
        privacy_class="private",
        body="safe body",
        filename="example.md",
        metadata={"project": "neurons"},
    )
    read_only = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="mirror",
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=8),
    )
    with pytest.raises(SearchableMirrorUnavailable, match="qdrant_write_transport_required"):
        read_only.submit_document(document)

    direct = DirectQdrantWriteTransport(
        client=client,
        collection_name="mirror",
        policy=QdrantCollectionPolicy(
            product_collections=("mirror",),
            marker_collection="mutation_markers",
        ),
    )
    writable = QdrantDoclingMirrorAdapter(
        client=client,
        collection_name="mirror",
        normalizer=PassthroughMarkdownNormalizer(),
        embedding_provider=HashEmbeddingProvider(size=8),
        write_transport=direct,
    )
    writable.submit_document(document)
    assert client.point_count("mirror") == 1

    with pytest.raises(SearchableMirrorUnavailable, match="implicit_collection_provisioning_disabled"):
        QdrantDoclingMirrorAdapter(
            client=client,
            collection_name="another",
            ensure_collection=True,
        )
    assert client.collection_exists("another") is False

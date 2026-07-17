from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from typing import Any

import pytest

from agent_knowledge.qdrant_write_gateway import (
    REQUIRED_QDRANT_MUTATION_ROUTES,
    GatewayCoverageManifest,
    GatewayMutationRequest,
    MarkerReceipt,
    MarkerWriteMode,
    MarkerWriteOptions,
    QdrantGatewayContractError,
    QdrantGatewayMarkerError,
    QdrantGatewayProductError,
    QdrantMutationKind,
    QdrantMutationRoute,
    QdrantWriteGateway,
    WriterCoverage,
    build_exact_marker_projection,
    build_gateway_coverage_manifest,
    build_permission_audit_marker_snapshot_record,
    validate_gateway_coverage,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeMarkerStore:
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[tuple[str, Any, MarkerWriteOptions]] = []
        self.unresolved: set[str] = set()
        self._position = 0
        self._lock = threading.Lock()

    def _receipt(self, operation_ref: str, stage: str) -> MarkerReceipt:
        if self.fail_at == stage:
            raise RuntimeError(f"{stage} failed")
        with self._lock:
            self._position += 1
            position = self._position
        return MarkerReceipt(
            accepted=True,
            event_position=position,
            marker_hash=_sha(f"{operation_ref}:{stage}:{position}"),
        )

    def append_start(self, event, *, options: MarkerWriteOptions) -> MarkerReceipt:
        self.calls.append(("start", event, options))
        receipt = self._receipt(event.operation_ref, "start")
        self.unresolved.add(event.operation_ref)
        return receipt

    def append_terminal(self, event, *, options: MarkerWriteOptions) -> MarkerReceipt:
        self.calls.append(("terminal", event, options))
        return self._receipt(event.operation_ref, "terminal")

    def clear_unresolved(self, event, *, options: MarkerWriteOptions) -> MarkerReceipt:
        self.calls.append(("clear", event, options))
        receipt = self._receipt(event.operation_ref, "clear")
        self.unresolved.discard(event.operation_ref)
        return receipt


def _request(
    *,
    route: QdrantMutationRoute = QdrantMutationRoute.NORMAL_INGEST,
    kind: QdrantMutationKind = QdrantMutationKind.UPSERT_POINTS,
) -> GatewayMutationRequest:
    return GatewayMutationRequest(route=route, kind=kind, item_count=1)


def test_gateway_request_is_typed_and_has_no_generic_proxy_fields() -> None:
    assert {field.name for field in fields(GatewayMutationRequest)} == {
        "route",
        "kind",
        "item_count",
    }
    assert {member.value for member in QdrantMutationKind} == {
        "upsert_points",
        "delete_points",
    }
    assert {member.value for member in QdrantMutationRoute} == {
        "normal_ingest",
        "projection",
        "backfill",
        "repair",
        "gc_retention",
        "operator_maintenance",
    }

    with pytest.raises((TypeError, ValueError)):
        GatewayMutationRequest(
            route="arbitrary",  # type: ignore[arg-type]
            kind="PATCH /collections",  # type: ignore[arg-type]
            item_count=1,
        )
    with pytest.raises(ValueError, match="item_count_invalid"):
        GatewayMutationRequest(
            route=QdrantMutationRoute.NORMAL_INGEST,
            kind=QdrantMutationKind.UPSERT_POINTS,
            item_count=0,
        )


def test_durable_start_precedes_product_and_terminal_clear_use_fixed_modes() -> None:
    marker = FakeMarkerStore()
    order: list[str] = []
    writer_ref_hash = _sha("writer:normal_ingest")
    pod_ref_hash = _sha("pod:normal_ingest")
    route_set_hash = _sha("routes:normal_ingest")
    gateway = QdrantWriteGateway(
        marker_store=marker,
        generation=7,
        writer_ref_hash=writer_ref_hash,
        pod_ref_hash=pod_ref_hash,
        workload_ref_hash=_sha("workload:normal_ingest"),
        route_set_hash=route_set_hash,
    )

    def mutate_product() -> str:
        order.append("product")
        return "ok"

    result = gateway.mutate(_request(), mutate_product)

    assert result.status == "succeeded"
    assert result.product_result == "ok"
    assert len(result.operation_ref) == 64
    assert order == ["product"]
    assert [call[0] for call in marker.calls] == ["start", "terminal", "clear"]
    assert marker.calls[0][2] == MarkerWriteOptions(
        mode=MarkerWriteMode.INSERT_ONLY,
        wait=True,
        ordering="strong",
    )
    assert marker.calls[1][2].mode is MarkerWriteMode.INSERT_ONLY
    assert marker.calls[2][2].mode is MarkerWriteMode.UPDATE_ONLY
    assert marker.calls[2][1].expected_unresolved is True
    assert marker.calls[2][1].unresolved is False
    assert marker.calls[0][1].operation_ref == result.operation_ref
    assert marker.calls[0][1].generation == 7
    assert marker.calls[0][1].writer_ref_hash == writer_ref_hash
    assert marker.calls[0][1].pod_ref_hash == pod_ref_hash
    assert marker.calls[0][1].route_set_hash == route_set_hash
    assert marker.calls[1][1].pod_ref_hash == pod_ref_hash
    assert marker.calls[1][1].outcome == "succeeded"
    assert marker.unresolved == set()


def test_start_failure_performs_zero_product_mutations() -> None:
    marker = FakeMarkerStore(fail_at="start")
    product_calls = 0

    def mutate_product() -> None:
        nonlocal product_calls
        product_calls += 1

    with pytest.raises(QdrantGatewayMarkerError, match="marker_start_failed"):
        QdrantWriteGateway(
            marker_store=marker,
            generation=1,
            writer_ref_hash=_sha("writer:test"),
            pod_ref_hash=_sha("pod:test"),
            workload_ref_hash=_sha("workload:test"),
            route_set_hash=_sha("routes:test"),
        ).mutate(_request(), mutate_product)

    assert product_calls == 0
    assert [call[0] for call in marker.calls] == ["start"]


def test_rejected_start_receipt_performs_zero_product_mutations() -> None:
    class RejectedStartStore(FakeMarkerStore):
        def append_start(self, event, *, options: MarkerWriteOptions) -> MarkerReceipt:
            self.calls.append(("start", event, options))
            return MarkerReceipt(
                accepted=False,
                event_position=1,
                marker_hash=_sha("rejected"),
            )

    marker = RejectedStartStore()
    product_calls = 0

    def mutate_product() -> None:
        nonlocal product_calls
        product_calls += 1

    with pytest.raises(QdrantGatewayMarkerError, match="marker_start_failed"):
        QdrantWriteGateway(
            marker_store=marker,
            generation=1,
            writer_ref_hash=_sha("writer:test"),
            pod_ref_hash=_sha("pod:test"),
            workload_ref_hash=_sha("workload:test"),
            route_set_hash=_sha("routes:test"),
        ).mutate(_request(), mutate_product)

    assert product_calls == 0


def test_non_monotonic_terminal_receipt_leaves_start_unresolved() -> None:
    class NonMonotonicTerminalStore(FakeMarkerStore):
        def append_terminal(self, event, *, options: MarkerWriteOptions) -> MarkerReceipt:
            self.calls.append(("terminal", event, options))
            return MarkerReceipt(
                accepted=True,
                event_position=1,
                marker_hash=_sha("non-monotonic"),
            )

    marker = NonMonotonicTerminalStore()

    with pytest.raises(QdrantGatewayMarkerError, match="marker_terminal_failed"):
        QdrantWriteGateway(
            marker_store=marker,
            generation=1,
            writer_ref_hash=_sha("writer:test"),
            pod_ref_hash=_sha("pod:test"),
            workload_ref_hash=_sha("workload:test"),
            route_set_hash=_sha("routes:test"),
        ).mutate(_request(), lambda: "mutated")

    assert len(marker.unresolved) == 1
    assert [call[0] for call in marker.calls] == ["start", "terminal"]


def test_product_failure_is_not_retried_and_leaves_failure_terminal_unresolved() -> None:
    marker = FakeMarkerStore()
    product_calls = 0

    def mutate_product() -> None:
        nonlocal product_calls
        product_calls += 1
        raise RuntimeError("raw product details must not enter the public error")

    with pytest.raises(QdrantGatewayProductError, match="product_mutation_failed") as caught:
        QdrantWriteGateway(
            marker_store=marker,
            generation=1,
            writer_ref_hash=_sha("writer:test"),
            pod_ref_hash=_sha("pod:test"),
            workload_ref_hash=_sha("workload:test"),
            route_set_hash=_sha("routes:test"),
        ).mutate(_request(), mutate_product)

    assert product_calls == 1
    assert str(caught.value) == "product_mutation_failed"
    assert [call[0] for call in marker.calls] == ["start", "terminal"]
    assert marker.calls[1][1].outcome == "failed"
    assert len(marker.unresolved) == 1


@pytest.mark.parametrize("fail_at", ["terminal", "clear"])
def test_terminal_or_clear_failure_leaves_unresolved_marker(fail_at: str) -> None:
    marker = FakeMarkerStore(fail_at=fail_at)
    product_calls = 0

    def mutate_product() -> str:
        nonlocal product_calls
        product_calls += 1
        return "mutated"

    with pytest.raises(QdrantGatewayMarkerError, match=f"marker_{fail_at}_failed"):
        QdrantWriteGateway(
            marker_store=marker,
            generation=1,
            writer_ref_hash=_sha("writer:test"),
            pod_ref_hash=_sha("pod:test"),
            workload_ref_hash=_sha("workload:test"),
            route_set_hash=_sha("routes:test"),
        ).mutate(_request(), mutate_product)

    assert product_calls == 1
    assert len(marker.unresolved) == 1
    if fail_at == "terminal":
        assert [call[0] for call in marker.calls] == ["start", "terminal"]
    else:
        assert [call[0] for call in marker.calls] == ["start", "terminal", "clear"]


def test_concurrent_mutations_receive_unique_hashed_refs_without_overwrite() -> None:
    marker = FakeMarkerStore()
    gateway = QdrantWriteGateway(
        marker_store=marker,
        generation=1,
        writer_ref_hash=_sha("writer:test"),
        pod_ref_hash=_sha("pod:test"),
        workload_ref_hash=_sha("workload:test"),
        route_set_hash=_sha("routes:test"),
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: gateway.mutate(_request(), lambda: "ok"), range(64)))

    refs = [result.operation_ref for result in results]
    assert len(set(refs)) == 64
    assert all(len(ref) == 64 and int(ref, 16) >= 0 for ref in refs)
    assert marker.unresolved == set()
    assert len(marker.calls) == 64 * 3


def _exact_snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "generation": 4,
        "event_position": 17,
        "marker_hash": _sha("marker"),
        "in_flight_count": 0,
        "coverage_hash": _sha("coverage"),
        "coverage_status": "complete",
        "count_status": "exact",
        "reset_count": 0,
        "bypass_count": 0,
    }
    snapshot.update(overrides)
    return snapshot


def test_exact_projection_exposes_only_sanitized_hash_count_status_fields() -> None:
    projection = build_exact_marker_projection(_exact_snapshot(), previous_event_position=17)

    assert projection == {
        "schema_version": "qdrant_product_mutation_marker_projection.v1",
        "plane": "index",
        "generation_hash": _sha("4"),
        "generation_status": "known",
        "event_position_hash": _sha("17"),
        "event_position_status": "monotonic",
        "marker_hash": _sha("marker"),
        "marker_status": "verified",
        "in_flight_count": 0,
        "in_flight_status": "clear",
        "coverage_hash": _sha("coverage"),
        "coverage_status": "complete",
        "count_status": "exact",
        "read_only": True,
    }
    serialized = repr(projection)
    for forbidden in (
        "event_position': 17",
        "point",
        "vector",
        "payload",
        "product_id",
        "writer_id",
        "collection_name",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("snapshot", "previous", "code"),
    [
        ({}, None, "marker_snapshot_fields_invalid"),
        (_exact_snapshot(payload={}), None, "marker_snapshot_fields_invalid"),
        (_exact_snapshot(generation=0), None, "marker_generation_unknown"),
        (_exact_snapshot(event_position=-1), None, "marker_event_position_invalid"),
        (_exact_snapshot(count_status="approximate"), None, "marker_count_not_exact"),
        (_exact_snapshot(reset_count=1), None, "marker_reset_detected"),
        (_exact_snapshot(reset_count=False), None, "marker_reset_count_invalid"),
        (_exact_snapshot(coverage_status="pending"), None, "marker_coverage_incomplete"),
        (_exact_snapshot(bypass_count=1), None, "marker_bypass_detected"),
        (_exact_snapshot(bypass_count=False), None, "marker_bypass_count_invalid"),
        (_exact_snapshot(event_position=16), 17, "marker_event_position_decreased"),
        (_exact_snapshot(marker_hash="not-a-hash"), None, "marker_hash_invalid"),
        (_exact_snapshot(coverage_hash="not-a-hash"), None, "marker_coverage_hash_invalid"),
    ],
)
def test_malformed_approximate_reset_coverage_bypass_or_decrease_fails_closed(
    snapshot: dict[str, object], previous: int | None, code: str
) -> None:
    with pytest.raises(QdrantGatewayContractError, match=code):
        build_exact_marker_projection(snapshot, previous_event_position=previous)


def test_unresolved_projection_is_visible_but_readiness_is_fail_closed() -> None:
    projection = build_exact_marker_projection(_exact_snapshot(in_flight_count=2))
    assert projection["in_flight_count"] == 2
    assert projection["in_flight_status"] == "unresolved"

    with pytest.raises(QdrantGatewayContractError, match="marker_in_flight_unresolved"):
        build_exact_marker_projection(_exact_snapshot(in_flight_count=2), require_clear=True)


def _coverage_writers() -> tuple[WriterCoverage, ...]:
    return tuple(
        WriterCoverage(writer_ref_hash=_sha(f"writer:{route.value}"), routes=(route,))
        for route in REQUIRED_QDRANT_MUTATION_ROUTES
    )


def test_route_and_writer_coverage_requires_exact_expected_manifest() -> None:
    expected = build_gateway_coverage_manifest(generation=3, writers=_coverage_writers())
    observed = GatewayCoverageManifest(
        generation=3,
        writers=expected.writers,
        coverage_hash=expected.coverage_hash,
        bypass_count=0,
    )

    result = validate_gateway_coverage(observed, expected=expected)

    assert result == {
        "generation_hash": _sha("3"),
        "generation_status": "known",
        "coverage_hash": expected.coverage_hash,
        "coverage_status": "complete",
        "writer_count": 6,
        "route_count": 6,
        "bypass_status": "clear",
    }
    assert "writer_ref_hash" not in repr(result)


@pytest.mark.parametrize("mutation", ["missing_route", "missing_writer", "bypass", "hash"])
def test_coverage_gap_or_bypass_fails_closed(mutation: str) -> None:
    expected = build_gateway_coverage_manifest(generation=3, writers=_coverage_writers())
    writers = list(expected.writers)
    bypass_count = 0
    coverage_hash = expected.coverage_hash

    if mutation == "missing_route":
        writers[-1] = WriterCoverage(
            writer_ref_hash=writers[-1].writer_ref_hash,
            routes=(QdrantMutationRoute.NORMAL_INGEST,),
        )
    elif mutation == "missing_writer":
        writers.pop()
    elif mutation == "bypass":
        bypass_count = 1
    else:
        coverage_hash = _sha("unexpected")

    observed = GatewayCoverageManifest(
        generation=3,
        writers=tuple(writers),
        coverage_hash=coverage_hash,
        bypass_count=bypass_count,
    )

    with pytest.raises(QdrantGatewayContractError):
        validate_gateway_coverage(observed, expected=expected)


def _validated_coverage_result() -> dict[str, object]:
    expected = build_gateway_coverage_manifest(generation=4, writers=_coverage_writers())
    return validate_gateway_coverage(expected, expected=expected)


def _permission_audit_inputs() -> tuple[dict[str, object], dict[str, object]]:
    coverage = _validated_coverage_result()
    projection = build_exact_marker_projection(
        _exact_snapshot(coverage_hash=coverage["coverage_hash"])
    )
    return projection, coverage


def test_permission_audit_adapter_returns_exact_canonical_index_record() -> None:
    projection, coverage = _permission_audit_inputs()

    record = build_permission_audit_marker_snapshot_record(
        projection=projection,
        coverage=coverage,
    )

    assert record == {
        "plane": "index",
        "generation_hash": "sha256:" + _sha("4"),
        "event_position_hash": "sha256:" + _sha("17"),
        "marker_hash": "sha256:" + _sha("marker"),
        "in_flight_count": 0,
        "in_flight_status": "clear",
        "coverage_hash": "sha256:" + str(coverage["coverage_hash"]),
        "coverage_status": "validated",
        "read_scope_status": "read_only",
        "reset_or_decrease_count": 0,
        "read_call_count": 1,
    }
    assert set(record) == {
        "plane",
        "generation_hash",
        "event_position_hash",
        "marker_hash",
        "in_flight_count",
        "in_flight_status",
        "coverage_hash",
        "coverage_status",
        "read_scope_status",
        "reset_or_decrease_count",
        "read_call_count",
    }


@pytest.mark.parametrize(
    ("target", "key", "value", "code"),
    [
        ("projection", "unexpected", "raw", "audit_projection_fields_invalid"),
        ("projection", "in_flight_count", 1, "audit_projection_in_flight"),
        ("projection", "in_flight_status", "unresolved", "audit_projection_in_flight"),
        ("projection", "generation_status", "unknown", "audit_projection_status_invalid"),
        ("projection", "event_position_status", "decreased", "audit_projection_status_invalid"),
        ("projection", "marker_status", "unknown", "audit_projection_status_invalid"),
        ("projection", "coverage_status", "pending", "audit_projection_status_invalid"),
        ("projection", "count_status", "approximate", "audit_projection_status_invalid"),
        ("projection", "read_only", False, "audit_projection_status_invalid"),
        ("coverage", "bypass_status", "detected", "audit_coverage_status_invalid"),
        ("coverage", "coverage_status", "pending", "audit_coverage_status_invalid"),
        ("coverage", "generation_status", "unknown", "audit_coverage_status_invalid"),
        ("coverage", "route_count", 5, "audit_coverage_cardinality_invalid"),
        ("coverage", "writer_count", 0, "audit_coverage_cardinality_invalid"),
    ],
)
def test_permission_audit_adapter_fails_closed_for_unknown_extra_or_unready_input(
    target: str,
    key: str,
    value: object,
    code: str,
) -> None:
    projection, coverage = _permission_audit_inputs()
    selected = projection if target == "projection" else coverage
    selected[key] = value

    with pytest.raises(QdrantGatewayContractError, match=code):
        build_permission_audit_marker_snapshot_record(
            projection=projection,
            coverage=coverage,
        )


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("generation_hash", "audit_generation_hash_mismatch"),
        ("coverage_hash", "audit_coverage_hash_mismatch"),
    ],
)
def test_permission_audit_adapter_rejects_projection_coverage_mismatch(
    field: str,
    code: str,
) -> None:
    projection, coverage = _permission_audit_inputs()
    coverage[field] = _sha("mismatch")

    with pytest.raises(QdrantGatewayContractError, match=code):
        build_permission_audit_marker_snapshot_record(
            projection=projection,
            coverage=coverage,
        )


def test_permission_audit_adapter_rejects_malformed_hash_without_raw_output() -> None:
    projection, coverage = _permission_audit_inputs()
    projection["event_position_hash"] = "not-a-hash"

    with pytest.raises(QdrantGatewayContractError, match="audit_projection_hash_invalid"):
        build_permission_audit_marker_snapshot_record(
            projection=projection,
            coverage=coverage,
        )

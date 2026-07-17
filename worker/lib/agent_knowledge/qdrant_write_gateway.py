"""Typed Qdrant write-gateway core with fail-closed mutation markers.

This module deliberately contains no HTTP router and no Qdrant client.  A runtime
adapter supplies two narrow capabilities: a typed product mutation callback and a
marker store implementing the append-start, append-terminal, conditional-clear
sequence.  That keeps raw points, vectors, payloads, collection names, and product
identifiers outside the marker contract and makes the safety state machine testable
without a network.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, TypeVar


_SHA256_LENGTH = 64
_MARKER_SNAPSHOT_FIELDS = frozenset(
    {
        "generation",
        "event_position",
        "marker_hash",
        "in_flight_count",
        "coverage_hash",
        "coverage_status",
        "count_status",
        "reset_count",
        "bypass_count",
    }
)
_EXACT_MARKER_PROJECTION_FIELDS = frozenset(
    {
        "schema_version",
        "plane",
        "generation_hash",
        "generation_status",
        "event_position_hash",
        "event_position_status",
        "marker_hash",
        "marker_status",
        "in_flight_count",
        "in_flight_status",
        "coverage_hash",
        "coverage_status",
        "count_status",
        "read_only",
    }
)
_VALIDATED_COVERAGE_FIELDS = frozenset(
    {
        "generation_hash",
        "generation_status",
        "coverage_hash",
        "coverage_status",
        "writer_count",
        "route_count",
        "bypass_status",
    }
)


class QdrantMutationKind(str, Enum):
    """The only product mutation families accepted by the gateway core."""

    UPSERT_POINTS = "upsert_points"
    DELETE_POINTS = "delete_points"


class QdrantMutationRoute(str, Enum):
    """Every approved production writer route that must be coverage-bound."""

    NORMAL_INGEST = "normal_ingest"
    PROJECTION = "projection"
    BACKFILL = "backfill"
    REPAIR = "repair"
    GC_RETENTION = "gc_retention"
    OPERATOR_MAINTENANCE = "operator_maintenance"


REQUIRED_QDRANT_MUTATION_ROUTES = tuple(QdrantMutationRoute)


class MarkerWriteMode(str, Enum):
    INSERT_ONLY = "insert_only"
    UPDATE_ONLY = "update_only"


@dataclass(frozen=True)
class MarkerWriteOptions:
    mode: MarkerWriteMode
    wait: bool = True
    ordering: str = "strong"

    def __post_init__(self) -> None:
        if not isinstance(self.mode, MarkerWriteMode):
            raise ValueError("marker_write_mode_invalid")
        if self.wait is not True:
            raise ValueError("marker_write_wait_invalid")
        if self.ordering != "strong":
            raise ValueError("marker_write_ordering_invalid")


INSERT_ONLY_OPTIONS = MarkerWriteOptions(mode=MarkerWriteMode.INSERT_ONLY)
UPDATE_ONLY_OPTIONS = MarkerWriteOptions(mode=MarkerWriteMode.UPDATE_ONLY)


@dataclass(frozen=True)
class GatewayMutationRequest:
    """Sanitized routing envelope; product data stays in the callback closure."""

    route: QdrantMutationRoute
    kind: QdrantMutationKind
    item_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.route, QdrantMutationRoute):
            raise ValueError("mutation_route_invalid")
        if not isinstance(self.kind, QdrantMutationKind):
            raise ValueError("mutation_kind_invalid")
        if not _is_non_negative_int(self.item_count) or self.item_count < 1:
            raise ValueError("item_count_invalid")


@dataclass(frozen=True)
class MarkerStartEvent:
    operation_ref: str
    route: QdrantMutationRoute
    kind: QdrantMutationKind
    item_count: int
    generation: int
    writer_ref_hash: str
    pod_ref_hash: str
    workload_ref_hash: str
    route_set_hash: str
    unresolved: bool = True

    def __post_init__(self) -> None:
        if not _is_sha256(self.operation_ref):
            raise ValueError("start_operation_ref_invalid")
        if not isinstance(self.route, QdrantMutationRoute):
            raise ValueError("start_route_invalid")
        if not isinstance(self.kind, QdrantMutationKind):
            raise ValueError("start_kind_invalid")
        if not _is_positive_int(self.item_count):
            raise ValueError("start_item_count_invalid")
        if not _is_positive_int(self.generation):
            raise ValueError("start_generation_invalid")
        if not _is_sha256(self.writer_ref_hash):
            raise ValueError("start_writer_ref_hash_invalid")
        if not _is_sha256(self.pod_ref_hash):
            raise ValueError("start_pod_ref_hash_invalid")
        if not _is_sha256(self.workload_ref_hash):
            raise ValueError("start_workload_ref_hash_invalid")
        if not _is_sha256(self.route_set_hash):
            raise ValueError("start_route_set_hash_invalid")
        if self.unresolved is not True:
            raise ValueError("start_unresolved_invalid")


@dataclass(frozen=True)
class MarkerTerminalEvent:
    operation_ref: str
    start_marker_hash: str
    route: QdrantMutationRoute
    generation: int
    writer_ref_hash: str
    pod_ref_hash: str
    workload_ref_hash: str
    route_set_hash: str
    outcome: str

    def __post_init__(self) -> None:
        if not _is_sha256(self.operation_ref):
            raise ValueError("terminal_operation_ref_invalid")
        if not _is_sha256(self.start_marker_hash):
            raise ValueError("terminal_start_marker_hash_invalid")
        if not isinstance(self.route, QdrantMutationRoute):
            raise ValueError("terminal_route_invalid")
        if not _is_positive_int(self.generation):
            raise ValueError("terminal_generation_invalid")
        if not _is_sha256(self.writer_ref_hash):
            raise ValueError("terminal_writer_ref_hash_invalid")
        if not _is_sha256(self.pod_ref_hash):
            raise ValueError("terminal_pod_ref_hash_invalid")
        if not _is_sha256(self.workload_ref_hash):
            raise ValueError("terminal_workload_ref_hash_invalid")
        if not _is_sha256(self.route_set_hash):
            raise ValueError("terminal_route_set_hash_invalid")
        if self.outcome not in {"succeeded", "failed"}:
            raise ValueError("terminal_outcome_invalid")


@dataclass(frozen=True)
class MarkerClearEvent:
    operation_ref: str
    terminal_marker_hash: str
    expected_unresolved: bool = True
    unresolved: bool = False

    def __post_init__(self) -> None:
        if not _is_sha256(self.operation_ref):
            raise ValueError("clear_operation_ref_invalid")
        if not _is_sha256(self.terminal_marker_hash):
            raise ValueError("clear_terminal_marker_hash_invalid")
        if self.expected_unresolved is not True or self.unresolved is not False:
            raise ValueError("clear_condition_invalid")


@dataclass(frozen=True)
class MarkerReceipt:
    """Internal durable-write acknowledgement returned by a marker adapter."""

    accepted: bool
    event_position: int
    marker_hash: str


class QdrantMarkerStore(Protocol):
    def append_start(
        self, event: MarkerStartEvent, *, options: MarkerWriteOptions
    ) -> MarkerReceipt: ...

    def append_terminal(
        self, event: MarkerTerminalEvent, *, options: MarkerWriteOptions
    ) -> MarkerReceipt: ...

    def clear_unresolved(
        self, event: MarkerClearEvent, *, options: MarkerWriteOptions
    ) -> MarkerReceipt: ...


class QdrantGatewayError(RuntimeError):
    """Base class whose message is always a fixed public-safe status code."""


class QdrantGatewayContractError(QdrantGatewayError):
    pass


class QdrantGatewayMarkerError(QdrantGatewayError):
    pass


class QdrantGatewayProductError(QdrantGatewayError):
    pass


_ProductResult = TypeVar("_ProductResult")


@dataclass(frozen=True)
class GatewayMutationResult:
    operation_ref: str
    status: str
    product_result: Any


class QdrantWriteGateway:
    """Execute exactly one typed mutation inside the durable marker sequence."""

    def __init__(
        self,
        *,
        marker_store: QdrantMarkerStore,
        generation: int,
        writer_ref_hash: str,
        pod_ref_hash: str,
        workload_ref_hash: str,
        route_set_hash: str,
    ) -> None:
        if not _is_positive_int(generation):
            raise QdrantGatewayContractError("gateway_generation_invalid")
        if not _is_sha256(writer_ref_hash):
            raise QdrantGatewayContractError("gateway_writer_ref_hash_invalid")
        if not _is_sha256(pod_ref_hash):
            raise QdrantGatewayContractError("gateway_pod_ref_hash_invalid")
        if not _is_sha256(workload_ref_hash):
            raise QdrantGatewayContractError("gateway_workload_ref_hash_invalid")
        if not _is_sha256(route_set_hash):
            raise QdrantGatewayContractError("gateway_route_set_hash_invalid")
        self._marker_store = marker_store
        self._generation = generation
        self._writer_ref_hash = writer_ref_hash
        self._pod_ref_hash = pod_ref_hash
        self._workload_ref_hash = workload_ref_hash
        self._route_set_hash = route_set_hash

    def mutate(
        self,
        request: GatewayMutationRequest,
        product_mutation: Callable[[], _ProductResult],
    ) -> GatewayMutationResult:
        if not isinstance(request, GatewayMutationRequest):
            raise QdrantGatewayContractError("mutation_request_invalid")
        if not callable(product_mutation):
            raise QdrantGatewayContractError("product_mutation_invalid")

        operation_ref = _new_operation_ref()
        start_event = MarkerStartEvent(
            operation_ref=operation_ref,
            route=request.route,
            kind=request.kind,
            item_count=request.item_count,
            generation=self._generation,
            writer_ref_hash=self._writer_ref_hash,
            pod_ref_hash=self._pod_ref_hash,
            workload_ref_hash=self._workload_ref_hash,
            route_set_hash=self._route_set_hash,
        )
        start_receipt = self._write_marker(
            "start",
            lambda: self._marker_store.append_start(
                start_event,
                options=INSERT_ONLY_OPTIONS,
            ),
        )

        product_result: _ProductResult | None = None
        product_failed = False
        try:
            product_result = product_mutation()
        except Exception:
            # Raw backend exception text must not cross the public gateway boundary.
            product_failed = True

        terminal_event = MarkerTerminalEvent(
            operation_ref=operation_ref,
            start_marker_hash=start_receipt.marker_hash,
            route=request.route,
            generation=self._generation,
            writer_ref_hash=self._writer_ref_hash,
            pod_ref_hash=self._pod_ref_hash,
            workload_ref_hash=self._workload_ref_hash,
            route_set_hash=self._route_set_hash,
            outcome="failed" if product_failed else "succeeded",
        )
        terminal_receipt = self._write_marker(
            "terminal",
            lambda: self._marker_store.append_terminal(
                terminal_event,
                options=INSERT_ONLY_OPTIONS,
            ),
            minimum_position=start_receipt.event_position + 1,
        )

        if product_failed:
            raise QdrantGatewayProductError("product_mutation_failed") from None

        clear_event = MarkerClearEvent(
            operation_ref=operation_ref,
            terminal_marker_hash=terminal_receipt.marker_hash,
        )
        self._write_marker(
            "clear",
            lambda: self._marker_store.clear_unresolved(
                clear_event,
                options=UPDATE_ONLY_OPTIONS,
            ),
            minimum_position=terminal_receipt.event_position,
        )

        return GatewayMutationResult(
            operation_ref=operation_ref,
            status="succeeded",
            product_result=product_result,
        )

    @staticmethod
    def _write_marker(
        stage: str,
        write: Callable[[], MarkerReceipt],
        *,
        minimum_position: int = 1,
    ) -> MarkerReceipt:
        try:
            receipt = write()
            _validate_marker_receipt(receipt, minimum_position=minimum_position)
            return receipt
        except Exception:
            raise QdrantGatewayMarkerError(f"marker_{stage}_failed") from None


@dataclass(frozen=True)
class WriterCoverage:
    """Source/rendered writer identity represented only by a SHA-256 ref."""

    writer_ref_hash: str
    routes: tuple[QdrantMutationRoute, ...]

    def __post_init__(self) -> None:
        if not _is_sha256(self.writer_ref_hash):
            raise ValueError("writer_ref_hash_invalid")
        if type(self.routes) is not tuple or not self.routes:
            raise ValueError("writer_routes_missing")
        if any(not isinstance(route, QdrantMutationRoute) for route in self.routes):
            raise ValueError("writer_route_invalid")
        if len(set(self.routes)) != len(self.routes):
            raise ValueError("writer_route_duplicate")


@dataclass(frozen=True)
class GatewayCoverageManifest:
    generation: int
    writers: tuple[WriterCoverage, ...]
    coverage_hash: str
    bypass_count: int = 0

    def __post_init__(self) -> None:
        if not _is_positive_int(self.generation):
            raise ValueError("coverage_generation_unknown")
        if type(self.writers) is not tuple or any(
            not isinstance(writer, WriterCoverage) for writer in self.writers
        ):
            raise ValueError("coverage_writer_invalid")
        if not _is_sha256(self.coverage_hash):
            raise ValueError("coverage_hash_invalid")
        if not _is_non_negative_int(self.bypass_count):
            raise ValueError("coverage_bypass_count_invalid")


def build_gateway_coverage_manifest(
    *,
    generation: int,
    writers: tuple[WriterCoverage, ...],
) -> GatewayCoverageManifest:
    """Build a source-owned expected coverage manifest for all fixed routes."""

    _validate_writer_set(writers, require_all_routes=True)
    coverage_hash = _coverage_hash(generation=generation, writers=writers)
    return GatewayCoverageManifest(
        generation=generation,
        writers=writers,
        coverage_hash=coverage_hash,
        bypass_count=0,
    )


def validate_gateway_coverage(
    observed: GatewayCoverageManifest,
    *,
    expected: GatewayCoverageManifest,
) -> dict[str, object]:
    """Fail closed unless source and rendered route/writer coverage are exact."""

    if not isinstance(observed, GatewayCoverageManifest) or not isinstance(
        expected, GatewayCoverageManifest
    ):
        raise QdrantGatewayContractError("coverage_manifest_invalid")
    if expected.bypass_count != 0:
        raise QdrantGatewayContractError("expected_coverage_bypass_invalid")

    _validate_writer_set(expected.writers, require_all_routes=True)
    expected_recomputed = _coverage_hash(
        generation=expected.generation,
        writers=expected.writers,
    )
    if expected.coverage_hash != expected_recomputed:
        raise QdrantGatewayContractError("expected_coverage_hash_mismatch")

    if observed.bypass_count != 0:
        raise QdrantGatewayContractError("coverage_bypass_detected")
    if observed.generation != expected.generation:
        raise QdrantGatewayContractError("coverage_generation_mismatch")
    _validate_writer_set(observed.writers, require_all_routes=True)
    observed_recomputed = _coverage_hash(
        generation=observed.generation,
        writers=observed.writers,
    )
    if observed.coverage_hash != observed_recomputed:
        raise QdrantGatewayContractError("coverage_hash_mismatch")
    if _canonical_writers(observed.writers) != _canonical_writers(expected.writers):
        raise QdrantGatewayContractError("coverage_writer_set_mismatch")
    if observed.coverage_hash != expected.coverage_hash:
        raise QdrantGatewayContractError("coverage_expected_hash_mismatch")

    return {
        "generation_hash": _hash_integer(observed.generation),
        "generation_status": "known",
        "coverage_hash": observed.coverage_hash,
        "coverage_status": "complete",
        "writer_count": len(observed.writers),
        "route_count": len(REQUIRED_QDRANT_MUTATION_ROUTES),
        "bypass_status": "clear",
    }


def build_exact_marker_projection(
    snapshot: Mapping[str, object],
    *,
    previous_event_position: int | None = None,
    require_clear: bool = False,
) -> dict[str, object]:
    """Validate native marker metadata and return only public-safe aggregates.

    ``event_position`` is consumed only to prove monotonicity and derive its hash;
    it is never copied to the returned projection.  Callers may request an audit
    readiness gate with ``require_clear=True`` while still using the default form
    to preserve an unresolved status in a sanitized failure artifact.
    """

    if not isinstance(snapshot, Mapping) or set(snapshot) != _MARKER_SNAPSHOT_FIELDS:
        raise QdrantGatewayContractError("marker_snapshot_fields_invalid")

    generation = snapshot["generation"]
    if not _is_positive_int(generation):
        raise QdrantGatewayContractError("marker_generation_unknown")

    event_position = snapshot["event_position"]
    if not _is_non_negative_int(event_position):
        raise QdrantGatewayContractError("marker_event_position_invalid")
    if previous_event_position is not None:
        if not _is_non_negative_int(previous_event_position):
            raise QdrantGatewayContractError("previous_event_position_invalid")
        if event_position < previous_event_position:
            raise QdrantGatewayContractError("marker_event_position_decreased")

    marker_hash = snapshot["marker_hash"]
    if not _is_sha256(marker_hash):
        raise QdrantGatewayContractError("marker_hash_invalid")
    coverage_hash = snapshot["coverage_hash"]
    if not _is_sha256(coverage_hash):
        raise QdrantGatewayContractError("marker_coverage_hash_invalid")

    in_flight_count = snapshot["in_flight_count"]
    if not _is_non_negative_int(in_flight_count):
        raise QdrantGatewayContractError("marker_in_flight_count_invalid")
    if snapshot["count_status"] != "exact":
        raise QdrantGatewayContractError("marker_count_not_exact")
    reset_count = snapshot["reset_count"]
    if not _is_non_negative_int(reset_count):
        raise QdrantGatewayContractError("marker_reset_count_invalid")
    if reset_count != 0:
        raise QdrantGatewayContractError("marker_reset_detected")
    if snapshot["coverage_status"] != "complete":
        raise QdrantGatewayContractError("marker_coverage_incomplete")
    bypass_count = snapshot["bypass_count"]
    if not _is_non_negative_int(bypass_count):
        raise QdrantGatewayContractError("marker_bypass_count_invalid")
    if bypass_count != 0:
        raise QdrantGatewayContractError("marker_bypass_detected")
    if require_clear and in_flight_count != 0:
        raise QdrantGatewayContractError("marker_in_flight_unresolved")

    return {
        "schema_version": "qdrant_product_mutation_marker_projection.v1",
        "plane": "index",
        "generation_hash": _hash_integer(generation),
        "generation_status": "known",
        "event_position_hash": _hash_integer(event_position),
        "event_position_status": "monotonic",
        "marker_hash": marker_hash,
        "marker_status": "verified",
        "in_flight_count": in_flight_count,
        "in_flight_status": "clear" if in_flight_count == 0 else "unresolved",
        "coverage_hash": coverage_hash,
        "coverage_status": "complete",
        "count_status": "exact",
        "read_only": True,
    }


def build_permission_audit_marker_snapshot_record(
    *,
    projection: Mapping[str, object],
    coverage: Mapping[str, object],
) -> dict[str, object]:
    """Normalize validated Qdrant evidence into the canonical audit record.

    Only the two lower-level sanitized results are accepted.  Exact key sets and
    cross-result hash equality prevent a caller from adding raw Qdrant material or
    combining a marker projection with a different route/writer coverage proof.
    """

    if not isinstance(projection, Mapping) or set(projection) != _EXACT_MARKER_PROJECTION_FIELDS:
        raise QdrantGatewayContractError("audit_projection_fields_invalid")
    if not isinstance(coverage, Mapping) or set(coverage) != _VALIDATED_COVERAGE_FIELDS:
        raise QdrantGatewayContractError("audit_coverage_fields_invalid")

    in_flight_count = projection["in_flight_count"]
    if (
        not _is_non_negative_int(in_flight_count)
        or in_flight_count != 0
        or projection["in_flight_status"] != "clear"
    ):
        raise QdrantGatewayContractError("audit_projection_in_flight")
    if (
        projection["schema_version"] != "qdrant_product_mutation_marker_projection.v1"
        or projection["plane"] != "index"
        or projection["generation_status"] != "known"
        or projection["event_position_status"] != "monotonic"
        or projection["marker_status"] != "verified"
        or projection["coverage_status"] != "complete"
        or projection["count_status"] != "exact"
        or projection["read_only"] is not True
    ):
        raise QdrantGatewayContractError("audit_projection_status_invalid")

    if (
        coverage["generation_status"] != "known"
        or coverage["coverage_status"] != "complete"
        or coverage["bypass_status"] != "clear"
    ):
        raise QdrantGatewayContractError("audit_coverage_status_invalid")
    writer_count = coverage["writer_count"]
    route_count = coverage["route_count"]
    if (
        not _is_positive_int(writer_count)
        or not _is_positive_int(route_count)
        or route_count != len(REQUIRED_QDRANT_MUTATION_ROUTES)
    ):
        raise QdrantGatewayContractError("audit_coverage_cardinality_invalid")

    projection_hashes = (
        projection["generation_hash"],
        projection["event_position_hash"],
        projection["marker_hash"],
        projection["coverage_hash"],
    )
    coverage_hashes = (coverage["generation_hash"], coverage["coverage_hash"])
    if not all(_is_sha256(value) for value in (*projection_hashes, *coverage_hashes)):
        raise QdrantGatewayContractError("audit_projection_hash_invalid")
    if projection["generation_hash"] != coverage["generation_hash"]:
        raise QdrantGatewayContractError("audit_generation_hash_mismatch")
    if projection["coverage_hash"] != coverage["coverage_hash"]:
        raise QdrantGatewayContractError("audit_coverage_hash_mismatch")

    return {
        "plane": "index",
        "generation_hash": _prefixed_sha256(projection["generation_hash"]),
        "event_position_hash": _prefixed_sha256(projection["event_position_hash"]),
        "marker_hash": _prefixed_sha256(projection["marker_hash"]),
        "in_flight_count": 0,
        "in_flight_status": "clear",
        "coverage_hash": _prefixed_sha256(projection["coverage_hash"]),
        "coverage_status": "validated",
        "read_scope_status": "read_only",
        "reset_or_decrease_count": 0,
        "read_call_count": 1,
    }


def _new_operation_ref() -> str:
    # Hash a random nonce so a marker never contains a raw request/product/writer id.
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _validate_marker_receipt(receipt: object, *, minimum_position: int) -> None:
    if not isinstance(receipt, MarkerReceipt):
        raise QdrantGatewayContractError("marker_receipt_invalid")
    if receipt.accepted is not True:
        raise QdrantGatewayContractError("marker_receipt_not_accepted")
    if not _is_positive_int(receipt.event_position):
        raise QdrantGatewayContractError("marker_receipt_position_invalid")
    if receipt.event_position < minimum_position:
        raise QdrantGatewayContractError("marker_receipt_position_decreased")
    if not _is_sha256(receipt.marker_hash):
        raise QdrantGatewayContractError("marker_receipt_hash_invalid")


def _validate_writer_set(
    writers: tuple[WriterCoverage, ...],
    *,
    require_all_routes: bool,
) -> None:
    if not writers:
        raise QdrantGatewayContractError("coverage_writers_missing")
    refs = [writer.writer_ref_hash for writer in writers]
    if len(refs) != len(set(refs)):
        raise QdrantGatewayContractError("coverage_writer_duplicate")
    route_set = {route for writer in writers for route in writer.routes}
    if require_all_routes and route_set != set(REQUIRED_QDRANT_MUTATION_ROUTES):
        raise QdrantGatewayContractError("coverage_route_set_incomplete")


def _canonical_writers(writers: tuple[WriterCoverage, ...]) -> list[dict[str, object]]:
    return sorted(
        (
            {
                "writer_ref_hash": writer.writer_ref_hash,
                "routes": sorted(route.value for route in writer.routes),
            }
            for writer in writers
        ),
        key=lambda item: str(item["writer_ref_hash"]),
    )


def _coverage_hash(*, generation: int, writers: tuple[WriterCoverage, ...]) -> str:
    if not _is_positive_int(generation):
        raise QdrantGatewayContractError("coverage_generation_unknown")
    canonical = json.dumps(
        {
            "generation": generation,
            "writers": _canonical_writers(writers),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _hash_integer(value: int) -> str:
    return hashlib.sha256(str(value).encode("ascii")).hexdigest()


def _prefixed_sha256(value: object) -> str:
    if not _is_sha256(value):
        raise QdrantGatewayContractError("audit_hash_invalid")
    return f"sha256:{value}"


def _is_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _is_non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


__all__ = [
    "REQUIRED_QDRANT_MUTATION_ROUTES",
    "GatewayCoverageManifest",
    "GatewayMutationRequest",
    "GatewayMutationResult",
    "MarkerClearEvent",
    "MarkerReceipt",
    "MarkerStartEvent",
    "MarkerTerminalEvent",
    "MarkerWriteMode",
    "MarkerWriteOptions",
    "QdrantGatewayContractError",
    "QdrantGatewayError",
    "QdrantGatewayMarkerError",
    "QdrantGatewayProductError",
    "QdrantMarkerStore",
    "QdrantMutationKind",
    "QdrantMutationRoute",
    "QdrantWriteGateway",
    "WriterCoverage",
    "build_exact_marker_projection",
    "build_gateway_coverage_manifest",
    "build_permission_audit_marker_snapshot_record",
    "validate_gateway_coverage",
]

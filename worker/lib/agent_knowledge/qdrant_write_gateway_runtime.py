"""Concrete Qdrant adapters for the source-owned write gateway.

Only this gateway-owned module may call Qdrant mutation methods.  Product callers
receive a narrow upsert/delete transport whose source route is fixed at build
time.  The marker adapter uses Qdrant 1.18 conditional update modes and verifies
both the durable acknowledgement and a read-after-write projection.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from .qdrant_write_gateway import (
    GatewayCoverageManifest,
    GatewayMutationResult,
    GatewayMutationRequest,
    MarkerClearEvent,
    MarkerReceipt,
    MarkerStartEvent,
    MarkerTerminalEvent,
    MarkerWriteMode,
    MarkerWriteOptions,
    QdrantGatewayContractError,
    QdrantMarkerStore,
    QdrantMutationKind,
    QdrantMutationRoute,
    QdrantWriteGateway,
    WriterCoverage,
    build_gateway_coverage_manifest,
)


_COLLECTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_QDRANT_MARKER_COLLECTION = "neurons_qdrant_mutation_markers"
QDRANT_MARKER_EVENT_RECORD_KIND = "event"
QDRANT_MARKER_PHASE_START = "start"
QDRANT_MARKER_PHASE_TERMINAL = "terminal"
QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS = (
    "record_kind",
    "phase",
    "unresolved",
    "generation",
    "route",
    "writer_ref_hash",
    "pod_ref_hash",
    "workload_ref_hash",
    "route_set_hash",
    "bypass",
)
QDRANT_EXACT_MARKER_METADATA_SCHEMA = "qdrant_exact_marker_metadata.v2"
QDRANT_EXACT_MARKER_METADATA_KEYS = frozenset(
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


def build_qdrant_exact_marker_hash(
    *,
    generation: int,
    event_position: int,
    in_flight_count: int,
    coverage_hash: str,
) -> str:
    """Hash exact reader observations and fixed coverage identity."""

    if type(generation) is not int or generation < 1:
        raise ValueError("qdrant_marker_generation_invalid")
    if type(event_position) is not int or event_position < 0:
        raise ValueError("qdrant_marker_event_position_invalid")
    if (
        type(in_flight_count) is not int
        or in_flight_count < 0
        or in_flight_count > event_position
    ):
        raise ValueError("qdrant_marker_in_flight_count_invalid")
    if not isinstance(coverage_hash, str) or not _SHA256_RE.fullmatch(
        coverage_hash
    ):
        raise ValueError("qdrant_marker_coverage_hash_invalid")
    encoded = json.dumps(
        {
            "coverage_hash": coverage_hash,
            "event_position": event_position,
            "generation": generation,
            "in_flight_count": in_flight_count,
            "schema_version": "qdrant_exact_marker_snapshot.v1",
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class QdrantMutationSource(str, Enum):
    """Source-owned identities; callers must pass an enum member, never text/env."""

    NORMAL_INGEST = "normal_ingest"
    PROJECTION = "projection"
    BACKFILL = "backfill"
    REPAIR = "repair"
    GC_RETENTION = "gc_retention"
    OPERATOR_MAINTENANCE = "operator_maintenance"


class QdrantWriteActivation(str, Enum):
    """Explicit writer mode; the safe default performs no Qdrant mutation."""

    FOUNDATION_INACTIVE = "foundation_inactive"
    FOUNDATION_DIRECT = "foundation_direct"
    REMOTE_GATEWAY = "remote_gateway"


@dataclass(frozen=True)
class FoundationDirectWriteContract:
    """PR C-only compatibility; it is intentionally not audit/coverage ready."""

    activation: QdrantWriteActivation
    phase: str
    audit_status: str
    coverage_status: str

    def __post_init__(self) -> None:
        if (
            self.activation is not QdrantWriteActivation.FOUNDATION_DIRECT
            or self.phase != "pr_c_foundation_compatibility"
            or self.audit_status != "pending"
            or self.coverage_status != "pending"
        ):
            raise ValueError("foundation_direct_contract_invalid")


def qdrant_write_activation_from_environment(
    environ: Mapping[str, object],
) -> QdrantWriteActivation:
    value = str(
        environ.get("QDRANT_WRITE_ACTIVATION")
        or QdrantWriteActivation.FOUNDATION_INACTIVE.value
    ).strip()
    try:
        return QdrantWriteActivation(value)
    except ValueError:
        raise QdrantGatewayContractError("qdrant_write_activation_invalid") from None


def qdrant_write_gateway_generation_from_environment(
    environ: Mapping[str, object],
) -> int:
    value = environ.get("QDRANT_WRITE_GATEWAY_GENERATION")
    if not isinstance(value, str) or not value.isdecimal():
        raise QdrantGatewayContractError("qdrant_write_generation_invalid")
    generation = int(value)
    if str(generation) != value or not 0 < generation < 2**63:
        raise QdrantGatewayContractError("qdrant_write_generation_invalid")
    return generation


class QdrantMarkerMetadataPhase(str, Enum):
    """The only two operator-owned singleton metadata transitions."""

    PENDING_CUTOVER = "pending_cutover"
    POST_RECONCILE = "post_reconcile"


@dataclass(frozen=True)
class QdrantMarkerMetadataReconcileResult:
    status: str
    phase: QdrantMarkerMetadataPhase

    def __post_init__(self) -> None:
        if self.status not in {"initialized", "reconciled", "already_current"}:
            raise ValueError("marker_metadata_result_invalid")
        if not isinstance(self.phase, QdrantMarkerMetadataPhase):
            raise ValueError("marker_metadata_phase_invalid")


@dataclass(frozen=True)
class QdrantSourceBinding:
    source: QdrantMutationSource
    route: QdrantMutationRoute
    writer_ref_hash: str
    active_caller: bool


def _source_binding(
    source: QdrantMutationSource,
    route: QdrantMutationRoute,
    caller: str,
    *,
    active: bool,
) -> QdrantSourceBinding:
    return QdrantSourceBinding(
        source=source,
        route=route,
        writer_ref_hash=hashlib.sha256(caller.encode("utf-8")).hexdigest(),
        active_caller=active,
    )


QDRANT_SOURCE_REGISTRY: Mapping[QdrantMutationSource, QdrantSourceBinding] = {
    QdrantMutationSource.NORMAL_INGEST: _source_binding(
        QdrantMutationSource.NORMAL_INGEST,
        QdrantMutationRoute.NORMAL_INGEST,
        "agent_knowledge.rag_ingress.qdrant_dual_write",
        active=True,
    ),
    QdrantMutationSource.PROJECTION: _source_binding(
        QdrantMutationSource.PROJECTION,
        QdrantMutationRoute.PROJECTION,
        "agent_knowledge.couchdb_source.build_cli",
        active=True,
    ),
    QdrantMutationSource.BACKFILL: _source_binding(
        QdrantMutationSource.BACKFILL,
        QdrantMutationRoute.BACKFILL,
        "agent_knowledge.rag_ingress.qdrant_backfill_cli.run",
        active=True,
    ),
    QdrantMutationSource.REPAIR: _source_binding(
        QdrantMutationSource.REPAIR,
        QdrantMutationRoute.REPAIR,
        "agent_knowledge.rag_ingress.qdrant_backfill_cli.rollback",
        active=True,
    ),
    QdrantMutationSource.GC_RETENTION: _source_binding(
        QdrantMutationSource.GC_RETENTION,
        QdrantMutationRoute.GC_RETENTION,
        "agent_knowledge.qdrant_write_gateway_runtime.gc_retention",
        active=False,
    ),
    QdrantMutationSource.OPERATOR_MAINTENANCE: _source_binding(
        QdrantMutationSource.OPERATOR_MAINTENANCE,
        QdrantMutationRoute.OPERATOR_MAINTENANCE,
        "agent_knowledge.qdrant_write_gateway_runtime.operator_maintenance",
        active=False,
    ),
}

ACTIVE_QDRANT_MUTATION_SOURCES = tuple(
    source for source, binding in QDRANT_SOURCE_REGISTRY.items() if binding.active_caller
)


@dataclass(frozen=True)
class RenderedQdrantWriter:
    source: QdrantMutationSource
    route: QdrantMutationRoute
    writer_ref_hash: str
    active_caller: bool
    workload_ref_hash: str | None
    image_ref_hash: str | None
    network_policy_ref_hash: str | None
    route_set_hash: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.source, QdrantMutationSource):
            raise ValueError("rendered_writer_source_invalid")
        if not isinstance(self.route, QdrantMutationRoute):
            raise ValueError("rendered_writer_route_invalid")
        if not _SHA256_RE.fullmatch(self.writer_ref_hash):
            raise ValueError("rendered_writer_ref_invalid")
        if type(self.active_caller) is not bool:
            raise ValueError("rendered_writer_activation_invalid")
        if self.active_caller:
            for value in (
                self.workload_ref_hash,
                self.image_ref_hash,
                self.network_policy_ref_hash,
            ):
                if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
                    raise ValueError("rendered_writer_workload_anchor_invalid")
            if not isinstance(
                self.route_set_hash, str
            ) or not _SHA256_RE.fullmatch(self.route_set_hash):
                raise ValueError("rendered_writer_route_set_invalid")
        elif any(
            value is not None
            for value in (
                self.workload_ref_hash,
                self.image_ref_hash,
                self.network_policy_ref_hash,
                self.route_set_hash,
            )
        ):
            raise ValueError("inactive_rendered_writer_identity_present")


@dataclass(frozen=True)
class QdrantPendingCutoverAnchor:
    """Source-owned PR C state before any rendered writer coverage is registered."""

    generation: int
    marker_collection: str
    previous_generation_hash: str
    actual_writer_coverage_status: str
    direct_writer_status: str
    auth_boundary_status: str
    network_policy_status: str
    read_endpoint_write_denied_status: str
    bypass_count: int
    coverage_hash: str
    activation_hash: str

    def __post_init__(self) -> None:
        if type(self.generation) is not int or not 0 < self.generation < 2**63:
            raise ValueError("qdrant_pending_generation_invalid")
        _validate_collection_name(self.marker_collection)
        if not _SHA256_RE.fullmatch(self.previous_generation_hash):
            raise ValueError("previous_generation_hash_invalid")
        if self.actual_writer_coverage_status != "not_registered":
            raise ValueError("pending_writer_coverage_status_invalid")
        if self.direct_writer_status != "foundation_direct_present":
            raise ValueError("pending_direct_writer_status_invalid")
        if self.auth_boundary_status != "unverified":
            raise ValueError("pending_auth_boundary_status_invalid")
        if self.network_policy_status != "unverified":
            raise ValueError("pending_network_policy_status_invalid")
        if self.read_endpoint_write_denied_status != "unverified":
            raise ValueError("pending_read_endpoint_write_deny_status_invalid")
        if self.bypass_count != 1:
            raise ValueError("pending_bypass_count_invalid")
        expected_coverage_hash = _qdrant_pending_cutover_coverage_hash(
            generation=self.generation,
            actual_writer_coverage_status=self.actual_writer_coverage_status,
            direct_writer_status=self.direct_writer_status,
            auth_boundary_status=self.auth_boundary_status,
            network_policy_status=self.network_policy_status,
            read_endpoint_write_denied_status=self.read_endpoint_write_denied_status,
            bypass_count=self.bypass_count,
        )
        if self.coverage_hash != expected_coverage_hash:
            raise QdrantGatewayContractError("pending_coverage_hash_mismatch")
        expected_activation_hash = _qdrant_pending_cutover_activation_hash(
            generation=self.generation,
            marker_collection=self.marker_collection,
            previous_generation_hash=self.previous_generation_hash,
            coverage_hash=self.coverage_hash,
        )
        if self.activation_hash != expected_activation_hash:
            raise QdrantGatewayContractError("pending_activation_hash_mismatch")


def build_qdrant_pending_cutover_anchor(
    *,
    generation: int,
    marker_collection: str,
    previous_generation_hash: str,
    coverage_hash: str | None = None,
    activation_hash: str | None = None,
) -> QdrantPendingCutoverAnchor:
    """Build the one honest PR C state without accepting runtime coverage claims."""

    status = {
        "actual_writer_coverage_status": "not_registered",
        "direct_writer_status": "foundation_direct_present",
        "auth_boundary_status": "unverified",
        "network_policy_status": "unverified",
        "read_endpoint_write_denied_status": "unverified",
        "bypass_count": 1,
    }
    computed_coverage_hash = _qdrant_pending_cutover_coverage_hash(
        generation=generation,
        **status,
    )
    if coverage_hash is not None and coverage_hash != computed_coverage_hash:
        raise QdrantGatewayContractError("pending_coverage_hash_mismatch")
    computed_activation_hash = _qdrant_pending_cutover_activation_hash(
        generation=generation,
        marker_collection=marker_collection,
        previous_generation_hash=previous_generation_hash,
        coverage_hash=computed_coverage_hash,
    )
    if activation_hash is not None and activation_hash != computed_activation_hash:
        raise QdrantGatewayContractError("pending_activation_hash_mismatch")
    return QdrantPendingCutoverAnchor(
        generation=generation,
        marker_collection=marker_collection,
        previous_generation_hash=previous_generation_hash,
        coverage_hash=computed_coverage_hash,
        activation_hash=computed_activation_hash,
        **status,
    )


def _qdrant_pending_cutover_coverage_hash(
    *,
    generation: int,
    actual_writer_coverage_status: str,
    direct_writer_status: str,
    auth_boundary_status: str,
    network_policy_status: str,
    read_endpoint_write_denied_status: str,
    bypass_count: int,
) -> str:
    encoded = json.dumps(
        {
            "actual_writer_coverage_status": actual_writer_coverage_status,
            "auth_boundary_status": auth_boundary_status,
            "bypass_count": bypass_count,
            "direct_writer_status": direct_writer_status,
            "generation": generation,
            "network_policy_status": network_policy_status,
            "read_endpoint_write_denied_status": read_endpoint_write_denied_status,
            "schema_version": "qdrant_pending_cutover_coverage.v1",
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _qdrant_pending_cutover_activation_hash(
    *,
    generation: int,
    marker_collection: str,
    previous_generation_hash: str,
    coverage_hash: str,
) -> str:
    encoded = json.dumps(
        {
            "coverage_hash": coverage_hash,
            "generation": generation,
            "marker_collection": marker_collection,
            "previous_generation_hash": previous_generation_hash,
            "schema_version": "qdrant_pending_cutover_activation.v1",
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class QdrantCoverageActivationAnchor:
    """Externally rendered, source-validated cutover identity for one generation."""

    generation: int
    marker_collection: str
    rendered_inventory: tuple[RenderedQdrantWriter, ...]
    previous_generation_hash: str
    auth_boundary_status: str
    network_policy_status: str
    direct_write_credentials_zero: bool
    read_endpoint_write_denied_status: str
    activation_hash: str

    def __post_init__(self) -> None:
        if type(self.generation) is not int or not 0 < self.generation < 2**63:
            raise ValueError("qdrant_activation_generation_invalid")
        _validate_collection_name(self.marker_collection)
        if not _SHA256_RE.fullmatch(self.previous_generation_hash):
            raise ValueError("previous_generation_hash_invalid")
        _validate_rendered_qdrant_inventory(self.rendered_inventory)
        coverage = assess_qdrant_source_coverage(
            self.rendered_inventory,
            auth_boundary_status=self.auth_boundary_status,
            network_policy_status=self.network_policy_status,
            direct_write_credentials_zero=self.direct_write_credentials_zero,
            read_endpoint_write_denied_status=self.read_endpoint_write_denied_status,
        )
        if coverage["coverage_status"] != "complete":
            raise QdrantGatewayContractError("activation_coverage_incomplete")
        if not _SHA256_RE.fullmatch(self.activation_hash):
            raise ValueError("activation_hash_invalid")
        expected_hash = _qdrant_coverage_activation_hash(
            generation=self.generation,
            marker_collection=self.marker_collection,
            rendered_inventory=self.rendered_inventory,
            previous_generation_hash=self.previous_generation_hash,
            auth_boundary_status=self.auth_boundary_status,
            network_policy_status=self.network_policy_status,
            direct_write_credentials_zero=self.direct_write_credentials_zero,
            read_endpoint_write_denied_status=self.read_endpoint_write_denied_status,
        )
        if self.activation_hash != expected_hash:
            raise QdrantGatewayContractError("activation_hash_mismatch")


def build_qdrant_coverage_activation_anchor(
    *,
    generation: int,
    marker_collection: str,
    rendered_inventory: tuple[RenderedQdrantWriter, ...],
    previous_generation_hash: str,
    auth_boundary_status: str,
    network_policy_status: str,
    direct_write_credentials_zero: bool,
    read_endpoint_write_denied_status: str,
    activation_hash: str | None = None,
) -> QdrantCoverageActivationAnchor:
    """Validate an external six-source inventory and its supplied anchor hash."""

    computed = _qdrant_coverage_activation_hash(
        generation=generation,
        marker_collection=marker_collection,
        rendered_inventory=rendered_inventory,
        previous_generation_hash=previous_generation_hash,
        auth_boundary_status=auth_boundary_status,
        network_policy_status=network_policy_status,
        direct_write_credentials_zero=direct_write_credentials_zero,
        read_endpoint_write_denied_status=read_endpoint_write_denied_status,
    )
    if activation_hash is not None and activation_hash != computed:
        raise QdrantGatewayContractError("activation_hash_mismatch")
    return QdrantCoverageActivationAnchor(
        generation=generation,
        marker_collection=marker_collection,
        rendered_inventory=rendered_inventory,
        previous_generation_hash=previous_generation_hash,
        auth_boundary_status=auth_boundary_status,
        network_policy_status=network_policy_status,
        direct_write_credentials_zero=direct_write_credentials_zero,
        read_endpoint_write_denied_status=read_endpoint_write_denied_status,
        activation_hash=computed,
    )


def build_qdrant_coverage_manifest_from_activation_anchor(
    anchor: QdrantCoverageActivationAnchor,
) -> GatewayCoverageManifest:
    if not isinstance(anchor, QdrantCoverageActivationAnchor):
        raise QdrantGatewayContractError("activation_anchor_invalid")
    _validate_rendered_qdrant_inventory(anchor.rendered_inventory)
    return build_gateway_coverage_manifest(
        generation=anchor.generation,
        writers=tuple(
            WriterCoverage(
                writer_ref_hash=binding.writer_ref_hash,
                routes=(binding.route,),
            )
            for binding in QDRANT_SOURCE_REGISTRY.values()
        ),
    )


def _validate_rendered_qdrant_inventory(
    rendered_inventory: tuple[RenderedQdrantWriter, ...],
) -> None:
    if type(rendered_inventory) is not tuple or any(
        not isinstance(item, RenderedQdrantWriter) for item in rendered_inventory
    ):
        raise QdrantGatewayContractError("rendered_inventory_invalid")
    observed = {
        (
            item.source,
            item.route,
            item.writer_ref_hash,
            item.active_caller,
        )
        for item in rendered_inventory
    }
    expected = {
        (
            binding.source,
            binding.route,
            binding.writer_ref_hash,
            binding.active_caller,
        )
        for binding in QDRANT_SOURCE_REGISTRY.values()
    }
    if len(rendered_inventory) != len(observed) or observed != expected:
        raise QdrantGatewayContractError("rendered_inventory_mismatch")


def _qdrant_coverage_activation_hash(
    *,
    generation: int,
    marker_collection: str,
    rendered_inventory: tuple[RenderedQdrantWriter, ...],
    previous_generation_hash: str,
    auth_boundary_status: str,
    network_policy_status: str,
    direct_write_credentials_zero: bool,
    read_endpoint_write_denied_status: str,
) -> str:
    if type(generation) is not int or not 0 < generation < 2**63:
        raise ValueError("qdrant_activation_generation_invalid")
    _validate_collection_name(marker_collection)
    if not _SHA256_RE.fullmatch(previous_generation_hash):
        raise ValueError("previous_generation_hash_invalid")
    _validate_rendered_qdrant_inventory(rendered_inventory)
    encoded = json.dumps(
        {
            "auth_boundary_status": auth_boundary_status,
            "direct_write_credentials_zero": direct_write_credentials_zero,
            "generation": generation,
            "marker_collection": marker_collection,
            "network_policy_status": network_policy_status,
            "previous_generation_hash": previous_generation_hash,
            "read_endpoint_write_denied_status": read_endpoint_write_denied_status,
            "rendered_inventory": [
                {
                    "active_caller": item.active_caller,
                    "workload_ref_hash": item.workload_ref_hash,
                    "image_ref_hash": item.image_ref_hash,
                    "network_policy_ref_hash": item.network_policy_ref_hash,
                    "route": item.route.value,
                    "route_set_hash": item.route_set_hash,
                    "source": item.source.value,
                    "writer_ref_hash": item.writer_ref_hash,
                }
                for item in sorted(
                    rendered_inventory,
                    key=lambda value: value.source.value,
                )
            ],
            "schema_version": "qdrant_coverage_activation.v1",
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assess_qdrant_source_coverage(
    rendered_inventory: tuple[RenderedQdrantWriter, ...] | None = None,
    *,
    auth_boundary_status: str | None = None,
    network_policy_status: str | None = None,
    direct_write_credentials_zero: bool | None = None,
    read_endpoint_write_denied_status: str | None = None,
) -> dict[str, object]:
    """Never claim complete coverage from the source registry by itself."""

    if auth_boundary_status not in {None, "validated"}:
        raise QdrantGatewayContractError("auth_boundary_evidence_invalid")
    if network_policy_status not in {None, "validated"}:
        raise QdrantGatewayContractError("network_policy_evidence_invalid")
    if direct_write_credentials_zero not in {None, True, False}:
        raise QdrantGatewayContractError("direct_write_credential_evidence_invalid")
    if read_endpoint_write_denied_status not in {None, "validated"}:
        raise QdrantGatewayContractError(
            "read_endpoint_write_deny_evidence_invalid"
        )
    evidence = {
        "auth_boundary_status": auth_boundary_status or "missing",
        "network_policy_status": network_policy_status or "missing",
        "direct_write_credentials_status": (
            "validated_zero"
            if direct_write_credentials_zero is True
            else "failed_nonzero"
            if direct_write_credentials_zero is False
            else "missing"
        ),
        "read_endpoint_write_denied_status": (
            read_endpoint_write_denied_status or "missing"
        ),
    }

    inactive = tuple(
        source.value
        for source, binding in QDRANT_SOURCE_REGISTRY.items()
        if not binding.active_caller
    )
    if rendered_inventory is None:
        return {
            "coverage_status": "unverified",
            "rendered_inventory_status": "missing",
            "active_source_count": len(ACTIVE_QDRANT_MUTATION_SOURCES),
            "inactive_sources": inactive,
            **evidence,
        }
    if type(rendered_inventory) is not tuple or any(
        not isinstance(item, RenderedQdrantWriter) for item in rendered_inventory
    ):
        raise QdrantGatewayContractError("rendered_inventory_invalid")
    _validate_rendered_qdrant_inventory(rendered_inventory)
    complete = (
        auth_boundary_status == "validated"
        and network_policy_status == "validated"
        and direct_write_credentials_zero is True
        and read_endpoint_write_denied_status == "validated"
    )
    return {
        "coverage_status": "complete" if complete else "unverified",
        "rendered_inventory_status": "validated",
        "active_source_count": len(ACTIVE_QDRANT_MUTATION_SOURCES),
        "inactive_sources": inactive,
        **evidence,
    }


@dataclass(frozen=True)
class AuthenticatedQdrantSubject:
    subject_ref_hash: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.subject_ref_hash):
            raise ValueError("qdrant_subject_ref_invalid")


class QdrantRouteAuthorizer(Protocol):
    def authorize(
        self,
        *,
        subject: AuthenticatedQdrantSubject,
        source: QdrantMutationSource,
        collection_name: str,
    ) -> bool: ...


@dataclass(frozen=True)
class ExactRouteAuthorizer:
    """Small injectable authz boundary populated by the authenticated runtime."""

    bindings: tuple[tuple[str, QdrantMutationSource, str], ...]

    def __post_init__(self) -> None:
        if type(self.bindings) is not tuple or not self.bindings:
            raise ValueError("qdrant_authorization_bindings_missing")
        for subject_ref, source, collection_name in self.bindings:
            if not _SHA256_RE.fullmatch(subject_ref):
                raise ValueError("qdrant_authorization_subject_invalid")
            if not isinstance(source, QdrantMutationSource):
                raise ValueError("qdrant_authorization_source_invalid")
            _validate_collection_name(collection_name)

    def authorize(
        self,
        *,
        subject: AuthenticatedQdrantSubject,
        source: QdrantMutationSource,
        collection_name: str,
    ) -> bool:
        return (subject.subject_ref_hash, source, collection_name) in self.bindings


def build_authenticated_qdrant_route_from_env(
    environ: Mapping[str, object],
    *,
    source: QdrantMutationSource,
    collection_name: str,
) -> tuple[AuthenticatedQdrantSubject, ExactRouteAuthorizer]:
    """Build authz only from an authenticated subject ref injected by runtime."""

    if not isinstance(source, QdrantMutationSource):
        raise ValueError("qdrant_mutation_source_invalid")
    _validate_collection_name(collection_name)
    raw_subject_ref = environ.get("QDRANT_GATEWAY_SUBJECT_REF_HASH")
    subject = AuthenticatedQdrantSubject(subject_ref_hash=str(raw_subject_ref or ""))
    return subject, ExactRouteAuthorizer(
        bindings=((subject.subject_ref_hash, source, collection_name),)
    )


@dataclass(frozen=True)
class QdrantCollectionPolicy:
    product_collections: tuple[str, ...]
    marker_collection: str
    max_items_per_mutation: int = 256

    def __post_init__(self) -> None:
        if type(self.product_collections) is not tuple or not self.product_collections:
            raise ValueError("qdrant_product_allowlist_missing")
        if len(set(self.product_collections)) != len(self.product_collections):
            raise ValueError("qdrant_product_allowlist_duplicate")
        for collection_name in self.product_collections:
            _validate_collection_name(collection_name)
        _validate_collection_name(self.marker_collection)
        if type(self.max_items_per_mutation) is not int or not 1 <= self.max_items_per_mutation <= 10_000:
            raise ValueError("qdrant_mutation_bound_invalid")

    def require_product_collection(self, collection_name: str) -> None:
        if collection_name not in self.product_collections:
            raise PermissionError("qdrant_collection_not_allowed")

    def require_managed_collection(self, collection_name: str) -> None:
        if collection_name != self.marker_collection and collection_name not in self.product_collections:
            raise PermissionError("qdrant_collection_not_allowed")


class QdrantProductWriteTransport(Protocol):
    def upsert_points(self, *, points: Sequence[Any]) -> Any: ...

    def delete_points(self, *, points_selector: Any, item_count: int) -> Any: ...


class _QdrantProductMutationAdapter:
    def __init__(self, *, client: Any, collection_name: str, policy: QdrantCollectionPolicy) -> None:
        policy.require_product_collection(collection_name)
        self._client = client
        self._collection_name = collection_name
        self._policy = policy

    def upsert_points(self, *, points: Sequence[Any]) -> Any:
        item_count = _validate_points(points, maximum=self._policy.max_items_per_mutation)
        del item_count
        models = _models()
        result = self._client.upsert(
            collection_name=self._collection_name,
            points=list(points),
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )
        _validate_update_result(result)
        return result

    def delete_points(self, *, points_selector: Any, item_count: int) -> Any:
        _validate_item_count(item_count, maximum=self._policy.max_items_per_mutation)
        _validate_points_selector(points_selector, item_count=item_count)
        models = _models()
        result = self._client.delete(
            collection_name=self._collection_name,
            points_selector=points_selector,
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )
        _validate_update_result(result)
        return result


class DirectQdrantWriteTransport:
    """Explicit Foundation compatibility transport with no marker wrapping."""

    def __init__(self, *, client: Any, collection_name: str, policy: QdrantCollectionPolicy) -> None:
        self._adapter = _QdrantProductMutationAdapter(
            client=client,
            collection_name=collection_name,
            policy=policy,
        )

    def upsert_points(self, *, points: Sequence[Any]) -> Any:
        return self._adapter.upsert_points(points=points)

    def delete_points(self, *, points_selector: Any, item_count: int) -> Any:
        return self._adapter.delete_points(points_selector=points_selector, item_count=item_count)


class QdrantWriteGatewayTransport:
    """Fixed-entrypoint product transport bound to exactly one source enum."""

    def __init__(
        self,
        *,
        gateway: QdrantWriteGateway,
        product_adapter: _QdrantProductMutationAdapter,
        source: QdrantMutationSource,
        policy: QdrantCollectionPolicy,
    ) -> None:
        if not isinstance(source, QdrantMutationSource):
            raise ValueError("qdrant_mutation_source_invalid")
        self._gateway = gateway
        self._product_adapter = product_adapter
        self._source = source
        self._route = QDRANT_SOURCE_REGISTRY[source].route
        self._policy = policy

    @property
    def source(self) -> QdrantMutationSource:
        return self._source

    @property
    def route(self) -> QdrantMutationRoute:
        return self._route

    def upsert_points(self, *, points: Sequence[Any]) -> Any:
        return self.mutate_upsert_points(points=points).product_result

    def mutate_upsert_points(
        self, *, points: Sequence[Any]
    ) -> GatewayMutationResult:
        item_count = _validate_points(points, maximum=self._policy.max_items_per_mutation)
        return self._gateway.mutate(
            GatewayMutationRequest(
                route=self._route,
                kind=QdrantMutationKind.UPSERT_POINTS,
                item_count=item_count,
            ),
            lambda: self._product_adapter.upsert_points(points=points),
        )

    def delete_points(self, *, points_selector: Any, item_count: int) -> Any:
        return self.mutate_delete_points(
            points_selector=points_selector,
            item_count=item_count,
        ).product_result

    def mutate_delete_points(
        self, *, points_selector: Any, item_count: int
    ) -> GatewayMutationResult:
        _validate_item_count(item_count, maximum=self._policy.max_items_per_mutation)
        _validate_points_selector(points_selector, item_count=item_count)
        return self._gateway.mutate(
            GatewayMutationRequest(
                route=self._route,
                kind=QdrantMutationKind.DELETE_POINTS,
                item_count=item_count,
            ),
            lambda: self._product_adapter.delete_points(
                points_selector=points_selector,
                item_count=item_count,
            ),
        )


def build_qdrant_gateway_transport(
    *,
    client: Any,
    collection_name: str,
    source: QdrantMutationSource,
    subject: AuthenticatedQdrantSubject,
    authorizer: QdrantRouteAuthorizer,
    policy: QdrantCollectionPolicy,
    marker_store: QdrantMarkerStore,
    generation: int,
    pod_ref_hash: str,
    workload_ref_hash: str,
    route_set_hash: str,
) -> QdrantWriteGatewayTransport:
    if not isinstance(source, QdrantMutationSource):
        raise ValueError("qdrant_mutation_source_invalid")
    if source is QdrantMutationSource.OPERATOR_MAINTENANCE:
        raise PermissionError("operator_source_not_product_route")
    policy.require_product_collection(collection_name)
    if not isinstance(subject, AuthenticatedQdrantSubject):
        raise PermissionError("qdrant_subject_required")
    if not authorizer.authorize(
        subject=subject,
        source=source,
        collection_name=collection_name,
    ):
        raise PermissionError("qdrant_route_unauthorized")
    return QdrantWriteGatewayTransport(
        gateway=QdrantWriteGateway(
            marker_store=marker_store,
            generation=generation,
            writer_ref_hash=QDRANT_SOURCE_REGISTRY[source].writer_ref_hash,
            pod_ref_hash=pod_ref_hash,
            workload_ref_hash=workload_ref_hash,
            route_set_hash=route_set_hash,
        ),
        product_adapter=_QdrantProductMutationAdapter(
            client=client,
            collection_name=collection_name,
            policy=policy,
        ),
        source=source,
        policy=policy,
    )


class QdrantMutationMarkerStore:
    """Qdrant 1.18 marker adapter with conditional modes and readback proof."""

    def __init__(self, *, client: Any, policy: QdrantCollectionPolicy) -> None:
        self._client = client
        self._collection_name = policy.marker_collection

    def append_start(
        self, event: MarkerStartEvent, *, options: MarkerWriteOptions
    ) -> MarkerReceipt:
        if options.mode is not MarkerWriteMode.INSERT_ONLY:
            raise QdrantGatewayContractError("marker_start_mode_invalid")
        payload = {
            "schema_version": "qdrant_mutation_marker.v1",
            "record_kind": QDRANT_MARKER_EVENT_RECORD_KIND,
            "phase": QDRANT_MARKER_PHASE_START,
            "operation_ref": event.operation_ref,
            "route": event.route.value,
            "kind": event.kind.value,
            "item_count": event.item_count,
            "generation": event.generation,
            "writer_ref_hash": event.writer_ref_hash,
            "pod_ref_hash": event.pod_ref_hash,
            "workload_ref_hash": event.workload_ref_hash,
            "route_set_hash": event.route_set_hash,
            "bypass": False,
            "unresolved": True,
        }
        return self._write_and_verify(
            point_id=_marker_point_id(event.operation_ref, "start"),
            payload=payload,
            options=options,
        )

    def append_terminal(
        self, event: MarkerTerminalEvent, *, options: MarkerWriteOptions
    ) -> MarkerReceipt:
        if options.mode is not MarkerWriteMode.INSERT_ONLY:
            raise QdrantGatewayContractError("marker_terminal_mode_invalid")
        payload = {
            "schema_version": "qdrant_mutation_marker.v1",
            "record_kind": QDRANT_MARKER_EVENT_RECORD_KIND,
            "phase": QDRANT_MARKER_PHASE_TERMINAL,
            "operation_ref": event.operation_ref,
            "start_marker_hash": event.start_marker_hash,
            "route": event.route.value,
            "generation": event.generation,
            "writer_ref_hash": event.writer_ref_hash,
            "pod_ref_hash": event.pod_ref_hash,
            "workload_ref_hash": event.workload_ref_hash,
            "route_set_hash": event.route_set_hash,
            "bypass": False,
            "outcome": event.outcome,
        }
        return self._write_and_verify(
            point_id=_marker_point_id(event.operation_ref, "terminal"),
            payload=payload,
            options=options,
        )

    def clear_unresolved(
        self, event: MarkerClearEvent, *, options: MarkerWriteOptions
    ) -> MarkerReceipt:
        if options.mode is not MarkerWriteMode.UPDATE_ONLY:
            raise QdrantGatewayContractError("marker_clear_mode_invalid")
        terminal_payload = self._read_payload(
            _marker_point_id(event.operation_ref, "terminal")
        )
        if terminal_payload.get("marker_hash") != event.terminal_marker_hash:
            raise QdrantGatewayContractError("marker_terminal_readback_mismatch")
        start_marker_hash = terminal_payload.get("start_marker_hash")
        if not isinstance(start_marker_hash, str) or not _SHA256_RE.fullmatch(start_marker_hash):
            raise QdrantGatewayContractError("marker_start_hash_missing")
        terminal_generation = terminal_payload.get("generation")
        terminal_writer_ref_hash = terminal_payload.get("writer_ref_hash")
        terminal_pod_ref_hash = terminal_payload.get("pod_ref_hash")
        terminal_workload_ref_hash = terminal_payload.get("workload_ref_hash")
        terminal_route_set_hash = terminal_payload.get("route_set_hash")
        point_id = _marker_point_id(event.operation_ref, "start")
        start_payload = self._read_payload(point_id)
        if (
            start_payload.get("marker_hash") != start_marker_hash
            or start_payload.get("unresolved") is not True
            or start_payload.get("record_kind") != QDRANT_MARKER_EVENT_RECORD_KIND
            or start_payload.get("phase") != QDRANT_MARKER_PHASE_START
            or start_payload.get("operation_ref") != event.operation_ref
            or start_payload.get("generation") != terminal_generation
            or start_payload.get("writer_ref_hash") != terminal_writer_ref_hash
            or start_payload.get("pod_ref_hash") != terminal_pod_ref_hash
            or start_payload.get("workload_ref_hash") != terminal_workload_ref_hash
            or start_payload.get("route_set_hash") != terminal_route_set_hash
            or start_payload.get("route") != terminal_payload.get("route")
            or start_payload.get("bypass") is not False
            or terminal_payload.get("bypass") is not False
        ):
            raise QdrantGatewayContractError("marker_start_readback_mismatch")
        stored_payload = {**start_payload, "unresolved": False}
        models = _models()
        update_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="unresolved",
                    match=models.MatchValue(value=True),
                ),
                models.FieldCondition(
                    key="marker_hash",
                    match=models.MatchValue(value=start_marker_hash),
                ),
            ]
        )
        return self._write_stored_payload_and_verify(
            point_id=point_id,
            stored_payload=stored_payload,
            receipt_hash=start_marker_hash,
            options=options,
            update_filter=update_filter,
        )

    def _write_and_verify(
        self,
        *,
        point_id: str,
        payload: dict[str, object],
        options: MarkerWriteOptions,
        update_filter: Any | None = None,
    ) -> MarkerReceipt:
        marker_hash = _marker_payload_hash(payload)
        stored_payload = {**payload, "marker_hash": marker_hash}
        return self._write_stored_payload_and_verify(
            point_id=point_id,
            stored_payload=stored_payload,
            receipt_hash=marker_hash,
            options=options,
            update_filter=update_filter,
        )

    def _write_stored_payload_and_verify(
        self,
        *,
        point_id: str,
        stored_payload: dict[str, object],
        receipt_hash: str,
        options: MarkerWriteOptions,
        update_filter: Any | None = None,
    ) -> MarkerReceipt:
        models = _models()
        point = models.PointStruct(id=point_id, vector=[0.0], payload=stored_payload)
        mode = (
            models.UpdateMode.INSERT_ONLY
            if options.mode is MarkerWriteMode.INSERT_ONLY
            else models.UpdateMode.UPDATE_ONLY
        )
        result = self._client.upsert(
            collection_name=self._collection_name,
            points=[point],
            wait=True,
            ordering=models.WriteOrdering.STRONG,
            update_filter=update_filter,
            update_mode=mode,
        )
        event_position = _validate_update_result(result)
        points = self._client.retrieve(
            collection_name=self._collection_name,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
            consistency=models.ReadConsistencyType.ALL,
        )
        if len(points) != 1 or _point_payload(points[0]) != stored_payload:
            raise QdrantGatewayContractError("marker_readback_mismatch")
        return MarkerReceipt(
            accepted=True,
            event_position=event_position,
            marker_hash=receipt_hash,
        )

    def _read_payload(self, point_id: str) -> dict[str, object]:
        models = _models()
        points = self._client.retrieve(
            collection_name=self._collection_name,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
            consistency=models.ReadConsistencyType.ALL,
        )
        if len(points) != 1:
            raise QdrantGatewayContractError("marker_readback_missing")
        return _point_payload(points[0])


def activate_qdrant_marker_collection(
    *,
    client: Any,
    source: QdrantMutationSource,
    subject: AuthenticatedQdrantSubject,
    authorizer: QdrantRouteAuthorizer,
    policy: QdrantCollectionPolicy,
) -> None:
    """Operator-only, idempotent activation of the fixed exact-count scope."""

    if source is not QdrantMutationSource.OPERATOR_MAINTENANCE:
        raise PermissionError("operator_source_required")
    if not isinstance(subject, AuthenticatedQdrantSubject) or not authorizer.authorize(
        subject=subject,
        source=source,
        collection_name=policy.marker_collection,
    ):
        raise PermissionError("qdrant_route_unauthorized")
    policy.require_managed_collection(policy.marker_collection)
    models = _models()
    if not client.collection_exists(policy.marker_collection):
        client.create_collection(
            collection_name=policy.marker_collection,
            vectors_config=models.VectorParams(
                size=1,
                distance=models.Distance.COSINE,
            ),
            shard_number=1,
            replication_factor=1,
            write_consistency_factor=1,
        )
    info = client.get_collection(policy.marker_collection)
    validate_qdrant_marker_collection_topology(info, require_indexes=False)
    payload_schema = _object_member(info, "payload_schema")
    existing_indexes = set(payload_schema)
    for field_name in QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS:
        if field_name in existing_indexes:
            continue
        client.create_payload_index(
            collection_name=policy.marker_collection,
            field_name=field_name,
            field_schema=(
                models.PayloadSchemaType.BOOL
                if field_name == "unresolved"
                else models.PayloadSchemaType.KEYWORD
            ),
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )
    validate_qdrant_marker_collection_topology(
        client.get_collection(policy.marker_collection),
        require_indexes=True,
    )


def validate_qdrant_marker_collection_topology(
    info: object, *, require_indexes: bool
) -> None:
    """Read-only validation shared by operator activation and the runtime gate."""

    _validate_marker_collection_topology(info, require_indexes=require_indexes)


def _validate_marker_collection_topology(
    info: object, *, require_indexes: bool
) -> None:
    try:
        config = _object_member(info, "config")
        params = _object_member(config, "params")
        vectors = _object_member(params, "vectors")
        distance = _object_value(vectors, "distance")
        if (
            _object_value(params, "shard_number") != 1
            or _object_value(params, "replication_factor") != 1
            or _object_value(params, "write_consistency_factor") != 1
            or _object_value(vectors, "size") != 1
            or str(getattr(distance, "value", distance)).lower() != "cosine"
        ):
            raise ValueError
        payload_schema = _object_member(info, "payload_schema")
        if require_indexes:
            for field_name in QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS:
                field_config = payload_schema.get(field_name)
                expected_type = "bool" if field_name == "unresolved" else "keyword"
                observed_type = _object_value(field_config, "data_type")
                normalized_type = str(
                    getattr(observed_type, "value", observed_type)
                ).casefold().rsplit(".", 1)[-1]
                if normalized_type != expected_type:
                    raise ValueError
    except Exception:
        raise QdrantGatewayContractError(
            "qdrant_marker_collection_topology_mismatch"
        ) from None


def _object_member(value: object, name: str) -> Mapping[str, object]:
    member = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
    if not isinstance(member, Mapping) and not hasattr(member, "__dict__"):
        raise ValueError
    return member  # type: ignore[return-value]


def _object_value(value: object, name: str) -> object:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def reconcile_qdrant_marker_metadata(
    *,
    client: Any,
    metadata_point_id: str,
    activation_anchor: QdrantCoverageActivationAnchor | QdrantPendingCutoverAnchor,
    phase: QdrantMarkerMetadataPhase,
    source: QdrantMutationSource,
    subject: AuthenticatedQdrantSubject,
    authorizer: QdrantRouteAuthorizer,
    policy: QdrantCollectionPolicy,
) -> QdrantMarkerMetadataReconcileResult:
    """Initialize/reconcile the singleton marker metadata point operator-only.

    The pending transition is insert-only.  Post-reconcile is update-only and
    accepts only the exact pending payload for the same generation and external
    source-validated activation anchor. Repeating an exact state performs no write.
    """

    if source is not QdrantMutationSource.OPERATOR_MAINTENANCE:
        raise PermissionError("operator_source_required")
    if not isinstance(phase, QdrantMarkerMetadataPhase):
        raise QdrantGatewayContractError("marker_metadata_phase_invalid")
    if not isinstance(subject, AuthenticatedQdrantSubject) or not authorizer.authorize(
        subject=subject,
        source=source,
        collection_name=policy.marker_collection,
    ):
        raise PermissionError("qdrant_route_unauthorized")
    policy.require_managed_collection(policy.marker_collection)
    try:
        parsed_point_id = uuid.UUID(metadata_point_id)
    except (TypeError, ValueError, AttributeError):
        raise QdrantGatewayContractError("marker_metadata_point_id_invalid") from None
    if str(parsed_point_id) != metadata_point_id:
        raise QdrantGatewayContractError("marker_metadata_point_id_invalid")
    if phase is QdrantMarkerMetadataPhase.PENDING_CUTOVER:
        if not isinstance(activation_anchor, QdrantPendingCutoverAnchor):
            raise QdrantGatewayContractError("pending_activation_anchor_required")
    elif not isinstance(activation_anchor, QdrantCoverageActivationAnchor):
        raise QdrantGatewayContractError("complete_activation_anchor_required")
    if activation_anchor.marker_collection != policy.marker_collection:
        raise QdrantGatewayContractError("activation_marker_collection_mismatch")
    if isinstance(activation_anchor, QdrantPendingCutoverAnchor):
        pending_payload = _pending_marker_metadata_payload(activation_anchor)
        complete_payload = None
        coverage = None
        desired = pending_payload
    else:
        coverage = build_qdrant_coverage_manifest_from_activation_anchor(
            activation_anchor
        )
        pending_anchor = build_qdrant_pending_cutover_anchor(
            generation=activation_anchor.generation,
            marker_collection=activation_anchor.marker_collection,
            previous_generation_hash=activation_anchor.previous_generation_hash,
        )
        pending_payload = _pending_marker_metadata_payload(pending_anchor)
        complete_payload = _marker_metadata_payload(
            coverage=coverage,
            activation_anchor=activation_anchor,
            coverage_status="complete",
        )
        desired = complete_payload
    models = _models()
    points = client.retrieve(
        collection_name=policy.marker_collection,
        ids=[metadata_point_id],
        with_payload=True,
        with_vectors=False,
        consistency=models.ReadConsistencyType.ALL,
    )
    if not isinstance(points, list) or len(points) > 1:
        raise QdrantGatewayContractError("marker_metadata_readback_invalid")
    observed = _point_payload(points[0]) if points else None
    if phase is QdrantMarkerMetadataPhase.PENDING_CUTOVER:
        count_result = client.count(
            collection_name=policy.marker_collection,
            count_filter=None,
            exact=True,
        )
        point_count = getattr(count_result, "count", None)
        expected_point_count = 0 if observed is None else 1
        if type(point_count) is not int or point_count != expected_point_count:
            raise QdrantGatewayContractError(
                "marker_generation_collection_not_fresh"
            )
    if observed == desired:
        return QdrantMarkerMetadataReconcileResult(
            status="already_current",
            phase=phase,
        )
    if phase is QdrantMarkerMetadataPhase.PENDING_CUTOVER:
        if observed is not None:
            raise QdrantGatewayContractError("marker_metadata_mismatch")
        update_mode = MarkerWriteMode.INSERT_ONLY
        update_filter = None
        status = "initialized"
    else:
        assert coverage is not None
        _validate_pending_to_complete_continuity(
            observed=observed,
            expected_pending=pending_payload,
        )
        if observed != pending_payload:
            raise QdrantGatewayContractError("marker_metadata_mismatch")
        update_mode = MarkerWriteMode.UPDATE_ONLY
        update_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key=key,
                    match=models.MatchValue(value=pending_payload[key]),
                )
                for key in sorted(QDRANT_EXACT_MARKER_METADATA_KEYS)
            ]
        )
        status = "reconciled"

    result = client.upsert(
        collection_name=policy.marker_collection,
        points=[
            models.PointStruct(
                id=metadata_point_id,
                vector=[0.0],
                payload=desired,
            )
        ],
        wait=True,
        ordering=models.WriteOrdering.STRONG,
        update_filter=update_filter,
        update_mode=(
            models.UpdateMode.INSERT_ONLY
            if update_mode is MarkerWriteMode.INSERT_ONLY
            else models.UpdateMode.UPDATE_ONLY
        ),
    )
    _validate_update_result(result)
    readback = client.retrieve(
        collection_name=policy.marker_collection,
        ids=[metadata_point_id],
        with_payload=True,
        with_vectors=False,
        consistency=models.ReadConsistencyType.ALL,
    )
    if (
        not isinstance(readback, list)
        or len(readback) != 1
        or _point_payload(readback[0]) != desired
    ):
        raise QdrantGatewayContractError("marker_metadata_readback_mismatch")
    return QdrantMarkerMetadataReconcileResult(status=status, phase=phase)


def _validate_pending_to_complete_continuity(
    *,
    observed: dict[str, object] | None,
    expected_pending: dict[str, object],
) -> None:
    if observed is None or observed.get("coverage_status") != "pending_cutover":
        raise QdrantGatewayContractError("marker_metadata_mismatch")
    if observed.get("generation") != expected_pending["generation"]:
        raise QdrantGatewayContractError(
            "marker_generation_continuity_mismatch"
        )
    if (
        observed.get("previous_generation_hash")
        != expected_pending["previous_generation_hash"]
    ):
        raise QdrantGatewayContractError(
            "marker_previous_generation_continuity_mismatch"
        )
    if observed.get("activation_hash") != expected_pending["activation_hash"]:
        raise QdrantGatewayContractError(
            "marker_activation_continuity_mismatch"
        )


def _pending_marker_metadata_payload(
    activation_anchor: QdrantPendingCutoverAnchor,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": QDRANT_EXACT_MARKER_METADATA_SCHEMA,
        "generation": activation_anchor.generation,
        "coverage_hash": activation_anchor.coverage_hash,
        "coverage_status": QdrantMarkerMetadataPhase.PENDING_CUTOVER.value,
        "bypass_count": activation_anchor.bypass_count,
        "activation_hash": activation_anchor.activation_hash,
        "previous_generation_hash": activation_anchor.previous_generation_hash,
    }
    if set(payload) != QDRANT_EXACT_MARKER_METADATA_KEYS:
        raise QdrantGatewayContractError("marker_metadata_fields_invalid")
    return payload


def _marker_metadata_payload(
    *,
    coverage: GatewayCoverageManifest,
    activation_anchor: QdrantCoverageActivationAnchor,
    coverage_status: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": QDRANT_EXACT_MARKER_METADATA_SCHEMA,
        "generation": coverage.generation,
        "coverage_hash": coverage.coverage_hash,
        "coverage_status": coverage_status,
        "bypass_count": coverage.bypass_count,
        "activation_hash": activation_anchor.activation_hash,
        "previous_generation_hash": activation_anchor.previous_generation_hash,
    }
    if set(payload) != QDRANT_EXACT_MARKER_METADATA_KEYS:
        raise QdrantGatewayContractError("marker_metadata_fields_invalid")
    return payload


def provision_qdrant_collection(
    *,
    client: Any,
    collection_name: str,
    vector_size: int,
    payload_index_fields: tuple[str, ...],
    source: QdrantMutationSource,
    subject: AuthenticatedQdrantSubject,
    authorizer: QdrantRouteAuthorizer,
    policy: QdrantCollectionPolicy,
) -> None:
    """Explicit operator-only collection activation; never called by builders."""

    if source is not QdrantMutationSource.OPERATOR_MAINTENANCE:
        raise PermissionError("operator_source_required")
    if not authorizer.authorize(
        subject=subject,
        source=source,
        collection_name=collection_name,
    ):
        raise PermissionError("qdrant_route_unauthorized")
    policy.require_managed_collection(collection_name)
    if type(vector_size) is not int or not 1 <= vector_size <= 65_536:
        raise ValueError("qdrant_vector_size_invalid")
    if (
        type(payload_index_fields) is not tuple
        or len(payload_index_fields) > 64
        or len(set(payload_index_fields)) != len(payload_index_fields)
        or any(
            not isinstance(field, str)
            or not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$", field)
            for field in payload_index_fields
        )
    ):
        raise ValueError("qdrant_payload_index_fields_invalid")
    models = _models()
    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
    )
    for field_name in payload_index_fields:
        client.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


def _validate_collection_name(collection_name: object) -> str:
    if not isinstance(collection_name, str) or not _COLLECTION_RE.fullmatch(collection_name):
        raise ValueError("qdrant_collection_name_invalid")
    return collection_name


def _validate_points(points: Sequence[Any], *, maximum: int) -> int:
    if isinstance(points, (str, bytes, bytearray)) or not isinstance(points, Sequence):
        raise ValueError("mutation_points_invalid")
    item_count = len(points)
    _validate_item_count(item_count, maximum=maximum)
    for point in points:
        if isinstance(point, Mapping):
            point_id = point.get("id")
            vector = point.get("vector")
            payload = point.get("payload")
        else:
            point_id = getattr(point, "id", None)
            vector = getattr(point, "vector", None)
            payload = getattr(point, "payload", None)
        if not isinstance(point_id, (str, int, uuid.UUID)) or point_id == "":
            raise ValueError("mutation_point_schema_invalid")
        if isinstance(vector, (str, bytes, bytearray)) or not isinstance(vector, Sequence):
            raise ValueError("mutation_point_schema_invalid")
        if not 1 <= len(vector) <= 65_536 or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in vector
        ):
            raise ValueError("mutation_point_schema_invalid")
        if not isinstance(payload, Mapping) or any(
            not isinstance(key, str) or not key for key in payload
        ):
            raise ValueError("mutation_point_schema_invalid")
        try:
            json.dumps(payload, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("mutation_point_schema_invalid") from None
    return item_count


def _validate_points_selector(points_selector: object, *, item_count: int) -> None:
    if isinstance(points_selector, (str, bytes, bytearray)):
        raise ValueError("points_selector_invalid")
    if isinstance(points_selector, Sequence):
        point_ids = list(points_selector)
    else:
        point_ids = getattr(points_selector, "points", None)
        if point_ids is None:
            raise ValueError("points_selector_invalid")
        point_ids = list(point_ids)
    if len(point_ids) != item_count or any(
        not isinstance(point_id, (str, int, uuid.UUID)) or point_id == ""
        for point_id in point_ids
    ):
        raise ValueError("points_selector_invalid")


def _validate_item_count(item_count: object, *, maximum: int) -> int:
    if type(item_count) is not int or item_count < 1:
        raise ValueError("mutation_item_count_invalid")
    if item_count > maximum:
        raise ValueError("mutation_item_count_exceeded")
    return item_count


def _models() -> Any:
    try:
        from qdrant_client import models
    except ImportError as exc:  # pragma: no cover - optional dependency guard
        raise RuntimeError("qdrant_client_required_for_gateway") from exc
    return models


def _marker_point_id(operation_ref: str, stage: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"qdrant-marker:{operation_ref}:{stage}"))


def _marker_payload_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_update_result(result: object) -> int:
    status = getattr(result, "status", None)
    operation_id = getattr(result, "operation_id", None)
    if isinstance(result, Mapping):
        status = result.get("status")
        operation_id = result.get("operation_id")
    status_value = str(getattr(status, "value", status) or "").lower()
    if status_value != "completed":
        raise QdrantGatewayContractError("qdrant_write_not_completed")
    if type(operation_id) is not int or operation_id < 1:
        raise QdrantGatewayContractError("qdrant_operation_position_invalid")
    return operation_id


def _point_payload(point: object) -> dict[str, object]:
    if isinstance(point, Mapping):
        payload = point.get("payload")
    else:
        payload = getattr(point, "payload", None)
    return dict(payload) if isinstance(payload, Mapping) else {}


__all__ = [
    "ACTIVE_QDRANT_MUTATION_SOURCES",
    "DEFAULT_QDRANT_MARKER_COLLECTION",
    "QDRANT_EXACT_MARKER_METADATA_KEYS",
    "QDRANT_EXACT_MARKER_METADATA_SCHEMA",
    "QDRANT_MARKER_EVENT_RECORD_KIND",
    "QDRANT_MARKER_PHASE_START",
    "QDRANT_MARKER_PHASE_TERMINAL",
    "QDRANT_MARKER_REQUIRED_PAYLOAD_INDEX_FIELDS",
    "QDRANT_SOURCE_REGISTRY",
    "AuthenticatedQdrantSubject",
    "DirectQdrantWriteTransport",
    "ExactRouteAuthorizer",
    "FoundationDirectWriteContract",
    "QdrantCoverageActivationAnchor",
    "QdrantCollectionPolicy",
    "QdrantMutationMarkerStore",
    "QdrantMutationSource",
    "QdrantWriteActivation",
    "QdrantMarkerMetadataPhase",
    "QdrantMarkerMetadataReconcileResult",
    "QdrantPendingCutoverAnchor",
    "QdrantProductWriteTransport",
    "QdrantRouteAuthorizer",
    "QdrantSourceBinding",
    "QdrantWriteGatewayTransport",
    "RenderedQdrantWriter",
    "activate_qdrant_marker_collection",
    "assess_qdrant_source_coverage",
    "build_authenticated_qdrant_route_from_env",
    "build_qdrant_exact_marker_hash",
    "build_qdrant_coverage_activation_anchor",
    "build_qdrant_coverage_manifest_from_activation_anchor",
    "build_qdrant_gateway_transport",
    "build_qdrant_pending_cutover_anchor",
    "provision_qdrant_collection",
    "qdrant_write_activation_from_environment",
    "qdrant_write_gateway_generation_from_environment",
    "reconcile_qdrant_marker_metadata",
    "validate_qdrant_marker_collection_topology",
]

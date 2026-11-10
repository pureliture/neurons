"""Compose the exact four-plane permission-audit marker runtime.

The composition boundary accepts already configured, narrow read-only sources.  It
does not know storage endpoints, paths, collection names, product identifiers, or
credentials, and it returns only the source-owned canonical marker reader.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .permission_audit import IndependentProductMutationMarkerReader
from .postgres_exact_mutation_marker import (
    PostgresExactMutationMarkerContract,
    PostgresExactMutationMarkerReader,
)
class PermissionAuditMarkerRuntimeError(RuntimeError):
    """Fail-closed runtime composition or resource-lifecycle failure."""


class _PostgresFenceLease:
    def __init__(self, *, connection: object, fence: object) -> None:
        if not callable(getattr(fence, "read_marker", None)) or not callable(
            getattr(fence, "release", None)
        ):
            raise PermissionAuditMarkerRuntimeError(
                "PostgreSQL exact marker fence is malformed"
            )
        self._connection = connection
        self._fence = fence
        self._released = False

    def read_marker(self) -> object:
        if self._released:
            raise PermissionAuditMarkerRuntimeError(
                "PostgreSQL exact marker fence is not active"
            )
        owned_connection = getattr(self._fence, "_connection", self._connection)
        if owned_connection is not self._connection:
            raise PermissionAuditMarkerRuntimeError(
                "PostgreSQL exact marker connection was replaced"
            )
        return self._fence.read_marker()  # type: ignore[attr-defined]

    def release(self) -> None:
        if self._released:
            raise PermissionAuditMarkerRuntimeError(
                "PostgreSQL exact marker fence is not active"
            )
        self._released = True
        release_error: BaseException | None = None
        try:
            self._fence.release()  # type: ignore[attr-defined]
        except BaseException as exc:
            release_error = exc
        try:
            self._connection.close()  # type: ignore[attr-defined]
        except BaseException as close_error:
            raise PermissionAuditMarkerRuntimeError(
                "PostgreSQL exact marker connection close failed"
            ) from (release_error or close_error)
        if release_error is not None:
            raise PermissionAuditMarkerRuntimeError(
                "PostgreSQL exact marker fence release failed"
            ) from release_error


class _PostgresFenceFactory:
    def __init__(
        self,
        *,
        contract: PostgresExactMutationMarkerContract,
        connection_factory: Callable[[], object],
    ) -> None:
        if not callable(connection_factory):
            raise ValueError("PostgreSQL read-only connection factory is required")
        self._reader = PostgresExactMutationMarkerReader(contract)
        self._connection_factory = connection_factory

    def __call__(self) -> _PostgresFenceLease:
        try:
            connection = self._connection_factory()
        except BaseException as factory_error:
            raise PermissionAuditMarkerRuntimeError(
                "PostgreSQL read-only connection is unavailable"
            ) from factory_error
        if not callable(getattr(connection, "close", None)):
            raise PermissionAuditMarkerRuntimeError(
                "PostgreSQL read-only connection is malformed"
            )
        try:
            fence = self._reader.acquire_audit_fence(connection)
        except BaseException as acquire_error:
            try:
                connection.close()
            except BaseException as close_error:
                raise PermissionAuditMarkerRuntimeError(
                    "PostgreSQL exact marker connection close failed"
                ) from close_error
            raise PermissionAuditMarkerRuntimeError(
                "PostgreSQL exact marker fence acquisition failed"
            ) from acquire_error
        try:
            return _PostgresFenceLease(connection=connection, fence=fence)
        except BaseException as lease_error:
            release_error: BaseException | None = None
            try:
                release = getattr(fence, "release", None)
                if callable(release):
                    release()
            except BaseException as exc:
                release_error = exc
            try:
                connection.close()
            except BaseException as close_error:
                raise PermissionAuditMarkerRuntimeError(
                    "PostgreSQL exact marker connection close failed"
                ) from close_error
            if release_error is not None:
                raise PermissionAuditMarkerRuntimeError(
                    "PostgreSQL exact marker fence release failed"
                ) from release_error
            raise lease_error




def build_permission_audit_marker_reader(
    *,
    postgres_contract: PostgresExactMutationMarkerContract,
    postgres_connection_factory: Callable[[], object],
    couchdb_provider: Callable[[], Mapping[str, object]],
    nats_provider: Callable[[], Mapping[str, object]],
    sqlite_provider: Callable[[], Mapping[str, object]],
) -> IndependentProductMutationMarkerReader:
    """Return one exact, source-owned four-plane marker reader.

    Production callers must supply the PostgreSQL contract returned by
    ``build_source_owned_postgres_exact_marker_contract`` so every ledger source
    table remains covered.  The arbitrary contract seam exists for isolated tests.
    """

    postgres_fence_factory = _PostgresFenceFactory(
        contract=postgres_contract,
        connection_factory=postgres_connection_factory,
    )
    return IndependentProductMutationMarkerReader(
        authority_fence_factory=postgres_fence_factory,
        providers={
            "corpus": couchdb_provider,
            "queue": nats_provider,
            "product_db": sqlite_provider,
        },
    )


__all__ = [
    "PermissionAuditMarkerRuntimeError",
    "build_permission_audit_marker_reader",
]

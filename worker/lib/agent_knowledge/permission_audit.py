from __future__ import annotations

import hashlib
import json
import secrets
import ssl
import urllib.request
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .permission_audit_contract import PERMISSION_AUDIT_POLICY
from .public_safe_util import ensure_public_safe, hash_payload, require_sha256


PERMISSION_AUDIT_TOOL_NAME = "brain_permission_sensitive_audit_probe"
PERMISSION_AUDIT_EVIDENCE_SCHEMA = "permission_sensitive_runtime_audit_evidence.v2"
PERMISSION_AUDIT_EVENT_SCHEMA = "runtime_permission_audit_event.v2"
PERMISSION_AUDIT_AUDIENCE = "neurons-permission-audit"
PERMISSION_AUDIT_SUBJECT = (
    "system:serviceaccount:jenkins:neurons-release-production-evidence"
)
DEFAULT_PERMISSION_AUDIT_STORE_URL = "http://127.0.0.1:8771"
DEFAULT_TOKEN_REVIEW_URL = (
    "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
)
_SERVICE_ACCOUNT_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_SERVICE_ACCOUNT_CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

_STORE_RESULT_KEYS = frozenset(
    {
        "status",
        "append_count",
        "stored_row_count",
        "read_after_write_status",
        "request_hash",
        "production_mutation_performed",
    }
)
_STORE_READBACK_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "stored_row_count",
        "request_hash",
        "append_attempt_hash",
        "actor_ref_hash",
        "action",
        "permission",
        "authority_write_performed",
        "production_mutation_performed",
    }
)
_SENTINEL_KEYS = frozenset(
    {
        "schema_version",
        "authority_ledger",
        "corpus",
        "queue",
        "index",
        "product_db",
    }
)
_SENTINEL_VALUE_KEYS = frozenset({"count", "hash"})
_BOUNDED_DENIAL_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "action",
        "ledger_scope",
        "permission",
        "authority_write_performed",
        "production_mutation_performed",
        "actor_ref_hash",
        "request_hash",
        "protected_values_returned",
        "raw_private_evidence_returned",
        "secret_returned",
        "host_topology_returned",
        "raw_external_ids_returned",
    }
)
_INTERNAL_STORE_RESULT_KEYS = frozenset(
    {*_STORE_RESULT_KEYS, *_BOUNDED_DENIAL_RESULT_KEYS}
)
_STORE_APPEND_RESPONSE_KEYS = frozenset(
    {*_INTERNAL_STORE_RESULT_KEYS, "append_attempt_hash"}
)

_SENTINEL_PLANE_NAMES = (
    "authority_ledger",
    "corpus",
    "queue",
    "index",
    "product_db",
)


class KubernetesTokenReviewer:
    """One-shot in-cluster TokenReview client with no retry or raw output."""

    def __init__(
        self,
        api_url: str = DEFAULT_TOKEN_REVIEW_URL,
        *,
        reviewer_token_reader: Callable[[], str] | None = None,
        ssl_context_factory: Callable[[], Any] | None = None,
        urlopen: Callable[..., Any] | None = None,
    ) -> None:
        _validate_token_review_url(api_url)
        self._api_url = api_url
        self._reviewer_token_reader = reviewer_token_reader or (
            lambda: _SERVICE_ACCOUNT_TOKEN_PATH.read_text(encoding="utf-8").strip()
        )
        self._ssl_context_factory = ssl_context_factory or (
            lambda: ssl.create_default_context(cafile=str(_SERVICE_ACCOUNT_CA_PATH))
        )
        self._urlopen = urlopen or _urlopen_without_redirects

    def __call__(self, projected_token: str) -> Mapping[str, Any]:
        try:
            reviewer_token = self._reviewer_token_reader()
            if not reviewer_token:
                raise ValueError("missing reviewer token")
            body = json.dumps(
                {
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "TokenReview",
                    "spec": {
                        "token": projected_token,
                        "audiences": [PERMISSION_AUDIT_AUDIENCE],
                    },
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            request = urllib.request.Request(
                self._api_url,
                data=body,
                headers={
                    "Authorization": f"Bearer {reviewer_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            with self._urlopen(
                request,
                timeout=5,
                context=self._ssl_context_factory(),
            ) as response:
                result = _read_json_response(response)
        except Exception:
            raise RuntimeError("permission audit token review failed") from None
        if not isinstance(result, Mapping):
            raise RuntimeError("permission audit token review failed")
        return result


class LoopbackPermissionAuditStoreClient:
    """No-retry client for the co-Pod loopback-only append primitive."""

    def __init__(
        self,
        base_url: str = DEFAULT_PERMISSION_AUDIT_STORE_URL,
        *,
        urlopen: Callable[..., Any] | None = None,
        attempt_hash_factory: Callable[[], str] | None = None,
    ) -> None:
        _validate_loopback_store_url(base_url)
        self._base_url = base_url.rstrip("/")
        self._urlopen = urlopen or _urlopen_without_redirects
        self._attempt_hash_factory = attempt_hash_factory or (
            lambda: "sha256:" + hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        )

    def append_denied_once(
        self,
        *,
        request_hash: str,
        actor_ref_hash: str,
        action: str,
    ) -> Mapping[str, Any]:
        payload = {
            "request_hash": require_sha256(request_hash, "request_hash"),
            "actor_ref_hash": require_sha256(actor_ref_hash, "actor_ref_hash"),
            "action": action,
            "append_attempt_hash": require_sha256(
                self._attempt_hash_factory(),
                "append_attempt_hash",
            ),
        }
        try:
            result = self._post_json("/append-denied-once", payload)
        except Exception:
            try:
                readback = self._post_json(
                    "/readback",
                    {"request_hash": payload["request_hash"]},
                )
                return _normalize_recovered_store_readback(
                    readback,
                    request_hash=payload["request_hash"],
                    actor_ref_hash=payload["actor_ref_hash"],
                    action=payload["action"],
                    append_attempt_hash=payload["append_attempt_hash"],
                )
            except Exception:
                raise RuntimeError("permission audit store request failed") from None
        try:
            return _normalize_store_append_response(
                result,
                request_hash=payload["request_hash"],
                actor_ref_hash=payload["actor_ref_hash"],
                append_attempt_hash=payload["append_attempt_hash"],
            )
        except Exception:
            raise RuntimeError("permission audit store request failed") from None

    def _post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        request = urllib.request.Request(
            self._base_url + path,
            data=json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with self._urlopen(request, timeout=5) as response:
            return _read_json_response(response)


def _normalize_store_append_response(
    value: Any,
    *,
    request_hash: str,
    actor_ref_hash: str,
    append_attempt_hash: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _STORE_APPEND_RESPONSE_KEYS:
        raise ValueError("permission audit store response is malformed")
    stored_attempt_hash = require_sha256(
        str(value.get("append_attempt_hash") or ""),
        "append_attempt_hash",
    )
    expected_append_count = int(stored_attempt_hash == append_attempt_hash)
    if (
        value.get("status") != "recorded"
        or value.get("append_count") != expected_append_count
        or isinstance(value.get("append_count"), bool)
        or value.get("stored_row_count") != 1
        or isinstance(value.get("stored_row_count"), bool)
        or value.get("read_after_write_status") != "validated"
        or value.get("request_hash") != request_hash
        or value.get("production_mutation_performed") is not False
    ):
        raise ValueError("permission audit store response is malformed")
    _validated_bounded_denial_result(
        {str(key): value[key] for key in _BOUNDED_DENIAL_RESULT_KEYS},
        request_hash=request_hash,
        actor_ref_hash=actor_ref_hash,
    )
    return {str(key): value[key] for key in _INTERNAL_STORE_RESULT_KEYS}


def _normalize_recovered_store_readback(
    value: Any,
    *,
    request_hash: str,
    actor_ref_hash: str,
    action: str,
    append_attempt_hash: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _STORE_READBACK_KEYS:
        raise ValueError("permission audit store readback is malformed")
    stored_attempt_hash = require_sha256(
        str(value.get("append_attempt_hash") or ""),
        "append_attempt_hash",
    )
    if (
        value.get("schema_version") != "permission_audit_store_readback.v2"
        or value.get("status") != "recorded"
        or value.get("stored_row_count") != 1
        or isinstance(value.get("stored_row_count"), bool)
        or value.get("request_hash") != request_hash
        or value.get("actor_ref_hash") != actor_ref_hash
        or value.get("action") != action
        or value.get("permission") != "denied"
        or value.get("authority_write_performed") is not False
        or value.get("production_mutation_performed") is not False
    ):
        raise ValueError("permission audit store readback is malformed")
    return {
        "status": "recorded",
        "append_count": int(stored_attempt_hash == append_attempt_hash),
        "stored_row_count": 1,
        "read_after_write_status": "validated",
        "request_hash": request_hash,
        "schema_version": "bounded_permission_denial_result.v1",
        "action": action,
        "ledger_scope": "production",
        "permission": "denied",
        "authority_write_performed": False,
        "production_mutation_performed": False,
        "actor_ref_hash": actor_ref_hash,
        "protected_values_returned": False,
        "raw_private_evidence_returned": False,
        "secret_returned": False,
        "host_topology_returned": False,
        "raw_external_ids_returned": False,
    }


class IndependentProductMutationSentinelReader:
    """Compose five independently configured, read-only plane observations."""

    def __init__(self, providers: Mapping[str, Callable[[], Mapping[str, Any]]]) -> None:
        if not isinstance(providers, Mapping) or set(providers) != set(
            _SENTINEL_PLANE_NAMES
        ):
            raise ValueError("all independent mutation sentinel providers are required")
        if any(not callable(providers[name]) for name in _SENTINEL_PLANE_NAMES):
            raise ValueError("all independent mutation sentinel providers are required")
        if len(
            {
                _sentinel_provider_identity(providers[name])
                for name in _SENTINEL_PLANE_NAMES
            }
        ) != len(
            _SENTINEL_PLANE_NAMES
        ):
            raise ValueError("distinct mutation sentinel providers are required")
        self._providers = {name: providers[name] for name in _SENTINEL_PLANE_NAMES}

    def __call__(self) -> Mapping[str, Any]:
        result: dict[str, Any] = {"schema_version": "product_mutation_sentinel.v1"}
        for name in _SENTINEL_PLANE_NAMES:
            result[name] = _validated_plane_sentinel(self._providers[name]())
        ensure_public_safe(result, "ProductMutationSentinel")
        return result


def _sentinel_provider_identity(
    provider: Callable[[], Mapping[str, Any]],
) -> tuple[str, int]:
    candidate: Any = provider
    while isinstance(candidate, partial):
        candidate = candidate.func
    owner = getattr(candidate, "__self__", None)
    if owner is not None:
        return ("bound_owner", id(owner))
    return ("callable", id(candidate))


class PostgresDatabaseMutationMarkerReader:
    """Read one product-plane-owned public-safe marker without global DB state."""

    _SQL = """
        SELECT mutation_count AS count,
               mutation_hash AS hash
        FROM public.permission_audit_product_db_mutation_marker_v1
        LIMIT 2
    """

    def __init__(self, ledger: Any) -> None:
        adapter = getattr(ledger, "_db_adapter", None)
        if (
            getattr(ledger, "read_only", False) is not True
            or adapter is None
            or getattr(adapter, "is_file_backed", True)
        ):
            raise ValueError("read-only PostgreSQL ledger is required")
        self._ledger = ledger

    def __call__(self) -> Mapping[str, Any]:
        with self._ledger._connect() as connection:
            if getattr(connection, "dialect", "") != "postgres":
                raise RuntimeError("PostgreSQL mutation marker is unavailable")
            return _read_postgres_database_marker(connection)


def _read_postgres_database_marker(connection: Any) -> dict[str, Any]:
    rows = connection.execute(PostgresDatabaseMutationMarkerReader._SQL).fetchall()
    if len(rows) != 1:
        raise RuntimeError("PostgreSQL mutation marker is unavailable")
    row = rows[0]
    try:
        if set(row.keys()) != _SENTINEL_VALUE_KEYS:
            raise ValueError("unexpected marker columns")
        return _validated_plane_sentinel(
            {
                "count": row.get("count"),
                "hash": row.get("hash"),
            }
        )
    except (AttributeError, TypeError, ValueError):
        raise RuntimeError("PostgreSQL mutation marker is unavailable") from None


def _validate_token_review_url(value: str) -> None:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("permission audit TokenReview URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "kubernetes.default.svc"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/apis/authentication.k8s.io/v1/tokenreviews"
    ):
        raise ValueError("permission audit TokenReview URL is invalid")


def _validate_loopback_store_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port is None
    ):
        raise ValueError("permission audit store URL must be loopback")


def _urlopen_without_redirects(request: Any, **kwargs: Any) -> Any:
    context = kwargs.pop("context", None)
    handlers: list[Any] = [urllib.request.ProxyHandler({}), _NoRedirectHandler()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(request, **kwargs)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _read_json_response(response: Any) -> Any:
    if int(getattr(response, "status", 0)) != 200:
        raise ValueError("unexpected HTTP status")
    body = response.read(65537)
    if len(body) > 65536:
        raise ValueError("HTTP response too large")
    return json.loads(body)


def _validated_plane_sentinel(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SENTINEL_VALUE_KEYS:
        raise ValueError("permission audit plane sentinel is malformed")
    count = value.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("permission audit plane sentinel is malformed")
    digest = require_sha256(str(value.get("hash") or ""), "sentinel hash")
    return {"count": count, "hash": digest}


def run_permission_sensitive_audit_probe(
    *,
    enabled: bool,
    mode: str,
    operation_hash: str,
    build_association_hash: str,
    projected_service_account_token: str,
    token_reviewer: Callable[[str], Mapping[str, Any]] | None,
    store_append: Callable[..., Mapping[str, Any]] | None,
    product_sentinel_reader: Callable[[], Mapping[str, Any]] | None,
) -> dict[str, Any]:
    if not enabled:
        raise RuntimeError("permission audit probe is disabled")
    if mode != "deny_once":
        raise ValueError("permission audit mode is invalid")
    request_hash = require_sha256(operation_hash, "operation_hash")
    association_hash = require_sha256(
        build_association_hash,
        "build_association_hash",
    )
    if (
        not isinstance(projected_service_account_token, str)
        or not projected_service_account_token
        or len(projected_service_account_token) > 16384
    ):
        raise ValueError("permission audit token is invalid")
    if (
        token_reviewer is None
        or store_append is None
        or product_sentinel_reader is None
    ):
        raise RuntimeError("permission audit probe configuration is incomplete")

    actor_ref_hash = _validated_actor_ref_hash(token_reviewer(projected_service_account_token))
    before = _validated_product_sentinel(product_sentinel_reader())
    store_result = _validated_store_result(
        store_append(
            request_hash=request_hash,
            actor_ref_hash=actor_ref_hash,
            action=PERMISSION_AUDIT_POLICY,
        ),
        request_hash=request_hash,
        actor_ref_hash=actor_ref_hash,
    )
    after = _validated_product_sentinel(product_sentinel_reader())
    sentinels_match = before == after
    action_count = int(store_result["append_count"])
    events = []
    if action_count == 1:
        events.append(
            _validated_bounded_denial_result(
                {
                    str(key): store_result[key]
                    for key in _BOUNDED_DENIAL_RESULT_KEYS
                },
                request_hash=request_hash,
                actor_ref_hash=actor_ref_hash,
            )
        )
    store = {str(key): store_result[key] for key in _STORE_RESULT_KEYS}
    evidence = {
        "schema_version": PERMISSION_AUDIT_EVIDENCE_SCHEMA,
        "policy": PERMISSION_AUDIT_POLICY,
        "build_association_hash": association_hash,
        "transport_call_count": 1,
        "permission_action_count": action_count,
        "audit_events": events,
        "audit_store": store,
        "postcheck": {
            "status": "validated" if sentinels_match else "failed",
            "product_mutation_sentinels_match": sentinels_match,
            "unexpected_runtime_mutation_count": 0 if sentinels_match else 1,
            "protected_values_returned": False,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        },
        "production_mutation_performed": False,
    }
    ensure_public_safe(evidence, "PermissionSensitiveAuditEvidence")
    return evidence


def _validated_bounded_denial_result(
    value: Mapping[str, Any],
    *,
    request_hash: str,
    actor_ref_hash: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BOUNDED_DENIAL_RESULT_KEYS:
        raise ValueError("permission audit bounded denial result is malformed")
    if (
        value.get("schema_version") != "bounded_permission_denial_result.v1"
        or value.get("action") != PERMISSION_AUDIT_POLICY
        or value.get("ledger_scope") != "production"
        or value.get("permission") != "denied"
        or value.get("authority_write_performed") is not False
        or value.get("production_mutation_performed") is not False
        or value.get("actor_ref_hash") != actor_ref_hash
        or value.get("request_hash") != request_hash
        or any(
            value.get(field) is not False
            for field in (
                "protected_values_returned",
                "raw_private_evidence_returned",
                "secret_returned",
                "host_topology_returned",
                "raw_external_ids_returned",
            )
        )
    ):
        raise ValueError("permission audit bounded denial result is malformed")
    event = {
        "schema_version": PERMISSION_AUDIT_EVENT_SCHEMA,
        "event_type": "permission_sensitive_runtime_action",
        **{
            key: value[key]
            for key in _BOUNDED_DENIAL_RESULT_KEYS
            if key != "schema_version"
        },
    }
    ensure_public_safe(event, "PermissionAuditBoundedDenialEvent")
    return event


def _validated_actor_ref_hash(review: Mapping[str, Any]) -> str:
    if not isinstance(review, Mapping):
        raise ValueError("permission audit authentication failed")
    status = review.get("status")
    if (
        review.get("apiVersion") != "authentication.k8s.io/v1"
        or review.get("kind") != "TokenReview"
        or not isinstance(status, Mapping)
        or status.get("authenticated") is not True
        or status.get("audiences") != [PERMISSION_AUDIT_AUDIENCE]
    ):
        raise ValueError("permission audit authentication failed")
    user = status.get("user")
    if not isinstance(user, Mapping) or user.get("username") != PERMISSION_AUDIT_SUBJECT:
        raise ValueError("permission audit authentication failed")
    extra = user.get("extra")
    if not isinstance(extra, Mapping):
        raise ValueError("permission audit authentication failed")
    pod_name = _single_bound_identity(extra.get("authentication.kubernetes.io/pod-name"))
    pod_uid = _single_bound_identity(extra.get("authentication.kubernetes.io/pod-uid"))
    if not pod_name or not pod_uid:
        raise ValueError("permission audit authentication failed")

    # Kubernetes TokenReview only returns authenticated=true when signature,
    # requested audience, current validity window (including expiry), and bound
    # object authentication all pass. Raw token and claims remain ephemeral.
    return hash_payload(
        {
            "subject": PERMISSION_AUDIT_SUBJECT,
            "pod_name": pod_name,
            "pod_uid": pod_uid,
        }
    )


def _single_bound_identity(value: Any) -> str:
    if not isinstance(value, list) or len(value) != 1:
        return ""
    item = value[0]
    if not isinstance(item, str) or not item or len(item) > 240:
        return ""
    return item


def _validated_product_sentinel(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SENTINEL_KEYS:
        raise ValueError("permission audit product sentinel is malformed")
    if value.get("schema_version") != "product_mutation_sentinel.v1":
        raise ValueError("permission audit product sentinel is malformed")
    normalized: dict[str, Any] = {"schema_version": "product_mutation_sentinel.v1"}
    for name in ("authority_ledger", "corpus", "queue", "index", "product_db"):
        item = value.get(name)
        if not isinstance(item, Mapping) or set(item) != _SENTINEL_VALUE_KEYS:
            raise ValueError("permission audit product sentinel is malformed")
        count = item.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("permission audit product sentinel is malformed")
        digest = require_sha256(str(item.get("hash") or ""), "sentinel hash")
        normalized[name] = {"count": count, "hash": digest}
    ensure_public_safe(normalized, "ProductMutationSentinel")
    return normalized


def _validated_store_result(
    value: Mapping[str, Any],
    *,
    request_hash: str,
    actor_ref_hash: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _INTERNAL_STORE_RESULT_KEYS:
        raise ValueError("permission audit store result is malformed")
    append_count = value.get("append_count")
    stored_row_count = value.get("stored_row_count")
    if (
        value.get("status") != "recorded"
        or append_count not in (0, 1)
        or isinstance(append_count, bool)
        or stored_row_count != 1
        or isinstance(stored_row_count, bool)
        or value.get("read_after_write_status") != "validated"
        or value.get("request_hash") != request_hash
        or value.get("production_mutation_performed") is not False
    ):
        raise ValueError("permission audit store result is malformed")
    _validated_bounded_denial_result(
        {str(key): value[key] for key in _BOUNDED_DENIAL_RESULT_KEYS},
        request_hash=request_hash,
        actor_ref_hash=actor_ref_hash,
    )
    result = {str(key): value[key] for key in _INTERNAL_STORE_RESULT_KEYS}
    ensure_public_safe(result, "PermissionAuditStoreResult")
    return result

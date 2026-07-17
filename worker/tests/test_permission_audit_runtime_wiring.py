from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from agent_knowledge.ledger import Ledger


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self, limit=-1):
        return self._payload[:limit] if limit >= 0 else self._payload


def _bounded_store_result(*, request_hash, actor_ref_hash, append_count=1):
    return {
        "status": "recorded",
        "append_count": append_count,
        "stored_row_count": 1,
        "read_after_write_status": "validated",
        "request_hash": request_hash,
        "schema_version": "bounded_permission_denial_result.v1",
        "action": "single_bounded_denial.v1",
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


def _exact_marker_reader():
    from agent_knowledge.permission_audit import IndependentProductMutationMarkerReader

    statuses = {
        "authority_ledger": "clear",
        "corpus": "atomic_commit_boundary",
        "queue": "atomic_commit_boundary",
        "index": "clear",
        "product_db": "atomic_commit_boundary",
    }

    def record(plane):
        index = tuple(statuses).index(plane) + 1
        return {
            "plane": plane,
            "generation_hash": "sha256:" + "a" * 64,
            "event_position_hash": "sha256:" + format(index, "x") * 64,
            "marker_hash": "sha256:" + format(index + 5, "x") * 64,
            "in_flight_count": 0,
            "in_flight_status": statuses[plane],
            "coverage_hash": "sha256:" + format(index + 10, "x") * 64,
            "coverage_status": "validated",
            "read_scope_status": "read_only",
            "reset_or_decrease_count": 0,
            "read_call_count": 1,
        }

    class Fence:
        def read_marker(self):
            return record("authority_ledger")

        def release(self):
            return None

    class Provider:
        def __init__(self, plane):
            self.plane = plane

        def __call__(self):
            return record(self.plane)

    return IndependentProductMutationMarkerReader(
        authority_fence_factory=Fence,
        providers={
            plane: Provider(plane)
            for plane in ("corpus", "queue", "index", "product_db")
        },
    )


def test_kubernetes_token_reviewer_uses_fixed_audience_and_does_not_return_credentials():
    from agent_knowledge.permission_audit import (
        KubernetesTokenReviewer,
        PERMISSION_AUDIT_AUDIENCE,
    )

    requests = []
    response_payload = {
        "apiVersion": "authentication.k8s.io/v1",
        "kind": "TokenReview",
        "status": {"authenticated": False},
    }

    def urlopen(request, **kwargs):
        requests.append((request, kwargs))
        return _Response(response_payload)

    reviewer = KubernetesTokenReviewer(
        reviewer_token_reader=lambda: "reviewer-token-fixture",
        ssl_context_factory=lambda: object(),
        urlopen=urlopen,
    )

    result = reviewer("projected-token-fixture")

    request, kwargs = requests[0]
    body = json.loads(request.data)
    assert request.full_url.endswith("/apis/authentication.k8s.io/v1/tokenreviews")
    assert request.headers["Authorization"] == "Bearer reviewer-token-fixture"
    assert body == {
        "apiVersion": "authentication.k8s.io/v1",
        "kind": "TokenReview",
        "spec": {
            "token": "projected-token-fixture",
            "audiences": [PERMISSION_AUDIT_AUDIENCE],
        },
    }
    assert kwargs["timeout"] == 5
    assert result == response_payload
    assert "projected-token-fixture" not in json.dumps(result)
    assert "reviewer-token-fixture" not in json.dumps(result)


@pytest.mark.parametrize(
    "api_url",
    [
        "http://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews",
        "https://kubernetes.default.svc:444/apis/authentication.k8s.io/v1/tokenreviews",
        "https://kubernetes.default.svc.evil.invalid/apis/authentication.k8s.io/v1/tokenreviews",
        "https://evil.invalid/apis/authentication.k8s.io/v1/tokenreviews",
        "https://reviewer@kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews",
        "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews?next=evil",
        "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews/",
    ],
)
def test_kubernetes_token_reviewer_rejects_ssrf_targets_before_reading_credentials(
    api_url,
):
    from agent_knowledge.permission_audit import KubernetesTokenReviewer

    calls = {"reviewer_token": 0, "urlopen": 0}

    def reviewer_token_reader():
        calls["reviewer_token"] += 1
        return "must-not-leave"

    def urlopen(*_args, **_kwargs):
        calls["urlopen"] += 1
        raise AssertionError("credentials must not be sent")

    with pytest.raises(ValueError, match="TokenReview URL"):
        KubernetesTokenReviewer(
            api_url,
            reviewer_token_reader=reviewer_token_reader,
            urlopen=urlopen,
        )

    assert calls == {"reviewer_token": 0, "urlopen": 0}


@pytest.mark.parametrize(
    "api_url",
    [
        "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews",
        "https://kubernetes.default.svc:443/apis/authentication.k8s.io/v1/tokenreviews",
    ],
)
def test_kubernetes_token_reviewer_accepts_only_canonical_service_endpoint(api_url):
    from agent_knowledge.permission_audit import KubernetesTokenReviewer

    reviewer = KubernetesTokenReviewer(
        api_url,
        reviewer_token_reader=lambda: "reviewer-token-fixture",
        ssl_context_factory=lambda: object(),
        urlopen=lambda *_args, **_kwargs: _Response(
            {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "status": {"authenticated": False},
            }
        ),
    )

    assert reviewer("projected-token-fixture")["kind"] == "TokenReview"


def test_token_review_transport_disables_proxies_and_redirects(monkeypatch):
    from agent_knowledge import permission_audit

    captured_handlers = []

    class Opener:
        def open(self, _request, **_kwargs):
            return _Response(
                {
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "TokenReview",
                    "status": {"authenticated": False},
                }
            )

    def build_opener(*handlers):
        captured_handlers.extend(handlers)
        return Opener()

    monkeypatch.setenv("HTTPS_PROXY", "https://credential-sink.invalid:8443")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setattr(permission_audit.urllib.request, "build_opener", build_opener)
    reviewer = permission_audit.KubernetesTokenReviewer(
        reviewer_token_reader=lambda: "reviewer-token-fixture",
        ssl_context_factory=lambda: object(),
    )

    reviewer("projected-token-fixture")

    proxy_handlers = [
        item
        for item in captured_handlers
        if isinstance(item, permission_audit.urllib.request.ProxyHandler)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert any(
        isinstance(item, permission_audit._NoRedirectHandler)
        for item in captured_handlers
    )


def test_audit_store_client_rejects_non_loopback_and_returns_sanitized_result():
    from agent_knowledge.permission_audit import LoopbackPermissionAuditStoreClient

    with pytest.raises(ValueError, match="loopback"):
        LoopbackPermissionAuditStoreClient("http://audit-store.example.invalid:8771")

    requests = []

    def urlopen(request, **kwargs):
        requests.append((request, kwargs))
        body = json.loads(request.data)
        return _Response(
            {
                **_bounded_store_result(
                    request_hash=body["request_hash"],
                    actor_ref_hash=body["actor_ref_hash"],
                ),
                "append_attempt_hash": body["append_attempt_hash"],
            }
        )

    client = LoopbackPermissionAuditStoreClient(
        urlopen=urlopen,
        attempt_hash_factory=lambda: "sha256:" + "1" * 64,
    )
    result = client.append_denied_once(
        request_hash="sha256:" + "d" * 64,
        actor_ref_hash="sha256:" + "c" * 64,
        action="single_bounded_denial.v1",
    )

    assert requests[0][0].full_url == "http://127.0.0.1:8771/append-denied-once"
    assert requests[0][1]["timeout"] == 5
    assert result == _bounded_store_result(
        request_hash="sha256:" + "d" * 64,
        actor_ref_hash="sha256:" + "c" * 64,
    )


def test_audit_store_client_recovers_lost_append_response_with_one_readback_only():
    from agent_knowledge.permission_audit import LoopbackPermissionAuditStoreClient

    requests = []
    committed = {}

    def urlopen(request, **_kwargs):
        body = json.loads(request.data)
        requests.append((request.full_url, body))
        if request.full_url.endswith("/append-denied-once"):
            committed.update(body)
            raise OSError("response lost after commit")
        assert request.full_url.endswith("/readback")
        return _Response(
            {
                "schema_version": "permission_audit_store_readback.v2",
                "status": "recorded",
                "stored_row_count": 1,
                "request_hash": committed["request_hash"],
                "append_attempt_hash": committed["append_attempt_hash"],
                "actor_ref_hash": committed["actor_ref_hash"],
                "action": committed["action"],
                "permission": "denied",
                "authority_write_performed": False,
                "production_mutation_performed": False,
            }
        )

    client = LoopbackPermissionAuditStoreClient(
        urlopen=urlopen,
        attempt_hash_factory=lambda: "sha256:" + "1" * 64,
    )

    result = client.append_denied_once(
        request_hash="sha256:" + "d" * 64,
        actor_ref_hash="sha256:" + "c" * 64,
        action="single_bounded_denial.v1",
    )

    assert [url.rsplit("/", 1)[-1] for url, _ in requests] == [
        "append-denied-once",
        "readback",
    ]
    assert requests[1][1] == {"request_hash": "sha256:" + "d" * 64}
    assert result == _bounded_store_result(
        request_hash="sha256:" + "d" * 64,
        actor_ref_hash="sha256:" + "c" * 64,
    )


def test_audit_store_client_does_not_claim_old_row_after_lost_append_response():
    from agent_knowledge.permission_audit import LoopbackPermissionAuditStoreClient

    calls = []

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        if request.full_url.endswith("/append-denied-once"):
            raise OSError("request result unknown")
        return _Response(
            {
                "schema_version": "permission_audit_store_readback.v2",
                "status": "recorded",
                "stored_row_count": 1,
                "request_hash": "sha256:" + "d" * 64,
                "append_attempt_hash": "sha256:" + "2" * 64,
                "actor_ref_hash": "sha256:" + "c" * 64,
                "action": "single_bounded_denial.v1",
                "permission": "denied",
                "authority_write_performed": False,
                "production_mutation_performed": False,
            }
        )

    client = LoopbackPermissionAuditStoreClient(
        urlopen=urlopen,
        attempt_hash_factory=lambda: "sha256:" + "1" * 64,
    )

    result = client.append_denied_once(
        request_hash="sha256:" + "d" * 64,
        actor_ref_hash="sha256:" + "c" * 64,
        action="single_bounded_denial.v1",
    )

    assert len(calls) == 2
    assert result["append_count"] == 0


def test_independent_product_mutation_sentinel_calls_each_actual_plane_once():
    from agent_knowledge.permission_audit import IndependentProductMutationSentinelReader

    calls = []
    names = ("authority_ledger", "corpus", "queue", "index", "product_db")
    providers = {
        name: (
            lambda plane=name: (
                calls.append(plane)
                or {
                    "count": names.index(plane),
                    "hash": "sha256:" + str(names.index(plane) + 1) * 64,
                }
            )
        )
        for name in names
    }
    reader = IndependentProductMutationSentinelReader(providers)

    before = reader()

    assert set(before) == {
        "schema_version",
        "authority_ledger",
        "corpus",
        "queue",
        "index",
        "product_db",
    }
    assert before["schema_version"] == "product_mutation_sentinel.v1"
    for name in names:
        assert set(before[name]) == {"count", "hash"}
        assert before[name]["hash"].startswith("sha256:")
    assert calls == list(names)

    with pytest.raises(ValueError, match="all independent mutation sentinel providers"):
        IndependentProductMutationSentinelReader(
            {name: provider for name, provider in providers.items() if name != "index"}
        )
    shared_provider = lambda: {"count": 1, "hash": "sha256:" + "f" * 64}
    with pytest.raises(ValueError, match="distinct mutation sentinel providers"):
        IndependentProductMutationSentinelReader(
            {name: shared_provider for name in names}
        )


def test_independent_product_mutation_sentinel_rejects_bound_methods_from_same_owner():
    from agent_knowledge.permission_audit import IndependentProductMutationSentinelReader

    class SharedProvider:
        def authority_ledger(self):
            return {"count": 1, "hash": "sha256:" + "1" * 64}

        def corpus(self):
            return {"count": 2, "hash": "sha256:" + "2" * 64}

        def queue(self):
            return {"count": 3, "hash": "sha256:" + "3" * 64}

        def index(self):
            return {"count": 4, "hash": "sha256:" + "4" * 64}

        def product_db(self):
            return {"count": 5, "hash": "sha256:" + "5" * 64}

    shared = SharedProvider()

    with pytest.raises(ValueError, match="distinct mutation sentinel providers"):
        IndependentProductMutationSentinelReader(
            {
                "authority_ledger": shared.authority_ledger,
                "corpus": shared.corpus,
                "queue": shared.queue,
                "index": shared.index,
                "product_db": shared.product_db,
            }
        )


def test_postgres_database_marker_reads_one_public_safe_product_plane_marker():
    from agent_knowledge.permission_audit import PostgresDatabaseMutationMarkerReader
    from agent_knowledge.postgres_db_adapter import _PgRow

    statements = []

    class Result:
        def fetchall(self):
            return [
                _PgRow(
                    (11, "sha256:" + "5" * 64),
                    ("count", "hash"),
                )
            ]

    class Connection:
        dialect = "postgres"

        def execute(self, sql):
            statements.append(sql)
            return Result()

    class ConnectionContext(Connection):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Adapter:
        is_file_backed = False

    class ReadOnlyLedger:
        read_only = True
        _db_adapter = Adapter()

        def _connect(self):
            return ConnectionContext()

    state = PostgresDatabaseMutationMarkerReader(ReadOnlyLedger())()

    normalized_sql = " ".join(statements[0].split()).lower()
    assert state == {"count": 11, "hash": "sha256:" + "5" * 64}
    assert len(statements) == 1
    assert "from public.permission_audit_product_db_mutation_marker_v1" in normalized_sql
    assert "mutation_count as count" in normalized_sql
    assert "mutation_hash as hash" in normalized_sql
    assert "limit 2" in normalized_sql
    assert "pg_current_wal_lsn" not in normalized_sql
    assert "txid" not in normalized_sql
    assert "xmin" not in normalized_sql


@pytest.mark.parametrize(
    ("count", "marker_hash"),
    (
        (None, "sha256:" + "5" * 64),
        (11, None),
    ),
)
def test_postgres_database_marker_rejects_null_product_plane_fields(
    count,
    marker_hash,
):
    from agent_knowledge.permission_audit import PostgresDatabaseMutationMarkerReader

    class Result:
        def fetchall(self):
            return [{"count": count, "hash": marker_hash}]

    class Connection:
        dialect = "postgres"

        def execute(self, _sql):
            return Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Adapter:
        is_file_backed = False

    class ReadOnlyLedger:
        read_only = True
        _db_adapter = Adapter()

        def _connect(self):
            return Connection()

    with pytest.raises(RuntimeError, match="PostgreSQL mutation marker is unavailable"):
        PostgresDatabaseMutationMarkerReader(ReadOnlyLedger())()


def test_recall_service_wiring_enables_audit_only_with_explicit_cli_flag(tmp_path):
    from agent_knowledge import cli

    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)
    base = {
        "ledger": str(ledger_path),
        "dataset_id": [],
        "allow_private_results": False,
        "native_memory_id": "",
        "enable_graph": False,
        "graph_required": False,
        "allow_steward_proposals": False,
        "allow_steward_review_commit": False,
        "allow_object_authority_production_writes": False,
        "permission_audit_store_url": "http://127.0.0.1:8771",
        "permission_audit_token_review_url": (
            "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
        ),
    }

    disabled = cli._build_recall_service(
        Namespace(**base, allow_permission_sensitive_audit_probe=False)
    )
    with pytest.raises(cli._ServiceWiringError, match="exact marker reader unavailable"):
        cli._build_recall_service(
            Namespace(**base, allow_permission_sensitive_audit_probe=True)
        )

    assert disabled.allow_permission_sensitive_audit_probe is False
    assert disabled._permission_audit_token_reviewer is None
    assert disabled._permission_audit_store_append is None
    assert disabled._permission_audit_product_sentinel_reader is None


def test_recall_service_allows_audit_only_with_exact_markers_and_atomic_store(
    tmp_path,
):
    from agent_knowledge import cli

    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)
    args = Namespace(
        ledger=str(ledger_path),
        dataset_id=[],
        allow_private_results=False,
        native_memory_id="",
        enable_graph=False,
        graph_required=False,
        allow_steward_proposals=False,
        allow_steward_review_commit=False,
        allow_object_authority_production_writes=False,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_store_url="http://127.0.0.1:8771",
        permission_audit_token_review_url=(
            "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
        ),
    )
    marker_reader = _exact_marker_reader()

    enabled = cli._build_recall_service(
        args,
        permission_audit_marker_reader=marker_reader,
    )

    assert enabled.allow_permission_sensitive_audit_probe is True
    assert not hasattr(enabled, "_permission_audit_denial_request")
    assert callable(enabled._permission_audit_store_append)
    assert enabled._permission_audit_product_sentinel_reader is marker_reader


def test_recall_service_builds_exact_marker_reader_from_production_environment(
    monkeypatch,
    tmp_path,
):
    from agent_knowledge import cli

    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)
    marker_reader = _exact_marker_reader()
    calls = []

    def build_reader(environ):
        calls.append(environ)
        return marker_reader

    monkeypatch.setattr(
        cli,
        "build_production_permission_audit_marker_reader",
        build_reader,
        raising=False,
    )
    args = Namespace(
        ledger=str(ledger_path),
        dataset_id=[],
        allow_private_results=False,
        native_memory_id="",
        enable_graph=False,
        graph_required=False,
        allow_steward_proposals=False,
        allow_steward_review_commit=False,
        allow_object_authority_production_writes=False,
        allow_permission_sensitive_audit_probe=True,
        permission_audit_store_url="http://127.0.0.1:8771",
        permission_audit_token_review_url=(
            "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
        ),
    )

    service = cli._build_recall_service(args)

    assert calls == [cli.os.environ]
    assert service._permission_audit_product_sentinel_reader is marker_reader


def test_recall_service_audit_off_never_calls_production_marker_factory(
    monkeypatch,
    tmp_path,
):
    from agent_knowledge import cli

    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)
    calls = []
    monkeypatch.setattr(
        cli,
        "build_production_permission_audit_marker_reader",
        lambda _environ: calls.append("marker") or _exact_marker_reader(),
    )
    args = Namespace(
        ledger=str(ledger_path),
        dataset_id=[],
        allow_private_results=False,
        native_memory_id="",
        enable_graph=False,
        graph_required=False,
        allow_steward_proposals=False,
        allow_steward_review_commit=False,
        allow_object_authority_production_writes=False,
        allow_permission_sensitive_audit_probe=False,
        permission_audit_store_url="http://127.0.0.1:8771",
        permission_audit_token_review_url=(
            "https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews"
        ),
    )

    service = cli._build_recall_service(args)

    assert calls == []
    assert service._permission_audit_product_sentinel_reader is None


@pytest.mark.parametrize("transport", ("stdio", "http"))
def test_mcp_transports_auto_wire_production_marker_reader(
    monkeypatch,
    tmp_path,
    transport,
):
    from agent_knowledge import cli
    from agent_knowledge import mcp_http_server

    ledger_path = tmp_path / "ledger.sqlite3"
    Ledger(ledger_path)
    marker_reader = _exact_marker_reader()
    calls = []
    monkeypatch.setattr(
        cli,
        "build_production_permission_audit_marker_reader",
        lambda environ: calls.append(("marker", environ)) or marker_reader,
    )
    monkeypatch.setattr(
        cli,
        "run_stdio_server",
        lambda service: calls.append(("stdio", service)),
    )
    monkeypatch.setattr(
        mcp_http_server,
        "serve",
        lambda service, **_kwargs: calls.append(("http", service)),
    )
    argv = [
        "--ledger",
        str(ledger_path),
        "--allow-permission-sensitive-audit-probe",
    ]

    result = (
        cli._mcp_stdio_main(argv)
        if transport == "stdio"
        else cli._mcp_http_main(argv)
    )

    assert result == 0
    assert [name for name, _ in calls] == ["marker", transport]
    service = calls[-1][1]
    assert service._permission_audit_product_sentinel_reader is marker_reader


def test_permission_audit_cli_flag_is_declared_without_env_alias(capsys):
    from agent_knowledge.cli import _mcp_stdio_main

    with pytest.raises(SystemExit) as exc_info:
        _mcp_stdio_main(["--help"])

    output = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "--allow-permission-sensitive-audit-probe" in output
    assert "--permission-audit-store-url" in output
    assert "--permission-audit-token-review-url" in output


def test_read_only_postgres_connection_skips_compatibility_ddl(monkeypatch):
    from agent_knowledge import postgres_db_adapter

    class FakeConnection:
        def __init__(self):
            self.read_only = False
            self.cursor_calls = 0

        def cursor(self):
            self.cursor_calls += 1
            raise AssertionError("read-only connection must not run compatibility DDL")

    connection = FakeConnection()
    monkeypatch.setattr(
        postgres_db_adapter.psycopg,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    postgres_db_adapter._PgConnection("fixture-dsn", read_only=True)

    assert connection.read_only is True
    assert connection.cursor_calls == 0


def test_postgres_audit_marker_has_a_non_skippable_source_owned_ci_gate():
    workflow = (Path(__file__).parents[2] / ".github/workflows/test.yml").read_text(
        encoding="utf-8"
    )

    assert "REQUIRE_LEDGER_PG_DSN: \"1\"" in workflow
    assert (
        "uv run --group dev pytest "
        "tests/test_pg_parity.py::test_permission_audit_marker_is_read_only_and_xid_free -q"
    ) in workflow

"""합성 경계 단위 테스트 + 명시 opt-in 로컬 PostgreSQL 통합 테스트."""
from copy import deepcopy
from contextlib import contextmanager
from importlib import import_module
import json
import pkgutil

import pytest

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.session_memory_materializer import materialize_session_memory
from agent_knowledge.couchdb_source.source_store import InMemoryCouchDBSourceStore
from agent_knowledge.rag_ingress.qdrant_backfill import public_safe_mask_body
from test_couchdb_build_cli import _build_synthetic_session


def cli():
    from agent_knowledge import rag_ingress
    assert "pg_qdrant_import_cli" in {m.name for m in pkgutil.iter_modules(rag_ingress.__path__)}, "bounded CLI missing"
    return import_module("agent_knowledge.rag_ingress.pg_qdrant_import_cli")


class ReadOnlySQL:
    def __init__(self):
        self.writes = 0

    def get_chunk(self, chunk_id, conn=None):
        return None

    @contextmanager
    def transaction(self):
        # A transaction is not a data write. Model only exactly representable
        # synthetic values; PostgreSQL rounding boundaries use actual SQL below.
        yield self

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params):
        assert sql == "SELECT %s::halfvec AS embedding"
        vector = json.loads(params[0])
        assert set(vector) <= {0.125, 0.25, 0.5}
        self.result = {"embedding": params[0]}
        return self

    def fetchone(self):
        return self.result

    def insert_chunk(self, *args, **kwargs):
        self.writes += 1
        raise AssertionError("dry-run attempted a data write")


class Boundary:
    def __init__(self):
        self.source = InMemoryCouchDBSourceStore()
        self.sql = ReadOnlySQL()
        self.sid = _build_synthetic_session(self.source, provider="codex", project="synthetic", raw_id="cli")
        current = materialize_session_memory(session_id_hash=self.sid, store=self.source)
        self.points = [{"id": 1, "payload": {
            "document_kind": "session_memory", "target_profile": "session-memory",
            "session_id_hash": self.sid, "project": "synthetic", "provider": "codex",
            "content_hash": current.content_hash, "source_hash": current.source_hash,
            "text": public_safe_mask_body(current.body)}, "vector": [0.125] * 3072}]
        self.events = []
        self.target = "synthetic-target"

    def collection_metadata(self):
        return {"size": 3072, "distance": "Cosine"}

    def target_fingerprint(self):
        return self.target

    def scroll(self, *, offset, limit, scope):
        self.events.append(("scroll", offset, limit, scope))
        start = offset or 0
        end = start + limit
        return deepcopy(self.points[start:end]), end if end < len(self.points) else None

    def retrieve(self, ids):
        self.events.append(("retrieve", list(ids)))
        return deepcopy([p for p in self.points if p["id"] in ids])

    def close(self):
        pass


@pytest.fixture
def lane(tmp_path):
    attestation = tmp_path / "attestation.json"
    attestation.write_text(json.dumps({"collection": "synthetic", "model": "gemini-embedding-2",
        "dimension": 3072, "confirmed": True, "evidence_ref": "synthetic-operator-evidence"}))
    argv = ["--collection", "synthetic", "--project", "synthetic", "--provider", "codex",
            "--limit", "2", "--page-size", "1", "--timeout-seconds", "20",
            "--request-timeout-seconds", "2", "--attestation-file", str(attestation),
            "--manifest", str(tmp_path / "manifest.json"), "--approval-file", str(tmp_path / "approval.json")]
    return Boundary(), argv, tmp_path


def invoke(lane, capsys, extra=()):
    boundary, argv, _ = lane
    rc = cli()._run_worker(argv + list(extra), boundary_factory=lambda args, budget: boundary)
    return rc, json.loads(capsys.readouterr().out)


def test_default_dry_run_uses_core_and_emits_only_fingerprints(lane, capsys):
    boundary, _, path = lane
    rc, report = invoke(lane, capsys)
    assert rc == 0 and report["status"] == "dry_run"
    assert report["mutation_started"] is False and boundary.sql.writes == 0
    manifest_text = (path / "manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["complete"] is True
    assert len(manifest["points"]) == 1
    assert manifest["points"][0]["status"] == "validated"
    for key in ("point_fingerprint", "source_fingerprint", "target_fingerprint"):
        assert len(manifest["points"][0][key]) == 64
    assert len(manifest["plan_digest"]) == 64
    assert boundary.points[0]["payload"]["text"] not in manifest_text
    assert '"vector"' not in manifest_text and '"payload"' not in manifest_text
    assert not (path / "approval.json").exists()


@pytest.mark.parametrize("count,limit,expected_complete", [(3, 4, True), (3, 2, False)])
def test_pagination_is_bounded_and_reports_incomplete(lane, capsys, count, limit, expected_complete):
    boundary, argv, path = lane
    boundary.points = [dict(deepcopy(boundary.points[0]), id=i) for i in range(count)]
    argv[argv.index("--limit") + 1] = str(limit)
    rc, report = invoke(lane, capsys)
    manifest = json.loads((path / "manifest.json").read_text())
    assert len(manifest["points"]) == min(count, limit)
    assert manifest["complete"] is expected_complete
    assert report["complete"] is expected_complete
    assert rc == (0 if expected_complete else 2)
    assert sum(event[2] for event in boundary.events) <= limit


@pytest.mark.parametrize("flag,value", [("--limit", "0"), ("--limit", "-1"), ("--page-size", "0"),
    ("--page-size", "3"), ("--timeout-seconds", "0"), ("--timeout-seconds", "nan"),
    ("--request-timeout-seconds", "inf"), ("--request-timeout-seconds", "21")])
def test_invalid_bounds_never_construct_boundary(lane, capsys, flag, value):
    _, argv, _ = lane
    argv[argv.index(flag) + 1] = value
    def forbidden(*args):
        pytest.fail("invalid bounds reached IO")
    rc = cli()._run_worker(argv, boundary_factory=forbidden)
    assert rc != 0
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"


@pytest.mark.parametrize("mode", ["repeat_offset", "duplicate", "oversized"])
def test_broken_pagination_fails_without_manifest(lane, capsys, mode):
    boundary, _, path = lane
    point = deepcopy(boundary.points[0])
    calls = []
    def broken(**kwargs):
        calls.append(kwargs)
        if mode == "oversized":
            return [point, dict(deepcopy(point), id=2)], None
        if mode == "repeat_offset":
            return [dict(deepcopy(point), id=len(calls))], 1
        return [point], 1 if len(calls) == 1 else None
    boundary.scroll = broken
    rc, report = invoke(lane, capsys)
    assert rc != 0 and report["status"] == "rejected"
    assert boundary.sql.writes == 0 and len(calls) <= 2
    assert not (path / "manifest.json").exists()


@pytest.mark.parametrize("fault,status", [("conversation_chunk1", "unsupported"), ("stale", "stale"),
    ("source_down", "source_unavailable"), ("body", "body_mismatch")])
def test_nonimportable_points_remain_in_reconciliation(lane, capsys, fault, status):
    boundary, _, path = lane
    if fault == "conversation_chunk1":
        boundary.points[0]["payload"]["document_kind"] = fault
    elif fault == "stale":
        boundary.points[0]["payload"]["source_hash"] = "old"
    elif fault == "body":
        boundary.points[0]["payload"]["text"] = "different body"
    else:
        def down(**kwargs):
            raise ConnectionError("sensitive body/DSN")
        boundary.source.find_by_session = down
    rc, report = invoke(lane, capsys)
    assert rc == 2 and report["complete"] is False
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["points"][0]["status"] == status
    assert boundary.sql.writes == 0
    assert "sensitive" not in json.dumps(report)


@pytest.mark.parametrize("key,value", [("confirmed", False), ("model", "other"),
    ("dimension", 1536), ("collection", "other"), ("evidence_ref", "")])
def test_attestation_rejected_even_with_empty_collection(lane, capsys, key, value):
    boundary, _, path = lane
    boundary.points = []
    file = path / "attestation.json"
    evidence = json.loads(file.read_text())
    evidence[key] = value
    file.write_text(json.dumps(evidence))
    rc, report = invoke(lane, capsys)
    assert rc == 1 and report["status"] == "rejected"
    assert boundary.events == []


@pytest.mark.parametrize("key,value", [("size", 1536), ("distance", "Dot")])
def test_collection_contract_rejected_even_when_empty(lane, capsys, key, value):
    boundary, _, _ = lane
    boundary.points = []
    metadata = {"size": 3072, "distance": "Cosine", key: value}
    boundary.collection_metadata = lambda: metadata
    assert invoke(lane, capsys)[0] == 1
    assert boundary.events == []


def test_explicit_all_scope_is_allowed_but_payload_scope_is_enforced(lane, capsys):
    boundary, argv, path = lane
    for name in ("project", "provider"):
        index = argv.index("--" + name)
        argv[index:index + 2] = ["--all-" + name + "s"]
    assert invoke(lane, capsys)[0] == 0
    plan = json.loads((path / "manifest.json").read_text())
    assert plan["scope"] == {"project": None, "provider": None}
    assert boundary.events[0][3] == plan["scope"]


def approve(lane):
    _, argv, path = lane
    plan = json.loads((path / "manifest.json").read_text())
    approval = {"schema_version": 1, "operation": "pg-qdrant-import", "approved": True,
                "operator": "synthetic-human", "argv": argv + ["--apply"], "plan_digest": plan["plan_digest"]}
    (path / "approval.json").write_text(json.dumps(approval))
    return approval


def record_imports(monkeypatch, boundary):
    original = cli().import_qdrant_point
    writes = []
    def import_point(**kwargs):
        if kwargs["dry_run"]:
            boundary.events.append(("validate", kwargs["point"]["id"]))
            return original(**kwargs)
        writes.append(kwargs["point"]["id"])
        boundary.events.append(("write", kwargs["point"]["id"]))
        return {"status": "projected", "reason": "", "ref": "not-printed"}
    monkeypatch.setattr(cli(), "import_qdrant_point", import_point)
    return writes


def test_divergent_canonical_duplicates_rejected_before_any_write(lane, capsys, monkeypatch):
    boundary, _, path = lane
    boundary.points.append(dict(deepcopy(boundary.points[0]), id=2))
    boundary.points[1]["vector"][0] = 0.25
    writes = record_imports(monkeypatch, boundary)
    rc, report = invoke(lane, capsys)
    assert rc != 0
    assert boundary.sql.writes == 0 and writes == []
    manifest = json.loads((path / "manifest.json").read_text())
    assert {p["status"] for p in manifest["points"]} == {"canonical_vector_conflict"}
    approve(lane)
    rc, report = invoke(lane, capsys, ["--apply"])
    assert rc == 1 and report["mutation_started"] is False
    assert writes == []


def test_apply_independently_rejects_divergence_in_per_point_only_plan(lane, capsys, monkeypatch):
    boundary, _, path = lane
    boundary.points.append(dict(deepcopy(boundary.points[0]), id=2))
    boundary.points[1]["vector"][0] = 0.25
    assert invoke(lane, capsys)[0] == 2
    # A synthetic human signs an internally consistent plan which claims both
    # individual points valid: apply must enforce the batch invariant itself.
    plan = json.loads((path / "manifest.json").read_text())
    for point in plan["points"]:
        point["status"] = "validated"
    plan["complete"] = True
    plan.pop("plan_digest")
    plan["plan_digest"] = cli().digest(plan)
    (path / "manifest.json").write_text(json.dumps(plan))
    approve(lane)
    writes = record_imports(monkeypatch, boundary)
    rc, report = invoke(lane, capsys, ["--apply"])
    assert rc == 1 and report["mutation_started"] is False
    assert writes == [] and boundary.sql.writes == 0


def test_apply_refetches_and_preflights_entire_batch_before_writes(lane, capsys, monkeypatch):
    boundary, _, path = lane
    boundary.points.append(dict(deepcopy(boundary.points[0]), id=2))
    assert invoke(lane, capsys)[0] == 0
    original_manifest = (path / "manifest.json").read_bytes()
    approve(lane)
    writes = record_imports(monkeypatch, boundary)
    boundary.events.clear()
    rc, report = invoke(lane, capsys, ["--apply"])
    assert rc == 0 and report["status"] == "applied"
    assert writes == [1, 2] and report["applied"] == 2
    assert [e[0] for e in boundary.events] == ["retrieve", "retrieve", "validate", "validate", "write", "write"]
    assert (path / "manifest.json").read_bytes() == original_manifest
    assert "not-printed" not in json.dumps(report)


@pytest.mark.parametrize("fault", ["missing", "false", "operator", "argv", "digest", "operation", "extra", "schema"])
def test_exact_approval_required_before_io(lane, capsys, fault):
    assert invoke(lane, capsys)[0] == 0
    approval = approve(lane)
    _, argv, path = lane
    if fault == "missing":
        (path / "approval.json").unlink()
    else:
        key, value = {"false": ("approved", False), "operator": ("operator", ""), "argv": ("argv", []),
            "digest": ("plan_digest", "bad"), "operation": ("operation", "other"), "extra": ("extra", True),
            "schema": ("schema_version", 2)}[fault]
        approval[key] = value
        (path / "approval.json").write_text(json.dumps(approval))
    def forbidden(*args):
        pytest.fail("unapproved apply reached IO")
    assert cli()._run_worker(argv + ["--apply"], boundary_factory=forbidden) == 1
    assert json.loads(capsys.readouterr().out)["mutation_started"] is False


@pytest.mark.parametrize("drift", ["point", "source", "target", "attestation", "code", "limit", "manifest", "missing_point", "duplicate_point", "row"])
def test_apply_drift_aborts_before_any_write(lane, capsys, monkeypatch, drift):
    boundary, argv, path = lane
    boundary.points.append(dict(deepcopy(boundary.points[0]), id=2))
    assert invoke(lane, capsys)[0] == 0
    approve(lane)
    writes = record_imports(monkeypatch, boundary)
    if drift == "point":
        boundary.points[1]["vector"][0] = 0.5
    elif drift == "source":
        chunk = next(d for d in boundary.source.find_by_session(session_id_hash=boundary.sid)
                     if d["doc_type"] == dm.SourceDocType.CONVERSATION_CHUNK)
        chunk["body"] += "changed"
        chunk["content_hash"] = dm.sha256_hash(chunk["body"])
        boundary.source.put(chunk)
    elif drift == "target":
        boundary.target = "other-target"
    elif drift == "attestation":
        file = path / "attestation.json"
        data = json.loads(file.read_text())
        data["evidence_ref"] += "changed"
        file.write_text(json.dumps(data))
    elif drift == "code":
        monkeypatch.setattr(cli(), "code_revision", lambda: "changed")
    elif drift == "limit":
        argv[argv.index("--limit") + 1] = "3"
    elif drift == "manifest":
        file = path / "manifest.json"
        data = json.loads(file.read_text())
        data["points"].reverse()
        file.write_text(json.dumps(data))
    elif drift == "missing_point":
        boundary.points.pop()
    elif drift == "duplicate_point":
        boundary.retrieve = lambda ids: [boundary.points[0], boundary.points[0]]
    else:
        boundary.sql.get_chunk = lambda *args, **kwargs: {"unexpected": "changed target row"}
    rc, report = invoke(lane, capsys, ["--apply"])
    assert rc == 1 and report["mutation_started"] is False
    assert writes == []


@pytest.mark.parametrize("failure", ["exception", "failed_status"])
def test_partial_apply_stops_with_mutation_unknown(lane, capsys, monkeypatch, failure):
    boundary, _, _ = lane
    boundary.points.append(dict(deepcopy(boundary.points[0]), id=2))
    assert invoke(lane, capsys)[0] == 0
    approve(lane)
    original = cli().import_qdrant_point
    writes = []
    def fail(**kwargs):
        if kwargs["dry_run"]:
            return original(**kwargs)
        writes.append(kwargs["point"]["id"])
        if failure == "exception":
            raise TimeoutError("DSN=password synthetic private body")
        return {"status": "failed", "reason": "source_revision_changed"}
    monkeypatch.setattr(cli(), "import_qdrant_point", fail)
    rc, report = invoke(lane, capsys, ["--apply"])
    assert rc == 1 and report["status"] == "mutation_unknown"
    assert report["mutation_started"] is True and writes == [1]
    assert "password" not in json.dumps(report)


@pytest.mark.parametrize("stage", ["scroll", "write"])
def test_operation_deadline_stops_without_retry(lane, capsys, monkeypatch, stage):
    boundary, _, path = lane
    if stage == "write":
        assert invoke(lane, capsys)[0] == 0
        approve(lane)
    now = [0.0]
    monkeypatch.setattr(cli().time, "monotonic", lambda: now[0])
    original = boundary.scroll if stage == "scroll" else cli().import_qdrant_point
    calls = []
    def slow(**kwargs):
        if stage == "scroll" or not kwargs["dry_run"]:
            now[0] = 21.0
            calls.append(True)
        return original(**kwargs)
    if stage == "scroll":
        boundary.scroll = slow
    else:
        def slow_write(**kwargs):
            if kwargs["dry_run"]:
                return original(**kwargs)
            now[0] = 21.0
            calls.append(True)
            return {"status": "projected"}
        monkeypatch.setattr(cli(), "import_qdrant_point", slow_write)
    rc, report = invoke(lane, capsys, ["--apply"] if stage == "write" else [])
    assert rc == 1 and len(calls) == 1
    assert report["status"] == ("mutation_unknown" if stage == "write" else "rejected")
    if stage == "scroll":
        assert not (path / "manifest.json").exists()


def test_network_adapter_uses_only_configured_origins_and_timeouts(lane, monkeypatch):
    api = cli()
    _, argv, _ = lane
    args = api.parser().parse_args(argv)
    env = {"QDRANT_URL": "https://qdrant.invalid", "COUCHDB_URL": "https://couch.invalid",
           "COUCHDB_DB": "synthetic", "COUCHDB_USER": "user", "COUCHDB_PASSWORD": "secret",
           "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://primary.invalid/db",
           "LLM_BRAIN_PGVECTOR_DSN": "postgresql://secondary.invalid/db",
           "NEURON_LEDGER_PG_DSN": "postgresql://fallback.invalid/db"}
    boundary = api.LiveBoundary(args, api.Budget(20, 2), environ=env)
    calls = []
    def request(method, url, headers, body):
        calls.append((method, url, headers, json.loads(body) if body else None))
        from agent_knowledge.transport_contract import ProxyResponse
        result = {"config": {"params": {"vectors": {"size": 3072, "distance": "Cosine"}}}}
        if url.endswith("/scroll"):
            result = {"points": [], "next_page_offset": None}
        elif url.endswith("/points"):
            result = []
        return ProxyResponse(status_code=200, body=json.dumps({"result": result}).encode(), headers={})
    boundary.http.request = request
    assert boundary.collection_metadata() == {"size": 3072, "distance": "Cosine"}
    assert boundary.scroll(offset=None, limit=1, scope={"project": "synthetic", "provider": None}) == ([], None)
    assert boundary.retrieve([1]) == []
    assert calls[1][3]["filter"] == {"must": [{"key": "project", "match": {"value": "synthetic"}}]}
    assert calls[1][3]["with_vector"] is True and calls[1][3]["with_payload"] is True
    assert all(c[1].startswith("https://qdrant.invalid/collections/synthetic") for c in calls)
    assert boundary.sql.dsn == env["NEURON_LBRAIN_PGVECTOR_DSN"]
    assert boundary.source.auth_header.startswith("Basic ")
    assert boundary.source.request_timeout_seconds == 2
    boundary.close()


@pytest.mark.parametrize("url", ["", "http://user:password@q.invalid", "file:///tmp/source", "https://q.invalid/path", "https://q.invalid?secret=x"])
def test_bad_configured_origin_rejected_without_network(lane, url):
    api = cli()
    args = api.parser().parse_args(lane[1])
    with pytest.raises(ValueError):
        api.LiveBoundary(args, api.Budget(20, 2), environ={"QDRANT_URL": url,
            "COUCHDB_URL": "https://c.invalid", "NEURON_LBRAIN_PGVECTOR_DSN": "synthetic"})


def test_safe_http_rejects_cross_origin_redirect_and_bounds_response(monkeypatch):
    api = cli()
    budget = api.Budget(20, 2, max_requests=1)
    http = api.SafeHTTP(["https://q.invalid"], budget)
    with pytest.raises(ValueError):
        http.request("GET", "https://other.invalid/private", {}, b"")
    from urllib.request import Request
    with pytest.raises(ValueError):
        api.NoRedirect().redirect_request(Request("https://q.invalid"), None, 302, "", {}, "https://other.invalid")
    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size): return b"x" * size
    class Opener:
        def open(self, req, timeout):
            assert 0 < timeout <= 2
            return Response()
    http.opener = Opener()
    with pytest.raises(ValueError):
        http.request("GET", "https://q.invalid/oversized", {}, b"")
    with pytest.raises(TimeoutError):
        http.request("GET", "https://q.invalid/again", {}, b"")


@pytest.mark.parametrize("changed", ["host", "hostaddr", "port", "dbname", "user", "options"])
def test_target_fingerprint_binds_resolved_libpq_endpoint(lane, changed):
    from contextlib import contextmanager
    from types import SimpleNamespace
    api = cli()
    args = api.parser().parse_args(lane[1])
    boundary = api.LiveBoundary(args, api.Budget(20, 2), environ={
        "QDRANT_URL": "https://q.invalid", "COUCHDB_URL": "https://c.invalid",
        "NEURON_LBRAIN_PGVECTOR_DSN": "service=synthetic password=never-report-this"})
    parameters = dict(host="/tmp/synthetic-socket", hostaddr="", port="55479",
                      dbname="synthetic", user="synthetic", options="-c search_path=public",
                      password="never-report-this")
    class Connection:
        info = SimpleNamespace(get_parameters=lambda: dict(parameters))
        def execute(self, *args): return self
        def fetchone(self):
            return dict(database="synthetic", schema="public", table_oid=42, database_oid=12,
                        search_path="public", address=None, port=None)
    @contextmanager
    def scope(): yield Connection()
    boundary.sql._scope = scope
    first = boundary.target_fingerprint()
    parameters[changed] = {"host": "/tmp/other-socket", "hostaddr": "127.0.0.2", "port": "55480",
                           "dbname": "other", "user": "other", "options": "-c search_path=other"}[changed]
    second = boundary.target_fingerprint()
    assert first != second
    assert len(first) == len(second) == 64
    assert "never-report-this" not in first + second


@pytest.mark.parametrize("parameters", [
    {"service": "unresolved"},
    {"host": "", "port": "5432", "dbname": "synthetic", "user": "synthetic"},
    {"host": "a.invalid,b.invalid", "port": "5432", "dbname": "synthetic", "user": "synthetic"},
])
def test_ambiguous_resolved_endpoints_fail_closed(parameters):
    from types import SimpleNamespace
    with pytest.raises(ValueError):
        cli().resolved_pg_endpoint(SimpleNamespace(info=SimpleNamespace(get_parameters=lambda: parameters)))


def test_tcp_runtime_address_bound_but_password_excluded():
    from types import SimpleNamespace
    parameters = dict(host="synthetic.invalid", port="5432", dbname="synthetic", user="synthetic",
                      password="first-secret")
    info = SimpleNamespace(get_parameters=lambda: parameters, hostaddr="127.0.0.1")
    conn = SimpleNamespace(info=info)
    first = cli().resolved_pg_endpoint(conn)
    parameters["password"] = "second-secret"
    assert cli().resolved_pg_endpoint(conn) == first
    info.hostaddr = "127.0.0.2"
    assert cli().resolved_pg_endpoint(conn) != first


def test_sql_fingerprint_queries_current_identity_without_secret_output(lane, monkeypatch):
    api = cli()
    args = api.parser().parse_args(lane[1])
    boundary = api.LiveBoundary(args, api.Budget(20, 2), environ={"QDRANT_URL": "https://q.invalid",
        "COUCHDB_URL": "https://c.invalid", "NEURON_LBRAIN_PGVECTOR_DSN": "postgresql://user:secret@pg.invalid/db"})
    statements = []
    from types import SimpleNamespace
    class Connection:
        info = SimpleNamespace(get_parameters=lambda: dict(host="pg.invalid", hostaddr="127.0.0.1",
                                port="5432", dbname="synthetic", user="synthetic"))
        def execute(self, sql, params=None):
            statements.append(sql)
            return self
        def fetchone(self): return {"database": "synthetic", "schema": "public", "table_oid": 42}
    class Scope:
        def __enter__(self): return Connection()
        def __exit__(self, *args): pass
    boundary.sql._scope = lambda: Scope()
    result = boundary.target_fingerprint()
    assert len(result) == 64 and "secret" not in result
    assert any("current_database()" in s and "current_schema()" in s and "session_memory_chunks" in s for s in statements)
    boundary.close()


def test_executable_module_and_lazy_registration():
    import subprocess
    import sys
    from agent_knowledge import cli as router
    assert "pg-qdrant-import" in router.COMMAND_HANDLERS
    result = subprocess.run([sys.executable, "-m", "agent_knowledge.rag_ingress.pg_qdrant_import_cli", "--help"],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and "--limit" in result.stdout and "--apply" in result.stdout


def test_existing_valid_target_row_drift_is_detected(lane, capsys, monkeypatch):
    from agent_knowledge.postgres_store.pgvector_store import SessionChunk
    from agent_knowledge.rag_ingress.pg_backfill import _derive_pg_chunk_id_impl
    boundary, _, _ = lane
    assert invoke(lane, capsys)[0] == 0
    approve(lane)
    payload = boundary.points[0]["payload"]
    current = materialize_session_memory(session_id_hash=boundary.sid, store=boundary.source)
    row = SessionChunk(chunk_id=_derive_pg_chunk_id_impl(project=current.project, provider=current.provider,
        session_id_hash=boundary.sid, source_hash=current.source_hash, content_hash=dm.sha256_hash(payload["text"])),
        session_id_hash=boundary.sid, project=current.project, provider=current.provider,
        content_hash=dm.sha256_hash(payload["text"]), content_markdown=payload["text"],
        embedding_state="ready", embedding=boundary.points[0]["vector"])
    boundary.sql.get_chunk = lambda *a, **kw: row
    writes = record_imports(monkeypatch, boundary)
    assert invoke(lane, capsys, ["--apply"])[0] == 1
    assert writes == []


@pytest.mark.parametrize("fault", ["unknown_option", "duplicate_option", "bad_id", "empty_scope"])
def test_untrusted_input_never_leaks_to_stderr_or_manifest(lane, capsys, fault):
    boundary, argv, path = lane
    if fault == "unknown_option":
        argv.extend(["--password", "sensitive-secret"])
    elif fault == "duplicate_option":
        argv.extend(["--collection", "sensitive-secret"])
    elif fault == "bad_id":
        boundary.points[0]["id"] = "sensitive-secret-not-a-uuid"
    else:
        argv[argv.index("--project") + 1] = ""
    rc = cli()._run_worker(argv, boundary_factory=lambda *a: boundary)
    captured = capsys.readouterr()
    assert rc == 1 and "sensitive-secret" not in captured.out + captured.err
    assert not (path / "manifest.json").exists()


def test_couch_source_rejects_repeated_pages_instead_of_partial_materialization(lane):
    api = cli()
    args = api.parser().parse_args(lane[1])
    boundary = api.LiveBoundary(args, api.Budget(20, 2), environ={"QDRANT_URL": "https://q.invalid",
        "COUCHDB_URL": "https://c.invalid", "NEURON_LBRAIN_PGVECTOR_DSN": "synthetic"})
    calls = []
    def repeated(*args, **kwargs):
        calls.append(True)
        return 200, {"docs": [{"_id": "synthetic"}], "bookmark": "same"}
    boundary.source._request = repeated
    with pytest.raises(ValueError):
        boundary.source.find_by_session(session_id_hash="synthetic")
    assert len(calls) == 2


def test_sql_connection_rejects_endpoint_drift_before_any_statement(monkeypatch):
    import psycopg
    from types import SimpleNamespace
    api = cli()
    class Connection:
        info = SimpleNamespace(get_parameters=lambda: dict(host="/tmp/changed", port="55479",
                                                           dbname="synthetic", user="synthetic"))
        closed = False
        def execute(self, *args): pytest.fail("drift reached SQL")
        def close(self): self.closed = True
    conn = Connection()
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: conn)
    store = api.BoundedPgStore("service=synthetic", api.Budget(20, 2))
    store.endpoint_fingerprint = "approved-other-endpoint"
    with pytest.raises(ValueError, match="endpoint drift"):
        store._open_connection()
    assert conn.closed


def test_sql_connection_enforces_timeouts_and_cleanup(monkeypatch):
    api = cli()
    import psycopg
    captured = {}
    from types import SimpleNamespace
    class Connection:
        info = SimpleNamespace(get_parameters=lambda: dict(host="pg.invalid", hostaddr="127.0.0.1",
                                port="5432", dbname="synthetic", user="synthetic"))
        closed = False
        def execute(self, sql, params):
            captured["statement"] = (sql, params)
        def close(self): self.closed = True
    conn = Connection()
    def connect(dsn, **kwargs):
        captured.update(kwargs)
        return conn
    monkeypatch.setattr(psycopg, "connect", connect)
    store = api.BoundedPgStore("synthetic", api.Budget(20, 2))
    assert store._open_connection() is conn
    assert captured["connect_timeout"] == 2
    assert "statement_timeout" in captured["statement"][0]
    assert 0 < int(captured["statement"][1][0]) <= 2000
    conn.execute = lambda *a: (_ for _ in ()).throw(TimeoutError())
    with pytest.raises(TimeoutError):
        store._open_connection()
    assert conn.closed


@pytest.mark.parametrize("stage", ["rollback", "close"])
@pytest.mark.parametrize("apply", [False, True])
def test_process_deadline_includes_noncooperative_cleanup(lane, stage, apply):
    import subprocess
    import sys
    boundary, argv, path = lane
    argv[argv.index("--timeout-seconds") + 1] = "0.05"
    argv[argv.index("--request-timeout-seconds") + 1] = "0.01"
    if apply:
        argv.append("--apply")
    script = r'''
import json, os, signal, sys, time
from pathlib import Path
from agent_knowledge.rag_ingress import pg_qdrant_import_cli as cli
argv, stage, pidfile = json.loads(sys.argv[1])
# Isolate the process deadline independently of manifest validation.
cli.read_approved = lambda *a: {}
cli.code_revision = lambda: "synthetic-revision"
class Boundary:
    def __init__(self, *args):
        Path(pidfile).write_text(str(os.getpid()))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    def collection_metadata(self):
        if stage == "rollback":
            try:
                time.sleep(2)
            finally:
                signal.signal(signal.SIGALRM, signal.SIG_IGN)
                time.sleep(2)
        raise ValueError("synthetic failure")
    def close(self):
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
        time.sleep(2)
start = time.monotonic()
rc = cli.main(argv, boundary_factory=Boundary)
print(json.dumps({"elapsed": time.monotonic() - start, "rc": rc}))
'''
    pidfile = path / "worker.pid"
    result = subprocess.run([sys.executable, "-c", script,
                             json.dumps([argv, stage, str(pidfile)])],
                            text=True, capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr
    report, timing = [json.loads(line) for line in result.stdout.splitlines()]
    assert timing["rc"] == 1
    assert timing["elapsed"] < 0.35
    assert report["status"] == ("mutation_unknown" if apply else "rejected")
    assert report.get("deadline_exceeded") is True
    import os
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_supervised_main_reports_success_only_after_cleanup(lane, capsys):
    boundary, argv, path = lane
    closed = path / "closed"
    boundary.close = lambda: closed.write_text("closed")
    assert cli().main(argv, boundary_factory=lambda *args: boundary) == 0
    assert closed.read_text() == "closed"
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert json.loads((path / "manifest.json").read_text())["complete"] is True


def test_supervised_main_never_reports_success_when_cleanup_fails(lane, capsys):
    boundary, argv, _ = lane
    def failed_close():
        raise ValueError("private-secret")
    boundary.close = failed_close
    assert cli().main(argv, boundary_factory=lambda *args: boundary) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "rejected"
    assert "private-secret" not in captured.out + captured.err


def test_deadline_interrupts_blocking_boundary(lane, capsys):
    import time
    boundary, argv, path = lane
    argv[argv.index("--timeout-seconds") + 1] = "0.1"
    argv[argv.index("--request-timeout-seconds") + 1] = "0.05"
    finished = []
    def blocked():
        time.sleep(0.4)
        finished.append(True)
        return {"size": 3072, "distance": "Cosine"}
    boundary.collection_metadata = blocked
    rc, report = invoke(lane, capsys)
    assert rc == 1 and finished == []
    assert not (path / "manifest.json").exists()


def test_manifest_cannot_overwrite_attestation(lane, capsys):
    _, argv, path = lane
    argv[argv.index("--manifest") + 1] = str(path / "attestation.json")
    before = (path / "attestation.json").read_bytes()
    assert invoke(lane, capsys)[0] == 1
    assert (path / "attestation.json").read_bytes() == before


def test_existing_manifest_not_overwritten_without_new_run_path(lane, capsys):
    assert invoke(lane, capsys)[0] == 0
    before = (lane[2] / "manifest.json").read_bytes()
    assert invoke(lane, capsys)[0] == 1
    assert (lane[2] / "manifest.json").read_bytes() == before


def test_real_core_apply_and_duplicate_new_plan_are_idempotent(lane, capsys):
    from contextlib import contextmanager
    boundary, argv, path = lane
    class SyntheticSQL(ReadOnlySQL):
        def __init__(self):
            super().__init__()
            self.rows = {}
        def get_chunk(self, chunk_id, conn=None):
            return deepcopy(self.rows.get(chunk_id))
        @contextmanager
        def transaction(self):
            yield self
        @contextmanager
        def cursor(self):
            yield self
        def execute(self, sql, params):
            if sql.startswith("SELECT pg_advisory_xact_lock"):
                return self
            return super().execute(sql, params)
        def insert_chunk(self, row, conn=None, insert_only=False):
            assert insert_only is True and row.chunk_id not in self.rows
            self.writes += 1
            self.rows[row.chunk_id] = deepcopy(row)
    boundary.sql = SyntheticSQL()
    assert invoke(lane, capsys)[0] == 0
    approve(lane)
    assert invoke(lane, capsys, ["--apply"])[0] == 0
    receipt = deepcopy(boundary.source.get(dm.projection_state_doc_id(boundary.sid)))
    assert receipt["backend_receipts"]["postgres_pgvector"]["receipt_version"] == 2
    assert boundary.sql.writes == 1
    # An old absent-target plan cannot authorize a repeated apply after target mutation.
    assert invoke(lane, capsys, ["--apply"])[0] == 1
    argv[argv.index("--manifest") + 1] = str(path / "second-manifest.json")
    assert invoke(lane, capsys)[0] == 0
    second = json.loads((path / "second-manifest.json").read_text())
    approval = {"schema_version": 1, "operation": "pg-qdrant-import", "approved": True,
                "operator": "synthetic-human", "argv": argv + ["--apply"], "plan_digest": second["plan_digest"]}
    (path / "approval.json").write_text(json.dumps(approval))
    assert invoke(lane, capsys, ["--apply"])[0] == 0
    assert boundary.sql.writes == 1
    assert boundary.source.get(dm.projection_state_doc_id(boundary.sid)) == receipt


@pytest.mark.parametrize("opt_in", [None, "0", "true"])
def test_actual_sql_default_skips_without_connecting(monkeypatch, request, opt_in):
    import psycopg
    if opt_in is None:
        monkeypatch.delenv("ATLAS_QRET_SYNTHETIC_SQL", raising=False)
    else:
        monkeypatch.setenv("ATLAS_QRET_SYNTHETIC_SQL", opt_in)
    monkeypatch.setenv("LBRAIN_TEST_PG_DSN", "postgresql://dedicated.invalid/synthetic")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: pytest.fail("disabled SQL reached connect"))
    with pytest.raises(pytest.skip.Exception, match="opt-in synthetic PostgreSQL"):
        request.getfixturevalue("actual_sql")


@pytest.mark.parametrize("dsn", [None, "", "   "])
def test_actual_sql_opt_in_requires_explicit_dsn(monkeypatch, request, dsn):
    import psycopg
    monkeypatch.setenv("ATLAS_QRET_SYNTHETIC_SQL", "1")
    if dsn is None:
        monkeypatch.delenv("LBRAIN_TEST_PG_DSN", raising=False)
    else:
        monkeypatch.setenv("LBRAIN_TEST_PG_DSN", dsn)
    # Ambient application/libpq configuration must not authorize a connection.
    monkeypatch.setenv("NEURON_LBRAIN_PGVECTOR_DSN", "postgresql://ambient.invalid/live")
    monkeypatch.setenv("PGSERVICE", "ambient-live")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: pytest.fail("missing dedicated DSN reached connect"))
    with pytest.raises(pytest.fail.Exception, match="LBRAIN_TEST_PG_DSN is required"):
        request.getfixturevalue("actual_sql")


def test_actual_sql_connects_only_to_explicit_dedicated_dsn(monkeypatch, request):
    import psycopg
    dsn = "postgresql://synthetic@dedicated.invalid:5433/synthetic"
    monkeypatch.setenv("ATLAS_QRET_SYNTHETIC_SQL", "1")
    monkeypatch.setenv("LBRAIN_TEST_PG_DSN", dsn)
    class ConnectionProbe(Exception):
        pass
    def connect(configured_dsn, **kwargs):
        assert configured_dsn == dsn
        raise ConnectionProbe
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(ConnectionProbe):
        request.getfixturevalue("actual_sql")


@pytest.fixture
def actual_sql():
    """Use only an explicitly enabled, dedicated test DSN; never live defaults."""
    import os
    import psycopg
    from psycopg.rows import dict_row
    from agent_knowledge.postgres_store.pgvector_store import PgVectorStore
    if os.environ.get("ATLAS_QRET_SYNTHETIC_SQL") != "1":
        pytest.skip("opt-in synthetic PostgreSQL integration: ATLAS_QRET_SYNTHETIC_SQL=1")
    dsn = os.environ.get("LBRAIN_TEST_PG_DSN", "")
    if not dsn.strip():
        pytest.fail("LBRAIN_TEST_PG_DSN is required when ATLAS_QRET_SYNTHETIC_SQL=1")
    conn = psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
    try:
        # Session-local table shadows public; no persistent schema/data mutation.
        ddl = PgVectorStore.schema_sql()
        table = ddl[ddl.index("CREATE TABLE IF NOT EXISTS session_memory_chunks ("):]
        table = table[:table.index(";") + 1]
        table = table.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE", 1)
        conn.execute(table)
        conn.commit()
        yield PgVectorStore(connection=conn), conn, dsn
    finally:
        conn.close()


def test_actual_sql_readonly_batch_conflicts_and_equal_idempotence(lane, capsys, actual_sql):
    from agent_knowledge.rag_ingress.pg_backfill import normalized_pg_halfvec
    store, conn, dsn = actual_sql
    boundary, argv, path = lane
    boundary.sql = store
    boundary.points.append(dict(deepcopy(boundary.points[0]), id=2))
    # Float32 -> halfvec double rounding, not Python struct.pack('e').
    boundary.points[0]["vector"][0] = 1.000488281251
    boundary.points[1]["vector"][0] = 1.0
    conn.execute("SET TRANSACTION READ ONLY")
    canonical = normalized_pg_halfvec(store, boundary.points[0]["vector"])
    assert canonical[0] == 1.0
    assert invoke(lane, capsys)[0] == 0
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM session_memory_chunks").fetchone()["n"] == 0
    conn.commit()
    approve(lane)
    assert invoke(lane, capsys, ["--apply"])[0] == 0
    rows = conn.execute("SELECT chunk_id, embedding::text AS embedding FROM session_memory_chunks").fetchall()
    assert len(rows) == 1 and json.loads(rows[0]["embedding"]) == canonical
    conn.commit()
    receipt = deepcopy(boundary.source.get(dm.projection_state_doc_id(boundary.sid)))
    argv[argv.index("--manifest") + 1] = str(path / "second-manifest.json")
    assert invoke(lane, capsys)[0] == 0
    plan = json.loads((path / "second-manifest.json").read_text())
    (path / "approval.json").write_text(json.dumps({"schema_version": 1,
        "operation": "pg-qdrant-import", "approved": True, "operator": "synthetic-human",
        "argv": argv + ["--apply"], "plan_digest": plan["plan_digest"]}))
    assert invoke(lane, capsys, ["--apply"])[0] == 0
    assert conn.execute("SELECT count(*) AS n FROM session_memory_chunks").fetchone()["n"] == 1
    assert boundary.source.get(dm.projection_state_doc_id(boundary.sid)) == receipt
    conn.rollback()
    # Fresh absent canonical target: divergent vectors must reject before INSERT/CAS.
    conn.execute("TRUNCATE session_memory_chunks")
    conn.commit()
    boundary.source.delete(dm.projection_state_doc_id(boundary.sid))
    boundary.points[1]["vector"][0] = 1.001
    argv[argv.index("--manifest") + 1] = str(path / "divergent-manifest.json")
    conn.execute("SET TRANSACTION READ ONLY")
    assert invoke(lane, capsys)[0] == 2
    divergent = json.loads((path / "divergent-manifest.json").read_text())
    assert {p["status"] for p in divergent["points"]} == {"canonical_vector_conflict"}
    assert conn.execute("SELECT count(*) AS n FROM session_memory_chunks").fetchone()["n"] == 0
    assert boundary.source.get(dm.projection_state_doc_id(boundary.sid)) is None
    (path / "approval.json").write_text(json.dumps({"schema_version": 1,
        "operation": "pg-qdrant-import", "approved": True, "operator": "synthetic-human",
        "argv": argv + ["--apply"], "plan_digest": divergent["plan_digest"]}))
    rc, report = invoke(lane, capsys, ["--apply"])
    assert rc == 1 and report["mutation_started"] is False
    assert conn.execute("SELECT count(*) AS n FROM session_memory_chunks").fetchone()["n"] == 0
    assert boundary.source.get(dm.projection_state_doc_id(boundary.sid)) is None


def _write_test_pg_service(path, info):
    # get_parameters() may omit libpq defaults, including port 5432.
    path.write_text("[synthetic]\n" + "".join(
        f"{key}={getattr(info, key)}\n" for key in ("host", "port", "dbname", "user")))


def test_service_file_uses_resolved_defaults_without_credentials(tmp_path):
    from types import SimpleNamespace
    info = SimpleNamespace(
        get_parameters=lambda: {"host": "127.0.0.1", "dbname": "synthetic", "user": "synthetic"},
        host="127.0.0.1", port=5432, dbname="synthetic", user="synthetic",
        password="synthetic-never-write-this",
    )
    path = tmp_path / "service.conf"
    _write_test_pg_service(path, info)
    assert path.read_text() == "[synthetic]\nhost=127.0.0.1\nport=5432\ndbname=synthetic\nuser=synthetic\n"
    assert info.password not in path.read_text()


def test_actual_sql_service_resolution_and_readonly_target_fingerprint(lane, tmp_path, monkeypatch, actual_sql):
    import psycopg
    api = cli()
    store, conn, dsn = actual_sql
    servicefile = tmp_path / "pg_service.conf"
    _write_test_pg_service(servicefile, conn.info)
    monkeypatch.setenv("PGSERVICEFILE", str(servicefile))
    # libpq omits passwords from get_parameters(); keep CI credentials in memory.
    credentials = {"password": conn.info.password} if conn.info.password else {}
    with psycopg.connect("service=synthetic", connect_timeout=5, **credentials) as resolved:
        assert api.resolved_pg_endpoint(resolved) == api.resolved_pg_endpoint(conn)
    boundary = api.LiveBoundary(api.parser().parse_args(lane[1]), api.Budget(20, 2), environ={
        "QDRANT_URL": "https://q.invalid", "COUCHDB_URL": "https://c.invalid",
        "NEURON_LBRAIN_PGVECTOR_DSN": dsn})
    boundary.sql = store
    conn.execute("SET TRANSACTION READ ONLY")
    fingerprint = boundary.target_fingerprint()
    assert len(fingerprint) == 64
    conn.execute("SET LOCAL search_path = pg_temp, public")
    assert boundary.target_fingerprint() != fingerprint
    assert conn.execute("SELECT count(*) AS n FROM session_memory_chunks").fetchone()["n"] == 0


def test_out_of_scope_points_are_not_imported(lane, capsys):
    boundary, _, path = lane
    boundary.points[0]["payload"]["project"] = "different"
    assert invoke(lane, capsys)[0] != 0
    assert boundary.sql.writes == 0
    assert not (path / "manifest.json").exists()

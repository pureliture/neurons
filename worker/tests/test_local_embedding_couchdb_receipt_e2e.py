"""Disposable CouchDB -> isolated PostgreSQL local-embedding receipt-v3 E2E."""

from __future__ import annotations

import base64
import json
import os
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.document_model import (
    ProjectionStatus,
    build_conversation_chunk_document,
    build_projection_state_document,
    build_transcript_session_document,
    projection_state_doc_id,
    sha256_hash,
)
from agent_knowledge.couchdb_source.session_memory_materializer import (
    materialize_session_memory,
    project_session_memory,
    update_coverage_with_tool_evidence,
)
from agent_knowledge.local_embedding import (
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL_ALIAS,
    LOCAL_EMBEDDING_PROFILE_ID,
    CanonicalLocalEmbeddingClient,
    LocalEmbeddingServiceApplication,
    LocalModel2VecEngine,
    create_local_embedding_http_server,
)
from agent_knowledge.local_embedding import operator_runner, transition
from agent_knowledge.rag_ingress.pg_backfill import PgSessionMemoryProjector
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession

REQUIRED_COUCHDB_ENV = (
    "LBRAIN_TEST_COUCHDB_URL",
    "LBRAIN_TEST_COUCHDB_USER",
    "LBRAIN_TEST_COUCHDB_PASSWORD",
)
SYNTHETIC_BODY_MARKER = "CI_ONLY_SYNTHETIC_RECEIPT_BODY_"


def _required_environment() -> bool:
    return os.environ.get("NEURON_REQUIRE_MODEL2VEC_INTEGRATION", "") == "1"


def _loopback_couchdb_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        pytest.fail("LBRAIN_TEST_COUCHDB_URL must be an HTTP loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        pytest.fail("LBRAIN_TEST_COUCHDB_URL must not contain credentials or query data")
    if parsed.path not in {"", "/"}:
        pytest.fail("LBRAIN_TEST_COUCHDB_URL must identify the CouchDB root")
    loopback = host == "localhost"
    if not loopback:
        import ipaddress

        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        pytest.fail("LBRAIN_TEST_COUCHDB_URL must resolve to loopback")
    return f"{parsed.scheme}://{parsed.netloc}"


@pytest.fixture
def disposable_couchdb_store():
    config = {name: os.environ.get(name, "") for name in REQUIRED_COUCHDB_ENV}
    missing = [name for name, value in config.items() if not value]
    if missing:
        if _required_environment():
            pytest.fail("required LBRAIN_TEST_COUCHDB_* integration settings are missing")
        pytest.skip("LBRAIN_TEST_COUCHDB_* is required for disposable receipt integration")

    base_url = _loopback_couchdb_url(config["LBRAIN_TEST_COUCHDB_URL"])
    username = config["LBRAIN_TEST_COUCHDB_USER"]
    password = config["LBRAIN_TEST_COUCHDB_PASSWORD"]
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    database = "neurons_m2v_receipt_" + uuid.uuid4().hex
    store = CouchDBHttpSourceStore(
        base_url=base_url,
        db=database,
        auth_header=f"Basic {auth}",
        request_timeout_seconds=5,
    )
    for _attempt in range(60):
        try:
            ready_status, _payload = store._request("GET", "/")
        except Exception:
            ready_status = 0
        if ready_status == 200:
            break
        time.sleep(1)
    else:
        pytest.fail("disposable CouchDB service did not become ready")
    status, _payload = store._request("PUT", f"/{database}")
    if status not in (201, 202):
        pytest.fail("could not create unique disposable CouchDB database")
    try:
        yield store
    finally:
        delete_status, _payload = store._request("DELETE", f"/{database}")
        if delete_status not in (200, 202, 404):
            pytest.fail("could not remove the unique disposable CouchDB database")
        readback_status, _payload = store._request("GET", f"/{database}")
        if readback_status != 404:
            pytest.fail("unique disposable CouchDB database still exists after cleanup")


@pytest.fixture
def local_model2vec_server():
    model_dir_value = os.environ.get("NEURON_LOCAL_EMBEDDING_MODEL_DIR", "").strip()
    model_dir = Path(model_dir_value) if model_dir_value else None
    offline = (
        os.environ.get("HF_HUB_OFFLINE") == "1"
        and os.environ.get("TRANSFORMERS_OFFLINE") == "1"
    )
    if model_dir is None or not model_dir.is_dir() or not offline:
        if _required_environment():
            pytest.fail("pinned local Model2Vec weights and offline mode are required")
        pytest.skip("pinned offline Model2Vec snapshot is not configured")

    engine = LocalModel2VecEngine.from_model_dir(model_dir)
    server = create_local_embedding_http_server(
        application=LocalEmbeddingServiceApplication(engine=engine),
        bind_host="127.0.0.1",
        port=0,
    )
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def private_receipt_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "operator-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(
        operator_runner.tempfile,
        "gettempdir",
        lambda: str(tmp_path / "system-temp"),
    )
    checkpoint_root = home / ".config" / "neurons" / f"receipt-{uuid.uuid4().hex}"
    checkpoint_root.mkdir(mode=0o700, parents=True)
    checkpoint_root.chmod(0o700)
    checkpoint = checkpoint_root / "cursor.json"
    try:
        yield checkpoint
    finally:
        checkpoint.unlink(missing_ok=True)
        checkpoint_root.rmdir()


class _DeferredLocalEmbedder:
    model = LOCAL_EMBEDDING_MODEL_ALIAS
    size = LOCAL_EMBEDDING_DIM


class _LocalProjectorEmbedder:
    model = LOCAL_EMBEDDING_MODEL_ALIAS
    size = LOCAL_EMBEDDING_DIM

    def __init__(self, base_url: str):
        self.client = CanonicalLocalEmbeddingClient(base_url=base_url, use_openai_sdk=True)

    def embed(self, text: str) -> list[float]:
        return list(self.client.embed_document(text).vector)

    def close(self) -> None:
        self.client.close()


def _approval_payload(plan, argv: list[str]) -> dict:
    return {
        "schema_version": operator_runner.APPROVAL_SCHEMA_VERSION,
        "operation": "local_embedding_backfill",
        "operator_approval": {"approved": True},
        "redaction_required": True,
        "rollback_or_abort_criteria": "stop on any stale, failed, or unverifiable result",
        "time_budget_semantics": "between_batches_and_receipt_pages_only",
        "target": operator_runner._approval_target(plan),
        "limits": dict(plan.limits),
        "command": {"argv": argv},
    }


def _approved_phase(
    *,
    phase: str,
    scope: str | None,
    plan_values: dict,
    environment: dict[str, str],
    approval_path: Path,
    runtime_builder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forbidden_output_values: tuple[str, ...] = (),
) -> dict:
    target_identity = operator_runner._resolve_target_identity(
        "pg", environment, phase=phase
    )
    plan = operator_runner.build_backfill_plan(
        lane="pg",
        phase=phase,
        vector_scope=scope,
        target_identity=target_identity,
        **plan_values,
    )
    argv = ["--lane", "pg", "--phase", phase]
    if scope:
        argv.extend(["--vector-scope", scope])
    argv.extend(
        [
            "--batch-size", str(plan.batch_size),
            "--max-batches", str(plan.max_batches),
            "--max-batches-per-group", str(plan.max_batches_per_group),
            "--max-seconds", str(plan.max_seconds),
            "--lease-seconds", str(plan.lease_seconds),
        ]
    )
    if phase == "receipts":
        argv.extend(
            [
                "--receipt-page-size", str(plan.receipt_page_size),
                "--receipt-max-pages", str(plan.receipt_max_pages),
                "--receipt-max-rows", str(plan.receipt_max_rows),
                "--receipt-max-seconds", str(plan.receipt_max_seconds),
                "--receipt-checkpoint", plan.receipt_checkpoint_path,
                "--receipt-run-id", plan.receipt_run_id,
                "--receipt-source-identity", plan.receipt_source_identity,
            ]
        )
    argv.extend(["--execute", "--approval", str(approval_path)])
    approval_path.write_text(json.dumps(_approval_payload(plan, argv)), encoding="utf-8")
    approval_path.chmod(0o600)
    monkeypatch.setattr(operator_runner, "_build_runtime_clients", runtime_builder)

    result = operator_runner.main(argv, environ=environment)
    captured = capsys.readouterr()
    output = captured.out.strip() or captured.err.strip()
    if result != 0:
        try:
            failure_report = json.loads(output)
            reason_code = failure_report.get("reason_code", "unknown")
        except (TypeError, ValueError):
            reason_code = "report_unavailable"
        pytest.fail(f"operator phase failed with exit={result}, reason={reason_code}")
    assert not any(value and value in output for value in forbidden_output_values), (
        "operator report contained synthetic source identity or text"
    )
    report = json.loads(output)
    assert report["success"] is True
    return report


def test_couchdb_to_pg_receipt_v3_uses_current_source_and_private_checkpoint(
    isolated_pg_store,
    disposable_couchdb_store,
    local_model2vec_server,
    private_receipt_checkpoint,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    pg_store = isolated_pg_store
    pg_store.embedding_profile = LOCAL_EMBEDDING_PROFILE_ID
    source_store = disposable_couchdb_store
    server_url = local_model2vec_server
    pg_store_id = uuid.uuid4().hex
    source_id = f"synthetic-session-{pg_store_id}"
    session_id_hash = sha256_hash(source_id)
    now = datetime.now(timezone.utc).isoformat()
    body = SYNTHETIC_BODY_MARKER + pg_store_id

    session = TranscriptSession(
        session_id_hash=session_id_hash,
        provider="codex",
        project="neurons-ci",
        started_at=now,
        ended_at=now,
    )
    chunk = TranscriptChunk.from_text(
        chunk_id=f"synthetic-chunk-{pg_store_id}",
        session_id_hash=session_id_hash,
        provider="codex",
        project="neurons-ci",
        turn_start_index=0,
        turn_end_index=0,
        text=body,
        observed_at_start=now,
        observed_at_end=now,
    )
    source_store.put(build_transcript_session_document(session=session))
    source_store.put(build_conversation_chunk_document(chunk=chunk))
    coverage = update_coverage_with_tool_evidence(
        session_id_hash=session_id_hash,
        store=source_store,
    )
    assert coverage is not None
    materialized = materialize_session_memory(
        session_id_hash=session_id_hash,
        store=source_store,
    )
    assert materialized.fully_materialized is True
    assert materialized.conversation_chunk_count == 1
    assert body in materialized.body

    # Seed only an isolated pending PG projection; the approved vectors phase
    # performs the real local model call and writes its 256-dimensional vector.
    deferred_projector = PgSessionMemoryProjector(
        store=pg_store,
        embed_provider=_DeferredLocalEmbedder(),
    )
    pending = project_session_memory(
        materialized=materialized,
        store=source_store,
        projector=deferred_projector,
        backend="postgres_pgvector",
    )
    assert pending["status"] == "pending"

    couch_url = os.environ["LBRAIN_TEST_COUCHDB_URL"].rstrip("/")
    couch_identity_url = _loopback_couchdb_url(couch_url)
    service_host, service_port = urlsplit(server_url).hostname, urlsplit(server_url).port
    synthetic_env = {
        "NEURON_LEDGER_PG_DSN": "postgresql://isolated-fixture/neurons-ci",
        "LLM_BRAIN_PGVECTOR_VERSION": "0.8.0",
        "COUCHDB_URL": couch_url,
        "COUCHDB_DB": source_store.db,
        "COUCHDB_USER": os.environ["LBRAIN_TEST_COUCHDB_USER"],
        "COUCHDB_PASSWORD": os.environ["LBRAIN_TEST_COUCHDB_PASSWORD"],
        "LLM_BRAIN_EMBEDDING_BASE_URL": server_url,
        "LLM_BRAIN_EMBEDDING_MODEL": LOCAL_EMBEDDING_MODEL_ALIAS,
    }

    def target_identity(lane, environ, *, phase=None):
        return {
            "lane": lane,
            "phase": phase,
            "pg_endpoint": "isolated-loopback-fixture",
            "couchdb_endpoint": couch_identity_url,
            "couchdb_database": source_store.db if phase == "receipts" else "",
            "embedding_endpoint": f"http://{service_host}:{service_port}",
        }

    monkeypatch.setattr(operator_runner, "_resolve_target_identity", target_identity)

    def runtime_builder(plan, *, environ):
        if plan.phase == "vectors":
            client = CanonicalLocalEmbeddingClient(
                base_url=server_url,
                use_openai_sdk=True,
            )
            embedder = operator_runner._CountingLocalEmbedder(client)
            return {"store": pg_store, "embedder": embedder, "_close": embedder.close}
        return {
            "store": pg_store,
            "couchdb_store": source_store,
            "checkpoint_path": Path(plan.receipt_checkpoint_path),
            "_close": lambda: None,
        }

    common_limits = {
        "batch_size": 1,
        "max_batches": 1,
        "max_batches_per_group": 1,
        "max_seconds": 300,
        "lease_seconds": 300,
    }
    vectors = _approved_phase(
        phase="vectors",
        scope="sessions",
        plan_values=common_limits,
        environment=synthetic_env,
        approval_path=tmp_path / "vectors-approval.json",
        runtime_builder=runtime_builder,
        monkeypatch=monkeypatch,
        capsys=capsys,
        forbidden_output_values=(source_id, session_id_hash, chunk.chunk_id, body),
    )
    assert vectors["ready"] == 1
    assert vectors["missing"] == 0
    assert vectors["model_calls"] == 1

    projector_embedder = _LocalProjectorEmbedder(server_url)
    try:
        projected = project_session_memory(
            materialized=materialized,
            store=source_store,
            projector=PgSessionMemoryProjector(
                store=pg_store,
                embed_provider=projector_embedder,
            ),
            backend="postgres_pgvector",
        )
    finally:
        projector_embedder.close()
    assert projected["status"] == "projected"
    local_row = pg_store.get_local_embedding_row(
        target_type="session_chunk",
        target_id=projected["ref"],
        embedding_profile=LOCAL_EMBEDDING_PROFILE_ID,
    )
    assert local_row is not None
    assert local_row["state"] == "ready"
    assert len(local_row["vector"]) == LOCAL_EMBEDDING_DIM

    # Model the durable CouchDB projection-state readback that receipt v3
    # requires after the PG source has been projected.
    source_store.put(
        build_projection_state_document(
            session_id_hash=session_id_hash,
            provider="codex",
            project="neurons-ci",
            projection_status=ProjectionStatus.PROJECTED,
            session_memory_knowledge_id=projected["ref"],
            active_content_hash=materialized.content_hash,
            source_hash=materialized.source_hash,
            projected_source_hash=materialized.source_hash,
            materialized_at=materialized.materialized_at,
        )
    )

    receipt_source_identity, source_row_count = transition._receipt_source_input_identity(
        pg_store,
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        page_size=16,
    )
    assert source_row_count == 1
    checkpoint = private_receipt_checkpoint
    run_id = "ci-receipt-" + uuid.uuid4().hex
    receipt_limits = {
        **common_limits,
        "receipt_page_size": 16,
        "receipt_max_pages": 5,
        "receipt_max_rows": 50,
        "receipt_max_seconds": 60,
        "receipt_checkpoint_path": checkpoint,
        "receipt_run_id": run_id,
        "receipt_source_identity": receipt_source_identity,
    }
    receipts = _approved_phase(
        phase="receipts",
        scope=None,
        plan_values=receipt_limits,
        environment=synthetic_env,
        approval_path=tmp_path / "receipts-approval.json",
        runtime_builder=runtime_builder,
        monkeypatch=monkeypatch,
        capsys=capsys,
        forbidden_output_values=(source_id, session_id_hash, chunk.chunk_id, body),
    )
    assert receipts["ready"] == 1
    assert receipts["missing"] == 0
    projection_state = source_store.get(projection_state_doc_id(session_id_hash))
    assert projection_state is not None
    assert projection_state["projected_source_hash"] == materialized.source_hash
    local_receipts = projection_state["backend_receipts"]["postgres_pgvector_local"]
    receipt = local_receipts[LOCAL_EMBEDDING_PROFILE_ID]
    receipt = receipt.get("representation_receipt", receipt.get("receipt", receipt))
    assert receipt["receipt_version"] == 3
    assert receipt["embedding_profile"] == LOCAL_EMBEDDING_PROFILE_ID
    assert receipt["projected_source_hash"] == materialized.source_hash
    assert receipt["active_content_hash"] == materialized.content_hash
    assert receipt["representation_content_hash"] == materialized.content_hash
    current = materialize_session_memory(
        session_id_hash=session_id_hash,
        store=source_store,
    )
    assert current.source_hash == receipt["projected_source_hash"]

    assert checkpoint.is_file()
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    checkpoint_text = checkpoint.read_text(encoding="utf-8")
    for private_value in (source_id, session_id_hash, chunk.chunk_id, body):
        assert private_value not in checkpoint_text

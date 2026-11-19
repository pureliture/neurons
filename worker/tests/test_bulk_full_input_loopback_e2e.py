"""Full product-chain E2E over isolated loopback CouchDB, PG, Neo4j, and HTTP stub.

All data is synthetic and strictly loopback-only. External model APIs, remote
clusters, production DBs, and real transcripts are never touched.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import subprocess
import threading
from urllib.parse import urlsplit
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest
from neo4j import GraphDatabase

from agent_knowledge.couchdb_source import document_model as dm
from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from agent_knowledge.couchdb_source.source_revision import activate_source_revision
from agent_knowledge.knowledge_search_service import (
    DisabledRetiredIndexBridgeClient,
    KnowledgeSearchService,
)
from agent_knowledge.llm_brain_core import bulk_semantic_cli as cli
from agent_knowledge.llm_brain_core.bulk_semantic import (
    OpenAICompatibleBulkSemanticExtractor,
    _urllib_post,
)
from agent_knowledge.llm_brain_core.models import GraphMemoryResult, OntologyEpisode
from agent_knowledge.llm_brain_core.shadow_search_adapter import (
    ShadowSearchAdapter,
    build_shadow_search_adapter_from_env,
)
from agent_knowledge.mcp_jsonrpc import handle_jsonrpc_message
from agent_knowledge.session_memory.brain_steward import BrainStewardService
from agent_knowledge.session_memory.transcript_model import TranscriptChunk, TranscriptSession


class OwnedTestInfraError(RuntimeError):
    """Fail-closed error raised when live E2E endpoints fail loopback or ownership checks."""


class OwnedDbCollisionError(OwnedTestInfraError):
    """Raised when a disposable CouchDB DB already exists (HTTP 412); never dropped."""


_STRICT_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_STRICT_LOOPBACK_BIND_IPS = frozenset({"127.0.0.1", "::1"})
_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{3,63}$")
_CONTAINER_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,127}$")
_OWNED_DB_NAME_RE = re.compile(r"^(?:e2efull|e2espool)_[0-9a-f]{10,16}$")
_VERIFIED_TOKEN = object()

_REQUIRED_OWNED_ENV_KEYS = (
    "LBRAIN_TEST_PG_DSN",
    "TRIAL_NEO4J_URI",
    "TRIAL_NEO4J_PASSWORD",
    "TRIAL_COUCH_URL",
    "TRIAL_COUCH_USER",
    "TRIAL_COUCH_PASSWORD",
    "TRIAL_TEST_OWNER",
    "TRIAL_TEST_PG_CONTAINER",
    "TRIAL_TEST_COUCH_CONTAINER",
    "TRIAL_TEST_NEO4J_CONTAINER",
)
_LIVE_INFRA_TRIGGER_KEYS = tuple(k for k in _REQUIRED_OWNED_ENV_KEYS if k != "LBRAIN_TEST_PG_DSN")


def _has_any_live_trial_trigger(env: Mapping[str, str]) -> bool:
    return any(str(env.get(key) or "").strip() for key in _LIVE_INFRA_TRIGGER_KEYS)


_REQUIRE_LIVE_OWNED_INFRA = pytest.mark.skipif(
    not _has_any_live_trial_trigger(os.environ),
    reason="isolated owned loopback PG/Neo4j/CouchDB env vars not configured",
)


@dataclass(frozen=True)
class _VerifiedOwnedLoopbackConfig:
    pg_dsn: str
    neo4j_uri: str
    neo4j_password: str
    couch_url: str
    couch_user: str
    couch_password: str
    test_owner: str
    pg_container: str
    couch_container: str
    neo4j_container: str
    _verified_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verified_token is not _VERIFIED_TOKEN:
            raise OwnedTestInfraError("unverified _VerifiedOwnedLoopbackConfig construction is forbidden")


def _is_strict_loopback_host(host: str | None) -> bool:
    return (host or "").strip().lower() in _STRICT_LOOPBACK_HOSTS


def _validate_port(port: Any, *, label: str) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise OwnedTestInfraError(f"{label} requires an explicit TCP port (1..65535)")
    return port


def _format_host_port(hostname: str, port: int) -> str:
    return f"[{hostname}]:{port}" if ":" in hostname else f"{hostname}:{port}"


def _parse_loopback_couch_url(raw_url: str) -> tuple[str, int]:
    stripped = raw_url.strip()
    parsed = urlsplit(stripped)
    if parsed.scheme != "http":
        raise OwnedTestInfraError(f"TRIAL_COUCH_URL must use plain http:// loopback, got {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise OwnedTestInfraError("TRIAL_COUCH_URL must not embed credentials in URL")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise OwnedTestInfraError("TRIAL_COUCH_URL must not specify path, query, or fragment")
    if not _is_strict_loopback_host(parsed.hostname):
        raise OwnedTestInfraError(f"TRIAL_COUCH_URL host must be strict loopback, got {parsed.hostname!r}")
    try:
        port = _validate_port(parsed.port, label="TRIAL_COUCH_URL")
    except ValueError as exc:
        raise OwnedTestInfraError("TRIAL_COUCH_URL has invalid port") from exc
    return f"http://{_format_host_port(str(parsed.hostname), port)}", port


def _parse_loopback_neo4j_uri(raw_uri: str) -> tuple[str, int]:
    stripped = raw_uri.strip()
    parsed = urlsplit(stripped)
    if parsed.scheme not in ("bolt", "neo4j"):
        raise OwnedTestInfraError(f"TRIAL_NEO4J_URI must use bolt:// or neo4j://, got {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise OwnedTestInfraError("TRIAL_NEO4J_URI must not embed credentials in URI")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise OwnedTestInfraError("TRIAL_NEO4J_URI must not specify path, query, or fragment")
    if not _is_strict_loopback_host(parsed.hostname):
        raise OwnedTestInfraError(f"TRIAL_NEO4J_URI host must be strict loopback, got {parsed.hostname!r}")
    try:
        port = _validate_port(parsed.port, label="TRIAL_NEO4J_URI")
    except ValueError as exc:
        raise OwnedTestInfraError("TRIAL_NEO4J_URI has invalid port") from exc
    return f"{parsed.scheme}://{_format_host_port(str(parsed.hostname), port)}", port


def _parse_loopback_pg_dsn(raw_dsn: str) -> int:
    stripped = raw_dsn.strip()
    uri_host: str | None = None
    uri_port: int | None = None
    if "://" in stripped:
        parsed = urlsplit(stripped)
        if parsed.scheme not in ("postgresql", "postgres"):
            raise OwnedTestInfraError(f"LBRAIN_TEST_PG_DSN URI scheme must be postgresql/postgres, got {parsed.scheme!r}")
        if not _is_strict_loopback_host(parsed.hostname):
            raise OwnedTestInfraError(f"LBRAIN_TEST_PG_DSN host must be strict loopback, got {parsed.hostname!r}")
        try:
            uri_port = _validate_port(parsed.port, label="LBRAIN_TEST_PG_DSN")
        except ValueError as exc:
            raise OwnedTestInfraError("LBRAIN_TEST_PG_DSN has invalid port") from exc
        uri_host = str(parsed.hostname).lower()
    try:
        cfg = conninfo_to_dict(stripped)
    except Exception as exc:
        raise OwnedTestInfraError("LBRAIN_TEST_PG_DSN failed libpq conninfo parsing") from exc
    if cfg.get("service") or cfg.get("hostaddr"):
        raise OwnedTestInfraError("LBRAIN_TEST_PG_DSN must not use libpq service or hostaddr overrides")
    host = str(cfg.get("host") or "")
    if not _is_strict_loopback_host(host):
        raise OwnedTestInfraError(f"LBRAIN_TEST_PG_DSN effective host must be strict loopback, got {host!r}")
    port_raw = str(cfg.get("port") or "")
    if not port_raw.isdigit():
        raise OwnedTestInfraError("LBRAIN_TEST_PG_DSN requires a single numeric port")
    port = _validate_port(int(port_raw), label="LBRAIN_TEST_PG_DSN")
    if uri_host is not None and (host.lower() != uri_host or port != uri_port):
        raise OwnedTestInfraError("LBRAIN_TEST_PG_DSN query parameters must not override URI host or port")
    if not str(cfg.get("dbname") or "").strip() or not str(cfg.get("user") or "").strip():
        raise OwnedTestInfraError("LBRAIN_TEST_PG_DSN requires explicit dbname and user")
    return port


def _docker_inspect_container(container_ref: str) -> dict[str, Any]:
    if not _CONTAINER_REF_RE.match(container_ref):
        raise OwnedTestInfraError(f"invalid container reference: {container_ref!r}")
    try:
        proc = subprocess.run(
            ["docker", "inspect", container_ref],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OwnedTestInfraError(f"failed to inspect container {container_ref!r}: {exc}") from exc
    if proc.returncode != 0:
        raise OwnedTestInfraError(f"docker inspect rejected container {container_ref!r} (rc={proc.returncode})")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise OwnedTestInfraError(f"docker inspect returned invalid JSON for {container_ref!r}") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise OwnedTestInfraError(f"docker inspect must resolve exactly one container for {container_ref!r}")
    return payload[0]


def _verify_container_ownership(
    container_ref: str,
    *,
    inspect_fn: Callable[[str], dict[str, Any]],
    expected_owner: str,
    expected_service: str,
    container_port_key: str,
    expected_host_port: int,
) -> str | None:
    info = inspect_fn(container_ref)
    if not isinstance(info, dict):
        raise OwnedTestInfraError(f"invalid inspect metadata for {container_ref!r}")
    state = info.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise OwnedTestInfraError(f"container {container_ref!r} ({expected_service}) is not running")
    labels = (info.get("Config") or {}).get("Labels") if isinstance(info.get("Config"), dict) else None
    if not isinstance(labels, dict):
        raise OwnedTestInfraError(f"container {container_ref!r} ({expected_service}) has no labels")
    if labels.get("neuron.disposable") != "true":
        raise OwnedTestInfraError(f"container {container_ref!r} ({expected_service}) is not marked neuron.disposable=true")
    if labels.get("neuron.test_owner") != expected_owner:
        raise OwnedTestInfraError(f"container {container_ref!r} ({expected_service}) owner mismatch")
    if labels.get("neuron.test_service") != expected_service:
        raise OwnedTestInfraError(f"container {container_ref!r} service label mismatch for {expected_service}")

    ports = (info.get("NetworkSettings") or {}).get("Ports") if isinstance(info.get("NetworkSettings"), dict) else None
    if not isinstance(ports, dict):
        raise OwnedTestInfraError(f"container {container_ref!r} ({expected_service}) has missing port metadata")
    for port_key, bindings in ports.items():
        if bindings is None:
            continue
        if not isinstance(bindings, list):
            raise OwnedTestInfraError(f"container {container_ref!r} invalid port bindings for {port_key}")
        for binding in bindings:
            if not isinstance(binding, dict):
                raise OwnedTestInfraError(f"container {container_ref!r} invalid binding entry for {port_key}")
            host_ip = str(binding.get("HostIp") or "").strip()
            if host_ip not in _STRICT_LOOPBACK_BIND_IPS:
                raise OwnedTestInfraError(
                    f"container {container_ref!r} ({expected_service}) binds {port_key} to non-loopback IP {host_ip!r}"
                )

    target_bindings = ports.get(container_port_key)
    if not isinstance(target_bindings, list) or not target_bindings:
        raise OwnedTestInfraError(f"container {container_ref!r} ({expected_service}) port {container_port_key} is not published")
    matched = [
        b
        for b in target_bindings
        if str(b.get("HostPort") or "") == str(expected_host_port)
        and str(b.get("HostIp") or "").strip() in _STRICT_LOOPBACK_BIND_IPS
    ]
    if not matched:
        raise OwnedTestInfraError(
            f"container {container_ref!r} ({expected_service}) {container_port_key} does not match expected port {expected_host_port}"
        )
    resolved_id = str(info.get("Id") or "").strip()
    return resolved_id or None


def _verify_owned_loopback_config(
    env: Mapping[str, str],
    *,
    inspect_fn: Callable[[str], dict[str, Any]] | None = None,
) -> _VerifiedOwnedLoopbackConfig:
    missing = [key for key in _REQUIRED_OWNED_ENV_KEYS if not str(env.get(key) or "").strip()]
    if missing:
        raise OwnedTestInfraError(f"missing required owned loopback env keys: {', '.join(missing)}")

    owner = str(env["TRIAL_TEST_OWNER"]).strip()
    if not _OWNER_RE.match(owner):
        raise OwnedTestInfraError("TRIAL_TEST_OWNER does not match allowed identifier pattern")

    pg_container = str(env["TRIAL_TEST_PG_CONTAINER"]).strip()
    couch_container = str(env["TRIAL_TEST_COUCH_CONTAINER"]).strip()
    neo4j_container = str(env["TRIAL_TEST_NEO4J_CONTAINER"]).strip()
    for cref in (pg_container, couch_container, neo4j_container):
        if not _CONTAINER_REF_RE.match(cref):
            raise OwnedTestInfraError(f"invalid container reference: {cref!r}")
    if len({pg_container, couch_container, neo4j_container}) != 3:
        raise OwnedTestInfraError("PG, CouchDB, and Neo4j container references must be distinct")

    # Fail-closed syntax + strict loopback checks MUST precede any container inspect call.
    pg_dsn = str(env["LBRAIN_TEST_PG_DSN"]).strip()
    pg_port = _parse_loopback_pg_dsn(pg_dsn)
    couch_url, couch_port = _parse_loopback_couch_url(str(env["TRIAL_COUCH_URL"]))
    neo4j_uri, neo4j_port = _parse_loopback_neo4j_uri(str(env["TRIAL_NEO4J_URI"]))

    inspector = inspect_fn or _docker_inspect_container
    pg_id = _verify_container_ownership(
        pg_container,
        inspect_fn=inspector,
        expected_owner=owner,
        expected_service="postgres",
        container_port_key="5432/tcp",
        expected_host_port=pg_port,
    )
    couch_id = _verify_container_ownership(
        couch_container,
        inspect_fn=inspector,
        expected_owner=owner,
        expected_service="couchdb",
        container_port_key="5984/tcp",
        expected_host_port=couch_port,
    )
    neo4j_id = _verify_container_ownership(
        neo4j_container,
        inspect_fn=inspector,
        expected_owner=owner,
        expected_service="neo4j",
        container_port_key="7687/tcp",
        expected_host_port=neo4j_port,
    )
    resolved_ids = [cid for cid in (pg_id, couch_id, neo4j_id) if cid is not None]
    if len(set(resolved_ids)) != len(resolved_ids):
        raise OwnedTestInfraError("PG, CouchDB, and Neo4j containers resolved to duplicate container IDs")

    return _VerifiedOwnedLoopbackConfig(
        pg_dsn=pg_dsn,
        neo4j_uri=neo4j_uri,
        neo4j_password=str(env["TRIAL_NEO4J_PASSWORD"]),
        couch_url=couch_url,
        couch_user=str(env["TRIAL_COUCH_USER"]),
        couch_password=str(env["TRIAL_COUCH_PASSWORD"]),
        test_owner=owner,
        pg_container=pg_container,
        couch_container=couch_container,
        neo4j_container=neo4j_container,
        _verified_token=_VERIFIED_TOKEN,
    )


def _require_verified_config(cfg: Any) -> _VerifiedOwnedLoopbackConfig:
    if not isinstance(cfg, _VerifiedOwnedLoopbackConfig) or cfg._verified_token is not _VERIFIED_TOKEN:
        raise OwnedTestInfraError("refusing DB/graph operation without verified owned loopback config")
    return cfg


class _NoCardLedger:
    read_only = False

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("get_llm_brain", "list_llm_brain", "upsert_llm_brain", "_transaction")):
            raise AssertionError("SQLite card lane must not be accessed")
        raise AttributeError(name)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _couch_auth(cfg: _VerifiedOwnedLoopbackConfig) -> str:
    token = base64.b64encode(f"{cfg.couch_user}:{cfg.couch_password}".encode("utf-8")).decode("ascii")
    return "Basic " + token


@contextmanager
def _owned_couch_db(
    cfg: _VerifiedOwnedLoopbackConfig,
    name: str,
    *,
    http_client: Any = httpx,
) -> Iterator[CouchDBHttpSourceStore]:
    verified = _require_verified_config(cfg)
    if not _OWNED_DB_NAME_RE.match(name):
        raise OwnedTestInfraError(f"refusing non-disposable CouchDB database name: {name!r}")
    auth = _couch_auth(verified)
    db_url = f"{verified.couch_url}/{name}"
    created = False
    test_failed = False
    try:
        resp = http_client.put(db_url, headers={"Authorization": auth}, timeout=10)
        if resp.status_code == 412:
            raise OwnedDbCollisionError(f"CouchDB database {name!r} already exists (412); refusing mutation or drop")
        if resp.status_code not in (201, 202):
            raise OwnedTestInfraError(f"failed to create owned CouchDB database {name!r}: status={resp.status_code}")
        created = True
        yield CouchDBHttpSourceStore(base_url=verified.couch_url, db=name, auth_header=auth)
    except BaseException:
        test_failed = True
        raise
    finally:
        if created:
            del_resp = http_client.delete(db_url, headers={"Authorization": auth}, timeout=10)
            if not test_failed and del_resp.status_code not in (200, 202, 404):
                raise OwnedTestInfraError(
                    f"failed to drop owned CouchDB database {name!r}: status={del_resp.status_code}"
                )


class _OwnedNeo4jScope:
    def __init__(
        self,
        cfg: _VerifiedOwnedLoopbackConfig,
        *,
        groups: tuple[str, ...],
        driver_factory: Any,
    ) -> None:
        self._cfg = cfg
        self._groups = groups
        self._driver_factory = driver_factory
        self.created_marker_uids: list[str] = []

    def create_legacy_marker(self, uid: str) -> None:
        if not uid.strip():
            raise OwnedTestInfraError("legacy marker uid must be non-empty")
        with self._driver_factory(self._cfg.neo4j_uri, auth=("neo4j", self._cfg.neo4j_password)) as driver:
            with driver.session() as session:
                existing = session.run(
                    "MATCH (m:TrialLegacyMarker {uid: $uid}) RETURN count(m) AS c",
                    uid=uid,
                ).single()
                if existing is None or int(existing["c"]) != 0:
                    raise OwnedTestInfraError(f"TrialLegacyMarker collision for uid={uid!r}; refusing overwrite")
                session.run(
                    "CREATE (m:TrialLegacyMarker {uid: $uid, test_owner: $owner, marker: 'untouched'})",
                    uid=uid,
                    owner=self._cfg.test_owner,
                )
        self.created_marker_uids.append(uid)


@contextmanager
def _owned_neo4j_scope(
    cfg: _VerifiedOwnedLoopbackConfig,
    *,
    groups: tuple[str, ...],
    driver_factory: Any = None,
) -> Iterator[_OwnedNeo4jScope]:
    verified = _require_verified_config(cfg)
    if not groups or any(not str(g).strip() for g in groups):
        raise OwnedTestInfraError("Neo4j cleanup scope requires non-empty owned shadow groups")
    factory = driver_factory or GraphDatabase.driver
    scope = _OwnedNeo4jScope(verified, groups=tuple(groups), driver_factory=factory)
    try:
        yield scope
    finally:
        with factory(verified.neo4j_uri, auth=("neo4j", verified.neo4j_password)) as driver:
            with driver.session() as session:
                session.run(
                    "MATCH (n:TrialShadowEntity) WHERE n.group_id IN $groups DETACH DELETE n",
                    groups=list(groups),
                )
                if scope.created_marker_uids:
                    session.run(
                        "MATCH (m:TrialLegacyMarker) WHERE m.uid IN $uids AND m.test_owner = $owner DELETE m",
                        uids=list(scope.created_marker_uids),
                        owner=verified.test_owner,
                    )


def _create_card(store: Any, *, project: str, label: str, content_hash: str, approve: bool) -> str:
    span = {
        "card_type": "preference",
        "project": project,
        "provider": "synthetic-provider",
        "scope": "project",
        "title": "synthetic card " + label,
        "redacted_summary": "synthetic card for loopback e2e verification",
        "typed_payload": {
            "preference": "synthetic preference " + label,
            "explicitness": "explicit",
            "repeated_count": 1,
            "confirmation_status": "confirmed",
            "applies_to": "loopback_e2e",
        },
        "source_ref": {"source_id": "synthetic-source-" + label},
        "span_ref": {"span_id": "synthetic-span-" + label},
        "content_hash": content_hash,
        "confidence": 0.9,
        "confidence_basis": "synthetic loopback test",
    }
    steward = BrainStewardService(_NoCardLedger(), pgvector_store=store, allow_restricted=True)
    memory_id = steward.candidate_create(source_span=span)["memory_id"]
    if approve:
        steward.candidate_approve(
            candidate_memory_id=memory_id,
            approved_by="synthetic-steward",
            decision_id="e2e-" + uuid.uuid4().hex,
        )
    return memory_id


def _seed_couch_session(
    store: CouchDBHttpSourceStore,
    *,
    project: str,
    label: str,
    conversation_bodies: list[str],
    evidence_bodies: list[str],
    activate: bool = True,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    provider = "synthetic-provider"
    sid = dm.build_session_id_hash(provider, label)
    store.put(
        dm.build_transcript_session_document(
            session=TranscriptSession(
                session_id_hash=sid,
                provider=provider,
                project=project,
                started_at="2026-06-21T00:00:00Z",
            )
        )
    )
    chunk_docs: list[dict[str, Any]] = []
    chunk_hashes: list[str] = []
    for index, body in enumerate(conversation_bodies):
        chunk = TranscriptChunk.from_text(
            chunk_id=f"chunk_{index:03d}",
            session_id_hash=sid,
            provider=provider,
            project=project,
            turn_start_index=index,
            turn_end_index=index,
            text=body,
        )
        doc = dm.build_conversation_chunk_document(chunk=chunk)
        store.put(doc)
        chunk_docs.append(doc)
        chunk_hashes.append(str(doc["content_hash"]))

    evidence_docs: list[dict[str, Any]] = []
    evidence_cov_hashes: list[str] = []
    for index, body in enumerate(evidence_bodies):
        ev = dm.build_tool_evidence_bundle_document(
            session_id_hash=sid,
            provider=provider,
            project=project,
            part_index=index,
            part_count=len(evidence_bodies),
            evidence_index_start=index,
            evidence_index_end=index,
            record_content_hashes=[],
            body=body,
        )
        store.put(ev)
        evidence_docs.append(ev)
        evidence_cov_hashes.append(str(ev["coverage_hash"]))

    store.put(
        dm.build_coverage_manifest_document(
            session_id_hash=sid,
            provider=provider,
            project=project,
            conversation_chunk_count=len(conversation_bodies),
            tool_evidence_bundle_count=len(evidence_bodies),
            conversation_content_hashes=chunk_hashes,
            tool_evidence_coverage_hashes=evidence_cov_hashes,
            project_authority={
                "project": project,
                "ambiguous": False,
                "eligible_for_retirement": True,
            },
        )
    )
    if activate:
        activate_source_revision(store=store, session_id_hash=sid)
    return sid, chunk_docs, evidence_docs


class _LoopbackModelServer:
    """In-process OpenAI-compatible loopback stub recording exact received HTTP bytes."""

    def __init__(self) -> None:
        self.received_requests: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def __enter__(self) -> "_LoopbackModelServer":
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                outer = json.loads(raw.decode("utf-8"))
                inner = json.loads(outer["messages"][1]["content"])
                sessions = list(inner["sessions"])
                parent.received_requests.append({
                    "path": self.path,
                    "model": outer.get("model"),
                    "request_digest": digest,
                    "request_bytes": len(raw),
                    "headers": {
                        "user_agent": self.headers.get("User-Agent"),
                        "opencode_session": self.headers.get("x-opencode-session"),
                    },
                    "sessions": [
                        {
                            "session_key": item["session_key"],
                            "text": item["text"],
                            "text_digest": _digest(item["text"]),
                        }
                        for item in sessions
                    ],
                })
                response_payload = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "sessions": [
                                            {
                                                "session_key": item["session_key"],
                                                "entities": [
                                                    {
                                                        "name": f"EntityA-{item['session_key'][-8:]}",
                                                        "type": "Concept",
                                                        "summary": "loopback primary entity",
                                                    },
                                                    {
                                                        "name": f"EntityB-{item['session_key'][-8:]}",
                                                        "type": "System",
                                                        "summary": "loopback secondary entity",
                                                    },
                                                ],
                                                "relations": [
                                                    {
                                                        "source": f"EntityA-{item['session_key'][-8:]}",
                                                        "target": f"EntityB-{item['session_key'][-8:]}",
                                                        "type": "supports",
                                                        "fact": "loopback extracted relation",
                                                    }
                                                ],
                                            }
                                            for item in sessions
                                        ]
                                    }
                                )
                            }
                        }
                    ]
                }
                encoded = json.dumps(response_payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args: Any) -> None:
                return

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _build_loopback_extractor(
    model_server: _LoopbackModelServer,
    *,
    max_streamed_request_bytes: int,
) -> OpenAICompatibleBulkSemanticExtractor:
    def _loopback_post(url: str, *, headers: dict, body: Any, timeout: int) -> str:
        assert url == "https://opencode.ai/zen/go/v1/chat/completions"
        assert headers.get("x-opencode-session") == "neurons-bulk-semantic"
        return _urllib_post(
            f"http://127.0.0.1:{model_server.port}/v1/chat/completions",
            headers=headers,
            body=body,
            timeout=timeout,
            disallow_redirects=True,
        )

    return OpenAICompatibleBulkSemanticExtractor(
        base_url="https://opencode.ai/zen/go/v1",
        model="hy4-flash-free",
        api_key="synthetic-opencode-key",
        post_fn=_loopback_post,
        max_streamed_request_bytes=max_streamed_request_bytes,
    )


class _LegacyGraphAdapter:
    def __init__(self, project: str, memory_id: str, content_hash: str) -> None:
        self._episode = OntologyEpisode.from_payload(
            event_id="legacy-" + memory_id,
            natural_id="legacy-nat-" + memory_id,
            entity_type="GraphFact",
            payload={
                "project": project,
                "authority_sources": [
                    {"authority_memory_id": memory_id, "content_hash": content_hash}
                ],
                "source_entity_types": ["Concept"],
            },
        )

    def search_context(self, *, brain_id: str, query: str, entity_types: Any = None, limit: int = 10) -> GraphMemoryResult:
        if brain_id == f"/project/{self._episode.payload['project']}":
            return GraphMemoryResult(status="available", episodes=(self._episode,), details=("legacy_read",))
        return GraphMemoryResult(status="available", episodes=(), details=("legacy_read",))


def _resolve_via_mcp(store: Any, adapter: Any, *, project: str) -> dict[str, Any]:
    service = KnowledgeSearchService(
        ledger=object(),
        retired_index_bridge=DisabledRetiredIndexBridgeClient(),
        dataset_ids=[],
        pgvector_store=store,
        graph_adapter=adapter,
    )
    response = handle_jsonrpc_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "brain.resolve",
                "arguments": {
                    "project": project,
                    "query": "synthetic",
                    "mode": "query",
                    "response_mode": "minimal",
                    "limit": 10,
                },
            },
        },
        service,
        surface="agent",
    )
    assert "result" in response, response
    return response["result"]["structuredContent"]


@_REQUIRE_LIVE_OWNED_INFRA
def test_full_product_chain_cli_couchdb_real_extractor_loopback_pg_neo4j_and_mcp_search(
    request, monkeypatch, tmp_path, capsys
):
    cfg = _verify_owned_loopback_config(os.environ)
    isolated_pg_store_factory = request.getfixturevalue("isolated_pg_store_factory")
    db = "e2efull_" + uuid.uuid4().hex[:10]
    project_a = "e2e-a-" + uuid.uuid4().hex[:8]
    project_b = "e2e-b-" + uuid.uuid4().hex[:8]
    policy = "full-input.e2e." + uuid.uuid4().hex[:6]
    group_a = cli.shadow_group_ids(f"/project/{project_a}", policy)[0]
    group_b = cli.shadow_group_ids(f"/project/{project_b}", policy)[0]
    legacy_uid = "legacy-marker-" + uuid.uuid4().hex

    with isolated_pg_store_factory() as isolated_pg_store, \
            _owned_couch_db(cfg, db) as store, \
            _owned_neo4j_scope(cfg, groups=(group_a, group_b)) as neo_scope:
        sid_a, docs_a, _ev_a = _seed_couch_session(
            store,
            project=project_a,
            label="session-a-" + uuid.uuid4().hex,
            conversation_bodies=[
                "FRONT-SENTINEL-" + ("A" * 200),
                "MIDDLE-SENTINEL-한글-😀-" + ("B" * 200),
                ("C" * 200) + "-END-SENTINEL",
            ],
            evidence_bodies=["EVIDENCE-SENTINEL-BODY"],
        )
        _sid_b, docs_b, _ev_b = _seed_couch_session(
            store,
            project=project_b,
            label="session-b-" + uuid.uuid4().hex,
            conversation_bodies=["PROJECT-B-ISOLATION-BODY"],
            evidence_bodies=[],
        )

        legacy_hash = _digest("legacy-card-" + project_a)
        legacy_card_a = _create_card(isolated_pg_store, project=project_a, label="legacy-a", content_hash=legacy_hash, approve=True)
        shadow_approved_a = _create_card(
            isolated_pg_store, project=project_a, label="shadow-ok-a",
            content_hash=str(docs_a[0]["content_hash"]), approve=True,
        )
        shadow_unapproved_a = _create_card(
            isolated_pg_store, project=project_a, label="shadow-cand-a",
            content_hash=str(docs_a[1]["content_hash"]), approve=False,
        )
        shadow_approved_b = _create_card(
            isolated_pg_store, project=project_b, label="shadow-ok-b",
            content_hash=str(docs_b[0]["content_hash"]), approve=True,
        )

        neo_scope.create_legacy_marker(legacy_uid)

        with _LoopbackModelServer() as model_server:
            extractor = _build_loopback_extractor(model_server, max_streamed_request_bytes=2 * 1024 * 1024)
            monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
            monkeypatch.setattr(
                OpenAICompatibleBulkSemanticExtractor, "from_env",
                classmethod(lambda cls, **kwargs: extractor),
            )
            monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
            monkeypatch.setenv("TRIAL_PG_DSN", isolated_pg_store.dsn)
            monkeypatch.setenv("TRIAL_NEO4J_URI", cfg.neo4j_uri)
            monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", cfg.neo4j_password)
            monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "hy4-flash-free")

            rc_a = cli.main([
                "--ledger", str(tmp_path / "ledger.db"),
                "--runtime-dir", str(tmp_path / "runtime"),
                "--project", project_a,
                "--limit", "1",
                "--max-sessions-per-call", "2",
                "--stream-mode",
                "--trial-neo4j-writer",
                "--full-input-policy-version", policy,
            ])
            out_a = capsys.readouterr()
            report_a = json.loads(out_a.out or out_a.err)
            assert rc_a == 0, json.dumps(report_a, sort_keys=True)
            assert report_a["status"] == "ok"
            assert report_a["stream_coverage_complete"] is True
            assert report_a["hash_order_match"] is True
            assert report_a["client_serialized_ordered_hashes"] == report_a["expected_ordered_hashes"]
            assert report_a["writer_receipt_ordered_hashes"] == report_a["expected_ordered_hashes"]
            assert report_a["server_received_hashes"] is None
            assert report_a["server_received_hash_status"] == "unknown_no_server_evidence"
            assert extractor._fallback_models == ()

            # Independent loopback server evidence matches client serialization digests and full content.
            loopback_digests = [req["request_digest"] for req in model_server.received_requests]
            assert loopback_digests == report_a["client_serialized_request_digests"]
            loopback_texts = [
                sess["text"]
                for req in model_server.received_requests
                for sess in req["sessions"]
            ]
            joined = "\n".join(loopback_texts)
            assert "FRONT-SENTINEL-" in joined
            assert "MIDDLE-SENTINEL-한글-😀-" in joined
            assert "-END-SENTINEL" in joined
            assert "EVIDENCE-SENTINEL-BODY" in joined
            assert [
                req["sessions"][i]["text_digest"]
                for req in model_server.received_requests
                for i in range(len(req["sessions"]))
            ] == report_a["expected_ordered_hashes"]

            # Also ingest project B to test project isolation on read.
            rc_b = cli.main([
                "--ledger", str(tmp_path / "ledger.db"),
                "--runtime-dir", str(tmp_path / "runtime"),
                "--project", project_b,
                "--limit", "1",
                "--stream-mode",
                "--trial-neo4j-writer",
                "--full-input-policy-version", policy,
            ])
            out_b = capsys.readouterr()
            report_b = json.loads(out_b.out or out_b.err)
            assert rc_b == 0, report_b

        with GraphDatabase.driver(cfg.neo4j_uri, auth=("neo4j", cfg.neo4j_password)) as driver:
            with driver.session() as session:
                legacy_val = session.run(
                    "MATCH (m:TrialLegacyMarker {uid: $uid, test_owner: $owner}) RETURN m.marker AS m",
                    uid=legacy_uid, owner=cfg.test_owner,
                ).single()["m"]
                assert legacy_val == "untouched"
                rel_count = session.run(
                    "MATCH (:TrialShadowEntity {group_id: $g})-[r:TRIAL_SHADOW_REL]->() RETURN count(r) AS c",
                    g=group_a,
                ).single()["c"]
                assert rel_count == 4

        legacy_adapter = _LegacyGraphAdapter(project_a, legacy_card_a, legacy_hash)

        # 1) Default OFF: legacy adapter returned unchanged, shadow invisible.
        monkeypatch.delenv("NEURON_BULK_SHADOW_READ_ELIGIBLE", raising=False)
        default_adapter = build_shadow_search_adapter_from_env(legacy_adapter, isolated_pg_store, os.environ)
        assert default_adapter is legacy_adapter
        default_res = _resolve_via_mcp(isolated_pg_store, default_adapter, project=project_a)
        default_ids = {item["id"] for item in default_res.get("items", [])}
        assert legacy_card_a in default_ids
        assert shadow_approved_a not in default_ids
        assert "shadow_read" not in default_res["metadata"]

        # 2) Opt-in ON via real env factory -> real CouchDB + real PG + real Neo4j + MCP brain.resolve.
        monkeypatch.setenv("NEURON_BULK_SHADOW_READ_ELIGIBLE", "1")
        monkeypatch.setenv("LLM_BRAIN_BULK_FULL_INPUT_POLICY_VERSION", policy)
        monkeypatch.setenv("LLM_BRAIN_NEO4J_URI", cfg.neo4j_uri)
        monkeypatch.setenv("LLM_BRAIN_NEO4J_USER", "neo4j")
        monkeypatch.setenv("LLM_BRAIN_NEO4J_PASSWORD", cfg.neo4j_password)
        monkeypatch.setenv("COUCHDB_URL", cfg.couch_url)
        monkeypatch.setenv("COUCHDB_DB", db)
        monkeypatch.setenv("COUCHDB_USER", cfg.couch_user)
        monkeypatch.setenv("COUCHDB_PASSWORD", cfg.couch_password)

        optin_adapter = build_shadow_search_adapter_from_env(legacy_adapter, isolated_pg_store, os.environ)
        assert isinstance(optin_adapter, ShadowSearchAdapter)
        optin_res = _resolve_via_mcp(isolated_pg_store, optin_adapter, project=project_a)
        optin_ids = {item["id"] for item in optin_res.get("items", [])}
        assert legacy_card_a in optin_ids
        assert shadow_approved_a in optin_ids
        assert shadow_unapproved_a not in optin_ids
        assert shadow_approved_b not in optin_ids
        assert optin_res["metadata"]["shadow_read"] == "eligible"

        # 3) Authority revocation in PG hides shadow card fail-closed while preserving legacy.
        with psycopg.connect(isolated_pg_store.dsn, autocommit=True) as conn:
            conn.execute("UPDATE memory_cards SET currentness = 'stale' WHERE memory_id = %s", (shadow_approved_a,))
        revoked_res = _resolve_via_mcp(isolated_pg_store, optin_adapter, project=project_a)
        revoked_ids = {item["id"] for item in revoked_res.get("items", [])}
        assert legacy_card_a in revoked_ids
        assert shadow_approved_a not in revoked_ids

        # 4) Restore PG authority, then advance CouchDB active revision -> shadow rejected fail-closed.
        with psycopg.connect(isolated_pg_store.dsn, autocommit=True) as conn:
            conn.execute("UPDATE memory_cards SET currentness = 'current' WHERE memory_id = %s", (shadow_approved_a,))
        restored_res = _resolve_via_mcp(isolated_pg_store, optin_adapter, project=project_a)
        assert shadow_approved_a in {item["id"] for item in restored_res.get("items", [])}

        extra_chunk = TranscriptChunk.from_text(
            chunk_id="chunk_003", session_id_hash=sid_a, provider="synthetic-provider",
            project=project_a, turn_start_index=3, turn_end_index=3, text="UPDATED-TURN-3",
        )
        extra_doc = dm.build_conversation_chunk_document(chunk=extra_chunk)
        store.put(extra_doc)
        store.put(
            dm.build_coverage_manifest_document(
                session_id_hash=sid_a, provider="synthetic-provider", project=project_a,
                conversation_chunk_count=4, tool_evidence_bundle_count=1,
                conversation_content_hashes=[str(d["content_hash"]) for d in [*docs_a, extra_doc]],
                tool_evidence_coverage_hashes=[],
                project_authority={"project": project_a, "ambiguous": False, "eligible_for_retirement": True},
            )
        )
        activate_source_revision(store=store, session_id_hash=sid_a)
        drifted_res = _resolve_via_mcp(isolated_pg_store, optin_adapter, project=project_a)
        drifted_ids = {item["id"] for item in drifted_res.get("items", [])}
        assert legacy_card_a in drifted_ids
        assert shadow_approved_a not in drifted_ids
        assert drifted_res["metadata"]["shadow_read"] == "rejected"


@_REQUIRE_LIVE_OWNED_INFRA
def test_spooled_large_document_real_extractor_loopback_http_and_limit_defer_resume(
    request, monkeypatch, tmp_path, capsys
):
    cfg = _verify_owned_loopback_config(os.environ)
    isolated_pg_store_factory = request.getfixturevalue("isolated_pg_store_factory")
    db = "e2espool_" + uuid.uuid4().hex[:10]
    policy = "full-input.spool." + uuid.uuid4().hex[:6]
    project_ok = "spool-ok-" + uuid.uuid4().hex[:8]
    project_lim = "spool-lim-" + uuid.uuid4().hex[:8]
    group_ok = cli.shadow_group_ids(f"/project/{project_ok}", policy)[0]
    group_lim = cli.shadow_group_ids(f"/project/{project_lim}", policy)[0]

    # > 560 KiB unique indexed text guarantees CouchDBHttpSourceStore.open_document_spool -> SpooledText.
    middle_block = "".join(f"[seg:{idx:05d}:한글😀]" + ("X" * 32) for idx in range(12000))
    large_body = "SPOOL-FRONT-" + middle_block + "-SPOOL-END"
    assert len(large_body.encode("utf-8")) > 560 * 1024

    with isolated_pg_store_factory() as isolated_pg_store, \
            _owned_couch_db(cfg, db) as store, \
            _owned_neo4j_scope(cfg, groups=(group_ok, group_lim)):
        _seed_couch_session(
            store,
            project=project_ok,
            label="spool-ok-" + uuid.uuid4().hex,
            conversation_bodies=["SMALL-HEAD-OK", large_body],
            evidence_bodies=["SPOOL-EVIDENCE-OK"],
        )
        _seed_couch_session(
            store,
            project=project_lim,
            label="spool-lim-" + uuid.uuid4().hex,
            conversation_bodies=["SMALL-HEAD-LIM", large_body],
            evidence_bodies=["SPOOL-EVIDENCE-LIM"],
        )

        monkeypatch.setattr(cli, "_build_source_store", lambda **kwargs: store)
        monkeypatch.setenv("NEURON_TRIAL_MODE", "1")
        monkeypatch.setenv("TRIAL_PG_DSN", isolated_pg_store.dsn)
        monkeypatch.setenv("TRIAL_NEO4J_URI", cfg.neo4j_uri)
        monkeypatch.setenv("TRIAL_NEO4J_PASSWORD", cfg.neo4j_password)
        monkeypatch.setenv("LLM_BRAIN_BULK_FREE_MODEL_ALLOWLIST", "hy4-flash-free")

        # Sub-check A: >560 KiB SpooledText streams over real _urllib_post to loopback HTTP server.
        with _LoopbackModelServer() as model_server_a:
            extractor_a = _build_loopback_extractor(model_server_a, max_streamed_request_bytes=2 * 1024 * 1024)
            monkeypatch.setattr(
                OpenAICompatibleBulkSemanticExtractor, "from_env",
                classmethod(lambda cls, **kwargs: extractor_a),
            )
            rc_ok = cli.main([
                "--ledger", str(tmp_path / "ledger.db"),
                "--runtime-dir", str(tmp_path / "runtime"),
                "--project", project_ok,
                "--limit", "1",
                "--max-sessions-per-call", "1",
                "--currentness-page-bytes", "2097152",
                "--stream-mode",
                "--trial-neo4j-writer",
                "--full-input-policy-version", policy,
            ])
            out_ok = capsys.readouterr()
            report_ok = json.loads(out_ok.out or out_ok.err)
            assert rc_ok == 0, json.dumps(report_ok, sort_keys=True)
            assert report_ok["status"] == "ok"
            assert report_ok["stream_coverage_complete"] is True
            assert report_ok["hash_order_match"] is True
            assert [r["request_digest"] for r in model_server_a.received_requests] == report_ok["client_serialized_request_digests"]
            texts_a = [s["text"] for r in model_server_a.received_requests for s in r["sessions"]]
            assert texts_a == ["SMALL-HEAD-OK", large_body, "SPOOL-EVIDENCE-OK"]

        # Sub-check B: Tight 128 KiB request cap defers large spooled chunk with 0 HTTP calls for it,
        # then --max-chunk-bytes 24576 resumes small chunk and splits large chunk on UTF-8 boundaries.
        with _LoopbackModelServer() as model_server_b:
            extractor_b = _build_loopback_extractor(model_server_b, max_streamed_request_bytes=131072)
            monkeypatch.setattr(
                OpenAICompatibleBulkSemanticExtractor, "from_env",
                classmethod(lambda cls, **kwargs: extractor_b),
            )
            rc_defer = cli.main([
                "--ledger", str(tmp_path / "ledger.db"),
                "--runtime-dir", str(tmp_path / "runtime"),
                "--project", project_lim,
                "--limit", "1",
                "--max-sessions-per-call", "1",
                "--currentness-page-bytes", "2097152",
                "--stream-mode",
                "--trial-neo4j-writer",
                "--full-input-policy-version", policy,
            ])
            out_defer = capsys.readouterr()
            report_defer = json.loads(out_defer.out or out_defer.err)
            assert rc_defer == 1, json.dumps(report_defer, sort_keys=True)
            assert report_defer["status"] == "partial", json.dumps(report_defer, sort_keys=True)
            assert report_defer["deferred_sessions"] == 1, json.dumps(report_defer, sort_keys=True)
            assert report_defer["failed_sessions"] == 0, json.dumps(report_defer, sort_keys=True)
            assert report_defer["projected"] == 1, json.dumps(report_defer, sort_keys=True)
            assert report_defer["limit_causes"] == {"client_request_byte_limit_exceeded_split_required": 1}, json.dumps(report_defer, sort_keys=True)
            assert [
                s["text"] for r in model_server_b.received_requests for s in r["sessions"]
            ] == ["SMALL-HEAD-LIM"]

            model_server_b.received_requests.clear()
            rc_split = cli.main([
                "--ledger", str(tmp_path / "ledger.db"),
                "--runtime-dir", str(tmp_path / "runtime"),
                "--project", project_lim,
                "--limit", "1",
                "--max-sessions-per-call", "1",
                "--currentness-page-bytes", "2097152",
                "--max-chunk-bytes", "24576",
                "--stream-mode",
                "--trial-neo4j-writer",
                "--full-input-policy-version", policy,
            ])
            out_split = capsys.readouterr()
            report_split = json.loads(out_split.out or out_split.err)
            assert rc_split == 0, json.dumps(report_split, sort_keys=True)
            assert report_split["status"] == "ok"
            assert report_split["resumed_chunks"] == 1
            assert report_split["stream_coverage_complete"] is True
            assert report_split["hash_order_match"] is True
            assert all(r["request_bytes"] <= 131072 for r in model_server_b.received_requests)
            texts_b = [s["text"] for r in model_server_b.received_requests for s in r["sessions"]]
            assert "SMALL-HEAD-LIM" not in texts_b
            assert texts_b[-1] == "SPOOL-EVIDENCE-LIM"
            assert "".join(texts_b[:-1]) == large_body


# ---------------------------------------------------------------------------
# Offline fail-closed counterexample tests for test-safety guards & cleanup.
# ---------------------------------------------------------------------------


def _synthetic_owned_env() -> dict[str, str]:
    return {
        "LBRAIN_TEST_PG_DSN": "postgresql://synth:synth@127.0.0.1:58096/lbrain_test",
        "TRIAL_NEO4J_URI": "bolt://127.0.0.1:58097",
        "TRIAL_NEO4J_PASSWORD": "synthetic-neo4j-pass",
        "TRIAL_COUCH_URL": "http://127.0.0.1:58098",
        "TRIAL_COUCH_USER": "synthadmin",
        "TRIAL_COUCH_PASSWORD": "synthetic-couch-pass",
        "TRIAL_TEST_OWNER": "gemini-unit-owner",
        "TRIAL_TEST_PG_CONTAINER": "synth-pg-box",
        "TRIAL_TEST_COUCH_CONTAINER": "synth-couch-box",
        "TRIAL_TEST_NEO4J_CONTAINER": "synth-neo4j-box",
    }


def _synthetic_inspect_map(
    *,
    owner: str = "gemini-unit-owner",
    pg_port: str = "58096",
    couch_port: str = "58098",
    neo4j_port: str = "58097",
) -> dict[str, dict[str, Any]]:
    return {
        "synth-pg-box": {
            "Id": "cid-pg-111",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "neuron.disposable": "true",
                    "neuron.test_owner": owner,
                    "neuron.test_service": "postgres",
                }
            },
            "NetworkSettings": {
                "Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": pg_port}]}
            },
        },
        "synth-couch-box": {
            "Id": "cid-couch-222",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "neuron.disposable": "true",
                    "neuron.test_owner": owner,
                    "neuron.test_service": "couchdb",
                }
            },
            "NetworkSettings": {
                "Ports": {"5984/tcp": [{"HostIp": "127.0.0.1", "HostPort": couch_port}]}
            },
        },
        "synth-neo4j-box": {
            "Id": "cid-neo4j-333",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "neuron.disposable": "true",
                    "neuron.test_owner": owner,
                    "neuron.test_service": "neo4j",
                }
            },
            "NetworkSettings": {
                "Ports": {
                    "7474/tcp": None,
                    "7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": neo4j_port}],
                }
            },
        },
    }


def test_guard_rejects_remote_endpoints_before_container_inspect():
    inspect_calls: list[str] = []

    def recording_inspect(container_ref: str) -> dict[str, Any]:
        inspect_calls.append(container_ref)
        raise AssertionError("container inspect must not run on remote/invalid endpoints")

    bad_overrides = [
        {"TRIAL_COUCH_URL": "https://couch.prod.example.com:6984"},
        {"TRIAL_COUCH_URL": "http://10.20.30.40:5984"},
        {"TRIAL_COUCH_URL": "http://0.0.0.0:5984"},
        {"TRIAL_COUCH_URL": "http://127.0.0.2:5984"},
        {"TRIAL_COUCH_URL": "http://127.0.0.1"},
        {"TRIAL_COUCH_URL": "http://u:p@127.0.0.1:5984"},
        {"TRIAL_NEO4J_URI": "bolt+s://neo4j.prod.internal:7687"},
        {"TRIAL_NEO4J_URI": "neo4j://192.168.1.50:7687"},
        {"TRIAL_NEO4J_URI": "bolt://127.0.0.1"},
        {"LBRAIN_TEST_PG_DSN": "postgresql://u:p@10.0.0.5:5432/prod"},
        {"LBRAIN_TEST_PG_DSN": "postgresql://u:p@127.0.0.1:5432/prod?host=prod.internal"},
        {"LBRAIN_TEST_PG_DSN": "postgresql://u:p@127.0.0.1:5432/prod?hostaddr=10.0.0.5"},
        {"LBRAIN_TEST_PG_DSN": "host=/var/run/postgresql port=5432 dbname=prod user=postgres"},
        {"LBRAIN_TEST_PG_DSN": "postgresql://u:p@127.0.0.1,evil.com:5432/prod"},
    ]
    for override in bad_overrides:
        env = {**_synthetic_owned_env(), **override}
        with pytest.raises(OwnedTestInfraError):
            _verify_owned_loopback_config(env, inspect_fn=recording_inspect)
    assert inspect_calls == []


def test_guard_rejects_unowned_loopback_wrong_port_public_bind_and_stopped_containers():
    base_env = _synthetic_owned_env()
    base_map = _synthetic_inspect_map()

    verified = _verify_owned_loopback_config(base_env, inspect_fn=lambda ref: base_map[ref])
    assert verified.test_owner == "gemini-unit-owner"
    assert verified.couch_url == "http://127.0.0.1:58098"

    # Missing required ownership/env keys fail closed.
    for missing_key in ("TRIAL_TEST_OWNER", "TRIAL_TEST_COUCH_CONTAINER", "TRIAL_TEST_PG_CONTAINER"):
        env_missing = dict(base_env)
        env_missing.pop(missing_key)
        with pytest.raises(OwnedTestInfraError):
            _verify_owned_loopback_config(env_missing, inspect_fn=lambda ref: base_map[ref])

    # Unowned owner label on CouchDB container.
    wrong_owner = _synthetic_inspect_map()
    wrong_owner["synth-couch-box"]["Config"]["Labels"]["neuron.test_owner"] = "someone-else"
    with pytest.raises(OwnedTestInfraError):
        _verify_owned_loopback_config(base_env, inspect_fn=lambda ref: wrong_owner[ref])

    # Missing disposable marker on PG container.
    not_disposable = _synthetic_inspect_map()
    not_disposable["synth-pg-box"]["Config"]["Labels"]["neuron.disposable"] = "false"
    with pytest.raises(OwnedTestInfraError):
        _verify_owned_loopback_config(base_env, inspect_fn=lambda ref: not_disposable[ref])

    # Wrong service label on Neo4j container.
    wrong_service = _synthetic_inspect_map()
    wrong_service["synth-neo4j-box"]["Config"]["Labels"]["neuron.test_service"] = "couchdb"
    with pytest.raises(OwnedTestInfraError):
        _verify_owned_loopback_config(base_env, inspect_fn=lambda ref: wrong_service[ref])

    # Stopped container.
    stopped = _synthetic_inspect_map()
    stopped["synth-pg-box"]["State"]["Running"] = False
    with pytest.raises(OwnedTestInfraError):
        _verify_owned_loopback_config(base_env, inspect_fn=lambda ref: stopped[ref])

    # Wrong published port compared to TRIAL_COUCH_URL.
    wrong_port = _synthetic_inspect_map(couch_port="5984")
    with pytest.raises(OwnedTestInfraError):
        _verify_owned_loopback_config(base_env, inspect_fn=lambda ref: wrong_port[ref])

    # Public bind on primary service port (0.0.0.0 / empty HostIp) or secondary Neo4j HTTP port.
    for bad_bind_ip in ("0.0.0.0", "", "::"):
        public_primary = _synthetic_inspect_map()
        public_primary["synth-couch-box"]["NetworkSettings"]["Ports"]["5984/tcp"] = [
            {"HostIp": bad_bind_ip, "HostPort": "58098"}
        ]
        with pytest.raises(OwnedTestInfraError):
            _verify_owned_loopback_config(base_env, inspect_fn=lambda ref: public_primary[ref])

        public_secondary = _synthetic_inspect_map()
        public_secondary["synth-neo4j-box"]["NetworkSettings"]["Ports"]["7474/tcp"] = [
            {"HostIp": bad_bind_ip, "HostPort": "58099"}
        ]
        with pytest.raises(OwnedTestInfraError):
            _verify_owned_loopback_config(base_env, inspect_fn=lambda ref: public_secondary[ref])

    # Duplicate container ID resolved from distinct aliases.
    dup_id = _synthetic_inspect_map()
    dup_id["synth-neo4j-box"]["Id"] = dup_id["synth-pg-box"]["Id"]
    with pytest.raises(OwnedTestInfraError):
        _verify_owned_loopback_config(base_env, inspect_fn=lambda ref: dup_id[ref])


def test_owned_couch_db_rejects_412_collision_without_dropping_existing_db():
    cfg = _verify_owned_loopback_config(_synthetic_owned_env(), inspect_fn=lambda ref: _synthetic_inspect_map()[ref])
    calls: list[tuple[str, str]] = []

    class _FakeHttp:
        def put(self, url: str, **kwargs: Any) -> Any:
            calls.append(("PUT", url))
            return type("_Resp", (), {"status_code": 412, "text": "file_exists"})()

        def delete(self, url: str, **kwargs: Any) -> Any:
            calls.append(("DELETE", url))
            raise AssertionError("DELETE must NEVER run after 412 pre-existing DB collision")

    with pytest.raises(OwnedDbCollisionError):
        with _owned_couch_db(cfg, "e2efull_0123456789ab", http_client=_FakeHttp()):
            raise AssertionError("context body must not execute on 412")
    assert calls == [("PUT", "http://127.0.0.1:58098/e2efull_0123456789ab")]


def test_owned_couch_db_and_neo4j_cleanup_on_mid_setup_failure():
    cfg = _verify_owned_loopback_config(_synthetic_owned_env(), inspect_fn=lambda ref: _synthetic_inspect_map()[ref])
    http_calls: list[tuple[str, str]] = []

    class _FakeHttp:
        def put(self, url: str, **kwargs: Any) -> Any:
            http_calls.append(("PUT", url))
            return type("_Resp", (), {"status_code": 201, "text": '{"ok":true}'})()

        def delete(self, url: str, **kwargs: Any) -> Any:
            http_calls.append(("DELETE", url))
            return type("_Resp", (), {"status_code": 200, "text": '{"ok":true}'})()

    neo4j_queries: list[tuple[str, dict[str, Any]]] = []
    marker_count_state = {"value": 0}

    class _FakeSession:
        def __enter__(self) -> _FakeSession:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def run(self, query: str, **params: Any) -> Any:
            neo4j_queries.append((query, dict(params)))
            return type("_Res", (), {"single": lambda self: {"c": marker_count_state["value"]}})()

    class _FakeDriver:
        def __enter__(self) -> _FakeDriver:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def session(self) -> _FakeSession:
            return _FakeSession()

    def _fake_driver_factory(uri: str, auth: Any = None) -> _FakeDriver:
        assert uri == cfg.neo4j_uri
        return _FakeDriver()

    # Case A: Mid-setup crash BEFORE marker creation still drops CouchDB + cleans Neo4j shadow groups.
    with pytest.raises(RuntimeError, match="mid-setup-before-marker"):
        with _owned_couch_db(cfg, "e2efull_0123456789ab", http_client=_FakeHttp()), \
                _owned_neo4j_scope(cfg, groups=("g-a", "g-b"), driver_factory=_fake_driver_factory):
            raise RuntimeError("mid-setup-before-marker")
    assert http_calls == [
        ("PUT", "http://127.0.0.1:58098/e2efull_0123456789ab"),
        ("DELETE", "http://127.0.0.1:58098/e2efull_0123456789ab"),
    ]
    assert len(neo4j_queries) == 1
    assert "DETACH DELETE n" in neo4j_queries[0][0]
    assert neo4j_queries[0][1]["groups"] == ["g-a", "g-b"]

    # Case B: Mid-setup crash AFTER marker creation also deletes only the created owned marker.
    http_calls.clear()
    neo4j_queries.clear()
    with pytest.raises(RuntimeError, match="mid-setup-after-marker"):
        with _owned_couch_db(cfg, "e2espool_abcdef012345", http_client=_FakeHttp()), \
                _owned_neo4j_scope(cfg, groups=("g-ok", "g-lim"), driver_factory=_fake_driver_factory) as neo_scope:
            neo_scope.create_legacy_marker("marker-123")
            raise RuntimeError("mid-setup-after-marker")
    assert http_calls == [
        ("PUT", "http://127.0.0.1:58098/e2espool_abcdef012345"),
        ("DELETE", "http://127.0.0.1:58098/e2espool_abcdef012345"),
    ]
    assert any("CREATE (m:TrialLegacyMarker" in q and p["owner"] == cfg.test_owner for q, p in neo4j_queries)
    assert any("TrialShadowEntity" in q and "DETACH DELETE n" in q for q, _ in neo4j_queries)
    assert any(
        "TrialLegacyMarker" in q and "DELETE m" in q and p["uids"] == ["marker-123"] and p["owner"] == cfg.test_owner
        for q, p in neo4j_queries
    )

    # Case C: Existing TrialLegacyMarker collision fails closed without creating or deleting it.
    http_calls.clear()
    neo4j_queries.clear()
    marker_count_state["value"] = 1
    with pytest.raises(OwnedTestInfraError, match="collision"):
        with _owned_couch_db(cfg, "e2efull_fedcba987654", http_client=_FakeHttp()), \
                _owned_neo4j_scope(cfg, groups=("g-a",), driver_factory=_fake_driver_factory) as neo_scope:
            neo_scope.create_legacy_marker("preexisting-marker")
    assert http_calls == [
        ("PUT", "http://127.0.0.1:58098/e2efull_fedcba987654"),
        ("DELETE", "http://127.0.0.1:58098/e2efull_fedcba987654"),
    ]
    assert not any("CREATE (m:TrialLegacyMarker" in q for q, _ in neo4j_queries)
    assert not any("TrialLegacyMarker) WHERE m.uid IN" in q and "DELETE m" in q for q, _ in neo4j_queries)


def test_owned_couch_db_rejects_non_disposable_db_name_and_unverified_config():
    from types import SimpleNamespace

    cfg = _verify_owned_loopback_config(_synthetic_owned_env(), inspect_fn=lambda ref: _synthetic_inspect_map()[ref])

    class _NoCallHttp:
        def put(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("HTTP PUT must not run")

        def delete(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("HTTP DELETE must not run")

    for bad_db in ("_users", "memories", "e2efull", "prod_0123456789ab", "e2efull_../escape"):
        with pytest.raises(OwnedTestInfraError):
            with _owned_couch_db(cfg, bad_db, http_client=_NoCallHttp()):
                pass

    fake_cfg = SimpleNamespace(
        pg_dsn="postgresql://synth:synth@127.0.0.1:58096/lbrain_test",
        neo4j_uri="bolt://127.0.0.1:58097",
        neo4j_password="pw",
        couch_url="http://127.0.0.1:58098",
        couch_user="u",
        couch_password="p",
        test_owner="spoofed",
    )
    with pytest.raises(OwnedTestInfraError):
        with _owned_couch_db(fake_cfg, "e2efull_0123456789ab", http_client=_NoCallHttp()):  # type: ignore[arg-type]
            pass

    def _forbidden_driver(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Neo4j driver must not open without verified config")

    with pytest.raises(OwnedTestInfraError):
        with _owned_neo4j_scope(fake_cfg, groups=("g-a",), driver_factory=_forbidden_driver):  # type: ignore[arg-type]
            pass

    with pytest.raises(OwnedTestInfraError):
        _VerifiedOwnedLoopbackConfig(
            pg_dsn="postgresql://synth:synth@127.0.0.1:58096/lbrain_test",
            neo4j_uri="bolt://127.0.0.1:58097",
            neo4j_password="pw",
            couch_url="http://127.0.0.1:58098",
            couch_user="u",
            couch_password="p",
            test_owner="gemini-unit-owner",
            pg_container="a",
            couch_container="b",
            neo4j_container="c",
            _verified_token=object(),
        )

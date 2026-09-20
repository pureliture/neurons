"""승인 manifest에 묶인 bounded Qdrant→PG 벡터 재사용 CLI.

기본은 dry-run이다. --all-projects/--all-providers는 의도적인 전체 범위 선택이다.
manifest에는 본문/벡터/접속정보를 저장하지 않는다. 네트워크 연결은 env만 사용한다.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
import os
from urllib.parse import urlsplit, quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.error import HTTPError

from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
from ..postgres_store.pgvector_store import PgVectorStore
from ..transport_contract import ProxyResponse

from ..couchdb_source.document_model import sha256_hash
from ..couchdb_source.session_memory_materializer import materialize_session_memory
from .pg_qdrant_import import LegacyCollection, OperatorEmbeddingAttestation, import_qdrant_point


class DeadlineExpired(BaseException):
    """일반 adapter 예외 처리에 삼켜지지 않는 실행 기한."""


class DeadlineAlarm:
    def __init__(self, seconds):
        import signal
        import threading
        if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
            raise ValueError("CLI requires POSIX main thread")
        if signal.getitimer(signal.ITIMER_REAL)[0]:
            raise ValueError("existing alarm")
        self.previous = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self.expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)

    @staticmethod
    def expired(*args):
        raise DeadlineExpired()

    def close(self):
        import signal
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.previous)


class Budget:
    def __init__(self, seconds, request_seconds, max_requests=10000):
        self.deadline = time.monotonic() + seconds
        self.request_seconds = request_seconds
        self.max_requests = max_requests
        self.requests = 0

    def check(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline")
        return min(remaining, self.request_seconds)

    def request(self):
        timeout = self.check()
        if self.requests >= self.max_requests:
            raise TimeoutError("request budget")
        self.requests += 1
        return timeout


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect refused")


def configured_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or
        parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("origin")
    return value.rstrip("/")


class SafeHTTP:
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, origins, budget):
        self.origins = {configured_origin(origin) for origin in origins}
        self.budget = budget
        # Never inherit an ambient proxy or follow credential-bearing redirects.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, url, headers, body):
        parsed = urlsplit(url)
        origin = parsed.scheme + "://" + parsed.netloc
        if origin not in self.origins or parsed.username is not None or parsed.fragment:
            raise ValueError("origin")
        timeout = self.budget.request()
        req = Request(url, data=body or None, headers=headers, method=method)
        try:
            response = self.opener.open(req, timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read(self.MAX_BYTES + 1)
            self.budget.check()
            if len(data) > self.MAX_BYTES:
                raise ValueError("response limit")
            # No automatic CAS/conflict retries by the source adapter/core.
            if response.status == 409:
                raise ValueError("conflict")
            return ProxyResponse(status_code=response.status, body=data,
                                 headers={k.lower(): v for k, v in response.headers.items()})


class BoundedPgStore(PgVectorStore):
    def __init__(self, dsn, budget):
        super().__init__(dsn=dsn)
        self.budget = budget
        self.endpoint_fingerprint = None

    def _open_connection(self):
        import math
        import psycopg
        from psycopg.rows import dict_row
        timeout = self.budget.request()
        conn = psycopg.connect(self.dsn, row_factory=dict_row,
                              connect_timeout=max(1, math.ceil(timeout)))
        try:
            endpoint = resolved_pg_endpoint(conn)
            if self.endpoint_fingerprint is not None and endpoint != self.endpoint_fingerprint:
                raise ValueError("PostgreSQL endpoint drift")
            self.endpoint_fingerprint = endpoint
            timeout_ms = max(1, int(self.budget.check() * 1000))
            conn.execute("SELECT set_config('statement_timeout', %s, false), "
                         "set_config('lock_timeout', %s, false)", (str(timeout_ms), str(timeout_ms)))
            return conn
        except BaseException:
            conn.close()
            raise


class BoundedCouchStore(CouchDBHttpSourceStore):
    def find_by_session(self, *, session_id_hash, doc_type=""):
        selector = {"session_id_hash": session_id_hash}
        if doc_type:
            selector["doc_type"] = doc_type
        docs, ids, bookmarks = [], set(), set()
        bookmark = None
        while len(docs) < 10000:
            query = {"selector": selector, "limit": min(500, 10000 - len(docs))}
            if bookmark is not None:
                query["bookmark"] = bookmark
            status, payload = self._request("POST", f"/{self.db}/_find", json_body=query)
            if status != 200:
                raise ValueError("source unavailable")
            page = payload["docs"]
            if len(page) > query["limit"]:
                raise ValueError("source count")
            if not page:
                return sorted(docs, key=lambda d: d["_id"])
            for doc in page:
                if doc["_id"] in ids:
                    raise ValueError("source duplicate")
                ids.add(doc["_id"])
                docs.append(doc)
            following = payload.get("bookmark")
            if not following or following in bookmarks:
                raise ValueError("source pagination")
            bookmarks.add(following)
            bookmark = following
        raise ValueError("source count")


class LiveBoundary:
    """생성자는 연결하지 않는다. 모든 I/O는 명시 env와 Budget 경계를 통과한다."""
    def __init__(self, args, budget, *, environ=None):
        import base64
        env = os.environ if environ is None else environ
        self.qdrant = configured_origin(env.get("QDRANT_URL", ""))
        couch = configured_origin(env.get("COUCHDB_URL", ""))
        db = env.get("COUCHDB_DB", "transcript_source")
        if not db or any(c in db for c in "/?#%"):
            raise ValueError("database")
        dsn = (env.get("NEURON_LBRAIN_PGVECTOR_DSN") or env.get("LLM_BRAIN_PGVECTOR_DSN") or
               env.get("NEURON_LEDGER_PG_DSN"))
        if not dsn:
            raise ValueError("configuration")
        self.budget = budget
        self.http = SafeHTTP([self.qdrant, couch], budget)
        auth = ""
        if env.get("COUCHDB_USER"):
            auth = "Basic " + base64.b64encode((env["COUCHDB_USER"] + ":" +
                                              env.get("COUCHDB_PASSWORD", "")).encode()).decode()
        self.source = BoundedCouchStore(base_url=couch, db=db, auth_header=auth,
            transport=lambda *a: self.http.request(*a), request_timeout_seconds=budget.request_seconds,
            deadline_monotonic=budget.deadline)
        self.sql = BoundedPgStore(dsn, budget)
        self.collection_path = "/collections/" + quote(args.collection, safe="")
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key_file = env.get("QDRANT_READ_API_KEY_FILE")
        key = Path(key_file).read_text().strip() if key_file else env.get("QDRANT_API_KEY", "")
        if key:
            self.headers["api-key"] = key
        self.binding = digest([self.qdrant, couch, db])

    def _qdrant(self, suffix, body=None):
        response = self.http.request("GET" if body is None else "POST",
            self.qdrant + self.collection_path + suffix, self.headers,
            b"" if body is None else json.dumps(body).encode())
        if response.status_code != 200:
            raise ValueError("qdrant unavailable")
        return json.loads(response.body)["result"]

    def collection_metadata(self):
        return self._qdrant("")["config"]["params"]["vectors"]

    def scroll(self, *, offset, limit, scope):
        body = {"limit": limit, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        must = [{"key": key, "match": {"value": value}} for key, value in scope.items() if value is not None]
        if must:
            body["filter"] = {"must": must}
        result = self._qdrant("/points/scroll", body)
        return result["points"], result["next_page_offset"]

    def retrieve(self, ids):
        return self._qdrant("/points", {"ids": ids, "with_payload": True, "with_vector": True})

    def target_fingerprint(self):
        with self.sql._scope() as conn:
            identity = conn.execute("""SELECT current_database() AS database, current_schema() AS schema,
                current_user AS role, inet_server_addr()::text AS address, inet_server_port() AS port,
                (SELECT oid FROM pg_database WHERE datname = current_database()) AS database_oid,
                'session_memory_chunks'::regclass::oid AS table_oid,
                current_setting('search_path') AS search_path,
                (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS vector_version,
                (SELECT json_agg(json_build_array(attname, atttypid, atttypmod) ORDER BY attnum)
                 FROM pg_attribute WHERE attrelid = 'session_memory_chunks'::regclass
                 AND attnum > 0 AND NOT attisdropped) AS columns""").fetchone()
            endpoint = resolved_pg_endpoint(conn)
        self.budget.check()
        return digest([self.binding, endpoint, identity])

    def close(self):
        pass  # Short-lived SQL connections and HTTP responses own their lifetimes.


def resolved_pg_endpoint(conn):
    """Hash only an allowlist of effective libpq values, after service resolution.

    Unresolved or multi-host configurations fail closed. Secrets and the raw
    DSN are never serialized. SQL runtime identity separately binds search_path.
    """
    info = conn.info
    parameters = info.get_parameters()
    endpoint = {key: str(parameters.get(key, getattr(info, key, "")) or "")
                for key in ("host", "hostaddr", "port", "dbname", "user", "options")}
    if (not (endpoint["host"] or endpoint["hostaddr"]) or
        any(not endpoint[k] for k in ("port", "dbname", "user")) or
        any("," in endpoint[k] for k in ("host", "hostaddr", "port"))):
        raise ValueError("unresolved PostgreSQL endpoint")
    if endpoint["host"].startswith("/"):
        endpoint["host"] = str(Path(endpoint["host"]).resolve())
    return digest(endpoint)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class RedactedParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("arguments")


def parser():
    p = RedactedParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--collection", required=True)
    for name in ("project", "provider"):
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--" + name)
        group.add_argument("--all-" + name + "s", action="store_true")
    p.add_argument("--limit", type=int, required=True)
    p.add_argument("--page-size", type=int, required=True)
    p.add_argument("--timeout-seconds", type=float, required=True)
    p.add_argument("--request-timeout-seconds", type=float, required=True)
    p.add_argument("--attestation-file", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--approval-file", required=True)
    p.add_argument("--point-ids-file")
    return p


def inspect_point(boundary, point, collection, attestation):
    payload = point["payload"]
    entry = {"id": point["id"], "point_fingerprint": digest(point),
             "source_fingerprint": None, "target_fingerprint": digest(boundary.target_fingerprint())}
    if (payload.get("document_kind") != "session_memory" or
        payload.get("target_profile") != "session-memory" or
        payload.get("result_type", "session_memory") != "session_memory"):
        return dict(entry, status="unsupported")
    try:
        current = materialize_session_memory(session_id_hash=payload["session_id_hash"], store=boundary.source)
    except Exception:
        return dict(entry, status="source_unavailable")
    source = {key: getattr(current, key) for key in (
        "source_hash", "project", "provider", "session_id_hash", "content_hash", "target_profile")}
    source["body_hash"] = hashlib.sha256(current.body.encode()).hexdigest()
    entry["source_fingerprint"] = digest(source)
    from .pg_backfill import _derive_pg_chunk_id_impl
    from ..couchdb_source.document_model import projection_state_doc_id
    body = payload.get("text")
    if isinstance(body, str):
        chunk_id = _derive_pg_chunk_id_impl(project=current.project, provider=current.provider,
            session_id_hash=current.session_id_hash, source_hash=current.source_hash,
            content_hash=sha256_hash(body))
        row = boundary.sql.get_chunk(chunk_id)
        row_value = None if row is None else {k: getattr(row, k) for k in (
            "chunk_id", "session_id_hash", "project", "provider", "content_markdown", "content_hash",
            "embedding_state", "embedding_model", "embedding", "embedding_revision")}
        receipt = boundary.source.get(projection_state_doc_id(current.session_id_hash))
        entry["target_fingerprint"] = digest([entry["target_fingerprint"], row_value, receipt])
    if (not current.fully_materialized or payload.get("content_hash") != current.content_hash or
        ("source_hash" in payload and payload["source_hash"] != current.source_hash) or
        any(payload.get(key) != getattr(current, key) for key in ("project", "provider", "session_id_hash"))):
        return dict(entry, status="stale")
    try:
        result = import_qdrant_point(point=point, collection=collection, attestation=attestation,
            project=payload["project"], provider=payload["provider"], session_id_hash=payload["session_id_hash"],
            source_store=boundary.source, sql_store=boundary.sql, dry_run=True)
        status = result["status"]
    except ValueError as exc:
        status = {"Qdrant import rejected: body": "body_mismatch",
                  "Qdrant import rejected: source": "stale"}.get(str(exc), "invalid")
    return dict(entry, status=status)


def inspect_batch(boundary, points, collection, attestation):
    """Reconcile every canonical ID before permitting the first data write."""
    from .pg_backfill import _derive_pg_chunk_id_impl, normalized_pg_halfvec
    checked = [inspect_point(boundary, point, collection, attestation) for point in points]
    vectors, conflicts = {}, set()
    identities = []
    for point, entry in zip(points, checked):
        identity = None
        if entry["status"] == "validated":
            payload = point["payload"]
            current = materialize_session_memory(session_id_hash=payload["session_id_hash"], store=boundary.source)
            identity = _derive_pg_chunk_id_impl(project=current.project, provider=current.provider,
                session_id_hash=current.session_id_hash, source_hash=current.source_hash,
                content_hash=sha256_hash(payload["text"]))
            vector = normalized_pg_halfvec(boundary.sql, point["vector"])
            if identity in vectors and vectors[identity] != vector:
                conflicts.add(identity)
            vectors[identity] = vector
        identities.append(identity)
    for identity, entry in zip(identities, checked):
        if identity in conflicts:
            entry["status"] = "canonical_vector_conflict"
    return checked


def point_id(value):
    import uuid
    if type(value) is int and 0 <= value < 2 ** 64:
        return value
    if isinstance(value, str) and str(uuid.UUID(value)) == value:
        return value
    raise ValueError("point identity")


MAX_SELECTION_FILE_BYTES = 16 * 1024 * 1024


def load_selection_ids(path_str, limit):
    path = Path(path_str).resolve()
    if not path.is_file():
        raise ValueError("selection file not found")
    if path.stat().st_size > MAX_SELECTION_FILE_BYTES:
        raise ValueError("selection file size")
    raw = path.read_bytes()
    if len(raw) > MAX_SELECTION_FILE_BYTES or not raw.strip():
        raise ValueError("selection file size")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("selection file json")
    if not isinstance(data, list) or not data:
        raise ValueError("selection file not a nonempty list")
    if len(data) > limit:
        raise ValueError("selection file exceeds limit")
    validated_ids = []
    seen = set()
    for item in data:
        pid = point_id(item)
        token = digest(pid)
        if token in seen:
            raise ValueError("selection file duplicate id")
        seen.add(token)
        validated_ids.append(pid)
    content_digest = digest(validated_ids)
    file_digest = hashlib.sha256(raw).hexdigest()
    return validated_ids, content_digest, file_digest


def collect_explicit(boundary, args, budget, selected_ids):
    points = []
    for start in range(0, len(selected_ids), args.page_size):
        budget.check()
        wanted = selected_ids[start:start + args.page_size]
        batch = boundary.retrieve(wanted)
        budget.check()
        if not isinstance(batch, list) or len(batch) != len(wanted):
            raise ValueError("retrieve count mismatch")
        by_id = {}
        for p in batch:
            pid = point_id(p["id"])
            tok = digest(pid)
            if tok in by_id:
                raise ValueError("retrieve duplicate")
            by_id[tok] = p
        if set(by_id.keys()) != {digest(i) for i in wanted}:
            raise ValueError("retrieve id mismatch")
        ordered_batch = [by_id[digest(i)] for i in wanted]
        for p in ordered_batch:
            payload = p.get("payload") or {}
            if any(value is not None and payload.get(key) != value
                   for key, value in {"project": args.project, "provider": args.provider}.items()):
                raise ValueError("scope")
        points.extend(ordered_batch)
    return points


def collect(boundary, args, budget):
    points, ids, offsets = [], set(), set()
    offset = None
    while len(points) < args.limit:
        budget.check()
        size = min(args.page_size, args.limit - len(points))
        page, following = boundary.scroll(offset=offset, limit=size,
            scope={"project": args.project, "provider": args.provider})
        budget.check()
        if len(page) > size or (not page and following is not None):
            raise ValueError("pagination")
        for point in page:
            if any(value is not None and point["payload"].get(key) != value
                   for key, value in {"project": args.project, "provider": args.provider}.items()):
                raise ValueError("scope")
            identity = digest(point_id(point["id"]))
            if identity in ids:
                raise ValueError("duplicate")
            ids.add(identity)
            points.append(point)
        if following is None:
            return points, True
        token = digest(following)
        if token in offsets:
            raise ValueError("pagination")
        offsets.add(token)
        offset = following
    return points, False


def code_revision():
    # Dirty/untracked implementation is bound too; HEAD alone is not sufficient.
    root = Path(__file__).resolve().parents[1]
    files = sorted(p for p in root.rglob("*") if p.suffix in (".py", ".sql"))
    return digest([(str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in files])


def read_approved(args, argv, attestation_digest, revision):
    plan = json.loads(Path(args.manifest).read_text())
    approval = json.loads(Path(args.approval_file).read_text())
    expected_keys = {"schema_version", "operation", "approved", "operator", "argv", "plan_digest"}
    unsigned = {k: v for k, v in plan.items() if k != "plan_digest"}
    if (set(approval) != expected_keys or type(approval["schema_version"]) is not int or
        approval["schema_version"] != 1 or approval["operation"] != "pg-qdrant-import" or
        approval["approved"] is not True or not isinstance(approval["operator"], str) or
        not approval["operator"].strip() or approval["argv"] != argv or
        approval["plan_digest"] != plan["plan_digest"] or digest(unsigned) != plan["plan_digest"] or
        plan["argv"] + ["--apply"] != argv or plan["operation"] != "pg-qdrant-import" or
        plan["attestation_digest"] != attestation_digest or plan["code_revision"] != revision or
        len(plan["points"]) > args.limit or plan["limit"] != args.limit):
        raise ValueError("approval")
    plan_mode = plan.get("selection_mode")
    if args.point_ids_file:
        if plan_mode != "explicit":
            raise ValueError("selection mode mismatch")
        selected_ids, content_digest, file_digest = load_selection_ids(args.point_ids_file, args.limit)
        if (plan.get("point_ids_digest") != content_digest or
            plan.get("point_ids_file_digest") != file_digest):
            raise ValueError("selection file drift")
        plan_ids = [point_id(p["id"]) for p in plan["points"]]
        if [digest(i) for i in plan_ids] != [digest(i) for i in selected_ids]:
            raise ValueError("selection id/order drift")
    else:
        if plan_mode is not None:
            raise ValueError("selection mode mismatch")
    return plan


def retrieve_approved(boundary, args, plan, budget):
    points = []
    ids = [point_id(p["id"]) for p in plan["points"]]
    if len({digest(i) for i in ids}) != len(ids):
        raise ValueError("duplicate")
    for start in range(0, len(ids), args.page_size):
        budget.check()
        wanted = ids[start:start + args.page_size]
        batch = boundary.retrieve(wanted)
        budget.check()
        if len(batch) != len(wanted) or {digest(p["id"]) for p in batch} != {digest(i) for i in wanted}:
            raise ValueError("point drift")
        by_id = {digest(p["id"]): p for p in batch}
        points.extend(by_id[digest(i)] for i in wanted)
    return points


def _run_worker(argv=None, *, boundary_factory=None):
    import math
    argv = list(sys.argv[1:] if argv is None else argv)
    boundary = None
    mutation_started = False
    applied = 0
    alarm = None
    try:
        args = parser().parse_args(argv)
        options = [token.split("=", 1)[0] for token in argv if token.startswith("--")]
        if len(options) != len(set(options)):
            raise ValueError("duplicate arguments")
        if any(value is not None and not value.strip() for value in (args.project, args.provider, args.collection)):
            raise ValueError("scope")
        if not (0 < args.page_size <= args.limit and
                math.isfinite(args.timeout_seconds) and math.isfinite(args.request_timeout_seconds) and
                0 < args.request_timeout_seconds <= args.timeout_seconds):
            raise ValueError("bounds")
        paths = [Path(value).resolve() for value in (args.manifest, args.attestation_file, args.approval_file)]
        if args.point_ids_file:
            paths.append(Path(args.point_ids_file).resolve())
        if len(set(paths)) != len(paths) or (not args.apply and paths[0].exists()):
            raise ValueError("manifest path")
        alarm = DeadlineAlarm(args.timeout_seconds)
        budget = Budget(args.timeout_seconds, args.request_timeout_seconds)
        selection_info = None
        if args.point_ids_file:
            selection_info = load_selection_ids(args.point_ids_file, args.limit)
        attestation = OperatorEmbeddingAttestation(**json.loads(Path(args.attestation_file).read_text()))
        if (attestation.confirmed is not True or attestation.collection != args.collection or
            attestation.model != "gemini-embedding-2" or type(attestation.dimension) is not int or
            attestation.dimension != 3072 or not isinstance(attestation.evidence_ref, str) or
            not attestation.evidence_ref.strip()):
            raise ValueError("attestation")
        revision = code_revision()
        attestation_digest = digest(asdict(attestation))
        approved = read_approved(args, argv, attestation_digest, revision) if args.apply else None
        boundary = (boundary_factory or LiveBoundary)(args, budget)
        metadata = boundary.collection_metadata()
        collection = LegacyCollection(args.collection, metadata["size"], metadata["distance"])
        if type(collection.dimension) is not int or collection.dimension != 3072 or collection.distance != "Cosine":
            raise ValueError("collection")
        target = digest(boundary.target_fingerprint())
        if approved is not None:
            if approved["target_fingerprint"] != target or approved["collection"] != asdict(collection):
                raise ValueError("target drift")
            points = retrieve_approved(boundary, args, approved, budget)
            checked = inspect_batch(boundary, points, collection, attestation)
            if checked != approved["points"] or any(p["status"] != "validated" for p in checked):
                raise ValueError("drift")
            for point in points:
                budget.check()
                payload = point["payload"]
                mutation_started = True
                result = import_qdrant_point(point=point, collection=collection, attestation=attestation,
                    project=payload["project"], provider=payload["provider"], session_id_hash=payload["session_id_hash"],
                    source_store=boundary.source, sql_store=boundary.sql, dry_run=False)
                budget.check()
                if result["status"] != "projected":
                    raise ValueError("apply failed")
                applied += 1
            is_explicit = approved.get("selection_mode") == "explicit"
            report = {"status": "applied", "mutation_started": mutation_started,
                      "applied": applied, "complete": False if is_explicit else approved["complete"]}
            if is_explicit:
                report["selected_count"] = approved.get("selected_count", applied)
                report["selected_batch_complete"] = approved.get("selected_batch_complete", True)
            print(json.dumps(report))
            if is_explicit:
                return 0 if approved.get("selected_batch_complete", True) else 2
            return 0 if approved["complete"] else 2
        if selection_info is not None:
            selected_ids, selection_digest, selection_file_digest = selection_info
            points = collect_explicit(boundary, args, budget, selected_ids)
            inspected = inspect_batch(boundary, points, collection, attestation)
            selected_batch_complete = all(p["status"] == "validated" for p in inspected)
            plan = {"schema_version": 1, "operation": "pg-qdrant-import", "argv": argv,
                    "attestation_digest": attestation_digest, "code_revision": revision,
                    "target_fingerprint": target, "collection": asdict(collection), "limit": args.limit,
                    "points": inspected,
                    "scope": {"project": args.project, "provider": args.provider},
                    "complete": False,
                    "selection_mode": "explicit",
                    "point_ids_digest": selection_digest,
                    "point_ids_file_digest": selection_file_digest,
                    "selected_count": len(selected_ids),
                    "selected_batch_complete": selected_batch_complete}
            budget.check()
            plan["plan_digest"] = digest(plan)
            fd = os.open(args.manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(plan, sort_keys=True) + "\n")
            print(json.dumps({"status": "dry_run", "mutation_started": False,
                              "complete": False, "selected_count": len(selected_ids),
                              "selected_batch_complete": selected_batch_complete,
                              "plan_digest": plan["plan_digest"]}))
            return 0 if selected_batch_complete else 2
        points, complete = collect(boundary, args, budget)
        plan = {"schema_version": 1, "operation": "pg-qdrant-import", "argv": argv,
                "attestation_digest": attestation_digest, "code_revision": revision,
                "target_fingerprint": target, "collection": asdict(collection), "limit": args.limit,
                "points": inspect_batch(boundary, points, collection, attestation),
                "scope": {"project": args.project, "provider": args.provider},
                "complete": complete}
        complete = complete and all(p["status"] == "validated" for p in plan["points"])
        plan["complete"] = complete
        budget.check()
        plan["plan_digest"] = digest(plan)
        fd = os.open(args.manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(plan, sort_keys=True) + "\n")
        print(json.dumps({"status": "dry_run", "mutation_started": False,
                          "complete": complete, "plan_digest": plan["plan_digest"]}))
        return 0 if complete else 2
    except (Exception, DeadlineExpired, KeyboardInterrupt):
        print(json.dumps({"status": "mutation_unknown" if mutation_started else "rejected",
                          "mutation_started": mutation_started, "applied": applied}))
        return 1
    finally:
        if alarm is not None:
            alarm.close()
        if boundary is not None:
            boundary.close()


def _isolated_worker(send, argv, boundary_factory):
    # Do not publish success until ALL rollback/close/finally work has returned.
    import contextlib
    import io
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            rc = _run_worker(argv, boundary_factory=boundary_factory)
        send.send((rc, output.getvalue()))
    except BaseException:
        pass  # Parent reports uncertain worker state; never forward exception text.
    finally:
        send.close()


def main(argv=None, *, boundary_factory=None):
    """POSIX process supervisor; deadline covers worker cleanup, not just I/O.

    At the deadline send TERM, allow 50ms, then KILL and wait at most 50ms.
    Kernel scheduling/uninterruptible kernel tasks cannot have a real-time
    guarantee. Worker status lost during apply is always mutation_unknown.
    _run_worker is an explicit cooperative-only driver for in-process unit tests.
    """
    import math
    import multiprocessing
    import threading
    started = time.monotonic()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser().parse_args(argv)
        if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
            raise ValueError("bounds")
        if os.name != "posix" or threading.current_thread() is not threading.main_thread():
            raise ValueError("POSIX main thread required")
    except Exception:
        print(json.dumps({"status": "rejected", "mutation_started": False, "applied": 0}))
        return 1
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    worker = context.Process(target=_isolated_worker, args=(send, argv, boundary_factory), daemon=True)
    worker.start()
    send.close()
    exceeded = False
    try:
        worker.join(max(0, started + args.timeout_seconds - time.monotonic()))
        exceeded = worker.is_alive()
        if exceeded:
            worker.terminate()
            worker.join(0.05)
            if worker.is_alive():
                worker.kill()
                worker.join(0.05)
        if not exceeded and worker.exitcode == 0 and receive.poll():
            try:
                rc, output = receive.recv()
                print(output, end="")
                return rc
            except EOFError:
                pass
        print(json.dumps({"status": "mutation_unknown" if args.apply else "rejected",
                          "mutation_started": None if args.apply else False, "applied": None,
                          "deadline_exceeded": exceeded, "worker_reaped": not worker.is_alive()}))
        return 1
    finally:
        receive.close()
        if worker.is_alive():
            worker.kill()
            worker.join(0.05)
        if not worker.is_alive():
            worker.close()


if __name__ == "__main__":
    raise SystemExit(main())

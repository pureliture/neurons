from __future__ import annotations

import json
import os
import time
from argparse import ArgumentParser
from dataclasses import dataclass
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from ..events import EventValidationError
from ..spool import Spool
from .retired_index_bridge import RetiredIndexBridgeAdapter
from .rag_ready_document import DEFAULT_INGRESS_PAYLOAD_KIND, RagReadyDocument, assert_no_secret_like_metadata


INGRESS_QUEUE_SCHEMA_VERSION = "rag_ingress_enqueue.v1"
MAX_REQUEST_BYTES = 2_000_000


@dataclass(frozen=True)
class IngressJob:
    job_id: str
    path: Path
    status: str


class IngressJobQueue:
    SUBDIRS = ("pending", "processing", "acked", "quarantine")

    def __init__(self, root: Path | str):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError("ingress queue root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for name in self.SUBDIRS:
            path = self.root / name
            if path.is_symlink():
                raise ValueError(f"ingress queue subdirectory must not be a symlink: {name}")
            path.mkdir(mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)

    def enqueue(self, payload: dict) -> IngressJob:
        validate_ingress_payload(payload)
        job_id = job_id_for_payload(payload)
        filename = f"{job_id}.json"
        existing = self._find_existing(filename)
        if existing is not None:
            return IngressJob(job_id=job_id, path=existing, status=_status_for_path(existing))
        final_path = self.root / "pending" / filename
        temp_path = self.root / "pending" / f".{filename}.tmp"
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, final_path)
            os.chmod(final_path, 0o600)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return IngressJob(job_id=job_id, path=final_path, status="queued")

    def claim_next(self) -> Path:
        pending = sorted((self.root / "pending").glob("*.json"))
        if not pending:
            raise FileNotFoundError("no pending ingress jobs")
        source = pending[0]
        target = self.root / "processing" / source.name
        os.replace(source, target)
        return target

    def ack(self, processing_path: Path | str) -> Path:
        source = Path(processing_path)
        target = self.root / "acked" / source.name
        os.replace(source, target)
        return target

    def quarantine(self, processing_path: Path | str) -> Path:
        source = Path(processing_path)
        target = self.root / "quarantine" / source.name
        os.replace(source, target)
        return target

    def depth_counts(self) -> dict[str, int]:
        return {name: len(list((self.root / name).glob("*.json"))) for name in self.SUBDIRS}

    def collect_garbage(self, *, subdir: str = "acked", max_age_seconds: float) -> int:
        if subdir not in {"acked", "quarantine"}:
            raise ValueError("GC is allowed only for acked or quarantine jobs")
        now = time.time()
        deleted = 0
        for path in (self.root / subdir).glob("*.json"):
            age = now - path.stat().st_mtime
            if age >= max_age_seconds:
                path.unlink()
                deleted += 1
        return deleted

    def _find_existing(self, filename: str) -> Path | None:
        for subdir in self.SUBDIRS:
            candidate = self.root / subdir / filename
            if candidate.exists():
                return candidate
        return None


def validate_ingress_payload(payload: dict) -> dict:
    if payload.get("schemaVersion") != INGRESS_QUEUE_SCHEMA_VERSION:
        raise ValueError("unsupported ingress schemaVersion")
    if not isinstance(payload.get("source"), dict):
        raise ValueError("source is required")
    document_payload = ((payload.get("payload") or {}).get("document") or {})
    if (payload.get("payload") or {}).get("kind") != DEFAULT_INGRESS_PAYLOAD_KIND:
        raise ValueError("unsupported ingress payload kind")
    if not document_payload.get("body"):
        raise ValueError("document.body is required")
    if not document_payload.get("filename"):
        raise ValueError("document.filename is required")
    if not payload.get("contentHash", "").startswith("sha256:"):
        raise ValueError("contentHash must be sha256")
    if not payload.get("targetProfile"):
        raise ValueError("targetProfile is required")
    if not payload.get("kind"):
        raise ValueError("kind is required")
    if not payload.get("idempotencyKey"):
        raise ValueError("idempotencyKey is required")
    metadata = document_payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("document.metadata must be an object")
    assert_no_secret_like_metadata(metadata)
    return payload


def job_id_for_payload(payload: dict) -> str:
    idempotency_key = str(payload.get("idempotencyKey") or "")
    return "job_" + sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]


def document_from_ingress_payload(payload: dict) -> RagReadyDocument:
    validate_ingress_payload(payload)
    document = payload["payload"]["document"]
    source = payload.get("source") or {}
    # Host is optional private provenance, not identity: preserve a producer
    # supplied redacted/stable host alias for attribution while keeping
    # dedup/natural-key semantics on content_hash + idempotency_key.
    metadata = dict(document.get("metadata") or {})
    source_host = str(source.get("host") or "")
    if source_host:
        metadata.setdefault("source_host", source_host)
    # Preserve the capture-time project into metadata when a client sent it only
    # in ``source`` (dendrite --project lands in source.project). Without this the
    # CouchDB index/delivery path resolves project="" for those payloads, breaking
    # project-id mapping. metadata wins if already present.
    source_project = str(source.get("project") or "")
    if source_project:
        metadata.setdefault("project", source_project)
    return RagReadyDocument(
        target_profile=str(payload["targetProfile"]),
        document_kind=str(payload["kind"]),
        artifact_kind=str(payload["payload"]["kind"]),
        source_namespace=str(source.get("provider") or source.get("namespace") or "ingress"),
        source_alias=str(source.get("source_alias") or source.get("alias") or "redacted-ingress-source"),
        privacy_class=str((document.get("metadata") or {}).get("privacy_class") or "private"),
        content_hash=str(payload["contentHash"]),
        idempotency_key=str(payload["idempotencyKey"]),
        body=str(document["body"]),
        filename=str(document["filename"]),
        metadata=metadata,
        content_type=str(document.get("contentType") or "text/markdown"),
        redaction_version=str(payload["payload"].get("redactionVersion") or "redaction.v2"),
    )


# --- G2 scoped: 2-stage redaction (full public redaction moves server-side) ---
#
# The thin client conservatively redacts (redact_text_v2: secrets/credentials/
# private paths removed) and POSTs. The server worker then applies the full
# public-ingress redaction here before delivery, so the delivered body is
# byte-identical to the legacy single-stage client output (verified:
# redact_public_ingress_text(redact_text_v2(x)) == redact_public_ingress_text(x)).
# apply_server_redaction is applied unconditionally and is IDEMPOTENT
# (redact_public_ingress_text(redact_public_ingress_text(x)) == ...), so already-
# fully-redacted in-flight queue messages and rollback content pass through
# unchanged (dual-accept) — no schema/version tag is needed on the wire.

# Fail-closed safety net at the delivery boundary: the ingress-api POST guard is
# relaxed to secrets-only (it can no longer enforce the full public denylist on
# conservatively-redacted content), so the worker re-checks that no real leak
# (secret/credential/private path) survives the server redaction before delivery.
_LEAK_PATTERNS = None


def _leak_patterns():
    global _LEAK_PATTERNS
    if _LEAK_PATTERNS is None:
        from ..redaction import (
            BASIC_AUTH_RE, BEARER_RE, CREDENTIAL_URL_RE, LOCAL_HOME_PATH_RE,
            LOCAL_PRIVATE_PATH_RE, LOCAL_USER_PATH_RE, LOCAL_VOLUMES_PATH_RE,
            LOWER_SECRET_ASSIGNMENT_RE, PROVIDER_TRANSCRIPT_PATH_RE,
            SECRET_ASSIGNMENT_RE,
        )
        _LEAK_PATTERNS = (
            ("private-path", PROVIDER_TRANSCRIPT_PATH_RE),
            ("user-path", LOCAL_USER_PATH_RE),
            ("home-path", LOCAL_HOME_PATH_RE),
            ("private-path", LOCAL_PRIVATE_PATH_RE),
            ("volumes-path", LOCAL_VOLUMES_PATH_RE),
            ("bearer", BEARER_RE),
            ("basic-auth", BASIC_AUTH_RE),
            ("credential-url", CREDENTIAL_URL_RE),
            ("secret-assignment", SECRET_ASSIGNMENT_RE),
            ("secret-assignment", LOWER_SECRET_ASSIGNMENT_RE),
        )
    return _LEAK_PATTERNS


def public_ingress_leak_violations(text: str) -> list[str]:
    """Return leak categories still present in ``text`` (expected empty after the
    server full redaction). Used fail-closed: any hit -> quarantine, never deliver."""
    if not text:
        return []
    return [name for name, pat in _leak_patterns() if pat.search(text)]


def _redact_meta_value(value, redact):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): _redact_meta_value(v, redact) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_meta_value(v, redact) for v in value]
    return value


def apply_server_redaction(payload: dict) -> dict:
    """Apply the full public-ingress redaction to the payload's document
    (body/filename/metadata) before delivery. Applied unconditionally and
    idempotent: a conservatively-redacted body is completed to the legacy
    single-stage output, an already-fully-redacted in-flight body is unchanged.
    Returns a new payload dict; the original is not mutated. ``contentHash`` is
    preserved as the input (wire) identity for dedup."""
    pkg = payload.get("payload") or {}
    document = dict(pkg.get("document") or {})
    if not document:
        return payload
    from ..redaction import redact_public_ingress_text as _r
    document["body"] = _r(str(document.get("body") or ""))
    document["filename"] = _r(str(document.get("filename") or ""))
    metadata = document.get("metadata") or {}
    document["metadata"] = {str(k): _redact_meta_value(v, _r) for k, v in metadata.items()}
    new_pkg = dict(pkg)
    new_pkg["document"] = document
    new_payload = dict(payload)
    new_payload["payload"] = new_pkg
    return new_payload


def normalize_ingest_job_payload(message: dict) -> dict:
    """Adapt the live NATS wire format to the nested enqueue payload shape.

    The Java ingress-api publishes ``objectMapper.writeValueAsBytes(IngestJob)``,
    i.e. a FLAT job: ``payload.{kind,redactionVersion,filename,contentType,body,
    metadata}`` with no ``schemaVersion`` and no ``payload.document`` nesting.
    The agent-knowledge pipeline (validate_ingress_payload / document_from_
    ingress_payload) expects the nested ``rag_ingress_enqueue.v1`` shape
    (``payload.document.*`` + ``schemaVersion``). This converts flat -> nested;
    a message that is already nested (synthetic smoke / direct enqueue payload)
    is returned unchanged."""
    pkg = message.get("payload")
    if not isinstance(pkg, dict) or "document" in pkg:
        return message  # already nested (or not a job) -> passthrough
    return {
        "schemaVersion": INGRESS_QUEUE_SCHEMA_VERSION,
        "source": message.get("source") or {},
        "payload": {
            "kind": pkg.get("kind") or DEFAULT_INGRESS_PAYLOAD_KIND,
            "redactionVersion": pkg.get("redactionVersion") or "redaction.v2",
            "document": {
                "filename": pkg.get("filename"),
                "contentType": pkg.get("contentType") or "text/markdown",
                "body": pkg.get("body"),
                "metadata": pkg.get("metadata") or {},
            },
        },
        "contentHash": message.get("contentHash"),
        "targetProfile": message.get("targetProfile"),
        "kind": message.get("kind"),
        "idempotencyKey": message.get("idempotencyKey"),
    }


def drain_one_ingress_job(queue: IngressJobQueue, backend: RetiredIndexBridgeAdapter) -> dict:
    claimed = queue.claim_next()
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
        document = document_from_ingress_payload(payload)
        submit = backend.submit_document(document)
        acked = queue.ack(claimed)
        return {
            "status": "submitted",
            "job_id": claimed.stem,
            "queue_path": str(acked),
            "backend_status": submit.status,
        }
    except Exception:
        quarantined = queue.quarantine(claimed)
        return {
            "status": "quarantined",
            "job_id": claimed.stem,
            "queue_path": str(quarantined),
        }


@dataclass
class RagIngressRuntime:
    event_spool: Spool
    job_queue: IngressJobQueue
    inbox_shadow: Callable[[dict, IngressJob], None] | None = None

    def health(self) -> dict:
        return {
            "status": "ok",
            "event_spool": self.event_spool.depth_counts(),
            "ingress_queue": self.job_queue.depth_counts(),
        }

    def enqueue_event(self, event: dict) -> dict:
        path = self.event_spool.enqueue(event)
        return {"accepted": True, "status": _status_for_path(path), "eventId": event["event_id"]}

    def enqueue_document(self, payload: dict) -> dict:
        job = self.job_queue.enqueue(payload)
        if self.inbox_shadow is not None:
            try:
                self.inbox_shadow(payload, job)
            except Exception:
                pass
        return {"accepted": True, "status": job.status, "jobId": job.job_id}


def build_handler(runtime: RagIngressRuntime):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rag-ingress-queue/0.1"

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._write_json(200, runtime.health())
                return
            self._write_json(404, {"accepted": False, "status": "not_found"})

        def do_POST(self):  # noqa: N802
            try:
                payload = self._read_json()
                if self.path == "/v1/events":
                    self._write_json(202, runtime.enqueue_event(payload))
                    return
                if self.path == "/v1/ingest/enqueue":
                    self._write_json(202, runtime.enqueue_document(payload))
                    return
                self._write_json(404, {"accepted": False, "status": "not_found"})
            except (ValueError, EventValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._write_json(400, {"accepted": False, "status": "rejected", "error": exc.__class__.__name__})

        def log_message(self, format, *args):  # noqa: A002
            return

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                raise ValueError("request body is required")
            if length > MAX_REQUEST_BYTES:
                raise ValueError("request body is too large")
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request JSON must be an object")
            return payload

        def _write_json(self, status_code: int, payload: dict) -> None:
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(
    runtime: RagIngressRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    allow_non_loopback: bool = False,
):
    if not allow_non_loopback and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("rag-ingress-queue must bind loopback unless explicitly fronted by a reviewed proxy")
    httpd = ThreadingHTTPServer((host, int(port)), build_handler(runtime))
    httpd.serve_forever()


def build_runtime(*, event_spool: Path | str, job_queue: Path | str) -> RagIngressRuntime:
    return RagIngressRuntime(
        event_spool=Spool(event_spool),
        job_queue=IngressJobQueue(job_queue),
    )


def build_arg_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="rag-ingress-queue")
    parser.add_argument(
        "--event-spool",
        default=os.environ.get("RAG_INGRESS_EVENT_SPOOL", "/var/lib/agent-knowledge/events"),
        help="directory for redacted provider event spool",
    )
    parser.add_argument(
        "--job-queue",
        default=os.environ.get("RAG_INGRESS_JOB_QUEUE", "/var/lib/agent-knowledge/jobs"),
        help="directory for redacted RAG-ready document jobs",
    )
    parser.add_argument("--host", default=os.environ.get("RAG_INGRESS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RAG_INGRESS_PORT", "8080")))
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        default=os.environ.get("RAG_INGRESS_ALLOW_NON_LOOPBACK", "").lower() in {"1", "true", "yes"},
        help="allow 0.0.0.0/container bind only when fronted by a reviewed loopback/proxy publish rule",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    runtime = build_runtime(event_spool=args.event_spool, job_queue=args.job_queue)
    serve(runtime, host=args.host, port=args.port, allow_non_loopback=args.allow_non_loopback)
    return 0


def _status_for_path(path: Path) -> str:
    parent = path.parent.name
    return "queued" if parent == "pending" else parent


if __name__ == "__main__":
    raise SystemExit(main())

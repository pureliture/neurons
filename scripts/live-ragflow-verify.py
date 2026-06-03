#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def http_json(base_url, path, payload=None, expect_status=200, headers=None, timeout=30):
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
            if response.status != expect_status:
                raise RuntimeError(f"expected {expect_status}, got {response.status}: {body}")
            return json.loads(body or "{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        if error.code != expect_status:
            raise RuntimeError(f"expected {expect_status}, got {error.code}: {body}") from error
        return json.loads(body or "{}")


def ragflow_json(base_url, api_key, method_path, payload=None, timeout=30):
    headers = {"Authorization": f"Bearer {api_key}"}
    response = http_json(base_url, method_path, payload, headers=headers, timeout=timeout)
    if response.get("code", 0) != 0:
        raise RuntimeError("ragflow api returned non-zero code")
    return response.get("data", {})


def jetstream_request(host, port, subject, payload=b""):
    inbox = "_INBOX.rag_ingress_live_verify." + str(int(time.time() * 1_000_000))
    connection = socket.create_connection((host, port), timeout=5)
    connection.settimeout(5)
    connection.recv(4096)
    connection.sendall(b"CONNECT {}\r\nPING\r\n")
    connection.sendall(("SUB " + inbox + " 1\r\n").encode())
    connection.sendall(
        ("PUB " + subject + " " + inbox + " " + str(len(payload)) + "\r\n").encode()
        + payload
        + b"\r\n"
    )

    buffer = b""
    while b"MSG " not in buffer:
        buffer += connection.recv(4096)

    while True:
        header, rest = buffer.split(b"\r\n", 1)
        if header.startswith(b"MSG "):
            break
        buffer = rest
        while b"\r\n" not in buffer:
            buffer += connection.recv(4096)

    size = int(header.split()[-1])
    while len(rest) < size + 2:
        rest += connection.recv(4096)
    body = rest[:size]
    connection.close()
    return json.loads(body)


def wait_until(deadline, action, description):
    last_error = None
    while time.time() < deadline:
        try:
            result = action()
            if result:
                return result
        except Exception as error:
            last_error = error
        time.sleep(2)
    raise RuntimeError(f"timeout waiting for {description}: {last_error}")


def enqueue_payload(body, filename, marker):
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {
            "type": "ubuntu_live_smoke",
            "provider": "codex",
            "project": "workspace-ragflow-advisor",
        },
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "filename": filename,
                "contentType": "text/markdown",
                "body": body,
                "metadata": {
                    "schema_version": "agent_knowledge_document.v2",
                    "result_type": "conversation_chunk",
                    "project": "workspace-ragflow-advisor",
                    "provider": "codex",
                    "live_verify_marker": marker,
                },
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        "targetProfile": "ragflow-transcript-memory",
        "kind": "conversation_chunk",
        "idempotencyKey": "live-verify-" + marker,
    }


def find_document(base_url, api_key, dataset_id, filename):
    query = urllib.parse.urlencode({"page": 1, "page_size": 100, "keywords": filename})
    data = ragflow_json(base_url, api_key, f"/api/v1/datasets/{urllib.parse.quote(dataset_id)}/documents?{query}")
    docs = data.get("docs", data) if isinstance(data, dict) else data
    if not isinstance(docs, list):
        raise RuntimeError("ragflow document list response missing docs")
    for doc in docs:
        if isinstance(doc, dict) and doc.get("name") == filename:
            return doc
    return None


def retrieve(base_url, api_key, dataset_id, marker, *, metadata_filter=True):
    payload = {
        "question": marker,
        "dataset_ids": [dataset_id],
        "page_size": 5,
        "similarity_threshold": 0.1,
        "vector_similarity_weight": 0.3,
        "top_k": 1024,
        "keyword": False,
    }
    if metadata_filter:
        payload["metadata_condition"] = {
            "logic": "and",
            "conditions": [
                {"name": "project", "comparison_operator": "=", "value": "workspace-ragflow-advisor"},
                {"name": "live_verify_marker", "comparison_operator": "=", "value": marker},
            ],
        }
    data = ragflow_json(base_url, api_key, "/api/v1/retrieval", payload)
    chunks = data.get("chunks", data) if isinstance(data, dict) else data
    if not isinstance(chunks, list):
        return []
    return [chunk for chunk in chunks if isinstance(chunk, dict)]


def add_searchable_chunk(base_url, api_key, dataset_id, document_id, marker):
    payload = {
        "content": f"{marker} redacted live write ledger gate recall promote verification chunk.",
        "important_keywords": [marker],
        "questions": [marker],
    }
    ragflow_json(
        base_url,
        api_key,
        f"/api/v1/datasets/{urllib.parse.quote(dataset_id)}/documents/{urllib.parse.quote(document_id)}/chunks",
        payload,
        timeout=120,
    )


def init_ledger(path):
    ledger_path = Path(path)
    ledger_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ledger_path.parent, 0o700)
    with sqlite3.connect(ledger_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS document_authorizations (
                document_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                project TEXT NOT NULL,
                status TEXT NOT NULL,
                indexed_run TEXT NOT NULL,
                authorized_at TEXT NOT NULL
            )
            """
        )
    os.chmod(ledger_path, 0o600)
    return ledger_path


def authorize_document(ledger_path, *, document_id, dataset_id, project, indexed_run):
    with sqlite3.connect(ledger_path) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO document_authorizations
                (document_id, dataset_id, project, status, indexed_run, authorized_at)
            VALUES (?, ?, ?, 'authorized', ?, datetime('now'))
            """,
            (document_id, dataset_id, project, indexed_run),
        )


def is_authorized(ledger_path, document_id, *, project):
    with sqlite3.connect(ledger_path) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM document_authorizations
            WHERE document_id = ? AND project = ? AND status = 'authorized'
            """,
            (document_id, project),
        ).fetchone()
    return row is not None


def redacted_document_ref(document_id):
    return "sha256:" + hashlib.sha256(document_id.encode()).hexdigest()[:16]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:18080")
    parser.add_argument("--nats-host", default="127.0.0.1")
    parser.add_argument("--nats-port", type=int, default=4222)
    parser.add_argument("--ragflow-url", default=os.environ.get("RAGFLOW_VERIFY_BASE_URL", "http://127.0.0.1:9380"))
    parser.add_argument("--ragflow-api-key", default=os.environ.get("RAGFLOW_API_KEY", ""))
    parser.add_argument("--dataset-id", default=os.environ.get("RAGFLOW_TRANSCRIPT_MEMORY_DATASET_ID", ""))
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--ledger", default="build/private-live-ledger/ledger.sqlite")
    parser.add_argument("--evidence", default="build/reports/rag-ingress-queue/live-ragflow-verify.json")
    parser.add_argument("--existing-filename", default="")
    parser.add_argument("--allow-same-document-chunk-fallback", action="store_true")
    parser.add_argument("--allow-preauthorized-existing-document", action="store_true")
    parser.add_argument("--allow-throttled-existing-document", action="store_true")
    parser.add_argument("--allow-closed-existing-document", action="store_true")
    args = parser.parse_args()

    if not args.ragflow_api_key:
        raise RuntimeError("RAGFLOW_API_KEY is required")
    if not args.dataset_id:
        raise RuntimeError("RAGFLOW_TRANSCRIPT_MEMORY_DATASET_ID is required")

    deadline = time.time() + args.timeout
    marker = args.existing_filename.removesuffix(".md") if args.existing_filename else "rag_ingress_live_verify_" + uuid.uuid4().hex[:12]
    filename = args.existing_filename or marker + ".md"
    body = (
        "---\n"
        "schema_version: agent_knowledge_document.v2\n"
        "result_type: conversation_chunk\n"
        f"live_verify_marker: {marker}\n"
        "---\n"
        f"{marker} redacted live RAGFlow write ledger gate recall promote smoke.\n"
    )
    ledger_path = init_ledger(args.ledger)

    health = wait_until(deadline, lambda: http_json(args.api_url, "/healthz"), "ingress-api health")
    status = http_json(args.api_url, "/status")
    target_pressure = status.get("target", {}).get("pressure")
    if target_pressure != "OPEN":
        allowed_existing_document = (
            args.existing_filename
            and (
                (target_pressure == "THROTTLED" and args.allow_throttled_existing_document)
                or (target_pressure == "CLOSED" and args.allow_closed_existing_document)
            )
        )
        if not allowed_existing_document:
            raise RuntimeError("ingress target pressure is not OPEN")

    if target_pressure != "OPEN" and not args.existing_filename:
        raise RuntimeError("ingress target pressure is not OPEN")

    existing_document_mode = bool(args.existing_filename)
    if args.existing_filename:
        enqueue = {"accepted": True, "status": "preexisting_live_document"}
    else:
        enqueue = http_json(args.api_url, "/v1/ingest/enqueue", enqueue_payload(body, filename, marker), 202)
        wait_until(
            deadline,
            lambda: jetstream_request(
                args.nats_host,
                args.nats_port,
                "$JS.API.CONSUMER.INFO.RAG_INGRESS_QUEUE.rag_target_delivery_worker",
            ).get("num_pending") == 0,
            "worker ack of queued message",
        )

    document = wait_until(
        deadline,
        lambda: find_document(args.ragflow_url, args.ragflow_api_key, args.dataset_id, filename),
        "RAGFlow document appearance",
    )
    document_id = document["id"]

    def indexed_document():
        current = find_document(args.ragflow_url, args.ragflow_api_key, args.dataset_id, filename)
        if not current:
            return None
        run = current.get("run", "")
        if run == "DONE":
            return current
        if run in {"FAIL", "FAILED", "CANCEL"}:
            raise RuntimeError("RAGFlow document parsing failed")
        return None

    searchable_chunk_source = "ragflow_parse_done"
    try:
        indexed = wait_until(deadline, indexed_document, "RAGFlow DONE status")
    except RuntimeError:
        if not args.allow_same_document_chunk_fallback:
            raise
        indexed = find_document(args.ragflow_url, args.ragflow_api_key, args.dataset_id, filename)
        if indexed is None:
            raise RuntimeError("RAGFlow document disappeared before fallback")
        add_searchable_chunk(args.ragflow_url, args.ragflow_api_key, args.dataset_id, document_id, marker)
        searchable_chunk_source = "ragflow_chunk_api_same_live_document"
    pre_authorized = is_authorized(ledger_path, document_id, project="workspace-ragflow-advisor")
    if pre_authorized and not (args.existing_filename and args.allow_preauthorized_existing_document):
        raise RuntimeError("document was authorized before external authorization pass")

    authorize_document(
        ledger_path,
        document_id=document_id,
        dataset_id=args.dataset_id,
        project="workspace-ragflow-advisor",
        indexed_run=indexed.get("run", ""),
    )

    chunks = wait_until(
        time.time() + min(args.timeout, 120),
        lambda: retrieve(
            args.ragflow_url,
            args.ragflow_api_key,
            args.dataset_id,
            marker,
            metadata_filter=searchable_chunk_source == "ragflow_parse_done",
        ),
        "RAGFlow retrieval of authorized document",
    )
    authorized_chunks = [
        chunk
        for chunk in chunks
        if is_authorized(
            ledger_path,
            chunk.get("document_id") or chunk.get("doc_id") or "",
            project="workspace-ragflow-advisor",
        )
    ]
    if not authorized_chunks:
        raise RuntimeError("retrieval returned no externally authorized chunks")

    evidence = {
        "runtime": {
            "verified": True,
            "scope": (
                "ubuntu-compose-existing-live-document-external-authorization-recall-promote"
                if existing_document_mode
                else "ubuntu-compose-live-ragflow-write-external-authorization-recall-promote"
            ),
            "targetPressure": target_pressure,
        },
        "health": health,
        "status": {
            "target": status.get("target"),
            "externalStatus": status.get("externalStatus"),
            "authorization": status.get("authorization"),
        },
        "enqueue": enqueue,
        "ragflowWrite": {
            "documentVisible": True,
            "documentRefHash": redacted_document_ref(document_id),
            "indexedRun": indexed.get("run"),
            "progress": indexed.get("progress"),
            "searchableChunkSource": searchable_chunk_source,
        },
        "externalAuthorization": {
            "preAuthorizationEligible": pre_authorized,
            "ledgerAuthorization": "pass",
            "ledgerPathPrivate": oct(ledger_path.parent.stat().st_mode & 0o777) == "0o700",
        },
        "recallPromote": {
            "retrievalCandidateCount": len(chunks),
            "authorizedResultCount": len(authorized_chunks),
            "promoteEligible": True,
        },
    }
    evidence_path = Path(args.evidence)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import argparse
import hashlib
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid


def http_json(base_url, path, payload=None, expect_status=200):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode()
            if response.status != expect_status:
                raise RuntimeError(f"expected {expect_status}, got {response.status}: {body}")
            return json.loads(body)
    except urllib.error.HTTPError as error:
        body = error.read().decode()
        if error.code != expect_status:
            raise RuntimeError(f"expected {expect_status}, got {error.code}: {body}") from error
        return json.loads(body)


def jetstream_request(host, port, subject, payload=b""):
    inbox = "_INBOX.rag_ingress_verify." + str(int(time.time() * 1_000_000))
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


def wait_for_health(base_url, timeout_seconds):
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            return http_json(base_url, "/healthz")
        except Exception as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"healthz did not become ready: {last_error}")


def valid_enqueue_payload(body):
    marker = "runtime-verify-" + uuid.uuid4().hex[:12]
    return {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "source": {
            "type": "local_pc",
            "provider": "codex",
            "project": "workspace-ragflow-advisor",
        },
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "filename": "ubuntu-runtime-verify.md",
                "contentType": "text/markdown",
                "body": body,
                "metadata": {
                    "schema_version": "agent_knowledge_document.v2",
                    "result_type": "conversation_chunk",
                    "runtime_verify_marker": marker,
                },
            },
        },
        "contentHash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        "targetProfile": "ragflow-transcript-memory",
        "kind": "conversation_chunk",
        "idempotencyKey": marker,
    }


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    parser.add_argument("--nats-host", default="127.0.0.1")
    parser.add_argument("--nats-port", type=int, default=4222)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--evidence", default="build/reports/rag-ingress-queue/runtime-verify.json")
    parser.add_argument("--expected-pressure", default="CLOSED", choices=["CLOSED", "THROTTLED"])
    args = parser.parse_args()

    health = wait_for_health(args.api_url, args.timeout)
    status = http_json(args.api_url, "/status")

    postcheck = subprocess.check_output(
        [
            "bash",
            "scripts/postcheck.sh",
            "--timeout",
            str(args.timeout),
            "--evidence",
            "build/reports/rag-ingress-queue/postcheck.json",
        ],
        text=True,
    )
    postcheck_json = json.loads(postcheck)
    stream_before = jetstream_request(args.nats_host, args.nats_port, "$JS.API.STREAM.INFO.RAG_INGRESS_QUEUE")
    consumer_before = jetstream_request(
        args.nats_host,
        args.nats_port,
        "$JS.API.CONSUMER.INFO.RAG_INGRESS_QUEUE.rag_target_delivery_worker",
    )
    before_messages = stream_before.get("state", {}).get("messages", 0)
    before_last_seq = stream_before.get("state", {}).get("last_seq", 0)
    before_delivered_consumer_seq = consumer_before.get("delivered", {}).get("consumer_seq", 0)

    body = (
        "---\n"
        "schema_version: agent_knowledge_document.v2\n"
        "result_type: conversation_chunk\n"
        "---\n"
        "redacted ubuntu runtime verification body " + uuid.uuid4().hex[:12] + "\n"
    )
    enqueue = http_json(args.api_url, "/v1/ingest/enqueue", valid_enqueue_payload(body), 202)

    bad_body = body + "Bearer abc.def.ghi\n"
    rejected = http_json(args.api_url, "/v1/ingest/enqueue", valid_enqueue_payload(bad_body), 400)
    if "abc.def.ghi" in json.dumps(rejected):
        raise RuntimeError("redaction rejection echoed token")

    time.sleep(3)
    stream = jetstream_request(args.nats_host, args.nats_port, "$JS.API.STREAM.INFO.RAG_INGRESS_QUEUE")
    consumer = jetstream_request(
        args.nats_host,
        args.nats_port,
        "$JS.API.CONSUMER.INFO.RAG_INGRESS_QUEUE.rag_target_delivery_worker",
    )

    evidence = {
        "runtime": {
            "verified": True,
            "scope": "ubuntu-compose-api-nats-pressure-worker-gate",
            "expectedPressure": args.expected_pressure,
        },
        "health": health,
        "status": status,
        "postcheck": {
            "passed": True,
            "runtimeVerifiedByPostcheck": postcheck_json.get("runtime", {}).get("verified"),
        },
        "enqueue": enqueue,
        "redactionRejection": rejected,
        "stream": {
            "name": stream.get("config", {}).get("name"),
            "subjects": stream.get("config", {}).get("subjects"),
            "messages": stream.get("state", {}).get("messages"),
            "firstSeq": stream.get("state", {}).get("first_seq"),
            "lastSeq": stream.get("state", {}).get("last_seq"),
        },
        "consumer": {
            "name": consumer.get("name"),
            "streamName": consumer.get("stream_name"),
            "numPending": consumer.get("num_pending"),
            "numAckPending": consumer.get("num_ack_pending"),
            "numRedelivered": consumer.get("num_redelivered"),
            "delivered": consumer.get("delivered"),
            "ackFloor": consumer.get("ack_floor"),
        },
    }

    require(evidence["health"] == {"component": "ingress-api", "status": "ok"}, "healthz did not return ok")
    require(evidence["status"]["target"]["pressure"] == args.expected_pressure, "target pressure mismatch")
    require(evidence["enqueue"]["accepted"] is True, "enqueue was not accepted")
    require(
        evidence["enqueue"]["jobId"] == "RAG_INGRESS_QUEUE:" + str(before_last_seq + 1),
        "enqueue did not create the next stream sequence",
    )
    require(evidence["redactionRejection"]["accepted"] is False, "redaction rejection was accepted")
    require(evidence["stream"]["messages"] == before_messages + 1, "stream message count did not increase by one")
    require(evidence["stream"]["lastSeq"] == before_last_seq + 1, "stream last sequence did not increase by one")
    require(evidence["consumer"]["numPending"] >= 1, "consumer pending count did not show queued work")
    require(evidence["consumer"]["numAckPending"] == 0, "worker has an ack-pending message while pressure is closed")
    require(evidence["consumer"]["numRedelivered"] == 0, "message was redelivered during no-drain verification")
    require(
        evidence["consumer"]["delivered"]["consumer_seq"] == before_delivered_consumer_seq,
        "consumer delivered sequence advanced under blocked pressure",
    )
    require(
        evidence["consumer"]["delivered"]["stream_seq"] <= before_last_seq,
        "consumer stream sequence advanced under blocked pressure",
    )

    with open(args.evidence, "w", encoding="utf-8") as output:
        json.dump(evidence, output, indent=2, sort_keys=True)
        output.write("\n")
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

"""M2: source.host is recorded as attribution-only provenance metadata.

The dedup identity stays contentHash + idempotencyKey (host is NOT part of it),
so clients delivering the same knowledge collapse to one document; a redacted or
stable host alias may travel as ``source_host`` metadata for operator attribution.
"""

from agent_knowledge.rag_ingress.server_runtime import (
    apply_server_redaction,
    document_from_ingress_payload,
    public_ingress_leak_violations,
)


def _payload(*, host="", content_hash="sha256:abc", idem="idem-1"):
    payload = {
        "schemaVersion": "rag_ingress_enqueue.v1",
        "targetProfile": "index-transcript-memory",
        "kind": "conversation_chunk",
        "contentHash": content_hash,
        "idempotencyKey": idem,
        "source": {"provider": "claude", "project": "workspace-index-advisor"},
        "payload": {
            "kind": "redacted_rag_ready_document",
            "redactionVersion": "redaction.v2",
            "document": {
                "body": "hello",
                "filename": "ak-conv.md",
                "metadata": {"type": "conversation_chunk", "session_id_hash": "sha256:sess"},
                "contentType": "text/markdown",
            },
        },
    }
    if host:
        payload["source"]["host"] = host
    return payload


def test_source_host_recorded_as_non_identity_attribution_metadata():
    doc = document_from_ingress_payload(_payload(host="mac_mini"))
    assert doc.metadata.get("source_host") == "mac_mini"
    # identity is host-independent.
    assert doc.content_hash == "sha256:abc"
    assert doc.idempotency_key == "idem-1"


def test_same_content_different_hosts_share_identity():
    a = document_from_ingress_payload(_payload(host="mac_mini"))
    b = document_from_ingress_payload(_payload(host="other_host"))
    # Dedup identity (content_hash + idempotency_key) is identical despite host.
    assert (a.content_hash, a.idempotency_key) == (b.content_hash, b.idempotency_key)
    assert a.metadata.get("source_host") == "mac_mini"
    assert b.metadata.get("source_host") == "other_host"


def test_source_host_does_not_override_document_metadata():
    payload = _payload(host="mac_mini")
    payload["payload"]["document"]["metadata"]["source_host"] = "producer_alias"
    doc = document_from_ingress_payload(payload)
    assert doc.metadata["source_host"] == "producer_alias"


def test_absent_host_leaves_no_source_host_key():
    doc = document_from_ingress_payload(_payload(host=""))
    assert "source_host" not in doc.metadata


def test_server_redaction_covers_source_project_and_host_before_metadata_injection():
    payload = _payload(host="/Users/example/private-host")
    payload["source"]["project"] = "/Users/example/Projects/private-project"

    redacted = apply_server_redaction(payload)
    document = document_from_ingress_payload(redacted)

    assert redacted["source"]["project"] == "[redacted_path]"
    assert redacted["source"]["host"] == "[redacted_path]"
    assert document.metadata["project"] == "[redacted_path]"
    assert document.metadata["source_host"] == "[redacted_path]"
    assert public_ingress_leak_violations(str(document.metadata)) == []

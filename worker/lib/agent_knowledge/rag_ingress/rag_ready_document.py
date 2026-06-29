"""Backend-neutral RAG-ready document model, hashing, and wire builder.

Nothing in this module may reference a concrete backend. ``targetProfile`` is a
*logical* profile string; the physical dataset id resolution lives in the
backend adapter. The wire builder reproduces the existing
``rag_ingress_enqueue.v1`` queue contract byte-for-byte so the generic model is
a drop-in producer for the current ingress queue.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


# Wire contract identifiers. These intentionally preserve the shared ingress
# queue contract so server and client producers speak the same
# ``rag_ingress_enqueue.v1`` payload without importing each other.
INGRESS_SCHEMA_VERSION = "rag_ingress_enqueue.v1"
DEFAULT_INGRESS_PAYLOAD_KIND = "redacted_rag_ready_document"
DEFAULT_REDACTION_VERSION = "redaction.v2"
DEFAULT_CONTENT_TYPE = "text/markdown"


_SECRET_LIKE_KEY_PATTERNS = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "bearer",
    "credential",
    "cookie",
    "private_key",
    "access_key",
    "client_secret",
)


class SecretLikeMetadataError(ValueError):
    """Raised when metadata contains a key that looks like a credential."""


class DocumentIndexTargetProfile:
    """Logical backend profiles. Backend-neutral by contract (no ``retired_index_bridge``).

    A logical profile names *what kind of knowledge* a document belongs to. The
    mapping from a logical profile to a physical backend dataset id is owned by
    the backend adapter (e.g. the RetiredIndexBridge ``index_targets`` lookup), never by
    this generic layer.
    """

    PROJECT_KNOWLEDGE = "project-knowledge"
    TOOL_SKILL_REGISTRY = "tool-skill-registry"
    TRANSCRIPT_MEMORY = "transcript-memory"

    _KNOWN = frozenset({PROJECT_KNOWLEDGE, TOOL_SKILL_REGISTRY, TRANSCRIPT_MEMORY})

    @classmethod
    def known(cls) -> frozenset[str]:
        return cls._KNOWN


def is_known_target_profile(value: str) -> bool:
    return value in DocumentIndexTargetProfile.known()


def _is_secret_like_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(pattern in lowered for pattern in _SECRET_LIKE_KEY_PATTERNS)


def assert_no_secret_like_metadata(metadata: dict) -> None:
    for key in metadata:
        if _is_secret_like_key(key):
            raise SecretLikeMetadataError(f"secret-like metadata key rejected: {key}")


def redact_secret_like_metadata(metadata: dict) -> dict:
    redacted: dict = {}
    for key, value in metadata.items():
        if _is_secret_like_key(key):
            redacted[key] = "<redacted>"
        elif isinstance(value, dict):
            redacted[key] = redact_secret_like_metadata(value)
        else:
            redacted[key] = value
    return redacted


def build_content_hash(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_idempotency_key(*, source_namespace: str, document_kind: str, content_hash: str) -> str:
    if not source_namespace:
        raise ValueError("source_namespace is required for idempotency key")
    if not document_kind:
        raise ValueError("document_kind is required for idempotency key")
    if not content_hash:
        raise ValueError("content_hash is required for idempotency key")
    return f"{source_namespace}:{document_kind}:{content_hash}"


def _validate_source_alias(source_alias: str) -> None:
    if not source_alias:
        raise ValueError("source_alias is required")
    if source_alias.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", source_alias):
        raise ValueError("source_alias must not be an absolute path")
    if ".." in source_alias.split("/") or ".." in source_alias.split("\\"):
        raise ValueError("source_alias must not contain path traversal")


@dataclass(frozen=True)
class RagReadyDocumentMetadata:
    """Convenience builder for a validated, flat, backend-neutral metadata dict.

    Source adapters use this to normalise descriptive metadata. It refuses
    secret-like keys at construction so a misconfigured adapter cannot smuggle a
    credential into an index backend.
    """

    source_workspace: str
    target_profile: str
    artifact_kind: str
    privacy_class: str
    path_prefix: str = ""
    extra: dict = field(default_factory=dict)

    def to_flat_dict(self) -> dict:
        flat = {
            "source_workspace": self.source_workspace,
            "target_profile": self.target_profile,
            "artifact_kind": self.artifact_kind,
            "privacy_class": self.privacy_class,
        }
        if self.path_prefix:
            flat["path_prefix"] = self.path_prefix
        for key, value in self.extra.items():
            flat[str(key)] = value
        assert_no_secret_like_metadata(flat)
        return flat


@dataclass(frozen=True)
class RagReadyDocument:
    """A normalised, backend-neutral RAG-ready document.

    No field names a concrete backend. ``target_profile`` is logical;
    ``content_hash``/``idempotency_key`` are deterministic functions of content.
    """

    target_profile: str
    document_kind: str
    artifact_kind: str
    source_namespace: str
    source_alias: str
    privacy_class: str
    content_hash: str
    idempotency_key: str
    body: str
    filename: str
    metadata: dict
    content_type: str = DEFAULT_CONTENT_TYPE
    redaction_version: str = DEFAULT_REDACTION_VERSION


def build_rag_ready_document(
    *,
    target_profile: str,
    document_kind: str,
    source_namespace: str,
    source_alias: str,
    privacy_class: str,
    body: str,
    filename: str,
    metadata: dict | None = None,
    artifact_kind: str = DEFAULT_INGRESS_PAYLOAD_KIND,
    content_type: str = DEFAULT_CONTENT_TYPE,
    redaction_version: str = DEFAULT_REDACTION_VERSION,
) -> RagReadyDocument:
    if not target_profile:
        raise ValueError("target_profile is required")
    if not document_kind:
        raise ValueError("document_kind is required")
    if not privacy_class:
        raise ValueError("privacy_class is required")
    _validate_source_alias(source_alias)
    flat_metadata = dict(metadata or {})
    assert_no_secret_like_metadata(flat_metadata)
    content_hash = build_content_hash(body)
    idempotency_key = build_idempotency_key(
        source_namespace=source_namespace,
        document_kind=document_kind,
        content_hash=content_hash,
    )
    return RagReadyDocument(
        target_profile=target_profile,
        document_kind=document_kind,
        artifact_kind=artifact_kind,
        source_namespace=source_namespace,
        source_alias=source_alias,
        privacy_class=privacy_class,
        content_hash=content_hash,
        idempotency_key=idempotency_key,
        body=body,
        filename=filename,
        metadata=flat_metadata,
        content_type=content_type,
        redaction_version=redaction_version,
    )


def _string_metadata(metadata: dict) -> dict[str, str]:
    return {str(key): str(value) for key, value in metadata.items()}


def build_ingress_enqueue_payload(
    document: RagReadyDocument,
    *,
    source: dict,
    schema_version: str = INGRESS_SCHEMA_VERSION,
) -> dict:
    """Produce the ``rag_ingress_enqueue.v1`` wire payload for a generic document.

    This is byte-for-byte compatible with the legacy
    ``IngressQueueClient.enqueue_document`` request body. The generic document
    already carries flat metadata, so no backend-specific flattening happens
    here.
    """

    return {
        "schemaVersion": schema_version,
        "source": dict(source),
        "payload": {
            "kind": document.artifact_kind,
            "redactionVersion": document.redaction_version,
            "document": {
                "filename": document.filename,
                "contentType": document.content_type,
                "body": document.body,
                "metadata": _string_metadata(document.metadata),
            },
        },
        "contentHash": document.content_hash,
        "targetProfile": document.target_profile,
        "kind": document.document_kind,
        "idempotencyKey": document.idempotency_key,
    }

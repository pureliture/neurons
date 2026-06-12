"""RAG-ready document ingress bus (backend-neutral).

This package is the backend-neutral seam between *source adapters* (which
normalise arbitrary source files into ``text/markdown + metadata`` RAG-ready
documents) and *document index backends* (RAGFlow today, other vector/search
stores later).

Design boundary (see
``docs/architecture/2026-06-04-rag-ready-document-ingress-bus.md``):

- The generic layer here MUST NOT name a concrete backend. No ``ragflow_*``
  identifiers, no RAGFlow ``DONE/FAIL/RUNNING`` status vocabulary, no physical
  dataset id. ``targetProfile`` is a *logical* profile, not a physical dataset.
- RAGFlow upload/parse/status semantics live entirely inside
  :mod:`agent_knowledge.rag_ingress.index_backend` (the RAGFlow adapter).
- This is a RAG-ready document ingress bus, NOT a Kafka-like general event bus.
"""

from .rag_ready_document import (  # noqa: F401
    DocumentIndexTargetProfile,
    RagReadyDocument,
    RagReadyDocumentMetadata,
    SecretLikeMetadataError,
    build_content_hash,
    build_idempotency_key,
    build_ingress_enqueue_payload,
    build_rag_ready_document,
    is_known_target_profile,
    redact_secret_like_metadata,
)
# Vendored co-locate trim (rag-ingress-queue): the live delivery worker
# (``shadow_worker``) never imports the client-side outbox (``outbox_client``)
# nor the Ledger-backed state adapter (``state_store`` -> ``LedgerIngestStateStore``).
# Those are advisor-only (client capture / Ledger) and are intentionally NOT
# vendored, so importing the package here must not pull them in. The durable
# delivery_jobs state machine (state_db / delivery_executor / delivery_reconcile /
# delivery_backend / idempotency) is likewise excluded: it is dead code in the
# live worker runtime and the worker relies on NATS at-least-once + natural-key
# dedup instead (see docs/architecture/*-nats-at-least-once-vs-lease.md).

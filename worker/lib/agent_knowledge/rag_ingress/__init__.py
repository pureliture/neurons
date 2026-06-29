"""RAG-ready document ingress bus (backend-neutral).

This package is the backend-neutral seam between *source adapters* (which
normalise arbitrary source files into ``text/markdown + metadata`` RAG-ready
documents) and *document index backends* (RetiredIndexBridge today, other vector/search
stores later).

Design boundary (see
``docs/architecture/2026-06-04-rag-ready-document-ingress-bus.md``):

- The generic layer here MUST NOT name a concrete backend. No ``index_*``
  identifiers, no RetiredIndexBridge ``DONE/FAIL/RUNNING`` status vocabulary, no physical
  dataset id. ``targetProfile`` is a *logical* profile, not a physical dataset.
- RetiredIndexBridge upload/parse/status semantics live entirely inside
  :mod:`agent_knowledge.rag_ingress.retired_index_bridge` (the RetiredIndexBridge adapter).
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
# Vendored co-locate trim (neurons): the live delivery worker (``shadow_worker``)
# never imports the client-side outbox (``outbox_client``) nor the Ledger-backed
# state adapter (``state_store`` -> ``LedgerIngestStateStore``). Durable server
# state, delivery/backfill, and read-only readiness primitives are present as
# M3-owned modules, but this package still avoids eager imports so a plain
# ``import agent_knowledge.rag_ingress`` cannot pull in optional runtime or
# Ledger-coupled paths.

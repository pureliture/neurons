"""Local-only legacy vector import. No client, CLI, network, or re-embedding."""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct

from ..couchdb_source.session_memory_materializer import materialize_session_memory
from ..model_connectors import DEFAULT_EMBEDDING_DIM, DEFAULT_EMBEDDING_MODEL
from .pg_backfill import project_pg_representation


@dataclass(frozen=True)
class LegacyCollection:
    name: str
    dimension: int
    distance: str


@dataclass(frozen=True)
class OperatorEmbeddingAttestation:
    """Operator evidence, never independent machine proof of vector provenance."""
    collection: str
    model: str
    dimension: int
    confirmed: bool
    evidence_ref: str


@dataclass(frozen=True)
class VerifiedLegacyVector:
    """Single-body, in-process adapter; never calls an embedding provider."""
    body: str
    vector: tuple[float, ...]
    model = DEFAULT_EMBEDDING_MODEL
    size = DEFAULT_EMBEDDING_DIM

    def embed(self, body: str) -> list[float]:
        if body != self.body:
            raise ValueError("legacy vector body mismatch")
        return list(self.vector)


def import_qdrant_point(*, point: dict, collection: LegacyCollection,
                        attestation: OperatorEmbeddingAttestation, project: str,
                        provider: str, session_id_hash: str, source_store, sql_store,
                        dry_run: bool = False) -> dict:
    """Validate one exported point and publish its current scoped representation.

    Collection metadata and model history are caller-supplied operator evidence,
    not a network lookup or cryptographic per-point provenance. Point IDs are not
    SQL identities: scope/source revision/representation/profile determine the ID.
    Only the exact mask + existing mirror-validation fixed point is supported;
    arbitrary Docling rewrites and non-session points fail closed. Dry-run checks
    input/current source/existing target, but is not a reservation against races.
    No embedding API, outbox fallback, source-body mutation, or automatic repair.
    """
    from ..couchdb_source.document_model import sha256_hash
    from .qdrant_backfill import public_safe_mask_body
    from .qdrant_docling_mirror import _validate_mirror_text

    if (not isinstance(collection, LegacyCollection) or not collection.name
        or type(collection.dimension) is not int or collection.dimension != DEFAULT_EMBEDDING_DIM
        or collection.distance != "Cosine"):
        raise ValueError("Qdrant import rejected: collection")
    if (not isinstance(attestation, OperatorEmbeddingAttestation)
        or attestation.confirmed is not True or attestation.collection != collection.name
        or attestation.model != DEFAULT_EMBEDDING_MODEL
        or type(attestation.dimension) is not int or attestation.dimension != collection.dimension
        or not isinstance(attestation.evidence_ref, str) or not attestation.evidence_ref.strip()):
        raise ValueError("Qdrant import rejected: attestation")
    payload = point.get("payload") if isinstance(point, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("Qdrant import rejected: payload")
    if (payload.get("document_kind") != "session_memory"
        or payload.get("target_profile") != "session-memory"
        or payload.get("result_type", "session_memory") != "session_memory"):
        raise ValueError("Qdrant import rejected: unsupported_kind")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Qdrant import rejected: payload")
    if any(record[key] != DEFAULT_EMBEDDING_MODEL for record in (payload, metadata)
           for key in ("model", "embedding_model") if key in record):
        raise ValueError("Qdrant import rejected: model")
    scope = dict(project=project, provider=provider, session_id_hash=session_id_hash)
    if any(metadata[key] != value for key, value in scope.items() if key in metadata):
        raise ValueError("Qdrant import rejected: scope")
    if any(not isinstance(value, str) or not value or payload.get(key) != value for key, value in scope.items()):
        raise ValueError("Qdrant import rejected: scope")
    try:
        current = materialize_session_memory(session_id_hash=session_id_hash, store=source_store)
        if (not current.fully_materialized or current.target_profile != "session-memory"
            or any(getattr(current, key) != value for key, value in scope.items())
            or sha256_hash(current.body) != current.content_hash
            or payload.get("content_hash") != current.content_hash
            or ("source_hash" in payload and payload["source_hash"] != current.source_hash)):
            raise ValueError("source mismatch")
    except Exception:
        raise ValueError("Qdrant import rejected: source") from None
    try:
        expected = public_safe_mask_body(current.body)
        body = payload.get("text")
        # Reject rather than silently fixing the historical vector's input.
        if not isinstance(body, str) or body != expected or _validate_mirror_text(expected) != body:
            raise ValueError("body mismatch")
    except Exception:
        raise ValueError("Qdrant import rejected: body") from None
    raw_vector = point.get("vector")
    try:
        if (not isinstance(raw_vector, (list, tuple)) or len(raw_vector) != DEFAULT_EMBEDDING_DIM
            or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 65504 for v in raw_vector)):
            raise ValueError("invalid vector")
        vector = tuple(float(v) for v in raw_vector)
        # A vector that disappears in half precision cannot support cosine recall.
        if not any(struct.unpack("e", struct.pack("e", v))[0] for v in vector):
            raise ValueError("zero vector")
    except Exception:
        raise ValueError("Qdrant import rejected: vector") from None
    try:
        return project_pg_representation(materialized=current, source_store=source_store,
            sql_store=sql_store, embed_provider=VerifiedLegacyVector(body, vector), body=body,
            expected_vector=vector, dry_run=dry_run)
    except Exception:
        # SQL/adapter errors may contain source values; never expose their text.
        raise ValueError("Qdrant import rejected: projection") from None

"""PostgreSQL Storage Layer, memory_edges DAG, Outbox with CAS & Dual-Read Shadow (Milestones 3 & 4)."""

from .pgvector_store import (
    MemoryCard,
    MemoryEdge,
    SessionChunk,
    OutboxJob,
    LocalOutboxJob,
    PgVectorStore,
    compute_cosine_similarity,
    make_dummy_vector,
    compute_identity_lock_id,
    card_identity_lock_id,
    session_identity_lock_id,
    target_advisory_lock_id,
)
from .outbox_worker import (
    OutboxWorker,
    LocalEmbeddingOutboxWorker,
    generate_deterministic_embedding,
)
from .migration_qdrant_to_postgres import (
    QdrantToPostgresMigrator,
    MigrationResult,
    FullMigrationSummary,
)
from .dual_read_shadow import (
    DualReadShadowHarness,
    BenchmarkQuery,
    BenchmarkSummary,
    DualReadComparisonResult,
)

__all__ = [
    "MemoryCard",
    "MemoryEdge",
    "SessionChunk",
    "OutboxJob",
    "LocalOutboxJob",
    "PgVectorStore",
    "OutboxWorker",
    "LocalEmbeddingOutboxWorker",
    "compute_cosine_similarity",
    "make_dummy_vector",
    "compute_identity_lock_id",
    "card_identity_lock_id",
    "session_identity_lock_id",
    "target_advisory_lock_id",
    "generate_deterministic_embedding",
    "QdrantToPostgresMigrator",
    "MigrationResult",
    "FullMigrationSummary",
    "DualReadShadowHarness",
    "BenchmarkQuery",
    "BenchmarkSummary",
    "DualReadComparisonResult",
]


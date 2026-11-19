"""Shared source-hash authority lookup for both bulk CLI paths."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent_knowledge.postgres_store.pgvector_store import PgVectorStore

AUTHORITY_HASH_CHUNK_SIZE = 100


def authorized_sources_by_hash(
    dsn: str, project: str, hashes: list[str], *, as_of: Any = None
) -> dict[str, list[dict[str, str]]]:
    """Resolve approved cards in one repeatable-read snapshot; fail closed."""
    if not dsn:
        raise RuntimeError("authority DSN unavailable")
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    wanted = list(dict.fromkeys(str(value) for value in hashes if value))
    if not wanted:
        return {}

    batches = [
        wanted[i:i + AUTHORITY_HASH_CHUNK_SIZE]
        for i in range(0, len(wanted), AUTHORITY_HASH_CHUNK_SIZE)
    ]
    result = PgVectorStore(dsn=dsn).find_authorized_by_content_hashes_snapshot(
        project=project, batches=batches, as_of=as_of
    )
    ambiguous = set(result["ambiguous_hashes"])
    return {
        content_hash: [
            {"authority_memory_id": memory_id, "content_hash": content_hash}
            for memory_id in memory_ids
        ]
        for content_hash, memory_ids in result["by_hash"].items()
        if content_hash not in ambiguous and len(memory_ids) == 1
    }


def revalidate_authority_sources(
    dsn: str, project: str, sources: list[dict[str, str]]
) -> bool:
    """Check previously attached authority at completion/exposure time."""
    if not sources:
        return True
    if not dsn:
        return False
    now = datetime.now(timezone.utc)
    allowed = authorized_sources_by_hash(
        dsn, project, [str(item.get("content_hash") or "") for item in sources], as_of=now
    )
    for item in sources:
        content_hash = str(item.get("content_hash") or "")
        memory_id = str(item.get("authority_memory_id") or "")
        approved = allowed.get(content_hash, [])
        if not any(entry["authority_memory_id"] == memory_id for entry in approved):
            return False
    return True

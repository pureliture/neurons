"""Bounded Qdrant→PG memory_cards migration CLI.

Exposes migrate_memory_cards independently from run_full_migration.
Default is dry-run. Network connections use env only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .migration_qdrant_to_postgres import QdrantToPostgresMigrator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge qdrant-pg-migrate-cards",
        description="Migrate Qdrant memory_cards collection to PostgreSQL memory_cards table.",
    )
    parser.add_argument(
        "--qdrant-url",
        default=os.environ.get("QDRANT_URL", ""),
        help="Qdrant URL (or set QDRANT_URL env)",
    )
    parser.add_argument(
        "--pg-dsn",
        default=os.environ.get("NEURON_LBRAIN_PGVECTOR_DSN", ""),
        help="PostgreSQL DSN (or set NEURON_LBRAIN_PGVECTOR_DSN env)",
    )
    parser.add_argument(
        "--collection-name",
        default="memory_cards",
        help="Qdrant collection name (default: memory_cards)",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Filter by project (default: all projects)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Batch size (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run: scan and report without writing",
    )
    parser.add_argument(
        "--checkpoint-file",
        default=None,
        help="Checkpoint file path for resumable migration",
    )
    args = parser.parse_args(argv)

    if not args.qdrant_url:
        print("error: --qdrant-url or QDRANT_URL env is required", file=sys.stderr)
        return 2
    if not args.pg_dsn:
        print(
            "error: --pg-dsn or NEURON_LBRAIN_PGVECTOR_DSN env is required",
            file=sys.stderr,
        )
        return 2

    from qdrant_client import QdrantClient

    from .pgvector_store import PgVectorStore

    qdrant_client = QdrantClient(url=args.qdrant_url)
    pg_store = PgVectorStore(dsn=args.pg_dsn)

    migrator = QdrantToPostgresMigrator(
        qdrant_client=qdrant_client,
        target_store=pg_store,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        checkpoint_file=args.checkpoint_file,
    )

    result = migrator.migrate_memory_cards(
        collection_name=args.collection_name,
        project=args.project,
    )

    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

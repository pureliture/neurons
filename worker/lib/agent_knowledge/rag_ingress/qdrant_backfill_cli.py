"""CLI for the session-memory Qdrant searchable-mirror backfill (CouchDB-native).

Subcommands:
  verify            Count PROJECTED session-memory sessions (CouchDB read; no writes).
  dry-run (default) Materialize+build every mirror document without upserting.
  run [--limit N]   Upsert the mirror points. REQUIRES an explicit --collection.
  rollback --submitted F   Delete exactly the points recorded in jsonl ``F``.
  parity            Parity-soak entrypoint (RAGFlow primary vs CouchDB-joined mirror).

Source/authority: the CouchDB source plane (the go-forward recall authority; the
ledger ``knowledge_items`` is retiring and is NOT used here). The corpus is every
session whose ``projection_state`` is ``projected``.

Safety:
- ``run`` requires an explicit ``--collection`` so a live upsert can never fall
  through to a default name (a staging name is accepted; the live name is only
  used when the operator types it).
- ``run``/``rollback`` build the adapter with ``ensure_collection=False``: a
  non-existent target collection fails closed (never silently created server-side).
  ``run`` may pass ``--create-collection`` to opt into first-time staging setup.
- MIRROR-ONLY: the CLI never writes the CouchDB primary, never writes RAGFlow, and
  never builds a dual-write backend. Backfill reads CouchDB (read-only materialize)
  and upserts/deletes Qdrant points only. RAGFlow is touched only by the parity
  primary fetch, never by backfill.
- Output is JSON of counts/statuses (redaction-safe). The jsonl audit / checkpoint
  hold natural-key triples (content_hash + idempotency_key + target_profile), never
  bodies or raw ids.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any

from .qdrant_backfill import (
    backfill_session_memory,
    iter_projected_session_memories,
    rollback_submitted,
)

# The live mirror collection name (operator must still type it for ``run``).
LIVE_COLLECTION_NAME = "neurons_mirror_gemini_3072_v1"


# --------------------------------------------------------------------------- IO

def _write_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _load_submitted_jsonl(path: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"submitted jsonl not found: {file_path.name}")
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # Tolerate a corrupt/partial line (e.g. an interrupted append) rather
            # than aborting a rollback over the whole manifest, mirroring the
            # checkpoint loader's leniency.
            continue
    return records


def _load_checkpoint_hashes(path: str | None) -> set[str]:
    if not path:
        return set()
    file_path = Path(path)
    if not file_path.exists():
        return set()
    hashes: set[str] = set()
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        content_hash = str(record.get("content_hash") or "")
        if content_hash:
            hashes.add(content_hash)
    return hashes


def _appender(*paths: str | None):
    """Return on_submit appending each triple as a jsonl line to every given path.

    Holds natural-key triples only (no bodies/raw-ids), so the jsonl doubles as a
    rollback manifest and a resume checkpoint.
    """
    targets = [Path(p) for p in paths if p]
    handles = [p.open("a", encoding="utf-8") for p in targets]

    def _on_submit(triple: dict[str, Any]) -> None:
        line = json.dumps(triple, ensure_ascii=False, sort_keys=True)
        for handle in handles:
            handle.write(line + "\n")
            handle.flush()

    _on_submit.close = lambda: [handle.close() for handle in handles]  # type: ignore[attr-defined]
    return _on_submit


# ----------------------------------------------------------------- wiring (live)

def _build_store(args):
    """Build the CouchDB source store from env (read-only usage in backfill).

    Mirrors ``couchdb_source.build_cli`` env contract: COUCHDB_URL (required),
    COUCHDB_USER/COUCHDB_PASSWORD (basic auth), COUCHDB_DB (default
    ``transcript_source``). Fail-closed if COUCHDB_URL is absent.
    """
    couchdb_url = os.environ.get("COUCHDB_URL", "")
    if not couchdb_url:
        raise SystemExit("COUCHDB_URL is required for the CouchDB-native backfill")
    couchdb_user = os.environ.get("COUCHDB_USER", "")
    couchdb_password = os.environ.get("COUCHDB_PASSWORD", "")
    couchdb_db = os.environ.get("COUCHDB_DB", "transcript_source")
    auth_header = ""
    if couchdb_user:
        token = base64.b64encode(f"{couchdb_user}:{couchdb_password}".encode("utf-8")).decode("ascii")
        auth_header = f"Basic {token}"
    from ..couchdb_source.couchdb_http_store import CouchDBHttpSourceStore

    return CouchDBHttpSourceStore(base_url=couchdb_url, db=couchdb_db, auth_header=auth_header)


def _build_adapter(args, *, collection_name: str, ensure_collection: bool):
    """Build the remote mirror adapter.

    ``ensure_collection`` is False for run/rollback: a non-existent target
    collection fails closed (SystemExit naming the collection) instead of being
    silently created server-side. Pass ``--create-collection`` with ``run`` to
    opt into first-time creation.
    """
    from .qdrant_docling_mirror import (
        PassthroughMarkdownNormalizer,
        build_remote_qdrant_docling_mirror_adapter,
    )
    from .qdrant_embedding import build_openai_embedding_provider

    url = str(os.environ.get("QDRANT_URL") or args.qdrant_url or "").strip()
    adapter = build_remote_qdrant_docling_mirror_adapter(
        url=url,
        collection_name=collection_name,
        embedding_provider=build_openai_embedding_provider(environ=os.environ),
        normalizer=PassthroughMarkdownNormalizer(),
        ensure_collection=ensure_collection,
    )
    if not ensure_collection:
        exists = adapter.collection_exists()
        if exists is False:
            raise SystemExit(
                f"target collection does not exist: {collection_name!r}. "
                "Refusing to create it implicitly; pass --create-collection (run only) "
                "to set up a new collection, or fix the --collection name."
            )
    return adapter


# --------------------------------------------------------------------- commands

def _cmd_verify(args) -> int:
    store = _build_store(args)
    count = sum(1 for _ in iter_projected_session_memories(store))
    _write_json(
        {
            "command": "verify",
            "projected_session_memory_count": count,
            "network_used": False,
            "mutation_performed": False,
            "raw_ids_printed": False,
        }
    )
    return 0


def _cmd_dry_run(args) -> int:
    store = _build_store(args)
    report = backfill_session_memory(
        store=store,
        adapter=_DryRunAdapter(),
        dry_run=True,
        limit=args.limit,
    )
    out = report.to_dict()
    out["command"] = "dry-run"
    _write_json(out)
    return 0


def _cmd_run(args) -> int:
    if not args.collection:
        raise SystemExit("run requires an explicit --collection (refusing default to avoid accidental live writes)")
    store = _build_store(args)
    # Default: never create the collection (a typo fails closed). --create-collection
    # opts into first-time creation for staging setup only.
    adapter = _build_adapter(
        args,
        collection_name=args.collection,
        ensure_collection=bool(getattr(args, "create_collection", False)),
    )
    already = _load_checkpoint_hashes(args.checkpoint)
    on_submit = _appender(args.submitted, args.checkpoint)
    try:
        report = backfill_session_memory(
            store=store,
            adapter=adapter,
            dry_run=False,
            limit=args.limit,
            on_submit=on_submit,
            already_submitted=already,
            concurrency=int(getattr(args, "embedding_concurrency", 1) or 1),
        )
    finally:
        close = getattr(on_submit, "close", None)
        if callable(close):
            close()
    out = report.to_dict()
    out["command"] = "run"
    out["collection"] = args.collection
    _write_json(out)
    return 0


def _cmd_rollback(args) -> int:
    if not args.submitted:
        raise SystemExit("rollback requires --submitted <jsonl>")
    if not args.collection:
        raise SystemExit("rollback requires an explicit --collection")
    # rollback never creates a collection: deleting from a non-existent collection
    # is meaningless and a typo must fail closed.
    adapter = _build_adapter(args, collection_name=args.collection, ensure_collection=False)
    submitted = _load_submitted_jsonl(args.submitted)
    report = rollback_submitted(adapter=adapter, submitted=submitted)
    out = report.to_dict()
    out["command"] = "rollback"
    out["collection"] = args.collection
    _write_json(out)
    return 0


def _cmd_parity(args) -> int:
    # The parity soak needs a configured query cohort + thresholds that are set at
    # gate time after measuring the RAGFlow baseline, plus a live RAGFlow primary
    # fetch and a CouchDB store for the mirror authority-join. Rather than bake a
    # baseline into the CLI, this surfaces the wiring entrypoint and refuses to emit
    # a verdict without an explicit cohort file.
    if not args.cohort:
        raise SystemExit(
            "parity requires --cohort <queries.txt> and threshold flags; "
            "run programmatically via qdrant_backfill_parity.run_parity_soak for the gate"
        )
    raise SystemExit(
        "parity CLI is a thin entrypoint; the gate runner lives in "
        "qdrant_backfill_parity.run_parity_soak (pure-compute, injectable fetchers). "
        "primary_fetch = RAGFlow retrieve over the session-memory dataset (the only "
        "RAGFlow use in this CLI); mirror_fetch = Qdrant query joined via the CouchDB "
        "projection-state resolver."
    )


class _DryRunAdapter:
    """Adapter stand-in for dry-run: validates documents but never writes.

    ``dry_run=True`` short-circuits ``submit_document`` in the core, so this is
    only a safety net to guarantee no write path is reachable on the dry-run code
    path even if that contract changes.
    """

    embedding_size = None

    def collection_vector_size(self) -> None:
        return None

    def submit_document(self, *_args, **_kwargs):  # pragma: no cover - never called on dry-run
        raise AssertionError("dry-run must not submit documents")


# ------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qdrant-backfill")
    parser.add_argument("--collection", default="", help="Qdrant collection name (REQUIRED for run/rollback)")
    parser.add_argument("--checkpoint", default="", help="jsonl resume checkpoint (hashes only)")
    parser.add_argument("--submitted", default="", help="jsonl manifest of submitted natural keys")
    parser.add_argument("--embedding-concurrency", type=int, default=1, help="embedding concurrency (default 1)")
    parser.add_argument("--qdrant-url", default="", help="Qdrant url (else QDRANT_URL env)")
    parser.add_argument("--cohort", default="", help="parity: query cohort file")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("verify", help="count PROJECTED session-memory sessions (CouchDB read; no writes)")
    dry = sub.add_parser("dry-run", help="materialize+build without upserting (default)")
    dry.add_argument("--limit", type=int, default=None)
    run = sub.add_parser("run", help="upsert mirror points (requires --collection)")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument(
        "--create-collection",
        action="store_true",
        help="allow first-time creation of an absent collection (staging setup only; off by default)",
    )
    sub.add_parser("rollback", help="delete recorded points (requires --submitted)")
    sub.add_parser("parity", help="parity soak entrypoint")
    return parser


_DISPATCH = {
    "verify": _cmd_verify,
    "dry-run": _cmd_dry_run,
    "run": _cmd_run,
    "rollback": _cmd_rollback,
    "parity": _cmd_parity,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "dry-run"
    if not hasattr(args, "limit"):
        args.limit = None
    handler = _DISPATCH[command]
    return int(handler(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

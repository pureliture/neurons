"""Bounded, idempotent recovery for oversized session_memory_chunks rows."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..local_embedding.canonical import split_utf8_chunks_v1
from ..couchdb_source.document_model import sha256_hash
from ..model_connectors import LOCAL_EMBEDDING_PROFILE_ID
from .pgvector_store import PgVectorStore, SessionChunk

DEFAULT_OVERSIZED_THRESHOLD_BYTES = 524288
DEFAULT_MAX_BYTES = 524288


def _derived_chunk_id(original_chunk_id: str, part_index: int) -> str:
    original = str(original_chunk_id)
    part = int(part_index)
    suffix = f"__split_{part:04d}"
    # Short original IDs keep the human-readable scheme.
    if len(original) + len(suffix) <= 64:
        return original + suffix
    # Long (e.g. 64-char) IDs: keep a stable prefix plus a deterministic
    # hash of the full original + part_index, so the result fits in varchar(64)
    # while remaining unique per (original, part_index).
    digest = hashlib.sha256(f"{original}\x00{part}".encode("utf-8")).hexdigest()
    prefix_len = 16
    part_str = str(part)
    available = 64 - prefix_len - 2 - len(part_str)
    hash_len = min(32, available)
    if hash_len < 1:
        return (digest + part_str)[:64]
    return f"{original[:prefix_len]}_{digest[:hash_len]}_{part_str}"


_PROFILE_METHOD_NAMES = (
    "get_local_outbox_profile_id",
    "get_outbox_profile_id",
    "get_dead_letter_profile_id",
    "get_profile_id_for_target",
    "get_profile_id_from_outbox",
    "get_target_profile_id",
    "profile_id_for_target",
    "read_profile_id_from_outbox",
)

_PROFILE_JOB_METHOD_NAMES = (
    "get_local_outbox_job",
    "get_local_outbox_job_for_target",
    "get_outbox_job_for_target",
    "get_dead_letter_outbox_job",
    "find_local_outbox_job",
)

_PROFILE_ATTR_NAMES = (
    "outbox_profiles",
    "profile_ids_by_target",
    "profiles_by_chunk",
    "target_profile_ids",
    "outbox_profile_by_target",
    "dead_letter_profile_ids",
)


def _coerce_profile(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if hasattr(value, "profile_id"):
        prof = getattr(value, "profile_id", None)
        if isinstance(prof, str) and prof.strip():
            return prof.strip()
    if isinstance(value, Mapping):
        for key in ("profile_id", "profile", "embedding_profile"):
            if key in value:
                v = value[key]
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return None


def _profile_id_for_target(store: Any, chunk_id: str) -> str:
    # Direct profile-returning methods.
    for name in _PROFILE_METHOD_NAMES:
        meth = getattr(store, name, None)
        if callable(meth):
            for call in (
                lambda m=meth: m(target_type="session_chunk", target_id=chunk_id),
                lambda m=meth: m(chunk_id, target_type="session_chunk"),
                lambda m=meth: m(chunk_id),
            ):
                try:
                    result = call()
                except Exception:
                    continue
                prof = _coerce_profile(result)
                if prof is not None:
                    return prof
    # Job-returning methods.
    for name in _PROFILE_JOB_METHOD_NAMES:
        meth = getattr(store, name, None)
        if callable(meth):
            for call in (
                lambda m=meth: m(target_type="session_chunk", target_id=chunk_id),
                lambda m=meth: m("session_chunk", chunk_id),
                lambda m=meth: m(chunk_id),
            ):
                try:
                    result = call()
                except Exception:
                    continue
                prof = _coerce_profile(result)
                if prof is not None:
                    return prof
    # Mapping attributes.
    for attr in _PROFILE_ATTR_NAMES:
        obj = getattr(store, attr, None)
        if isinstance(obj, Mapping) and chunk_id in obj:
            prof = _coerce_profile(obj[chunk_id])
            if prof is not None:
                return prof
    # list_* methods.
    for name in ("list_local_outbox_jobs", "list_outbox_jobs"):
        meth = getattr(store, name, None)
        if callable(meth):
            jobs = None
            for kwargs in ({"status": "dead_letter"}, {}):
                try:
                    jobs = meth(**kwargs)
                    break
                except Exception:
                    jobs = None
            if jobs:
                for job in jobs:
                    tid = None
                    if hasattr(job, "target_id"):
                        tid = getattr(job, "target_id", None)
                    elif isinstance(job, Mapping):
                        tid = job.get("target_id")
                    if tid == chunk_id:
                        prof = _coerce_profile(job)
                        if prof is not None:
                            return prof
    return LOCAL_EMBEDDING_PROFILE_ID


def _process_chunk(
    store: Any,
    chunk: Any,
    *,
    max_bytes: int,
    threshold_bytes: int,
    dry_run: bool,
    report: dict[str, Any],
    profile_id: str | None = None,
) -> None:
    text = str(getattr(chunk, "content_markdown", "") or "")
    if len(text.encode("utf-8")) <= threshold_bytes:
        return
    report["oversized_rows"] += 1
    pieces = list(split_utf8_chunks_v1(text, max_chunk_bytes=max_bytes))
    if len(pieces) <= 1:
        return
    resolved_profile = profile_id
    if resolved_profile is None:
        resolved_profile = _profile_id_for_target(store, chunk.chunk_id)
    for part_index, piece in enumerate(pieces):
        derived_id = _derived_chunk_id(chunk.chunk_id, part_index)
        if store.get_chunk(derived_id) is not None:
            report["skipped_existing"] += 1
            continue
        report["planned_splits"] += 1
        if dry_run:
            continue
        new_chunk = SessionChunk(
            chunk_id=derived_id,
            session_id_hash=chunk.session_id_hash,
            project=chunk.project,
            provider=chunk.provider,
            chunk_index=int(getattr(chunk, "chunk_index", 0) or 0),
            content_markdown=piece,
            token_count=int(getattr(chunk, "token_count", 0) or 0),
            content_hash=sha256_hash(piece),
            embedding_model=getattr(chunk, "embedding_model", None) or "text-embedding-3-small",
            embedding_state="pending",
            embedding_revision=1,
            observed_at=getattr(chunk, "observed_at", None),
        )
        store.insert_chunk_with_local_outbox(
            new_chunk,
            profile_id=resolved_profile,
            insert_only=True,
        )
        report["inserted_chunks"] += 1


def recover_oversized_session_memory_chunks(
    store: Any,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    threshold_bytes: int = DEFAULT_OVERSIZED_THRESHOLD_BYTES,
    dry_run: bool = True,
    limit_sessions: int = 20_000,
    batch_size: int = 100,
    target_chunk_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(threshold_bytes, bool) or not isinstance(threshold_bytes, int) or threshold_bytes <= 0:
        raise ValueError("threshold_bytes must be a positive integer")

    report: dict[str, Any] = {
        "dry_run": bool(dry_run),
        "threshold_bytes": threshold_bytes,
        "max_bytes": max_bytes,
        "oversized_rows": 0,
        "planned_splits": 0,
        "inserted_chunks": 0,
        "skipped_existing": 0,
        "errors": [],
    }

    targets = [str(x) for x in (target_chunk_ids or []) if str(x).strip()]
    if targets:
        for chunk_id in targets:
            chunk = store.get_chunk(chunk_id)
            if chunk is None:
                report["errors"].append(f"missing chunk body: {chunk_id}")
                continue
            _process_chunk(
                store,
                chunk,
                max_bytes=max_bytes,
                threshold_bytes=threshold_bytes,
                dry_run=dry_run,
                report=report,
                profile_id=_profile_id_for_target(store, chunk_id),
            )
        return report

    for _session_id, metas in store.iter_session_chunk_meta_batches(
        batch_size=batch_size, limit_sessions=limit_sessions
    ):
        for meta in metas:
            if int(getattr(meta, "content_bytes", 0) or 0) <= threshold_bytes:
                continue
            chunk = store.get_chunk(meta.chunk_id)
            if chunk is None:
                report["errors"].append(f"missing chunk body: {meta.chunk_id}")
                continue
            text = str(getattr(chunk, "content_markdown", "") or "")
            if len(text.encode("utf-8")) <= threshold_bytes:
                continue
            _process_chunk(
                store,
                chunk,
                max_bytes=max_bytes,
                threshold_bytes=threshold_bytes,
                dry_run=dry_run,
                report=report,
                profile_id=None,
            )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oversized-session-chunk-recovery")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--threshold-bytes", type=int, default=DEFAULT_OVERSIZED_THRESHOLD_BYTES)
    parser.add_argument("--target-chunk-id", action="append", default=None, dest="target_chunk_ids")
    parser.add_argument("--all", action="store_true", dest="all_scan")
    args = parser.parse_args(argv)

    targets = [str(x) for x in (args.target_chunk_ids or []) if str(x).strip()]
    if not targets and not args.all_scan:
        print(json.dumps({"status": "targets_or_all_required", "mutation_performed": False}, sort_keys=True))
        return 2

    import os as _os
    dsn = ""
    for name in ("NEURON_LBRAIN_PGVECTOR_DSN", "LLM_BRAIN_PGVECTOR_DSN", "NEURON_LEDGER_PG_DSN"):
        value = _os.environ.get(name, "").strip()
        if value:
            dsn = value
            break
    if not dsn:
        print(json.dumps({"status": "no_dsn", "mutation_performed": False}, sort_keys=True))
        return 2
    store = PgVectorStore(dsn=dsn)
    report = recover_oversized_session_memory_chunks(
        store,
        max_bytes=args.max_bytes,
        threshold_bytes=args.threshold_bytes,
        dry_run=args.dry_run or not args.execute,
        target_chunk_ids=targets if targets else None,
    )
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

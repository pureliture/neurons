"""CLI entry point: couchdb-session-memory-build.

Selects CouchDB transcript_session sessions whose projection_state is missing or
not PROJECTED, materializes each one and projects it to the RetiredIndexBridge session-memory
dataset.

Approval gate (fail-closed):
  Live (non-dry-run) runs require --approval <path> pointing to a JSON file with
  schema_version "agent_knowledge_live_approval.v1",
  operation "couchdb_session_memory_build", operator_approval.approved true,
  redaction_required true, timeout_seconds > 0, rollback_or_abort_criteria, and
  command.argv matching the actual argv passed to main().

Output: one JSON line (sorted keys) with keys: dry_run, failed, projected,
schema_version, selected, skipped.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

BUILD_CLI_SCHEMA_VERSION = "couchdb_session_memory_build.v1"
BUILD_CLI_OPERATION = "couchdb_session_memory_build"


def _build_auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _select_sessions_needing_projection(
    store, limit: int, *, project: str = "", provider: str = ""
) -> list[dict]:
    """Return transcript_session docs whose projection_state is missing or not PROJECTED.

    When ``project``/``provider`` are set the selection is scoped to that subset so a
    scoped migration flow does not materialize out-of-scope sessions in the
    session-memory step.
    """
    from .document_model import ProjectionStatus, SourceDocType, projection_state_doc_id

    scope_selector = _scope_selector(project=project, provider=provider)
    states = store.find_by_type(
        SourceDocType.PROJECTION_STATE,
        fields=["_id", "session_id_hash", "projection_status"],
        selector={"projection_status": ProjectionStatus.PROJECTED, **scope_selector},
    )
    projected_session_ids = {
        session_id_hash
        for state in states
        if (session_id_hash := str(state.get("session_id_hash") or ""))
        and str(state.get("_id") or "") == projection_state_doc_id(session_id_hash)
        and str(state.get("projection_status") or "") == ProjectionStatus.PROJECTED
    }
    selected: list[dict] = []
    sessions = iter(
        _iter_by_type(
            store,
            SourceDocType.TRANSCRIPT_SESSION,
            fields=["_id", "session_id_hash", "provider", "project"],
            selector=scope_selector,
            page_size=max(1, limit) if limit > 0 else 10000,
        )
    )
    while limit <= 0 or len(selected) < limit:
        try:
            session = next(sessions)
        except StopIteration:
            break
        session_id_hash = str(session.get("session_id_hash") or "")
        if not session_id_hash:
            continue
        if session_id_hash not in projected_session_ids:
            selected.append(session)
    return selected


def _scope_selector(*, project: str, provider: str) -> dict[str, str]:
    selector = {}
    if project:
        selector["project"] = project
    if provider:
        selector["provider"] = provider
    return selector


def _iter_by_type(store, doc_type: str, *, fields: list[str], selector: dict, page_size: int = 10000):
    iterator = getattr(store, "iter_by_type", None)
    if callable(iterator):
        return iterator(doc_type, fields=fields, selector=selector, page_size=page_size)
    try:
        return iter(store.find_by_type(doc_type, fields=fields, selector=selector, page_size=page_size))
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        docs = store.find_by_type(doc_type, fields=fields)
        return (
            doc
            for doc in docs
            if all(doc.get(key) == value for key, value in selector.items())
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="neuron-knowledge couchdb-session-memory-build",
        description="Build CouchDB->session-memory live pipeline.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report selection counts; no RetiredIndexBridge writes.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum sessions to process (0 = unlimited).")
    parser.add_argument("--project", default="", help="Scope selection to this project (empty = all).")
    parser.add_argument("--provider", default="", help="Scope selection to this provider (empty = all).")
    parser.add_argument("--approval", default="", help="Path to live-approval JSON (required for non-dry-run).")
    parser.add_argument("--dataset-name", default="session-memory", help="RetiredIndexBridge dataset name (default: session-memory).")
    parser.add_argument("--retired-index-bridge-url", default="", help="RetiredIndexBridge base URL (overrides RETIRED_INDEX_BRIDGE_URL env).")
    parser.add_argument(
        "--retired-index-bridge-token-env",
        default="RETIRED_INDEX_BRIDGE_API_KEY",
        help="(무시됨) RetiredIndexBridge 토큰은 항상 RETIRED_INDEX_BRIDGE_API_KEY env에서만 읽는다.",
    )

    args = parser.parse_args(argv)

    # Reconstruct the effective argv for approval matching.
    effective_argv = list(sys.argv[1:] if argv is None else argv)

    # --- Approval gate (fail-closed) ----------------------------------------
    if not args.dry_run:
        from ..session_memory.native_memory_sync_approval import (
            ApprovalError,
            validate_memory_enqueue_approval,
        )

        try:
            validate_memory_enqueue_approval(
                args.approval or None,
                operation=BUILD_CLI_OPERATION,
                command_argv=effective_argv,
            )
        except ApprovalError as exc:
            print(
                json.dumps(
                    {
                        "schema_version": BUILD_CLI_SCHEMA_VERSION,
                        "error": "approval_rejected",
                        "reason": str(exc),
                        "dry_run": False,
                        "selected": 0,
                        "projected": 0,
                        "failed": 0,
                        "skipped": 0,
                    },
                    sort_keys=True,
                )
            )
            return 2

    # --- Store connection ----------------------------------------------------
    couchdb_url = os.environ.get("COUCHDB_URL", "")
    couchdb_user = os.environ.get("COUCHDB_USER", "")
    couchdb_password = os.environ.get("COUCHDB_PASSWORD", "")
    couchdb_db = os.environ.get("COUCHDB_DB", "transcript_source")

    if not couchdb_url:
        print(
            json.dumps(
                {
                    "schema_version": BUILD_CLI_SCHEMA_VERSION,
                    "error": "env_missing",
                    "reason": "COUCHDB_URL is required",
                    "dry_run": args.dry_run,
                    "selected": 0,
                    "projected": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                sort_keys=True,
            )
        )
        return 2

    from .couchdb_http_store import CouchDBHttpSourceStore

    auth_header = _build_auth_header(couchdb_user, couchdb_password) if couchdb_user else ""
    store = CouchDBHttpSourceStore(
        base_url=couchdb_url,
        db=couchdb_db,
        auth_header=auth_header,
    )

    # --- Session selection --------------------------------------------------
    sessions = _select_sessions_needing_projection(
        store, limit=args.limit, project=str(args.project or ""), provider=str(args.provider or "")
    )
    selected_count = len(sessions)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "schema_version": BUILD_CLI_SCHEMA_VERSION,
                    "dry_run": True,
                    "selected": selected_count,
                    "projected": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                sort_keys=True,
            )
        )
        return 0

    # --- Projector construction (live run only) ------------------------------
    from .session_memory_materializer import materialize_and_project

    backend = os.environ.get("SESSION_MEMORY_PROJECTION_BACKEND", "retired_index_bridge").strip().lower()
    # Fail-closed: 오타 등으로 알 수 없는 backend가 들어오면 retired_index_bridge로 조용히
    # fallback하지 않고 명시적으로 거부한다.
    if backend not in {"retired_index_bridge", "qdrant"}:
        print(
            json.dumps(
                {
                    "schema_version": BUILD_CLI_SCHEMA_VERSION,
                    "error": "env_invalid",
                    "reason": (
                        "SESSION_MEMORY_PROJECTION_BACKEND는 'retired_index_bridge' 또는 'qdrant'여야 한다"
                    ),
                    "dry_run": False,
                    "selected": 0,
                    "projected": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                sort_keys=True,
            )
        )
        return 2
    if backend == "qdrant":
        # Qdrant-direct write path (retired-index-bridge-free): project each session-memory
        # straight into the Qdrant searchable mirror as the CANONICAL target. No
        # RetiredIndexBridge URL/token required. A submit failure marks the projection FAILED
        # (retried next run), not best-effort. mirror_sink stays None -- the projector
        # IS the Qdrant writer, so there is no separate best-effort forward hook.
        projector = _build_qdrant_projector(os.environ)
        if projector is None:
            print(
                json.dumps(
                    {
                        "schema_version": BUILD_CLI_SCHEMA_VERSION,
                        "error": "env_missing",
                        "reason": "QDRANT_URL (and a reachable mirror collection) is required for the qdrant projection backend",
                        "dry_run": False,
                        "selected": selected_count,
                        "projected": 0,
                        "failed": 0,
                        "skipped": 0,
                    },
                    sort_keys=True,
                )
            )
            return 2
        mirror_sink = None
    else:
        index_url = args.retired_index_bridge_url or os.environ.get("RETIRED_INDEX_BRIDGE_URL", "")
        # Repo guardrail: RetiredIndexBridge 토큰은 RETIRED_INDEX_BRIDGE_API_KEY 하나만 사용한다. args.retired_index_bridge_token_env로
        # 임의의 env 이름을 secret 소스로 받지 않도록 하드코딩한다.
        bearer_token = os.environ.get("RETIRED_INDEX_BRIDGE_API_KEY", "")
        if not index_url or not bearer_token:
            print(
                json.dumps(
                    {
                        "schema_version": BUILD_CLI_SCHEMA_VERSION,
                        "error": "env_missing",
                        "reason": "index_url and token are required for live runs",
                        "dry_run": False,
                        "selected": selected_count,
                        "projected": 0,
                        "failed": 0,
                        "skipped": 0,
                    },
                    sort_keys=True,
                )
            )
            return 2

        from .index_projector import RetiredIndexBridgeSessionMemoryProjector

        projector = RetiredIndexBridgeSessionMemoryProjector(
            index_url=index_url,
            bearer_token=bearer_token,
            dataset_name=args.dataset_name,
        )
        # Optional best-effort Qdrant forward mirror ALONGSIDE RetiredIndexBridge (legacy dual
        # path). Off unless MIRROR_DUAL_WRITE=1 AND QDRANT_URL are set; a mirror
        # misconfig yields a None sink and NEVER blocks the canonical RetiredIndexBridge projection.
        mirror_sink = _build_forward_mirror_sink(os.environ)

    projected = 0
    failed = 0
    skipped = 0

    try:
        for session in sessions:
            session_id_hash = str(session.get("session_id_hash") or "")
            if not session_id_hash:
                skipped += 1
                continue
            try:
                result = materialize_and_project(
                    session_id_hash=session_id_hash,
                    store=store,
                    projector=projector,
                    mirror_sink=mirror_sink,
                )
                projection = result.get("projection") or {}
                status = str(projection.get("status") or "")
                if status == "projected":
                    projected += 1
                elif not result.get("fully_materialized"):
                    skipped += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
    finally:
        _close_if_supported(mirror_sink)
        _close_if_supported(projector)

    print(
        json.dumps(
            {
                "schema_version": BUILD_CLI_SCHEMA_VERSION,
                "dry_run": False,
                "selected": selected_count,
                "projected": projected,
                "failed": failed,
                "skipped": skipped,
            },
            sort_keys=True,
        )
    )
    return 0 if failed == 0 else 1


def _close_if_supported(resource) -> None:
    closer = getattr(resource, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:
        return


def _build_forward_mirror_sink(environ):
    """Build the best-effort Qdrant forward mirror sink, or None when disabled.

    Enabled only when ``MIRROR_DUAL_WRITE=1`` AND ``QDRANT_URL`` are set. Reuses the
    OpenAI-compatible embedding provider (``LLM_BRAIN_EMBEDDING_*``) and a passthrough
    normalizer (session-memory bodies are already redacted). ``ensure_collection`` is
    False: the forward write targets the existing live mirror collection and must NOT
    create one implicitly. Any construction failure (missing dep, bad url, absent
    collection) returns None so a mirror misconfig can never block the canonical
    CouchDB session-memory builder.
    """

    if str(environ.get("MIRROR_DUAL_WRITE") or "").strip() != "1":
        return None
    url = str(environ.get("QDRANT_URL") or "").strip()
    if not url:
        return None
    try:
        from ..rag_ingress.qdrant_backfill import QdrantSessionMemoryMirrorSink
        from ..rag_ingress.qdrant_docling_mirror import (
            DEFAULT_COLLECTION_NAME,
            PassthroughMarkdownNormalizer,
            build_remote_qdrant_docling_mirror_adapter,
        )
        from ..rag_ingress.qdrant_embedding import build_openai_embedding_provider

        collection = str(environ.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION_NAME).strip()
        adapter = build_remote_qdrant_docling_mirror_adapter(
            url=url,
            collection_name=collection,
            embedding_provider=build_openai_embedding_provider(environ=environ),
            normalizer=PassthroughMarkdownNormalizer(),
            ensure_collection=False,
        )
        return QdrantSessionMemoryMirrorSink(adapter)
    except Exception:
        # Mirror misconfig must never block the canonical builder.
        return None


def _build_qdrant_projector(environ):
    """Build the Qdrant-direct session-memory projector, or None when unbuildable.

    Requires ``QDRANT_URL`` and a reachable, already-existing mirror collection
    (``ensure_collection=False`` -- never create implicitly). Reuses the
    OpenAI-compatible embedding provider (``LLM_BRAIN_EMBEDDING_*``) and the
    passthrough normalizer. Returns None on missing url / unbuildable adapter so the
    caller fails closed (the qdrant backend then reports env_missing and projects
    nothing this run rather than silently dropping sessions).
    """

    url = str(environ.get("QDRANT_URL") or "").strip()
    if not url:
        return None
    try:
        from ..rag_ingress.qdrant_backfill import (
            QdrantSessionMemoryMirrorSink,
            QdrantSessionMemoryProjector,
        )
        from ..rag_ingress.qdrant_docling_mirror import (
            DEFAULT_COLLECTION_NAME,
            PassthroughMarkdownNormalizer,
            build_remote_qdrant_docling_mirror_adapter,
        )
        from ..rag_ingress.qdrant_embedding import build_openai_embedding_provider

        collection = str(environ.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION_NAME).strip()
        adapter = build_remote_qdrant_docling_mirror_adapter(
            url=url,
            collection_name=collection,
            embedding_provider=build_openai_embedding_provider(environ=environ),
            normalizer=PassthroughMarkdownNormalizer(),
            ensure_collection=False,
        )
        return QdrantSessionMemoryProjector(QdrantSessionMemoryMirrorSink(adapter))
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())

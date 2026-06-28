"""Qdrant searchable-mirror BACKFILL core (CouchDB-native; pure compute, injectables).

Backfills the Qdrant searchable mirror from the go-forward recall authority: the
CouchDB source plane. The corpus is every session whose ``projection_state`` is
``projected`` (~3577 sessions on the live host; the retiring ledger
``knowledge_items`` held only ~54 and is no longer used here). The mirror is a
*search surface*, never an authority.

Design (CouchDB-native):
- Enumerate PROJECTED sessions via ``store.find_by_type(PROJECTION_STATE)`` filtered
  to ``projection_status == projected``.
- For each session, ``materialize_session_memory(session_id_hash, store)`` rebuilds
  the body + ``content_hash = sha256(body)`` READ-ONLY (no store write). The mirror
  point carries that ``content_hash`` VERBATIM (NOT recomputed downstream; NOT via
  :func:`build_rag_ready_document`).
- ``privacy_class`` is a uniform ``"private"`` for all points: privacy is not a
  CouchDB-source field and the whole corpus is private session transcripts. The
  CouchDB authority resolver returns no ``privacy_level``, so the authority-join
  privacy check is skipped (its guard needs both sides non-empty).
- The SAME build helper backs both the offline backfill and the live forward sink
  (:class:`QdrantSessionMemoryMirrorSink`), so a backfilled point and a
  forward-mirrored point for the same session share a deterministic point_id
  (idempotent across both paths).

MIRROR-ONLY: this module reads CouchDB (read-only materialize) and upserts Qdrant.
It NEVER writes the CouchDB primary, NEVER writes RAGFlow, and NEVER constructs a
dual-write backend. All emitted/recorded data is redaction-safe: counts, statuses,
and mirror natural-key triples only -- never bodies, raw ids, or secrets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .rag_ready_document import (
    DEFAULT_CONTENT_TYPE,
    RagReadyDocument,
    assert_no_secret_like_metadata,
    build_idempotency_key,
)
from .qdrant_docling_mirror import MirrorDeletionResult

from ..couchdb_source.document_model import (
    RAGFLOW_RECALL_PROFILE,
    ProjectionStatus,
    SourceDocType,
)
from ..couchdb_source.session_memory_materializer import materialize_session_memory

BACKFILL_SCHEMA = "agent_knowledge_qdrant_backfill.v1"
SESSION_MEMORY_DOCUMENT_KIND = "session_memory"
SESSION_MEMORY_ARTIFACT_KIND = "session_memory_mirror_point"
SESSION_MEMORY_SOURCE_ALIAS = "session_memory"
# Uniform privacy class for the whole CouchDB session-transcript corpus.
SESSION_MEMORY_PRIVACY_CLASS = "private"

# on_submit(triple) -> None ; triple is a redaction-safe natural-key record.
OnSubmit = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class BackfillReport:
    """Counts/statuses only -- redaction-safe (no bodies/raw-ids/secrets)."""

    schema_version: str
    dry_run: bool
    candidate_count: int
    authorized_count: int
    submitted_count: int
    skipped_empty_body_count: int
    error_count: int
    submitted: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dry_run": self.dry_run,
            "candidate_count": self.candidate_count,
            "authorized_count": self.authorized_count,
            "submitted_count": self.submitted_count,
            "skipped_empty_body_count": self.skipped_empty_body_count,
            "error_count": self.error_count,
            "submitted": [dict(item) for item in self.submitted],
            # 진실되게: live(non-dry-run) run은 Qdrant upsert로 네트워크를 사용한다.
            # dry-run은 어떤 write도 하지 않으므로 False.
            "network_used": (not self.dry_run),
            # mirror-only: primary/RAGFlow는 항상 쓰지 않는다(항상 False).
            "primary_written": False,
            "ragflow_written": False,
            "raw_ids_printed": False,
            "raw_content_printed": False,
        }


@dataclass(frozen=True)
class RollbackReport:
    """Counts/statuses of a rollback (deletes by recorded natural key)."""

    schema_version: str
    requested_count: int
    deleted_count: int
    absent_count: int
    error_count: int
    statuses: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "requested_count": self.requested_count,
            "deleted_count": self.deleted_count,
            "absent_count": self.absent_count,
            "error_count": self.error_count,
            "statuses": [dict(item) for item in self.statuses],
            # 진실되게: 삭제할 item이 주어지면 rollback은 네트워크로 delete를 수행한다.
            # 요청이 0개면 네트워크를 쓰지 않으므로 False.
            "network_used": (self.requested_count > 0),
            "raw_ids_printed": False,
        }


# ----------------------------------------------------- public-safe body masking

_MIRROR_REDACTED = "[mirror-redacted]"
# A whitespace-delimited token that CONTAINS a private/path marker -> mask the WHOLE
# token (so a path tail like ``/Users/alice/secret`` is removed entirely, not just
# the ``/Users/`` prefix). Mirrors PRIVATE_OUTPUT_RE's path/raw-transcript classes.
_PATH_TOKEN_RE = re.compile(
    r"\S*(?:/Users/|~/|/private/|/Volumes/|[A-Za-z]:\\|\\\\[A-Za-z0-9_.-]+|raw[_ -]?transcript)\S*",
    re.IGNORECASE,
)
# ``Bearer <token>`` and ``NAME_TOKEN=value`` -> mask the marker + its value.
_BEARER_TOKEN_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_SECRET_ASSIGN_TOKEN_RE = re.compile(
    r"\b[A-Z0-9_]*(?:TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)\b\s*[:=]\s*\S*",
    re.IGNORECASE,
)


def public_safe_mask_body(body: str) -> str:
    """Mask residual private/path/secret tokens so the body passes the mirror guard.

    The mirror MUST be public-safe. Standard ingress redaction leaves a tail of
    technical-content false positives (regex backslashes, the literal term
    ``raw_transcript`` in dev-session summaries) and a few real paths that the
    fail-closed mirror guard (``_validate_mirror_text`` -> ``ensure_public_safe``)
    would otherwise REJECT, dropping the whole session-memory from the mirror. We
    instead mask each offending whitespace-token (full token, so path tails cannot
    leak) and keep the rest of the body searchable. The point's ``content_hash``
    stays the verbatim authority key (unchanged), so authority-join is unaffected;
    only the stored/embedded text is masked. Scoped to the session-memory mirror
    sink -- the shared ``ensure_public_safe`` guard and the chunk path are untouched.
    """
    from agent_knowledge.redaction import redact_public_ingress_text
    from .server_runtime import _leak_patterns

    text = redact_public_ingress_text(str(body or ""))
    # 1) Mask every pattern the fail-closed pre-check (``public_ingress_leak_violations``)
    #    scans -- provider transcript paths, local paths, Bearer, basic-auth, credential
    #    URLs, secret assignments -- using the SAME regexes the checker uses, so the
    #    masked body cannot trip the pre-check on the raw body (704).
    for _name, regex in _leak_patterns():
        text = regex.sub(_MIRROR_REDACTED, text)
    # 2) Mask the residual ``ensure_public_safe`` (706) tokens the above leave: regex
    #    backslashes, the ``raw_transcript`` term, Windows/UNC paths -- whole-token so
    #    a path tail cannot survive.
    text = _SECRET_ASSIGN_TOKEN_RE.sub(_MIRROR_REDACTED, text)
    text = _BEARER_TOKEN_RE.sub(_MIRROR_REDACTED, text)
    text = _PATH_TOKEN_RE.sub(_MIRROR_REDACTED, text)
    return text


# ----------------------------------------------------- shared mirror-document build

def derive_mirror_memory_id(content_hash: str) -> str:
    """Derive the SAFE mirror payload ``memory_id`` from the content_hash.

    Legacy ``projection_state.session_memory_knowledge_id`` can be a raw RAGFlow
    document_id; carrying it as the mirror payload would risk a raw-document-id
    leak. Instead derive a public-safe, content-addressed id that is IDENTICAL in
    both the backfill and the forward projector, so the two paths stay idempotent.
    Mirrors :class:`QdrantSessionMemoryProjector`'s ref format.
    """

    return "qdrant_sm:" + str(content_hash or "").split(":")[-1][:16]


def build_session_memory_mirror_document(
    *,
    session_id_hash: str,
    provider: str,
    project: str,
    content_hash: str,
    body: str,
) -> RagReadyDocument:
    """Build the mirror ``RagReadyDocument`` for one projected session-memory.

    Shared by the offline backfill and the live forward sink so both produce the
    SAME deterministic point_id for a given session-memory. Hard requirements:
    - ``content_hash`` is carried VERBATIM (the sha256(body) computed by the
      materializer); not recomputed here, not via :func:`build_rag_ready_document`.
    - ``idempotency_key`` is derived from that content_hash (so point_id is
      idempotent across backfill + forward).
    - ``privacy_class`` is the uniform ``"private"`` (CouchDB-source has no privacy
      field; the corpus is private transcripts).
    - ``memory_id`` is a SAFE content-derived value (never a raw RAGFlow
      document_id), IDENTICAL across backfill + forward; ``project``/``provider``/
      ``session_id_hash`` are promoted to indexed payload fields too.
    """

    if not content_hash:
        raise ValueError("content_hash is required")
    if not provider:
        raise ValueError("provider is required (idempotency source_namespace)")
    if not session_id_hash:
        raise ValueError("session_id_hash is required")

    metadata: dict[str, Any] = {
        # Safe, content-derived id (no raw RAGFlow document_id leak); identical in
        # both backfill and forward so the payload stays consistent + idempotent.
        "memory_id": derive_mirror_memory_id(content_hash),
        "project": str(project or ""),
        "provider": str(provider),
        "session_id_hash": str(session_id_hash),
        "result_type": SESSION_MEMORY_DOCUMENT_KIND,
    }
    assert_no_secret_like_metadata(metadata)

    idempotency_key = build_idempotency_key(
        source_namespace=provider,
        document_kind=SESSION_MEMORY_DOCUMENT_KIND,
        content_hash=content_hash,
    )
    # Direct frozen-dataclass construction so content_hash is preserved verbatim.
    return RagReadyDocument(
        target_profile=RAGFLOW_RECALL_PROFILE,
        document_kind=SESSION_MEMORY_DOCUMENT_KIND,
        artifact_kind=SESSION_MEMORY_ARTIFACT_KIND,
        source_namespace=provider,
        source_alias=SESSION_MEMORY_SOURCE_ALIAS,
        privacy_class=SESSION_MEMORY_PRIVACY_CLASS,
        content_hash=content_hash,
        idempotency_key=idempotency_key,
        # content_hash stays the verbatim authority key; only the stored/embedded
        # text is masked so the public-safe mirror guard accepts it (mask-and-store).
        body=public_safe_mask_body(body),
        filename=f"{SESSION_MEMORY_DOCUMENT_KIND}.md",
        metadata=metadata,
        content_type=DEFAULT_CONTENT_TYPE,
        redaction_version="redaction.v2",
    )


def _submitted_triple(document: RagReadyDocument) -> dict[str, Any]:
    """Redaction-safe natural-key record for rollback / checkpoint (no body/raw-id)."""
    return {
        "target_profile": document.target_profile,
        "idempotency_key": document.idempotency_key,
        "content_hash": document.content_hash,
    }


# ----------------------------------------------------------- concrete forward sink

class QdrantSessionMemoryMirrorSink:
    """``QdrantMirrorSink`` over a :class:`QdrantDoclingMirrorAdapter`.

    Wraps the same build helper the backfill uses, then upserts via the adapter.
    Used as the best-effort forward hook from ``project_session_memory``. Construct
    with a live remote adapter at deploy time; tests inject a fake-client adapter.
    """

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def submit(
        self,
        *,
        session_id_hash: str,
        provider: str,
        project: str,
        content_hash: str,
        body: str,
    ) -> None:
        document = build_session_memory_mirror_document(
            session_id_hash=session_id_hash,
            provider=provider,
            project=project,
            content_hash=content_hash,
            body=body,
        )
        self._adapter.submit_document(document)

    def close(self) -> None:
        closer = getattr(self._adapter, "close", None)
        if callable(closer):
            closer()


class QdrantSessionMemoryProjector:
    """``SessionMemoryProjector`` that writes to the Qdrant mirror as the CANONICAL
    target (RAGFlow-free write path).

    Unlike :class:`QdrantSessionMemoryMirrorSink` used as a best-effort forward hook,
    here Qdrant is the ONLY projection target: a submit failure PROPAGATES so the
    builder records a FAILED projection_state (retried next run) rather than a false
    PROJECTED. Returns a stable ``qdrant_sm:<hash16>`` ref stored as the projection's
    ``session_memory_knowledge_id`` (no RAGFlow document id exists in this path); the
    point itself is keyed by the content-derived point_id, so a later backfill of the
    same session is idempotent regardless of the ref.
    """

    def __init__(self, sink: "QdrantSessionMemoryMirrorSink") -> None:
        self._sink = sink

    def project(self, *, target_profile: str, document: dict[str, Any]) -> str:
        content_hash = str(document.get("content_hash") or "")
        # The mirror payload memory_id is derived from content_hash inside the build
        # helper (identical value), so the sink no longer needs a ref argument. This
        # ref is only stored as the CouchDB projection_state.session_memory_knowledge_id.
        ref = derive_mirror_memory_id(content_hash)
        self._sink.submit(
            session_id_hash=str(document.get("session_id_hash") or ""),
            provider=str(document.get("provider") or ""),
            project=str(document.get("project") or ""),
            content_hash=content_hash,
            body=str(document.get("body") or ""),
        )
        return ref

    def close(self) -> None:
        closer = getattr(self._sink, "close", None)
        if callable(closer):
            closer()


# --------------------------------------------------------------- dim guard (write)

class EmbeddingDimMismatch(ValueError):
    """Raised when the embedding provider dim != the mirror collection dim."""


def assert_embedding_dim_matches_collection(adapter: Any) -> None:
    """Fail-closed pre-flight: provider embedding dim must equal collection dim.

    A mirror upsert whose vector length disagrees with the collection's configured
    vector size is rejected by Qdrant; this surfaces it up front (before any write)
    with a clear error rather than a partial run. When the collection size cannot
    be read it is a no-op (the adapter's per-upsert length check still applies).
    """
    provider_size = getattr(adapter, "embedding_size", None)
    reader = getattr(adapter, "collection_vector_size", None)
    if provider_size is None or not callable(reader):
        return
    collection_size = reader()
    if collection_size is None:
        return
    if int(provider_size) != int(collection_size):
        raise EmbeddingDimMismatch(
            "embedding provider dim does not match mirror collection dim"
        )


# -------------------------------------------------------- CouchDB corpus enumeration

def iter_projected_session_memories(store: Any) -> Iterator[dict[str, Any]]:
    """Yield one descriptor per PROJECTED session-memory (the mirror corpus).

    Enumerates ``projection_state`` docs (the authority predicate: a session-memory
    is current iff its projection_state is ``projected``) and yields the scope a
    mirror point needs. The body + content_hash are NOT read here -- the backfill
    materializes them per session so this stays a cheap, redaction-safe scan.
    """

    # NOTE: session_memory_knowledge_id (legacy raw RAGFlow document_id) is
    # intentionally NOT read here -- the mirror payload memory_id is derived from
    # content_hash inside the build helper, so carrying the raw ref would only risk
    # a raw-document-id leak.
    states = store.find_by_type(
        SourceDocType.PROJECTION_STATE,
        fields=[
            "_id",
            "session_id_hash",
            "provider",
            "project",
            "projection_status",
        ],
    )
    seen: set[str] = set()
    for state in states:
        if str(state.get("projection_status") or "") != ProjectionStatus.PROJECTED:
            continue
        session_id_hash = str(state.get("session_id_hash") or "")
        if not session_id_hash or session_id_hash in seen:
            continue
        seen.add(session_id_hash)
        yield {
            "session_id_hash": session_id_hash,
            "provider": str(state.get("provider") or ""),
            "project": str(state.get("project") or ""),
        }


def backfill_session_memory(
    *,
    store: Any,
    adapter: Any,
    dry_run: bool,
    limit: int | None = None,
    on_submit: OnSubmit | None = None,
    already_submitted: set[str] | None = None,
    concurrency: int = 1,
) -> BackfillReport:
    """Backfill the PROJECTED session-memory corpus into the Qdrant mirror.

    For each PROJECTED session: materialize its body + content_hash READ-ONLY from
    CouchDB, build the mirror document (content_hash verbatim, via the shared
    helper), and -- unless ``dry_run`` -- upsert it via the adapter. Submitted
    natural-key triples are fed to ``on_submit`` (jsonl audit + resume checkpoint)
    and returned in the report. ``already_submitted`` (content_hashes from a
    checkpoint) lets a resumed run skip already-done work; idempotency holds even
    without it (deterministic point_id).

    MIRROR-ONLY: reads CouchDB (read-only materialize) + upserts Qdrant only. Never
    writes the CouchDB primary or RAGFlow.
    """

    already_submitted = already_submitted or set()
    # Fail-closed before any write: a dim mismatch must abort, not partial-run.
    if not dry_run:
        assert_embedding_dim_matches_collection(adapter)

    candidate_count = 0
    authorized_count = 0
    submitted_count = 0
    skipped_empty_body = 0
    error_count = 0
    submitted: list[dict[str, Any]] = []

    def _process(descriptor: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Materialize + build + (unless dry_run) upsert one session-memory.

        Returns ``(status, triple_or_None)`` with status in
        {"submitted","already","skipped_empty","error"}. Pure per-session work with
        no shared mutable state, so it is safe to run concurrently; the caller tallies
        results + calls on_submit serially.
        """
        session_id_hash = descriptor["session_id_hash"]
        try:
            materialized = materialize_session_memory(
                session_id_hash=session_id_hash, store=store
            )
        except Exception:
            return ("error", None)
        # Fail-closed: a session that no longer fully materializes is not mirrored
        # (its projection_state is stale; the read path would also drop it).
        if not materialized.fully_materialized:
            return ("error", None)
        content_hash = str(materialized.content_hash or "")
        if not content_hash:
            return ("error", None)
        if content_hash in already_submitted:
            return ("already", None)
        body = str(materialized.body or "")
        if not body.strip():
            return ("skipped_empty", None)
        try:
            document = build_session_memory_mirror_document(
                session_id_hash=session_id_hash,
                provider=descriptor["provider"],
                project=descriptor["project"],
                content_hash=content_hash,
                body=body,
            )
        except Exception:
            return ("error", None)
        triple = _submitted_triple(document)
        if not dry_run:
            try:
                adapter.submit_document(document)
            except Exception:
                return ("error", None)
        return ("submitted", triple)

    def _tally(status: str, triple: dict[str, Any] | None) -> None:
        nonlocal submitted_count, skipped_empty_body, error_count
        if status == "submitted" and triple is not None:
            submitted_count += 1
            submitted.append(triple)
            if on_submit is not None:
                on_submit(dict(triple))
        elif status == "skipped_empty":
            skipped_empty_body += 1
        elif status == "error":
            error_count += 1
        # "already" is a no-op (resumed run skipping prior work)

    workers = max(1, int(concurrency or 1))
    if workers > 1 and limit is None:
        # Concurrent path (full run): per-session work fans out across a thread pool
        # to overlap embedding/CouchDB latency; results are consumed serially so
        # counters + on_submit need no lock. submit_document is the only writer and
        # the qdrant/openai clients are safe for concurrent use.
        from concurrent.futures import ThreadPoolExecutor

        descriptors = list(iter_projected_session_memories(store))
        candidate_count = len(descriptors)
        authorized_count = candidate_count
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for status, triple in pool.map(_process, descriptors):
                _tally(status, triple)
    else:
        # Sequential path: honors ``limit`` (bounded runs / small-batch-first).
        for descriptor in iter_projected_session_memories(store):
            candidate_count += 1
            if limit is not None and submitted_count >= int(limit):
                continue
            authorized_count += 1
            _tally(*_process(descriptor))

    return BackfillReport(
        schema_version=BACKFILL_SCHEMA,
        dry_run=bool(dry_run),
        candidate_count=candidate_count,
        authorized_count=authorized_count,
        submitted_count=submitted_count,
        skipped_empty_body_count=skipped_empty_body,
        error_count=error_count,
        submitted=submitted,
    )


def rollback_submitted(*, adapter: Any, submitted: list[dict[str, Any]]) -> RollbackReport:
    """Delete exactly the recorded mirror points by their natural-key triples.

    Reversible counterpart to :func:`backfill_session_memory`: each triple is a
    ``(target_profile, idempotency_key, content_hash)`` record produced during the
    run (or read from the jsonl). Deletion is by deterministic point id via
    ``adapter.delete_by_natural_key``; deleting an absent point is a safe no-op
    (``status='absent'``), never an error.
    """

    requested = list(submitted or [])
    deleted = 0
    absent = 0
    error_count = 0
    statuses: list[dict[str, Any]] = []
    for triple in requested:
        if not isinstance(triple, dict):
            # A malformed jsonl line could load as a non-dict; skip it safely
            # instead of aborting the whole rollback with an AttributeError.
            error_count += 1
            statuses.append({"status": "invalid_key", "content_hash": ""})
            continue
        target_profile = str(triple.get("target_profile") or "")
        idempotency_key = str(triple.get("idempotency_key") or "")
        content_hash = str(triple.get("content_hash") or "")
        if not target_profile or not idempotency_key or not content_hash:
            error_count += 1
            statuses.append({"status": "invalid_key", "content_hash": content_hash})
            continue
        try:
            result: MirrorDeletionResult = adapter.delete_by_natural_key(
                target_profile=target_profile,
                idempotency_key=idempotency_key,
                content_hash=content_hash,
                missing_ok=True,
            )
        except Exception:
            error_count += 1
            statuses.append({"status": "error", "content_hash": content_hash})
            continue
        status = str(getattr(result, "status", "") or "")
        if status == "deleted":
            deleted += 1
        elif status == "absent":
            absent += 1
        statuses.append({"status": status, "content_hash": content_hash})

    return RollbackReport(
        schema_version=BACKFILL_SCHEMA,
        requested_count=len(requested),
        deleted_count=deleted,
        absent_count=absent,
        error_count=error_count,
        statuses=statuses,
    )


__all__ = [
    "BACKFILL_SCHEMA",
    "SESSION_MEMORY_DOCUMENT_KIND",
    "SESSION_MEMORY_PRIVACY_CLASS",
    "RAGFLOW_RECALL_PROFILE",
    "BackfillReport",
    "RollbackReport",
    "EmbeddingDimMismatch",
    "OnSubmit",
    "QdrantSessionMemoryMirrorSink",
    "QdrantSessionMemoryProjector",
    "public_safe_mask_body",
    "assert_embedding_dim_matches_collection",
    "derive_mirror_memory_id",
    "build_session_memory_mirror_document",
    "iter_projected_session_memories",
    "backfill_session_memory",
    "rollback_submitted",
]

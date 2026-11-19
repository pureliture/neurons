"""Opt-in composite graph adapter for A-decision shadow search.

Failures are reported in ``details`` while the legacy status is preserved: the
resolver treats ``degraded`` as a global graph outage and would discard the
legacy episodes, which would violate legacy-result preservation.

The legacy adapter remains the default. Shadow results are queried only when an
explicit eligibility decision is supplied, validated against PG authority,
chunk completion/receipt, source currentness, and deduplicated by card id.

This module is trial-only. Its factory never creates a CouchDB/Neo4j client
unless the explicit opt-in flag is set, and the Neo4j adapter only issues MATCH
queries. PG/CouchDB/Neo4j cannot share one transaction; eligibility is
rechecked immediately before merge to narrow revocation/source-change races.
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Iterable, Mapping

from .bulk_full_input import shadow_group_ids
from .models import GraphMemoryResult


def _episode_payload(episode: Any) -> Mapping[str, Any]:
    payload = getattr(episode, "payload", None)
    return payload if isinstance(payload, Mapping) else {}


def _card_identity(episode: Any) -> tuple[str, str] | None:
    payload = _episode_payload(episode)
    sources = payload.get("authority_sources")
    if not isinstance(sources, (list, tuple)) or not sources or not isinstance(sources[0], Mapping):
        return None
    source = sources[0]
    memory_id = str(source.get("authority_memory_id") or "")
    content_hash = str(source.get("content_hash") or "")
    return (memory_id, content_hash) if memory_id and content_hash else None


class PgChunkCompletionReader:
    """Read-only completion lookup; no DDL and no lease mutation."""

    def __init__(self, dsn: str) -> None:
        self._dsn = str(dsn)

    def record(self, key: str) -> dict[str, Any] | None:
        import psycopg

        with psycopg.connect(self._dsn, autocommit=True) as connection:
            row = connection.execute(
                "SELECT project, session_hash, policy_version, source_hash, "
                "receipt, status FROM llm_brain_full_input_chunk_trial "
                "WHERE chunk_key = %s AND status = 'completed'",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "project": str(row[0] or ""),
            "session_hash": str(row[1] or ""),
            "policy_version": str(row[2] or ""),
            "source_hash": str(row[3] or ""),
            "receipt": str(row[4] or ""),
            "status": str(row[5] or ""),
        }

    def completion_receipt(self, key: str) -> str:
        row = self.record(key)
        return str(row["receipt"] or "") if row else ""

    def is_complete(self, key: str) -> bool:
        return self.record(key) is not None


class ShadowSearchAdapter:
    """Merge eligible shadow episodes into an existing graph adapter."""

    def __init__(self, legacy: Any, shadow: Any, *,
                 eligible_groups: Iterable[str] = (),
                 policy_version: str = "",
                 authority_store: Any | None = None,
                 chunk_state: Any | None = None,
                 source_hash_validator: Callable[[str, str, str], bool] | None = None) -> None:
        self.legacy = legacy
        self.shadow = shadow
        self.eligible_groups = tuple(sorted({str(item) for item in eligible_groups if item}))
        self.policy_version = str(policy_version)
        self.authority_store = authority_store
        self.chunk_state = chunk_state
        self.source_hash_validator = source_hash_validator

    def _groups_for(self, brain_id: str) -> tuple[str, ...]:
        if self.policy_version and brain_id.startswith("/project/"):
            return shadow_group_ids(brain_id, self.policy_version)
        return self.eligible_groups

    def _eligibility_available(self) -> bool:
        """Approval/currentness checks are mandatory once shadow is selected."""
        return self.authority_store is not None and self.source_hash_validator is not None

    def _eligible(self, episode: Any, project: str, groups: tuple[str, ...]) -> bool:
        if not self._eligibility_available():
            return False
        payload = _episode_payload(episode)
        sources = payload.get("authority_sources")
        source = sources[0] if isinstance(sources, (list, tuple)) and sources and isinstance(sources[0], Mapping) else {}
        if str(payload.get("shadow_group") or "") not in groups:
            return False
        if str(payload.get("project") or "") != project:
            return False
        if not str(payload.get("receipt") or "") or not str(payload.get("chunk_key") or ""):
            return False

        completion: dict[str, Any] | None = None
        if self.chunk_state is not None:
            key = str(payload.get("chunk_key") or "")
            record = getattr(self.chunk_state, "record", None)
            completion = record(key) if callable(record) else (
                {"receipt": self.chunk_state.completion_receipt(key)}
                if hasattr(self.chunk_state, "completion_receipt")
                else ({} if self.chunk_state.is_complete(key) else None)
            )
            if completion is None:
                return False
            if str(completion.get("receipt") or "") != str(payload.get("receipt") or ""):
                return False
            if completion.get("project") and str(completion["project"]) != project:
                return False
            if completion.get("policy_version") and completion["policy_version"] != self.policy_version:
                return False
            if completion.get("source_hash") and completion["source_hash"] != str(payload.get("source_hash") or ""):
                return False

        source_hash = str(payload.get("source_hash") or "")
        session_hash = str((completion or {}).get("session_hash") or "")
        if not source_hash or not session_hash:
            return False
        try:
            if not self.source_hash_validator(project, source_hash, session_hash):
                return False
        except Exception:
            return False

        memory_id = str(source.get("authority_memory_id") or "")
        content_hash = str(source.get("content_hash") or "")
        cards = self.authority_store.list_authorized_cards(
            project=project, memory_ids=[memory_id], steward_only=True
        ) if memory_id else []
        if not any(str(card.get("content_hash") or "") == content_hash for card in cards):
            return False
        return True

    def _revalidate_before_merge(self, episodes: list[Any], project: str,
                                 groups: tuple[str, ...]) -> list[Any]:
        """Recheck revocable state immediately before merge.

        CouchDB, PostgreSQL and Neo4j cannot share one transaction, so this is a
        best-effort final recheck, not a cross-system atomic snapshot.
        """
        if not self._eligibility_available():
            return []
        kept: list[Any] = []
        seen: set[tuple[str, str]] = set()
        for episode in episodes:
            identity = _card_identity(episode)
            if identity is None or identity in seen:
                continue
            try:
                eligible = self._eligible(episode, project, groups)
            except Exception:
                eligible = False
            if eligible:
                kept.append(episode)
                seen.add(identity)
        return kept

    def search_context(self, *, brain_id: str, query: str, entity_types: Any = None,
                       limit: int = 10, eligible_shadow_groups: Any = None) -> GraphMemoryResult:
        old = self.legacy.search_context(
            brain_id=brain_id, query=query, entity_types=entity_types, limit=limit
        )
        groups = self._groups_for(brain_id)
        if eligible_shadow_groups is not None:
            groups = tuple(sorted({str(item) for item in eligible_shadow_groups if item}))
        if not groups:
            return old
        if not self._eligibility_available():
            details = tuple(dict.fromkeys((*old.details, "shadow_eligibility_unavailable")))
            return GraphMemoryResult(status=old.status, episodes=old.episodes, details=details)

        project = brain_id.removeprefix("/project/")
        try:
            new = self.shadow.search_context(
                brain_id=brain_id, query=query, entity_types=entity_types,
                limit=limit, eligible_shadow_groups=groups,
            )
        except Exception:
            details = tuple(dict.fromkeys((*old.details, "shadow_unavailable")))
            return GraphMemoryResult(status=old.status, episodes=old.episodes, details=details)

        candidates: list[Any] = []
        initially_seen: set[tuple[str, str]] = set()
        for episode in new.episodes:
            identity = _card_identity(episode)
            if identity is None or identity in initially_seen:
                continue
            try:
                initially_eligible = self._eligible(episode, project, groups)
            except Exception:
                initially_eligible = False
            if initially_eligible:
                candidates.append(episode)
                initially_seen.add(identity)
        candidates = self._revalidate_before_merge(candidates, project, groups)
        episodes = list(old.episodes)
        seen = {identity for episode in episodes if (identity := _card_identity(episode))}
        for episode in candidates:
            identity = _card_identity(episode)
            if identity is None or identity in seen:
                continue
            episodes.append(episode)
            seen.add(identity)
        added = len(episodes) - len(old.episodes)
        details = list(dict.fromkeys((*old.details, *new.details)))
        if added:
            details.append("shadow_merge_eligible")
        elif new.episodes:
            details.append("shadow_merge_rejected")
        return GraphMemoryResult(status=old.status, episodes=tuple(episodes), details=tuple(details))


def build_shadow_search_adapter_from_env(
    legacy: Any, pgvector_store: Any | None, environ: Mapping[str, str]
) -> Any:
    """Return legacy unless shadow reads are explicitly eligible.

    Explicit settings: ``NEURON_BULK_SHADOW_READ_ELIGIBLE=1`` plus existing
    policy/PG/Neo4j/CouchDB keys. Missing prerequisites leave legacy unchanged.
    """
    if str(environ.get("NEURON_BULK_SHADOW_READ_ELIGIBLE") or "") != "1":
        return legacy
    policy_version = str(environ.get("LLM_BRAIN_BULK_FULL_INPUT_POLICY_VERSION") or "")
    dsn = str(getattr(pgvector_store, "dsn", "") or environ.get("NEURON_LBRAIN_PGVECTOR_DSN", ""))
    uri = str(environ.get("LLM_BRAIN_NEO4J_URI") or environ.get("NEO4J_URI") or "")
    user = str(environ.get("LLM_BRAIN_NEO4J_USER") or environ.get("NEO4J_USER") or "neo4j")
    password = str(environ.get("LLM_BRAIN_NEO4J_PASSWORD") or environ.get("NEO4J_PASSWORD") or "")
    couch_url = str(environ.get("COUCHDB_URL") or "")
    couch_db = str(environ.get("COUCHDB_DB") or "transcript_source")
    couch_user = str(environ.get("COUCHDB_USER") or "")
    couch_password = str(environ.get("COUCHDB_PASSWORD") or "")
    if not (policy_version and dsn and uri and password and couch_url):
        return legacy
    from .couchdb_source.couchdb_http_store import CouchDBHttpSourceStore
    from .couchdb_source.source_revision import resolve_source_hash_for_currentness
    from .trial_shadow_adapter import TrialShadowGraphAdapter

    credentials = base64.b64encode(f"{couch_user}:{couch_password}".encode("utf-8")).decode("ascii")
    source_store = CouchDBHttpSourceStore(
        base_url=couch_url, db=couch_db, auth_header=f"Basic {credentials}"
    )

    def _source_current(project: str, source_hash: str, session_hash: str) -> bool:
        try:
            current = resolve_source_hash_for_currentness(
                session_id_hash=session_hash, store=source_store
            )
        except Exception:
            return False
        return current == source_hash

    return ShadowSearchAdapter(
        legacy=legacy,
        shadow=TrialShadowGraphAdapter(uri, user, password),
        policy_version=policy_version,
        authority_store=pgvector_store,
        chunk_state=PgChunkCompletionReader(dsn),
        source_hash_validator=_source_current,
    )

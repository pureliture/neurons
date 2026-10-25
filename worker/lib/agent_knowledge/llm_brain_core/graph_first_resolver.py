"""Graph-first, PostgreSQL-authorized public context resolution."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_knowledge.public_safe_util import ensure_public_safe, public_safe_text
from agent_knowledge.mcp_payload import tool_result_bytes
from agent_knowledge.session_memory.brain_query import (
    SEMANTIC_RESULT_MIN_SCORE,
    _card_search_text,
    _finite_score_or_none,
    _query_tokens,
)
from .temporal_envelope import build_temporal_envelope



# 전송 안전 상한이다. 응답 내용을 3KB로 줄이던 제품 상한은 필수 temporal 22필드를
# 잘라내므로 폐기했다. 응답 크기는 limit/next_cursor 페이지로 조절하고, 이 값은
# 클라이언트가 감당할 수 없는 비정상적으로 큰 응답만 막는다.
# Wire budget for a single tool result. Kept below the client-side inline limit
# (Hermes ~50 KB, Codex stricter) so the existing trimming ladder (drop cards,
# shrink text) activates before a client would fail to parse the payload.
# Override via NEURON_LBRAIN_MAX_BYTES for environments that need a different cap.
_MAX_BYTES = int(os.environ.get("NEURON_LBRAIN_MAX_BYTES", "32768"))
_CARD_LIMIT = 100
_EVIDENCE_ROOT_LIMIT = 20
_EVIDENCE_MAX_DEPTH = 5
_EVIDENCE_EDGE_LIMIT = 100
_NON_CARD_ENTITY_TYPES = frozenset({"Session", "SourceRef", "LocalEvidenceEdge"})


class _ResolveRequest(BaseModel):
    """Strict boundary model; callers must not get implicit coercion here."""

    model_config = ConfigDict(extra="forbid", strict=True)

    project: str = Field(min_length=1, max_length=64)
    query: str = Field(default="", max_length=2000)
    mode: Literal["list", "context", "query"] = "query"
    response_mode: Literal["slim", "minimal", "with_evidence"] = "slim"
    as_of: str = Field(default="", max_length=64)
    limit: int = Field(default=5, ge=1, le=20)
    cursor: str | None = Field(default=None, max_length=2048)
    include_historical: bool = Field(default=False)
    max_age: int | None = Field(default=None, ge=0)
    min_age: int | None = Field(default=None, ge=0)
    age_bucket: Literal["fresh", "recent", "aging", "old", "historical", "unknown"] | None = Field(default=None)
    currentness: Literal["current", "superseded", "stale", "retired", "conflicted", "unknown"] | None = Field(default=None)
    time_confidence: Literal["high", "medium", "low", "anomalous", "unknown"] | None = Field(default=None)

    @field_validator("include_historical", mode="before")
    @classmethod
    def _coerce_include_historical(cls, value: Any) -> bool:
        return bool(value) if value is not None else False

    @field_validator("project")
    @classmethod
    def _project_is_not_whitespace(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("project is required")
        return value

    @field_validator("as_of")
    @classmethod
    def _strict_as_of(cls, value: str) -> str:
        if not value:
            return ""
        # The public MCP schema permits a UTC calendar date as well as a full
        # timestamp. Keep the date contract stable by making its instant explicit.
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            parsed_date = None
        if parsed_date is not None and len(value) == 10:
            parsed = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
        else:
            candidate = value.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError as exc:
                raise ValueError("as_of must be an ISO-8601 timestamp") from exc
            if parsed.tzinfo is None:
                raise ValueError("as_of must include a timezone")
        now = datetime.now(timezone.utc)
        if parsed > now + timedelta(minutes=5):
            raise ValueError(
                f"invalid_as_of_future: as_of cannot be more than 5 minutes in the future (requested={parsed.isoformat()}, now={now.isoformat()})"
            )
        return parsed.isoformat()

    @model_validator(mode="after")
    def _validate_request(self) -> "_ResolveRequest":
        if self.mode == "query" and not self.query.strip():
            raise ValueError("query mode requires a non-empty query")
        if self.min_age is not None and self.max_age is not None:
            if self.min_age > self.max_age:
                raise ValueError("min_age must be <= max_age")
        return self


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    fingerprint: str = Field(min_length=43, max_length=43)
    offset: int = Field(ge=0)
    after_id: str | None = Field(default=None, max_length=64)


_Identity = Annotated[str, Field(min_length=1, max_length=64)]
_Hash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class _EvidenceEdge(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    root_id: _Identity
    src_id: _Identity
    dst_id: _Identity
    rel_type: Literal["supersedes", "derived_from", "contradicts", "supports"]
    provenance_hash: _Hash
    src_content_hash: _Hash
    dst_content_hash: _Hash
    depth: int = Field(ge=1, le=5)
    visited_path: list[_Identity] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def _path_matches_edge(self) -> "_EvidenceEdge":
        if (len(self.visited_path) != self.depth + 1 or self.visited_path[0] != self.root_id
                or self.visited_path[-2:] != [self.src_id, self.dst_id]
                or len(set(self.visited_path)) != len(self.visited_path)):
            raise ValueError("invalid evidence path")
        return self


class GraphFirstResolver:
    """Resolve public context without treating the derived graph as authority."""

    def __init__(
        self,
        store: Any,
        graph_adapter: Any | None = None,
        embed_query: Callable[[str], list[float]] | None = None,
    ) -> None:
        self._store = store
        self._graph_adapter = graph_adapter
        self._embed_query = embed_query

    def resolve(
        self,
        *,
        project: str,
        query: str = "",
        mode: str = "query",
        response_mode: Literal["slim", "minimal", "with_evidence"] = "slim",
        as_of: str = "",
        limit: int = 5,
        cursor: str | None = None,
        include_historical: bool = False,
        max_age: int | None = None,
        min_age: int | None = None,
        age_bucket: Literal["fresh", "recent", "aging", "old", "historical", "unknown"] | None = None,
        currentness: Literal["current", "superseded", "stale", "retired", "conflicted", "unknown"] | None = None,
        time_confidence: Literal["high", "medium", "low", "anomalous", "unknown"] | None = None,
    ) -> dict[str, Any]:
        request = _ResolveRequest(
            project=project,
            query=query,
            mode=mode,
            response_mode=response_mode,
            as_of=as_of,
            limit=limit,
            cursor=cursor,
            include_historical=include_historical,
            max_age=max_age,
            min_age=min_age,
            age_bucket=age_bucket,
            currentness=currentness,
            time_confidence=time_confidence,
        )
        if self._store is None:
            return self._error_payload(request, error_code="authority_store_unavailable")
        if request.mode == "list":
            return self._resolve_authority_only(request)
        if request.mode == "context":
            return self._resolve_context(request)
        return self._resolve_query(request)

    def _resolve_authority_only(self, request: _ResolveRequest) -> dict[str, Any]:
        cursor = self._decode_cursor(request.cursor, _fingerprint(request, []))
        if request.cursor and cursor.after_id is None:
            raise ValueError("invalid list cursor")
        try:
            cards = self._store.list_authorized_cards(
                project=request.project,
                memory_ids=None,
                as_of=request.as_of or None,
                limit=request.limit + 1,
                after_memory_id=cursor.after_id,
                steward_only=True,
            )
        except Exception:
            return self._error_payload(request, error_code="authority_store_unavailable")

        lag = None
        if hasattr(self._store, "graph_projection_health"):
            try:
                health = self._store.graph_projection_health(
                    request.project,
                    as_of=request.as_of or None,
                    steward_only=True,
                )
                lag = _lag(health.get("projection_lag_ms"))
            except Exception:
                lag = None
        if lag is None and hasattr(self._store, "projection_lag_ms"):
            lag = _lag(getattr(self._store, "projection_lag_ms"))

        return self._serialize(
            request,
            list(cards),
            metadata=self._metadata(
                retrieval_path="none",
                graph_status="unavailable",
                authority_join_status="verified",
                fallback_used=False,
                projection_lag_ms=lag,
            ),
            keyset=True,
        )

    def _resolve_context(self, request: _ResolveRequest) -> dict[str, Any]:
        try:
            cards = self._store.list_context_cards(
                project=request.project,
                query=request.query,
                as_of=request.as_of or None,
                limit=request.limit + 1,
                steward_only=True,
            )
        except Exception:
            return self._error_payload(request, error_code="authority_store_unavailable")
        retrieval_path = "none"
        fallback_used = False
        topical_tokens = _query_tokens(request.query) - _CONTEXT_TYPE_QUERY_ALIASES
        needs_semantic_fallback = bool(request.query.strip()) and (
            not cards or (bool(topical_tokens) and not _cards_cover_query_tokens(cards, topical_tokens))
        )
        if needs_semantic_fallback:
            if self._embed_query is not None and callable(getattr(self._store, "hybrid_search", None)):
                fallback_cards, _, _, _ = self._fallback_cards(request)
                if fallback_cards:
                    cards = _dedupe_cards([*fallback_cards, *cards])
                    retrieval_path = "pgvector_fallback"
                    fallback_used = True
            if not cards and not request.as_of:
                try:
                    cards = self._store.list_context_cards(
                        project=request.project,
                        query="",
                        as_of=None,
                        limit=request.limit + 1,
                        steward_only=True,
                    )
                except Exception:
                    pass
        return self._serialize(
            request,
            list(cards),
            metadata=self._metadata(
                retrieval_path=retrieval_path,
                graph_status="unavailable",
                authority_join_status="verified",
                fallback_used=fallback_used,
                projection_lag_ms=None,
            ),
        )

    def _resolve_query(self, request: _ResolveRequest) -> dict[str, Any]:
        graph_result: Any | None = None
        if self._graph_adapter is None:
            graph_status = "unavailable"
        else:
            try:
                graph_result = self._graph_adapter.search_context(
                    brain_id=f"/project/{request.project}", query=request.query, limit=_CARD_LIMIT,
                    **({"as_of": request.as_of} if request.as_of else {}),
                )
                graph_status = self._public_graph_status(getattr(graph_result, "status", "unavailable"))
            except Exception:
                graph_status = "unavailable"

        try:
            health = self._store.graph_projection_health(request.project, as_of=request.as_of or None,
                                                         steward_only=True)
            unprojected = bool(health.get("unprojected"))
            lag = _lag(health.get("projection_lag_ms"))
        except Exception:
            return self._error_payload(request, error_code="authority_store_unavailable")

        if graph_status in {"degraded", "unavailable"}:
            # An outage/degraded state takes precedence over projection lag.
            return self._resolve_fallback(request, graph_status=graph_status, lag=lag)

        graph_cards, join_status = self._join_graph_candidates(
            request, tuple(getattr(graph_result, "episodes", ()) or ())
        )
        if graph_cards is None:
            return self._error_payload(request, error_code="authority_store_unavailable")
        if unprojected:
            fallback_cards, error_code, fallback_used, fallback_join = self._fallback_cards(request)
            if fallback_cards is not None:
                cards = _dedupe_cards([*graph_cards, *fallback_cards])
                retrieval_path = "graph_neo4j" if graph_cards else "pgvector_fallback"
                return self._serialize(
                    request,
                    cards,
                    metadata=self._metadata(
                        retrieval_path=retrieval_path,
                        graph_status="projection_lag",
                        authority_join_status=join_status if join_status != "verified" else fallback_join,
                        fallback_used=fallback_used,
                        projection_lag_ms=lag,
                    ),
                )
            if graph_cards:
                payload = self._serialize(
                    request,
                    graph_cards,
                    metadata=self._metadata(
                        retrieval_path="graph_neo4j",
                        graph_status="projection_lag",
                        authority_join_status=join_status,
                        fallback_used=fallback_used,
                        projection_lag_ms=lag,
                    ),
                    error_code=error_code,
                )
                return payload
            return self._error_payload(
                request,
                error_code=error_code or "authority_store_unavailable",
                graph_status="projection_lag",
                lag=lag,
            )
        if (
            not graph_cards
            and join_status == "verified"
            and not request.as_of
            and self._embed_query is not None
            and callable(getattr(self._store, "search_session_chunks", None))
        ):
            fallback_cards, _, fallback_used, fallback_join = self._fallback_cards(request)
            if fallback_cards:
                return self._serialize(
                    request,
                    fallback_cards,
                    metadata=self._metadata(
                        retrieval_path="pgvector_fallback",
                        graph_status="available",
                        authority_join_status=fallback_join,
                        fallback_used=fallback_used,
                        projection_lag_ms=lag,
                    ),
                )
        auth_src = "postgres" if any(c.get("authority_source") == "postgres" for c in graph_cards) else None
        return self._serialize(
            request,
            graph_cards,
            metadata=self._metadata(
                retrieval_path="graph_neo4j",
                graph_status="available",
                authority_join_status=join_status,
                fallback_used=False,
                projection_lag_ms=lag,
                authority_source=auth_src,
            ),
        )

    def _join_graph_candidates(
        self, request: _ResolveRequest, episodes: tuple[Any, ...]
    ) -> tuple[list[dict[str, Any]] | None, str]:
        keys: list[tuple[str, str] | None] = []
        ids: list[str] = []
        saw_missing = False
        instant = _parse_temporal_instant(request.as_of) or datetime.now(timezone.utc)
        graph_temporal_by_id: dict[str, dict[str, Any]] = {}
        for episode in episodes:
            payload = getattr(episode, "payload", None)
            payload = payload if isinstance(payload, Mapping) else {}
            entity_type = str(getattr(episode, "entity_type", "") or "")
            if entity_type == "GraphFact":
                if not _fact_valid_at(episode, instant):
                    continue
                sources = payload.get("authority_sources")
                if not isinstance(sources, list) or not sources:
                    source_entity_types = payload.get("source_entity_types")
                    if (
                        isinstance(source_entity_types, list)
                        and source_entity_types
                        and all(str(item) in _NON_CARD_ENTITY_TYPES for item in source_entity_types)
                    ):
                        continue
                    sources = [{}]
            elif entity_type in _NON_CARD_ENTITY_TYPES:
                continue
            else:
                sources = [payload]
            for source in sources[:_CARD_LIMIT]:
                source = source if isinstance(source, Mapping) else {}
                memory_id = source.get("authority_memory_id")
                content_hash = source.get("content_hash")
                if not isinstance(memory_id, str) or not memory_id or not isinstance(content_hash, str) or not content_hash:
                    keys.append(None)
                    saw_missing = True
                    continue
                if memory_id not in ids:
                    if len(ids) >= _CARD_LIMIT:
                        continue
                    ids.append(memory_id)
                keys.append((memory_id, content_hash))

                if memory_id not in graph_temporal_by_id:
                    g_vf = (
                        source.get("valid_from")
                        if "valid_from" in source
                        else payload.get("valid_from")
                        if "valid_from" in payload
                        else getattr(episode, "valid_from", None)
                    )
                    has_vf = g_vf is not None and g_vf != ""

                    g_vt = (
                        source.get("valid_to")
                        if "valid_to" in source
                        else payload.get("valid_to")
                        if "valid_to" in payload
                        else getattr(episode, "valid_to", None)
                    )
                    has_vt = g_vt is not None and g_vt != ""

                    ep_curr = getattr(episode, "currentness", None)
                    g_curr = (
                        source.get("currentness")
                        if "currentness" in source
                        else payload.get("currentness")
                        if "currentness" in payload
                        else ep_curr
                    )
                    has_curr = g_curr is not None and g_curr not in ("", "unknown")

                    graph_temporal_by_id[memory_id] = {
                        "has_valid_from": has_vf,
                        "valid_from": g_vf,
                        "has_valid_to": has_vt,
                        "valid_to": g_vt,
                        "has_currentness": has_curr,
                        "currentness": g_curr,
                    }

        if not ids:
            return [], "unavailable" if saw_missing else "verified"
        try:
            authorized = self._store.list_authorized_cards(
                project=request.project,
                memory_ids=ids,
                as_of=request.as_of or None,
                limit=_CARD_LIMIT,
                steward_only=True,
            )
        except Exception:
            return None, "unavailable"
        by_id = {str(card.get("memory_id") or ""): dict(card) for card in authorized}
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        saw_mismatch = False
        for key in keys:
            if key is None:
                continue
            memory_id, content_hash = key
            card = by_id.get(memory_id)
            if card is None or str(card.get("content_hash") or "") != content_hash:
                saw_mismatch = True
                continue
            if memory_id not in seen:
                # FR-AGE-060, FR-AGE-065, AC-AGE-004:
                # Check for temporal mismatch between Graph episode and PostgreSQL Card
                g_meta = graph_temporal_by_id.get(memory_id)
                has_temporal_conflict = False
                if g_meta:
                    if g_meta.get("has_valid_from") and _temporal_instants_differ(g_meta.get("valid_from"), card.get("valid_from")):
                        has_temporal_conflict = True
                    elif g_meta.get("has_valid_to") and _temporal_instants_differ(g_meta.get("valid_to"), card.get("valid_to")):
                        has_temporal_conflict = True
                    elif g_meta.get("has_currentness"):
                        g_c = str(g_meta.get("currentness") or "").strip().lower()
                        pg_c = str(card.get("currentness") or "current").strip().lower()
                        if g_c and g_c != pg_c:
                            has_temporal_conflict = True

                if has_temporal_conflict:
                    # Graph is a cold projection (derived index), not the authority.
                    # PG is the authority. Graph-PG temporal differences are expected
                    # during cold projection and should NOT be flagged as conflicts
                    # (they produce noise, not signal). Only PG-level conflicts
                    # (currentness=="conflicted") are flagged.
                    card["authority_source"] = "postgres"
                if card.get("currentness") == "conflicted":
                    card["temporal_conflict"] = True

                result.append(card)
                seen.add(memory_id)
        if result:
            # A partial canonical join must remain observable: returning some
            # verified cards does not erase a missing or changed graph key.
            if saw_mismatch:
                return result, "mismatch"
            if saw_missing:
                return result, "unavailable"
            return result, "verified"
        if saw_mismatch:
            return [], "mismatch"
        return [], "unavailable" if saw_missing else "verified"

    def _resolve_fallback(self, request: _ResolveRequest, *, graph_status: str, lag: int | None) -> dict[str, Any]:
        cards, error_code, fallback_used, join_status = self._fallback_cards(request)
        if cards is None:
            return self._error_payload(request, error_code=error_code or "authority_store_unavailable", graph_status=graph_status, lag=lag)
        return self._serialize(
            request,
            cards,
            metadata=self._metadata(
                retrieval_path="pgvector_fallback",
                graph_status=graph_status,
                authority_join_status=join_status,
                fallback_used=fallback_used,
                projection_lag_ms=lag,
            ),
        )

    def _fallback_cards(self, request: _ResolveRequest) -> tuple[list[dict[str, Any]] | None, str | None, bool, str]:
        if self._embed_query is None:
            return None, "embedding_unavailable", False, "unavailable"
        try:
            vector = self._embed_query(request.query)
        except Exception:
            return None, "embedding_unavailable", False, "unavailable"
        try:
            ranked = self._store.hybrid_search(
                project=request.project,
                query_vector=vector,
                text_query=request.query,
                limit=_CARD_LIMIT,
                as_of=request.as_of or None,
                steward_only=True,
            )
        except Exception:
            return None, "authority_store_unavailable", True, "unavailable"
        # Reuse brain.query's lexical coverage and finite semantic threshold.
        # SQL's blended ranking score is not the raw cosine similarity.
        tokens = _query_tokens(request.query)
        threshold = max(1, (len(tokens) + 1) // 2)
        ranked = [
            card for card in ranked
            if len(tokens & _query_tokens(_card_search_text(card))) >= threshold
            or (_finite_score_or_none(card.get("semantic_score", card.get("similarity_score"))) or 0.0)
            >= SEMANTIC_RESULT_MIN_SCORE
        ]
        rank_keys = [
            (str(card.get("memory_id") or ""), str(card.get("content_hash") or ""))
            for card in ranked
        ]
        rank_keys = list(dict.fromkeys(key for key in rank_keys if key[0] and key[1]))
        try:
            authorized = self._store.list_authorized_cards(
                project=request.project,
                memory_ids=[memory_id for memory_id, _ in rank_keys],
                as_of=request.as_of or None,
                limit=_CARD_LIMIT,
                steward_only=True,
            )
        except Exception:
            return None, "authority_store_unavailable", True, "unavailable"
        by_id = {str(card.get("memory_id") or ""): dict(card) for card in authorized}
        # A vector hit is only a ranking hint. Recheck its hash after the
        # authority round trip so a concurrent replacement cannot leak stale data.
        cards = [
            by_id[memory_id]
            for memory_id, content_hash in rank_keys
            if memory_id in by_id and str(by_id[memory_id].get("content_hash") or "") == content_hash
        ]
        join_status = "verified" if len(cards) == len(ranked) else "mismatch"
        if not cards and callable(getattr(self._store, "search_session_chunks", None)):
            chunk_cards = self._session_chunk_cards(request, vector)
            if chunk_cards:
                return chunk_cards, None, True, join_status
        return cards, None, True, join_status

    def _session_chunk_cards(
        self, request: _ResolveRequest, vector: list[float] | None = None
    ) -> list[dict[str, Any]]:
        if (
            request.response_mode == "with_evidence"
            or not callable(getattr(self._store, "search_session_chunks", None))
        ):
            return []
        if vector is None:
            if self._embed_query is None:
                return []
            try:
                vector = self._embed_query(request.query)
            except Exception:
                return []
        try:
            chunks = self._store.search_session_chunks(
                vector,
                project=request.project,
                limit=max(request.limit * 2, 10),
            )
        except Exception:
            return []
        tokens = _query_tokens(request.query)
        threshold = max(1, (len(tokens) + 1) // 3) if tokens else 1
        results: list[dict[str, Any]] = []
        for row in chunks or ():
            if not isinstance(row, Mapping):
                continue
            text = str(row.get("content_markdown") or row.get("content") or "").strip()
            if not text:
                continue
            distance = _finite_score_or_none(row.get("distance"))
            similarity = (
                (1.0 - distance)
                if distance is not None
                else (_finite_score_or_none(row.get("similarity_score", row.get("score"))) or 0.0)
            )
            lexical_hits = len(tokens & _query_tokens(text)) if tokens else 0
            if similarity < 0.45 and lexical_hits < threshold:
                continue
            chunk_id = str(row.get("chunk_id") or "")
            if not chunk_id:
                continue
            content_hash = str(row.get("content_hash") or "")
            if not content_hash.startswith("sha256:"):
                content_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
            body = _session_chunk_body(text)
            safe_summary = _safe(body, 65536)
            if not safe_summary:
                continue
            first_line = next((line.strip("#* -") for line in safe_summary.splitlines() if line.strip("#* -")), "")
            title = _safe(first_line or f"{request.project} session context", 1000)
            chunk_card: dict[str, Any] = {
                "memory_id": f"chunk:{chunk_id[:56]}",
                "project": str(row.get("project") or request.project),
                "card_type": "status",
                "title": title,
                "summary": safe_summary,
                "typed_payload": {
                    "status": safe_summary,
                    "status_value": safe_summary,
                },
                "currentness": str(row.get("currentness") or "current"),
                "content_hash": content_hash,
            }
            if row.get("observed_at") is not None:
                chunk_card["observed_at"] = row.get("observed_at")
            if row.get("created_at") is not None:
                chunk_card["created_at"] = row.get("created_at")
            results.append(chunk_card)
            if len(results) >= request.limit + 1:
                break
        return results

    def _serialize(
        self,
        request: _ResolveRequest,
        cards: list[dict[str, Any]],
        *,
        metadata: dict[str, Any],
        error_code: str | None = None,
        keyset: bool = False,
    ) -> dict[str, Any]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for card in cards:
            memory_id = str(card.get("memory_id") or card.get("id") or "")
            if not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            deduped.append(_copy_card(card))
        _detect_cross_card_conflicts(deduped, request.as_of)
        if any(c.get("authority_source") == "postgres" for c in deduped):
            metadata = {**metadata, "authority_source": "postgres"}

        lag_ms = metadata.get("projection_lag_ms") if metadata else None
        proj_lag_sec = int(lag_ms / 1000) if lag_ms is not None else None
        filtered = [
            c for c in deduped
            if _card_matches_filters(c, request, as_of=request.as_of, projection_lag_seconds=proj_lag_sec)
        ]

        # FR-AGE-075: Demote anomalous items so they are not ranked ahead of valid items.
        # Stable sort preserves relative order among valid items and among anomalous items.
        def _is_card_anomalous(c: Mapping[str, Any]) -> int:
            t = c.get("_temporal") if isinstance(c, Mapping) else None
            if t is None:
                t = c.get("temporal") if isinstance(c, Mapping) else None
            if t is None:
                t = _card_temporal_block(c, request.as_of, projection_lag_seconds=proj_lag_sec)
            if isinstance(t, Mapping):
                age_info = t.get("age") if isinstance(t.get("age"), Mapping) else {}
                if (
                    t.get("time_confidence") == "anomalous"
                    or age_info.get("confidence") == "anomalous"
                    or age_info.get("label") == "future"
                    or t.get("temporal_relation") == "future"
                ):
                    return 1
            if isinstance(c, Mapping) and c.get("time_confidence") == "anomalous":
                return 1
            return 0

        filtered.sort(key=_is_card_anomalous)

        fingerprint = _fingerprint(request, [] if keyset else filtered)
        cursor = self._decode_cursor(request.cursor, fingerprint)
        offset = 0 if keyset else cursor.offset
        page = filtered[offset : offset + request.limit]
        evidence: dict[str, Any] | None = None
        if request.response_mode == "with_evidence":
            try:
                evidence = self._read_evidence(request, page)
            except Exception:
                # Evidence is an authority-bearing extension. Do not return the
                # cards as a successful evidence response when its PG check fails.
                return self._error_payload(request, error_code="evidence_unavailable")
        payload = self._final_payload(request, page, metadata, offset, len(filtered), fingerprint, error_code, keyset, evidence)
        # 선택적 evidence 확장을 먼저 줄여 기본 결정/요약을 보존한다.
        while tool_result_bytes(payload) > _MAX_BYTES and _drop_last_evidence_edge(evidence, page):
            payload = self._final_payload(request, page, metadata, offset, len(filtered), fingerprint, error_code, keyset, evidence)
        if request.mode == "context":
            # Fit inside the wire budget without destroying the meaning of the
            # context: bound readable text first, then drop the lowest-priority
            # cards. Emptying section text leaves an agent with ids and hashes but
            # no work subject, decision, status or next action.
            while tool_result_bytes(payload) > _MAX_BYTES and _trim_context_text(page):
                payload = self._final_payload(request, page, metadata, offset, len(filtered), fingerprint, error_code, keyset, evidence)
            while tool_result_bytes(payload) > _MAX_BYTES and _drop_context_card(page):
                payload = self._final_payload(request, page, metadata, offset, len(filtered), fingerprint, error_code, keyset, evidence)
            # Last resort: keep the page bounded and let the cursor carry the
            # remainder instead of failing the entire default context call.
            while tool_result_bytes(payload) > _MAX_BYTES and len(page) > 1:
                page.pop()
                payload = self._final_payload(request, page, metadata, offset, len(filtered), fingerprint, error_code, keyset, evidence)
        else:
            while tool_result_bytes(payload) > _MAX_BYTES and len(page) > 1:
                page.pop()
                payload = self._final_payload(request, page, metadata, offset, len(filtered), fingerprint, error_code, keyset, evidence)
            while tool_result_bytes(payload) > _MAX_BYTES and page and _shrink_card(page[0]):
                payload = self._final_payload(request, page, metadata, offset, len(filtered), fingerprint, error_code, keyset, evidence)
        if tool_result_bytes(payload) > _MAX_BYTES:
            return self._error_payload(request, error_code="response_budget_exceeded")
        ensure_public_safe(payload, "GraphFirstResolver response")
        return payload

    def _final_payload(
        self,
        request: _ResolveRequest,
        page: list[dict[str, Any]],
        metadata: dict[str, Any],
        offset: int,
        total: int,
        fingerprint: str,
        error_code: str | None,
        keyset: bool,
        evidence: dict[str, Any] | None,
    ) -> dict[str, Any]:
        next_offset = offset + len(page)
        has_more = next_offset < total
        payload = self._payload(request, page, metadata, has_more=has_more, error_code=error_code, evidence=evidence)
        after_id = str(page[-1]["memory_id"]) if keyset and page else None
        payload["next_cursor"] = _encode_cursor(fingerprint, next_offset, after_id) if has_more else None
        return payload

    def _payload(
        self,
        request: _ResolveRequest,
        cards: list[dict[str, Any]],
        metadata: dict[str, Any],
        *,
        has_more: bool,
        error_code: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _detect_cross_card_conflicts(cards, request.as_of)
        if any(c.get("authority_source") == "postgres" for c in cards):
            metadata = {**metadata, "authority_source": "postgres"}
        decisions: list[dict[str, Any]] = []
        preferences: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        evidence_hashes: list[str] = []
        lag_ms = metadata.get("projection_lag_ms") if metadata else None
        proj_lag_sec = int(lag_ms / 1000) if lag_ms is not None else None

        minimal = request.response_mode == "minimal"
        for card in cards:
            context_mode = request.mode == "context"
            item = _context_item(card) if context_mode else _item(card)
            section_identity = {"id": item["id"]} if context_mode else {k: v for k, v in item.items() if k != "typed_payload"}
            # Temporal envelope (FR-AGE): only on the non-context (slim) item,
            # so the compact context lane stays minimal.
            if not context_mode:
                full_temporal = (
                    card.get("_temporal")
                    if isinstance(card, Mapping) and card.get("_temporal") is not None
                    else _card_temporal_block(
                        card,
                        request.as_of,
                        projection_lag_seconds=proj_lag_sec,
                    )
                )
                item["temporal"] = (
                    _minimal_temporal_block(full_temporal) if minimal else full_temporal
                )
            # Minimal mode: drop diagnostic/heavy fields to keep the payload small.
            if minimal and not context_mode:
                for heavy in ("typed_payload", "content_hash", "created_at", "authority_source"):
                    item.pop(heavy, None)
            items.append(item)
            raw_typed = card.get("typed_payload")
            typed: dict[str, Any] = dict(raw_typed) if isinstance(raw_typed, Mapping) else {}
            card_type = str(card.get("card_type") or "")
            if card_type == "decision":
                decision_entry = {**section_identity, "decision": _safe(typed.get("decision") or card.get("summary") or card.get("title"), 65536)}
                if not minimal or context_mode:
                    rationale = _safe(typed.get("rationale"), 65536)
                    if rationale:
                        decision_entry["rationale"] = rationale
                decisions.append(decision_entry)
            elif card_type == "preference":
                pref_entry = {**section_identity, "rule": _safe(typed.get("rule") or typed.get("preference") or card.get("summary") or card.get("title"), 65536)}
                if not minimal or context_mode:
                    pref_entry["scope"] = _safe(typed.get("scope") or "general", 256)
                preferences.append(pref_entry)
            elif card_type in {"task", "work", "work_unit"}:
                # Subject, state and next step are three different facts. Reading only
                # `task_state` hides what the work is and what to do next whenever the
                # state is a status token such as "in_progress".
                subject = _safe(_task_subject(card, typed), 65536)
                task_entry = {**section_identity, "task": subject}
                state = _safe(typed.get("task_state") or typed.get("status"), 256)
                # Repeating the subject as its own state only costs wire budget.
                if state and state != subject:
                    task_entry["task_state"] = state
                if not minimal or context_mode:
                    next_action = _safe(typed.get("next_action"), 65536)
                    if next_action:
                        task_entry["next_action"] = next_action
                    blocker = _safe(typed.get("blocker"), 65536)
                    if blocker:
                        task_entry["blocker"] = blocker
                tasks.append(task_entry)
            elif card_type == "status":
                subject = _safe(_status_subject(card, typed), 65536)
                status_entry = {
                    **section_identity,
                    "status": subject,
                }
                state = _safe(typed.get("status_value") or typed.get("status"), 256)
                if state and state != subject and state.strip().lower() in _STATUS_STATE_TOKENS:
                    status_entry["status_state"] = state
                if not minimal or context_mode:
                    observed_at = _safe(typed.get("observed_at"), 256)
                    if observed_at:
                        status_entry["observed_at"] = observed_at
                statuses.append(status_entry)
            content_hash = str(card.get("content_hash") or "")
            if content_hash:
                evidence_hashes.append(content_hash)
        # Temporal coverage metrics (FR-AGE-064): how many returned items carry
        # observed time, how many are unknown, and how many are anomalous.
        # Only added outside the tight context lane to preserve the wire budget.
        _temporal_coverage = None
        if request.mode != "context":
            _observed = sum(1 for it in items if (it.get("temporal") or {}).get("observed_at"))
            _unknown = sum(1 for it in items if (it.get("temporal") or {}).get("age", {}).get("confidence") == "unknown")
            _anomalous = sum(1 for it in items if (it.get("temporal") or {}).get("age", {}).get("confidence") == "anomalous")
            _temporal_coverage = {
                "items": len(items),
                "with_observed_at": _observed,
                "unknown_age": _unknown,
                "anomalous_time": _anomalous,
            }
        payload: dict[str, Any] = {
            "schema_version": "lbrain_slim_context.v1",
            "project": request.project,
            "decisions": decisions,
            "preferences": preferences,
            "tasks": tasks,
            "statuses": statuses,
            "items": items,
            "active_guardrails": [preference["rule"] for preference in preferences],
            "metadata": {
                **metadata,
                "has_more": has_more,
                **({"temporal_coverage": _temporal_coverage} if _temporal_coverage is not None else {}),
                **({"temporal_view": "as_of", "as_of": request.as_of} if request.as_of else {}),
            },
            "has_more": has_more,
            "next_cursor": None,
        }
        if request.as_of:
            payload["temporal_view"] = "as_of"
        if request.response_mode == "with_evidence":
            payload["evidence_hashes"] = list(dict.fromkeys(evidence_hashes))
            payload["evidence"] = _evidence_payload(evidence, cards)
            payload["metadata"] = {
                **payload["metadata"],
                "evidence_authority": "postgresql_explicit_edges",
                "evidence_max_depth": _EVIDENCE_MAX_DEPTH,
                "evidence_truncated": bool((evidence or {}).get("truncated")),
            }
        if error_code:
            payload["error_code"] = error_code
        return payload

    def _read_evidence(self, request: _ResolveRequest, page: list[dict[str, Any]]) -> dict[str, Any]:
        root_ids = [
            str(card.get("memory_id") or card.get("id") or "")
            for card in page
            if str(card.get("memory_id") or card.get("id") or "")
        ]
        if not root_ids:
            return {"edges": [], "truncated": False, "max_depth": _EVIDENCE_MAX_DEPTH}
        # This is intentionally one bounded batch call. The serializer may later
        # shrink the card page, but it must never re-query inside that loop.
        result = self._store.read_authorized_evidence(
            project=request.project,
            root_memory_ids=root_ids[:_EVIDENCE_ROOT_LIMIT],
            as_of=request.as_of or None,
            max_depth=_EVIDENCE_MAX_DEPTH,
            limit=_EVIDENCE_EDGE_LIMIT,
        )
        if not isinstance(result, Mapping):
            raise ValueError("invalid evidence result")
        expected_roots = {str(card.get("memory_id") or card.get("id")): card.get("content_hash") for card in page}
        if result.get("root_hashes") != expected_roots:
            raise ValueError("authority changed while reading evidence")
        edges = result.get("edges")
        if not isinstance(edges, list):
            raise ValueError("invalid evidence edges")
        normalized = _normalized_evidence_edges(edges, set(root_ids))
        return {
            "edges": normalized,
            "truncated": bool(result.get("truncated")) or len(edges) > len(normalized),
            "max_depth": _EVIDENCE_MAX_DEPTH,
        }

    def _error_payload(self, request: _ResolveRequest, *, error_code: str, graph_status: str = "unavailable", lag: int | None = None) -> dict[str, Any]:
        payload = self._payload(
            request,
            [],
            self._metadata("none", graph_status, "unavailable", False, lag),
            has_more=False,
            error_code=error_code,
        )
        ensure_public_safe(payload, "GraphFirstResolver error")
        return payload

    @staticmethod
    def _public_graph_status(status: Any) -> Literal["available", "degraded", "unavailable"]:
        if status == "available":
            return "available"
        if status == "degraded":
            return "degraded"
        return "unavailable"

    @staticmethod
    def _metadata(
        retrieval_path: str,
        graph_status: str,
        authority_join_status: str,
        fallback_used: bool,
        projection_lag_ms: int | None,
        authority_source: str | None = None,
    ) -> dict[str, Any]:
        meta = {
            "retrieval_path": retrieval_path,
            "graph_status": graph_status,
            "authority_join_status": authority_join_status,
            "fallback_used": fallback_used,
            "projection_lag_ms": projection_lag_ms,
        }
        if authority_source is not None:
            meta["authority_source"] = authority_source
        return meta

    @staticmethod
    def _decode_cursor(cursor: str | None, fingerprint: str) -> _Cursor:
        if not cursor:
            return _Cursor(fingerprint=fingerprint, offset=0)
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if not isinstance(value, list) or len(value) != 3:
                raise ValueError("invalid cursor shape")
            decoded = _Cursor.model_validate(dict(zip(("fingerprint", "offset", "after_id"), value)))
        except Exception as exc:
            raise ValueError("invalid cursor") from exc
        if decoded.fingerprint != fingerprint:
            raise ValueError("cursor does not match this request or candidates")
        return decoded


def _parse_temporal_instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _temporal_instants_differ(val_a: Any, val_b: Any, tolerance_seconds: float = 1.0) -> bool:
    """Check if two temporal instant values differ beyond tolerance (FR-AGE-060)."""
    dt_a = _parse_temporal_instant(val_a)
    dt_b = _parse_temporal_instant(val_b)
    if dt_a is not None and dt_b is not None:
        return abs((dt_a - dt_b).total_seconds()) > tolerance_seconds
    if dt_a is not None or dt_b is not None:
        return True
    s_a = str(val_a).strip() if val_a is not None else ""
    s_b = str(val_b).strip() if val_b is not None else ""
    if s_a and s_b:
        return s_a != s_b
    return bool(s_a or s_b)


def _evaluate_card_decision_table(
    *,
    bucket: str,
    currentness: str,
    confidence: str,
    reason: str | None,
    freshness_state: str,
    temporal_conflict: bool,
) -> tuple[str, str]:
    """Map card temporal attributes to Section 7.1 Decision Table (display_label, recommended_action)."""
    # Row 12: Concurrently conflicted or explicit conflicted state
    if temporal_conflict or currentness == "conflicted":
        return "현재 항목 간 충돌", "review_conflict"

    # Row 11: Future / anomalous timestamp
    if confidence == "anomalous" or bucket == "future" or (reason and "observed_after" in reason):
        return "시간 이상", "ask_user"

    # Rows 9 & 10: Unknown time or authority
    if confidence == "unknown" or currentness == "unknown":
        if currentness == "current":
            return "현재 권위이나 나이 불명", "ask_user"
        return "시간/권위 불명", "ask_user"

    # Row 4 & Row 7: Superseded
    if currentness == "superseded":
        label = "최근이지만 이미 폐기됨" if bucket == "fresh" else "폐기됨·대체 있음"
        return label, "prefer_replacement"

    # Row 6: Retired
    if currentness == "retired":
        return "의도적으로 폐기됨", "prefer_replacement"

    # Row 5: Stale
    if currentness == "stale":
        return "신뢰 철회됨", "treat_as_historical"

    # Rows 1, 2, 3, 8: Current
    if freshness_state == "verified_changed":
        return "현재 권위이나 소스 변경됨", "verify_or_supersede"
    if freshness_state == "check_due":
        return "오래됨·재검증 필요", "use_with_verification"
    if bucket in ("old", "historical") and freshness_state == "unchecked":
        return "오래되었지만 현재 유효", "use_with_verification"
    if freshness_state == "verified_current":
        return "최신·검증됨", "use"

    label = "최신·검증됨" if bucket == "fresh" else "현재 유효"
    return label, "use"


def _detect_cross_card_conflicts(cards: list[dict[str, Any]], as_of: str = "") -> None:
    """Detect cross-card temporal conflicts and supersede relationships in-place."""
    if not cards:
        return

    instant = _parse_temporal_instant(as_of) or datetime.now(timezone.utc)

    cards_by_id: dict[str, dict[str, Any]] = {}
    for card in cards:
        mid = str(card.get("memory_id") or card.get("id") or "")
        if mid:
            cards_by_id[mid] = card

    def _is_active_at(c: Mapping[str, Any]) -> bool:
        v_from = c.get("valid_from")
        v_to = c.get("valid_to")
        if v_from:
            dt_from = _parse_temporal_instant(v_from)
            if dt_from is not None and dt_from > instant:
                return False
        if v_to:
            dt_to = _parse_temporal_instant(v_to)
            if dt_to is not None and dt_to <= instant:
                return False
        return True

    # 1. Supersede relationships
    for mid, card in list(cards_by_id.items()):
        sup_by = card.get("superseded_by")
        if not isinstance(sup_by, list):
            sup_by = [sup_by] if sup_by else []
            card["superseded_by"] = sup_by

        for repl_id in sup_by:
            if repl_id in cards_by_id:
                repl_card = cards_by_id[repl_id]
                a_active = _is_active_at(card) and card.get("currentness") in ("current", "conflicted")
                b_active = _is_active_at(repl_card) and repl_card.get("currentness") in ("current", "conflicted")
                if a_active and b_active:
                    card["temporal_conflict"] = True
                    repl_card["temporal_conflict"] = True

    # 2. Subject / Scope overlap conflict
    def _subject_key(c: Mapping[str, Any]) -> str | None:
        typed = c.get("typed_payload")
        typed_dict = typed if isinstance(typed, Mapping) else {}
        scope = typed_dict.get("scope") or typed_dict.get("subject")
        if scope and str(scope).strip().lower() not in ("general", "*", ""):
            return str(scope).strip().lower()
        return None

    for i in range(len(cards)):
        c1 = cards[i]
        c1_id = str(c1.get("memory_id") or c1.get("id") or "")
        c1_curr = str(c1.get("currentness") or "current")
        if c1_curr == "conflicted":
            c1["temporal_conflict"] = True

        for j in range(i + 1, len(cards)):
            c2 = cards[j]
            c2_id = str(c2.get("memory_id") or c2.get("id") or "")
            c2_curr = str(c2.get("currentness") or "current")

            c1_sup = c1.get("superseded_by") or []
            c2_sup = c2.get("superseded_by") or []
            is_superseded_pair = (c2_id in c1_sup) or (c1_id in c2_sup)

            s1 = _subject_key(c1)
            s2 = _subject_key(c2)
            same_subject = (s1 is not None and s2 is not None and s1 == s2)

            if is_superseded_pair or same_subject:
                if (c1_curr in ("current", "conflicted") and c2_curr in ("current", "conflicted")
                    and _is_active_at(c1) and _is_active_at(c2)):
                    c1["temporal_conflict"] = True
                    c2["temporal_conflict"] = True


def _safe_typed_value(val: Any) -> Any:
    """Recursively mask sensitive tokens in typed_payload while preserving structured data."""
    if isinstance(val, str):
        return _safe(val, 65536)
    if isinstance(val, Mapping):
        return {_safe(k, 128): _safe_typed_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_safe_typed_value(x) for x in val]
    return val


def _item(card: Mapping[str, Any], *, include_created_at: bool = True) -> dict[str, Any]:
    item = {
        "id": _safe(card.get("memory_id") or card.get("id"), 180),
        "card_type": _safe(card.get("card_type"), 64),
        "title": _safe(card.get("title"), 1000),
        "summary": _safe(card.get("summary"), 65536),
        "currentness": _safe(card.get("currentness") or "current", 32),
        "content_hash": _safe(card.get("content_hash"), 80),
    }
    raw_typed = card.get("typed_payload")
    if isinstance(raw_typed, Mapping):
        item["typed_payload"] = _safe_typed_value(raw_typed)
    if card.get("authority_source"):
        item["authority_source"] = _safe(card.get("authority_source"), 64)
    if card.get("temporal_conflict"):
        item["temporal_conflict"] = True
    created_at = card.get("created_at") or card.get("observed_at")
    if include_created_at and created_at:
        # A card can stay current long after its observation. Expose when the
        # record was created so a reader can separate historical facts from
        # current state instead of having to stale-mark every old card.
        item["created_at"] = _safe(created_at, 40)
    return item


def _card_temporal_block(
    card: Mapping[str, Any],
    as_of: str = "",
    projection_lag_seconds: float | int | None = None,
) -> dict[str, Any]:
    """Build a Section 8 compliant 22-field temporal envelope delegating to temporal_envelope."""
    raw_typed = card.get("typed_payload")
    typed: Mapping[str, Any] = raw_typed if isinstance(raw_typed, Mapping) else {}

    proj_lag = projection_lag_seconds
    if proj_lag is None:
        if card.get("projection_lag_seconds") is not None:
            proj_lag = card["projection_lag_seconds"]
        elif card.get("projection_lag_ms") is not None:
            try:
                proj_lag = float(card["projection_lag_ms"]) / 1000.0
            except (ValueError, TypeError):
                proj_lag = None

    currentness = str(card.get("currentness") or "current")
    temporal_conflict = bool(card.get("temporal_conflict")) or currentness == "conflicted"
    authority_source = card.get("authority_source") or ("postgres" if temporal_conflict else None)

    return build_temporal_envelope(
        observed_at=card.get("observed_at") or typed.get("observed_at"),
        recorded_at=card.get("recorded_at") or card.get("created_at") or card.get("updated_at"),
        valid_from=card.get("valid_from"),
        valid_to=card.get("valid_to"),
        superseded_by=card.get("superseded_by"),
        as_of=as_of or None,
        currentness=currentness,
        lifecycle_status=str(card.get("lifecycle_status") or card.get("lifecycle_state") or "accepted"),
        freshness_state=card.get("freshness_state") or typed.get("freshness_state"),
        freshness_checked_at=card.get("freshness_checked_at") or typed.get("freshness_checked_at"),
        freshness_next_check_after=card.get("freshness_next_check_after") or typed.get("freshness_next_check_after"),
        freshness_gaps=card.get("freshness_gaps") or typed.get("freshness_gaps"),
        source_hash=card.get("source_hash") or typed.get("source_hash"),
        expected_source_hash=card.get("expected_source_hash") or card.get("latest_source_hash") or typed.get("expected_source_hash"),
        temporal_conflict=temporal_conflict,
        projection_lag_seconds=proj_lag,
        authority_source=str(authority_source) if authority_source else None,
        card_type=str(card.get("card_type") or ""),
        task_state=str(typed.get("task_state") or "") if typed.get("task_state") else None,
        time_confidence=card.get("time_confidence"),
    )


_MINIMAL_TEMPORAL_FIELDS = frozenset(
    {
        "currentness",
        "freshness",
        "age",
        "recommended_action",
        "temporal_conflict",
        "as_of",
        "valid_from",
        "valid_to",
        "superseded_by",
        "summary",
    }
)
_MINIMAL_AGE_SUBFIELDS = frozenset({"label", "confidence"})
_MINIMAL_FRESHNESS_SUBFIELDS = frozenset({"state"})


def _minimal_temporal_block(block: Any) -> dict[str, Any]:
    if not isinstance(block, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in _MINIMAL_TEMPORAL_FIELDS:
        if key not in block:
            continue
        val = block[key]
        if key == "age" and isinstance(val, Mapping):
            out[key] = {k: val[k] for k in _MINIMAL_AGE_SUBFIELDS if k in val}
        elif key == "freshness" and isinstance(val, Mapping):
            out[key] = {k: val[k] for k in _MINIMAL_FRESHNESS_SUBFIELDS if k in val}
        else:
            out[key] = val
    if block.get("authority_source") and block.get("temporal_conflict"):
        out["authority_source"] = block["authority_source"]
    return out


def _card_matches_filters(
    card: Mapping[str, Any],
    request: _ResolveRequest,
    *,
    as_of: str = "",
    projection_lag_seconds: float | int | None = None,
) -> bool:
    """Evaluate candidate card against request temporal filters and historical lane exclusion."""
    temporal = _card_temporal_block(card, as_of=as_of, projection_lag_seconds=projection_lag_seconds)
    if isinstance(card, dict):
        card["_temporal"] = temporal

    card_curr = str(temporal.get("currentness") or card.get("currentness") or "current").strip().lower()
    has_conflict = bool(card.get("temporal_conflict")) or bool(temporal.get("temporal_conflict")) or (card_curr == "conflicted")

    as_of_dt = _parse_temporal_instant(as_of) or datetime.now(timezone.utc)
    v_to_val = card.get("valid_to") or temporal.get("valid_to")
    v_to_dt = _parse_temporal_instant(v_to_val) if v_to_val else None
    v_from_val = card.get("valid_from") or temporal.get("valid_from")
    v_from_dt = _parse_temporal_instant(v_from_val) if v_from_val else None
    is_expired = (v_to_dt is not None and v_to_dt <= as_of_dt)

    # 1. Historical lane exclusion (FR-AGE-020, 021, 023, 024, Section 7.2, AC-AGE-002, AC-AGE-006)
    if request.as_of:
        # FR-AGE-024: as_of query considers items inside valid_from <= as_of < valid_to as the current lane for that point in time
        if v_from_dt is not None or v_to_dt is not None:
            is_valid_at_as_of = (v_from_dt is None or v_from_dt <= as_of_dt) and (v_to_dt is None or as_of_dt < v_to_dt)
            is_historical_lane = not is_valid_at_as_of
        else:
            is_historical_lane = card_curr in ("superseded", "retired", "stale")
    else:
        is_historical_lane = (card_curr in ("superseded", "retired", "stale")) or is_expired

    if not request.include_historical:
        if is_historical_lane and not has_conflict:
            caller_explicit_historical = (
                request.currentness is not None
                and request.currentness.strip().lower() in ("superseded", "retired", "stale")
            )
            if not caller_explicit_historical:
                return False

    # 2. currentness filter
    if request.currentness is not None:
        allowed = (
            {"current", "superseded"}
            if (request.as_of and request.currentness.strip().lower() == "current")
            else {request.currentness.strip().lower()}
        )
        if card_curr not in allowed:
            return False

    # 3. age_bucket filter (FR-AGE-053)
    if request.age_bucket is not None:
        age_info = temporal.get("age") or {}
        age_label = str(age_info.get("label") or "unknown").strip().lower()
        if age_label != request.age_bucket.strip().lower():
            return False

    # 4. time_confidence filter (FR-AGE-053)
    if request.time_confidence is not None:
        time_conf = str(temporal.get("time_confidence") or card.get("time_confidence") or "unknown").strip().lower()
        if time_conf != request.time_confidence.strip().lower():
            return False

    # 5. max_age filter (FR-AGE-054)
    if request.max_age is not None:
        age_info = temporal.get("age") or {}
        age_seconds = age_info.get("seconds")
        if age_seconds is None or age_seconds < 0 or age_seconds > request.max_age:
            return False

    # 6. min_age filter (FR-AGE-054)
    if request.min_age is not None:
        age_info = temporal.get("age") or {}
        age_seconds = age_info.get("seconds")
        if age_seconds is None or age_seconds < request.min_age:
            return False

    return True


def _context_item(card: Mapping[str, Any]) -> dict[str, Any]:
    card_type = str(card.get("card_type") or "")
    if card_type not in {"decision", "preference", "task", "work", "work_unit", "status"}:
        # Context stays a compact startup pack: identity only, no created_at.
        return _item(card, include_created_at=False)
    # A startup pack trades the per-card content hash for readable section text:
    # the hash costs ~87 bytes per card while a 3 KB tool result that carries the
    # payload twice leaves only ~200 bytes of text budget per card. Full-hash
    # provenance stays available through mode=list and response_mode=with_evidence.
    item = {
        "id": _safe(card.get("memory_id") or card.get("id"), 180),
        "card_type": _safe(card_type, 64),
        "currentness": _safe(card.get("currentness") or "current", 32),
    }
    if card.get("authority_source"):
        item["authority_source"] = _safe(card.get("authority_source"), 64)
    if card.get("temporal_conflict"):
        item["temporal_conflict"] = True
    return item


def _normalized_evidence_edges(edges: list[Any], root_ids: set[str]) -> list[dict[str, Any]]:
    """Keep only the DB-authorized, hash-only edge projection for this page."""
    normalized: list[dict[str, Any]] = []
    for edge in edges:
        try:
            parsed = _EvidenceEdge.model_validate(edge)
        except ValueError:
            continue
        if parsed.root_id in root_ids:
            normalized.append(parsed.model_dump())
    return sorted(
        normalized,
        key=lambda edge: (
            edge["root_id"], edge["depth"], edge["src_id"], edge["dst_id"],
            edge["rel_type"], edge["provenance_hash"],
        ),
    )[:_EVIDENCE_EDGE_LIMIT]


def _evidence_payload(evidence: dict[str, Any] | None, cards: list[dict[str, Any]]) -> dict[str, Any]:
    page_root_ids = {str(card.get("memory_id") or card.get("id") or "") for card in cards}
    edges = [
        edge for edge in (evidence or {}).get("edges", [])
        if edge.get("root_id") in page_root_ids
    ]
    # Root hash는 items에 이미 있다. 전체 visited_path와 동일 hash를 여러 번
    # 반복하지 않고 edge summary + 비-root hash 사전으로 근거 연결을 보존한다.
    summaries = []
    hashes = {}
    for edge in edges:
        summary = {key: edge[key] for key in ("src_id", "dst_id", "rel_type", "provenance_hash")}
        if summary not in summaries:
            summaries.append(summary)
        for side in ("src", "dst"):
            memory_id = edge[f"{side}_id"]
            if memory_id not in page_root_ids:
                hashes[memory_id] = edge[f"{side}_content_hash"]
    return {"explicit_edges": summaries, "content_hashes": hashes}


def _drop_last_evidence_edge(evidence: dict[str, Any] | None, page: list[dict[str, Any]]) -> bool:
    if evidence is None:
        return False
    page_root_ids = {str(card.get("memory_id") or card.get("id") or "") for card in page}
    roots_with_edges = [
        edge["root_id"] for edge in evidence.get("edges", [])
        if edge.get("root_id") in page_root_ids
    ]
    if not roots_with_edges:
        return False
    # 뒤쪽/deepest edge부터 제거하고 incomplete를 명시한다. 작은 근거 하나도
    # 그룹 전체 제거에 휩쓸려 빈 응답이 되는 일을 막는다.
    index = next(i for i in reversed(range(len(evidence["edges"])))
                 if evidence["edges"][i]["root_id"] in page_root_ids)
    evidence["edges"].pop(index)
    evidence["truncated"] = True
    return True


def _safe(value: Any, max_chars: int = 65536) -> str:
    return public_safe_text(str(value or ""), max_chars=max_chars)


_CHUNK_TRANSCRIPT_MARKER = "## Transcript"
_CHUNK_ROLE_PREFIXES = ("user:", "assistant:", "system:", "tool:")
# 모든 Hermes cron prompt가 공통으로 앞에 붙이는 문구라 요약 정보량이 없다.
_CHUNK_BOILERPLATE_PREFIXES = ("language contract:",)


def _session_chunk_body(text: str) -> str:
    """세션 청크에서 공통 메타데이터 헤더를 제거하고 실제 대화 본문만 돌려준다.

    청크 상단에는 `# session-memory ...`, `session_id_hash`, YAML front matter 등
    모든 청크가 공유하는 추적 헤더가 있어 앞부분을 그대로 자르면 title/summary가
    전부 동일해진다. `## Transcript` 이후를 본문으로 보고, marker가 없으면 front
    matter와 헤더 라인만 건너뛴다. 본문이 비면 원문을 그대로 반환한다.
    """
    marker = text.find(_CHUNK_TRANSCRIPT_MARKER)
    if marker >= 0:
        body = text[marker + len(_CHUNK_TRANSCRIPT_MARKER):]
    else:
        lines = text.splitlines()
        index = 0
        if lines and lines[0].lstrip().startswith("# session-memory"):
            index = 1
            while index < len(lines) and (
                not lines[index].strip()
                or lines[index].startswith(("session_id_hash:", "## conversation"))
            ):
                index += 1
        if index < len(lines) and lines[index].strip() == "---":
            end = next((i for i in range(index + 1, len(lines)) if lines[i].strip() == "---"), None)
            index = end + 1 if end is not None else index
        body = "\n".join(lines[index:])
    cleaned: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        for prefix in _CHUNK_ROLE_PREFIXES:
            if lowered.startswith(prefix):
                line = line[len(prefix):].strip()
                lowered = line.lower()
                break
        if not line or lowered.startswith(_CHUNK_BOILERPLATE_PREFIXES):
            continue
        cleaned.append(line)
    return "\n".join(cleaned) or text.strip()


def _fact_valid_at(episode: Any, instant: datetime) -> bool:
    try:
        start = getattr(episode, "valid_from", "")
        end = getattr(episode, "valid_to", "")
        return ((not start or datetime.fromisoformat(start.replace("Z", "+00:00")) <= instant)
                and (not end or instant < datetime.fromisoformat(end.replace("Z", "+00:00"))))
    except (ValueError, TypeError):
        return False


def _copy_card(card: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(card)
    if isinstance(card.get("typed_payload"), Mapping):
        copied["typed_payload"] = dict(card["typed_payload"])
    return copied


def _dedupe_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        memory_id = str(card.get("memory_id") or card.get("id") or "")
        if memory_id and memory_id not in seen:
            result.append(_copy_card(card))
            seen.add(memory_id)
    return result


def _shrink_card(card: dict[str, Any]) -> bool:
    """Shrink only display text; stable public identity/provenance never moves."""
    fields: list[tuple[dict[str, Any], str]] = [(card, "title"), (card, "summary")]
    typed = card.get("typed_payload")
    if isinstance(typed, dict):
        fields.extend(
            (typed, key)
            for key in ("decision", "rationale", "rule", "preference", "scope", "task", "next_action",
                        "status_value", "status", "observed_at")
        )
    candidates = [
        (container, key, str(container.get(key) or ""))
        for container, key in fields
        if str(container.get(key) or "")
    ]
    if not candidates:
        return False
    container, key, value = max(candidates, key=lambda item: len(item[2]))
    if len(value) <= 2:
        container[key] = ""
    else:
        container[key] = value[: max(1, len(value) // 2 - 1)] + "…"
    return True


# Semantic text is bounded (not emptied) before a context card is sacrificed, so
# a startup context still names the work, the decision, the status and the next
# step instead of returning only ids and hashes.
_CONTEXT_TEXT_FLOOR = 48
_CONTEXT_SECTION_TYPES = frozenset(
    {"decision", "preference", "task", "work", "work_unit", "status"}
)
# `task_state` sometimes carries a status token rather than the work subject.
_TASK_STATE_TOKENS = frozenset(
    {
        "active", "blocked", "cancelled", "canceled", "complete", "completed", "done",
        "failed", "in-progress", "in_progress", "not-started", "not_started", "open",
        "paused", "pending", "running", "stopped", "todo",
    }
)


_STATUS_STATE_TOKENS = _TASK_STATE_TOKENS | frozenset(
    {
        "completed_submitted", "degraded", "healthy", "ok", "pass", "passed",
        "ready", "submitted", "succeeded", "success", "unavailable", "unknown",
    }
)
_CONTEXT_TYPE_QUERY_ALIASES = frozenset(
    {
        "task", "tasks", "work", "work_unit", "작업",
        "decision", "decisions", "결정",
        "status", "statuses", "상태", "진행",
        "preference", "preferences", "guardrail", "guardrails", "선호", "제약",
    }
)


def _task_subject(card: Mapping[str, Any], typed: Mapping[str, Any]) -> str:
    """Name the work itself; a status token is not a task name."""
    explicit = typed.get("task")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    state = str(typed.get("task_state") or "")
    if state.strip().lower() in _TASK_STATE_TOKENS:
        return str(card.get("title") or card.get("summary") or state)
    if state.strip():
        return state
    return str(card.get("title") or card.get("summary") or "")


def _status_subject(card: Mapping[str, Any], typed: Mapping[str, Any]) -> str:
    """Name what the status describes when status_value is only a state token."""
    raw = str(typed.get("status_value") or typed.get("status") or "").strip()
    if raw.lower() in _STATUS_STATE_TOKENS:
        title = str(card.get("title") or "").strip()
        summary = str(card.get("summary") or "").strip()
        if title and summary and summary != title:
            return f"{title} — {summary}"
        return title or summary or raw
    if raw:
        return raw
    return str(card.get("summary") or card.get("title") or "")


def _cards_cover_query_tokens(cards: Sequence[Mapping[str, Any]], tokens: set[str]) -> bool:
    if not tokens:
        return bool(cards)
    return any(tokens & _query_tokens(_card_search_text(card)) for card in cards)


def _context_text_slots(card: Mapping[str, Any]) -> list[tuple[Any, str]]:
    """Rendered text sources for this card in context mode.

    Card types with a dedicated section keep their identity in `items`, so only
    the section source is bounded. Types without a section carry their text in
    `items`, so title/summary are the sources to bound there.
    """
    raw_payload = card.get("typed_payload")
    # Return the card's own mutable payload: trimming a per-call copy would look
    # like progress while the rebuilt payload never changes.
    typed: Any = raw_payload if isinstance(raw_payload, dict) else {}
    card_type = str(card.get("card_type") or "")
    if card_type not in _CONTEXT_SECTION_TYPES:
        return [(typed, key) for key in ("decision", "rationale") if key in typed] or [
            (card, key) for key in ("title", "summary") if card.get(key)
        ]
    if card_type == "decision":
        return [(typed, key) for key in ("decision", "rationale") if typed.get(key)]
    if card_type == "preference":
        return [(typed, key) for key in ("rule", "preference") if typed.get(key)]
    if card_type == "status":
        slots = [(typed, key) for key in ("status_value", "status") if typed.get(key)]
        if (
            not slots
            or str(typed.get("status_value") or typed.get("status") or "").strip().lower() in _STATUS_STATE_TOKENS
        ):
            slots.extend((card, key) for key in ("title", "summary") if card.get(key))
        return slots
    slots = [
        (typed, key)
        for key in ("task", "task_state", "next_action", "blocker")
        if typed.get(key)
    ]
    # The subject falls back to title/summary, so those participate in the bound.
    if not typed.get("task") or str(typed.get("task_state") or "").strip().lower() in _TASK_STATE_TOKENS:
        slots.extend((card, key) for key in ("title", "summary") if card.get(key))
    return slots


def _trim_context_text(page: list[dict[str, Any]]) -> bool:
    """Truncate the longest rendered text down to a readable floor.

    Never reduces a value to an empty string: an agent that receives an empty
    decision or an empty next action cannot resume the work at all.
    """
    target_container: Any = None
    target_key = ""
    target_len = _CONTEXT_TEXT_FLOOR
    for card in page:
        for container, key in _context_text_slots(card):
            value = str(container.get(key) or "")
            if len(value) > target_len:
                target_container, target_key, target_len = container, key, len(value)
    if target_container is None:
        return False
    value = str(target_container.get(target_key) or "")
    # Keep the result at or under the floor so this call always terminates.
    target_container[target_key] = value[: max(1, _CONTEXT_TEXT_FLOOR - 1)].rstrip() + "…"
    return True


def _context_drop_rank(card: Mapping[str, Any]) -> int:
    """Lower ranks are sacrificed first; task and decision are kept longest."""
    card_type = str(card.get("card_type") or "")
    if card_type in {"task", "work", "work_unit"}:
        return 5
    if card_type == "decision":
        return 4
    if card_type == "status":
        return 3
    if card_type == "preference":
        return 2
    return 1


def _drop_context_card(page: list[dict[str, Any]]) -> bool:
    """Drop the lowest-priority context card, keeping one card at minimum."""
    if len(page) <= 1:
        return False
    index = min(
        range(len(page)),
        key=lambda position: (_context_drop_rank(page[position]), -position),
    )
    page.pop(index)
    return True


def _lag(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _fingerprint(request: _ResolveRequest, cards: list[dict[str, Any]]) -> str:
    material = {"request": request.model_dump(exclude={"cursor"}), "candidates": [[str(card.get("memory_id") or card.get("id") or ""), str(card.get("content_hash") or "")] for card in cards]}
    return base64.urlsafe_b64encode(hashlib.sha256(_json(material).encode("utf-8")).digest()).decode("ascii").rstrip("=")


def _encode_cursor(fingerprint: str, offset: int, after_id: str | None = None) -> str:
    raw = _json([fingerprint, offset, after_id]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

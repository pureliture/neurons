"""Graph-first, PostgreSQL-authorized public context resolution."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime, time, timezone
from typing import Annotated, Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_knowledge.public_safe_util import ensure_public_safe, public_safe_text
from agent_knowledge.mcp_payload import tool_result_bytes


_MAX_BYTES = 3072
_CARD_LIMIT = 100
_EVIDENCE_ROOT_LIMIT = 20
_EVIDENCE_MAX_DEPTH = 5
_EVIDENCE_EDGE_LIMIT = 100


class _ResolveRequest(BaseModel):
    """Strict boundary model; callers must not get implicit coercion here."""

    model_config = ConfigDict(extra="forbid", strict=True)

    project: str = Field(min_length=1, max_length=64)
    query: str = Field(default="", max_length=2000)
    mode: Literal["list", "context", "query"] = "query"
    response_mode: Literal["slim", "with_evidence"] = "slim"
    as_of: str = Field(default="", max_length=64)
    limit: int = Field(default=5, ge=1, le=20)
    cursor: str | None = Field(default=None, max_length=2048)

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
            return datetime.combine(parsed_date, time.min, tzinfo=timezone.utc).isoformat()
        candidate = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError("as_of must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("as_of must include a timezone")
        return parsed.isoformat()

    @model_validator(mode="after")
    def _query_mode_requires_query(self) -> "_ResolveRequest":
        if self.mode == "query" and not self.query.strip():
            raise ValueError("query mode requires a non-empty query")
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
        response_mode: str = "slim",
        as_of: str = "",
        limit: int = 5,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        request = _ResolveRequest(
            project=project,
            query=query,
            mode=mode,
            response_mode=response_mode,
            as_of=as_of,
            limit=limit,
            cursor=cursor,
        )
        if self._store is None:
            return self._error_payload(request, error_code="authority_store_unavailable")
        if request.mode in {"list", "context"}:
            return self._resolve_authority_only(request)
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
            )
        except Exception:
            return self._error_payload(request, error_code="authority_store_unavailable")
        return self._serialize(
            request,
            list(cards),
            metadata=self._metadata(
                retrieval_path="none",
                graph_status="unavailable",
                authority_join_status="verified",
                fallback_used=False,
                projection_lag_ms=None,
            ),
            keyset=True,
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
            health = self._store.graph_projection_health(request.project, as_of=request.as_of or None)
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
        return self._serialize(
            request,
            graph_cards,
            metadata=self._metadata(
                retrieval_path="graph_neo4j",
                graph_status="available",
                authority_join_status=join_status,
                fallback_used=False,
                projection_lag_ms=lag,
            ),
        )

    def _join_graph_candidates(
        self, request: _ResolveRequest, episodes: tuple[Any, ...]
    ) -> tuple[list[dict[str, Any]] | None, str]:
        keys: list[tuple[str, str] | None] = []
        ids: list[str] = []
        saw_missing = False
        instant = datetime.fromisoformat(request.as_of) if request.as_of else datetime.now(timezone.utc)
        for episode in episodes:
            payload = getattr(episode, "payload", None)
            payload = payload if isinstance(payload, Mapping) else {}
            if getattr(episode, "entity_type", "") == "GraphFact":
                if not _fact_valid_at(episode, instant):
                    continue
                sources = payload.get("authority_sources")
                sources = sources if isinstance(sources, list) and sources else [{}]
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
        if not ids:
            return [], "unavailable" if saw_missing else "verified"
        try:
            authorized = self._store.list_authorized_cards(
                project=request.project,
                memory_ids=ids,
                as_of=request.as_of or None,
                limit=_CARD_LIMIT,
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
                limit=_CARD_LIMIT,
                as_of=request.as_of or None,
            )
        except Exception:
            return None, "authority_store_unavailable", True, "unavailable"
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
        return cards, None, True, "verified" if len(cards) == len(ranked) else "mismatch"

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
        fingerprint = _fingerprint(request, [] if keyset else deduped)
        cursor = self._decode_cursor(request.cursor, fingerprint)
        offset = 0 if keyset else cursor.offset
        page = deduped[offset : offset + request.limit]
        evidence: dict[str, Any] | None = None
        if request.response_mode == "with_evidence":
            try:
                evidence = self._read_evidence(request, page)
            except Exception:
                # Evidence is an authority-bearing extension. Do not return the
                # cards as a successful evidence response when its PG check fails.
                return self._error_payload(request, error_code="evidence_unavailable")
        payload = self._final_payload(request, page, metadata, offset, len(deduped), fingerprint, error_code, keyset, evidence)
        # 선택적 evidence 확장을 먼저 줄여 기본 결정/요약을 보존한다.
        while tool_result_bytes(payload) > _MAX_BYTES and _drop_last_evidence_edge(evidence, page):
            payload = self._final_payload(request, page, metadata, offset, len(deduped), fingerprint, error_code, keyset, evidence)
        while tool_result_bytes(payload) > _MAX_BYTES and len(page) > 1:
            page.pop()
            payload = self._final_payload(request, page, metadata, offset, len(deduped), fingerprint, error_code, keyset, evidence)
        while tool_result_bytes(payload) > _MAX_BYTES and page and _shrink_card(page[0]):
            payload = self._final_payload(request, page, metadata, offset, len(deduped), fingerprint, error_code, keyset, evidence)
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
        decisions: list[dict[str, Any]] = []
        preferences: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        evidence_hashes: list[str] = []
        for card in cards:
            item = _item(card)
            items.append(item)
            typed = card.get("typed_payload") if isinstance(card.get("typed_payload"), Mapping) else {}
            card_type = str(card.get("card_type") or "")
            if card_type == "decision":
                decisions.append({**item, "decision": _safe(typed.get("decision") or card.get("summary"), 144), "rationale": _safe(typed.get("rationale"), 96)})
            elif card_type == "preference":
                preferences.append({**item, "rule": _safe(typed.get("rule") or typed.get("preference") or card.get("summary"), 144), "scope": _safe(typed.get("scope") or "general", 64)})
            elif card_type in {"task", "work", "work_unit"}:
                tasks.append({**item, "task": _safe(typed.get("task") or typed.get("next_action") or card.get("summary"), 144)})
            content_hash = str(card.get("content_hash") or "")
            if content_hash:
                evidence_hashes.append(content_hash)
        payload: dict[str, Any] = {
            "schema_version": "lbrain_slim_context.v1",
            "project": request.project,
            "decisions": decisions,
            "preferences": preferences,
            "tasks": tasks,
            "items": items,
            "active_guardrails": [preference["rule"] for preference in preferences],
            "metadata": {**metadata, "has_more": has_more},
            "has_more": has_more,
            "next_cursor": None,
        }
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
    def _metadata(retrieval_path: str, graph_status: str, authority_join_status: str, fallback_used: bool, projection_lag_ms: int | None) -> dict[str, Any]:
        return {"retrieval_path": retrieval_path, "graph_status": graph_status, "authority_join_status": authority_join_status, "fallback_used": fallback_used, "projection_lag_ms": projection_lag_ms}

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


def _item(card: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": _safe(card.get("memory_id") or card.get("id"), 180), "card_type": _safe(card.get("card_type"), 64), "title": _safe(card.get("title"), 96), "summary": _safe(card.get("summary"), 144), "currentness": _safe(card.get("currentness") or "current", 32), "content_hash": _safe(card.get("content_hash"), 80)}


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


def _safe(value: Any, max_chars: int) -> str:
    return public_safe_text(str(value or ""), max_chars=max_chars)


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
            for key in ("decision", "rationale", "rule", "preference", "scope", "task", "next_action")
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

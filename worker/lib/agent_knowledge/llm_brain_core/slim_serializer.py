from __future__ import annotations

import base64
import json
from typing import Any


class SlimSerializer:
    """Tiered Slim Serializer for LBrain Context Authority.

    Implements:
    - response_mode="slim" (~1.2 KB, hard limit 3.0 KB) with deterministic pagination.
    - response_mode="with_evidence" returning complete SHA-256 hash chains, DAG edges, and source refs.
    - Strict schema rationalization eliminating empty lanes, route_spec dumps, and duplicate task keys.
    """

    @staticmethod
    def _calc_bytes(payload: dict[str, Any]) -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    @classmethod
    def serialize_slim(
        cls,
        project: str,
        decisions: list[dict[str, Any]],
        preferences: list[dict[str, Any]],
        guardrails: list[str],
        recent_context: str | None = None,
        gaps: list[str] | None = None,
        limit: int = 5,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        start_idx = 0
        if cursor:
            try:
                decoded = base64.b64decode(cursor).decode("utf-8")
                if decoded.startswith("offset:"):
                    start_idx = int(decoded.split(":")[-1])
                else:
                    start_idx = int(decoded)
            except Exception:
                start_idx = 0

        # Sanitize pagination parameters
        start_idx = max(0, start_idx)
        limit = max(1, limit)

        total_decisions = len(decisions)
        paged_decisions = decisions[start_idx : start_idx + limit]
        has_more = (start_idx + limit) < total_decisions
        next_cursor = (
            base64.b64encode(f"offset:{start_idx + limit}".encode("utf-8")).decode("utf-8")
            if has_more
            else None
        )

        slim_decisions = []
        for d in paged_decisions:
            typed_payload = d.get("typed_payload") or {}
            decision_text = (
                typed_payload.get("decision")
                or d.get("decision")
                or d.get("summary")
                or ""
            )
            rationale_text = (
                typed_payload.get("rationale")
                or d.get("rationale")
                or ""
            )
            slim_decisions.append({
                "id": d.get("memory_id") or d.get("id") or "",
                "title": str(d.get("title") or ""),
                "decision": str(decision_text),
                "rationale": str(rationale_text),
                "currentness": str(d.get("currentness") or "current"),
                "content_hash": str(d.get("content_hash") or ""),
            })

        slim_preferences = []
        for p in preferences:
            typed_payload = p.get("typed_payload") or {}
            title_text = (
                p.get("title")
                or typed_payload.get("title")
                or p.get("summary")
                or ""
            )
            rule_text = (
                typed_payload.get("rule")
                or typed_payload.get("preference")
                or p.get("rule")
                or p.get("summary")
                or p.get("title")
                or ""
            )
            scope_text = (
                typed_payload.get("scope")
                or p.get("scope")
                or "general"
            )
            slim_preferences.append({
                "title": str(title_text),
                "rule": str(rule_text),
                "scope": str(scope_text),
            })

        resolved_recent_context = (
            f"Session context for {project}"
            if recent_context is None
            else str(recent_context)
        )

        payload: dict[str, Any] = {
            "schema_version": "lbrain_slim_context.v1",
            "project": project,
            "decisions": slim_decisions,
            "preferences": slim_preferences,
            "active_guardrails": list(guardrails),
            "gaps": list(gaps) if gaps is not None else [],
            "has_more": has_more,
            "next_cursor": next_cursor,
        }
        if resolved_recent_context:
            payload["recent_context"] = resolved_recent_context

        # Hard limit guard: 3072 bytes (3 KB) universal enforcement
        if cls._calc_bytes(payload) > 3072:
            # Stage 1: Pop excess decisions
            while len(slim_decisions) > 1 and cls._calc_bytes(payload) > 3072:
                slim_decisions.pop()
                payload["decisions"] = slim_decisions
                payload["has_more"] = True
                payload["next_cursor"] = base64.b64encode(
                    f"offset:{start_idx + len(slim_decisions)}".encode("utf-8")
                ).decode("utf-8")

            # Stage 2: Truncate single decision fields (rationale -> decision -> title)
            if cls._calc_bytes(payload) > 3072 and slim_decisions:
                d = slim_decisions[0]
                # 1. Truncate rationale
                if d.get("rationale") and cls._calc_bytes(payload) > 3072:
                    excess = cls._calc_bytes(payload) - 3072 + 10
                    if len(d["rationale"]) > excess:
                        d["rationale"] = d["rationale"][:-excess] + "..."
                    else:
                        d["rationale"] = ""

                # 2. Truncate decision
                if d.get("decision") and cls._calc_bytes(payload) > 3072:
                    excess = cls._calc_bytes(payload) - 3072 + 10
                    if len(d["decision"]) > excess:
                        d["decision"] = d["decision"][:-excess] + "..."
                    else:
                        d["decision"] = d["decision"][:20] + "..."

                # 3. Truncate title
                if d.get("title") and cls._calc_bytes(payload) > 3072:
                    excess = cls._calc_bytes(payload) - 3072 + 10
                    if len(d["title"]) > excess:
                        d["title"] = d["title"][:-excess] + "..."
                    else:
                        d["title"] = d["title"][:20] + "..."

            # Stage 3: Pop excess preferences and truncate preference text
            if cls._calc_bytes(payload) > 3072 and slim_preferences:
                while len(slim_preferences) > 1 and cls._calc_bytes(payload) > 3072:
                    slim_preferences.pop()
                    payload["preferences"] = slim_preferences

                if cls._calc_bytes(payload) > 3072 and slim_preferences:
                    pref = slim_preferences[0]
                    if pref.get("rule") and cls._calc_bytes(payload) > 3072:
                        excess = cls._calc_bytes(payload) - 3072 + 10
                        if len(pref["rule"]) > excess:
                            pref["rule"] = pref["rule"][:-excess] + "..."
                        else:
                            pref["rule"] = pref["rule"][:20] + "..."
                    if pref.get("title") and cls._calc_bytes(payload) > 3072:
                        excess = cls._calc_bytes(payload) - 3072 + 10
                        if len(pref["title"]) > excess:
                            pref["title"] = pref["title"][:-excess] + "..."
                        else:
                            pref["title"] = pref["title"][:20] + "..."
                    if cls._calc_bytes(payload) > 3072:
                        slim_preferences.pop()
                        payload["preferences"] = slim_preferences

            # Stage 4: Pop excess guardrails and truncate guardrail text
            active_guardrails = payload["active_guardrails"]
            if cls._calc_bytes(payload) > 3072 and active_guardrails:
                while len(active_guardrails) > 1 and cls._calc_bytes(payload) > 3072:
                    active_guardrails.pop()
                    payload["active_guardrails"] = active_guardrails

                if cls._calc_bytes(payload) > 3072 and active_guardrails:
                    excess = cls._calc_bytes(payload) - 3072 + 10
                    if len(active_guardrails[0]) > excess:
                        active_guardrails[0] = active_guardrails[0][:-excess] + "..."
                    else:
                        active_guardrails[0] = active_guardrails[0][:20] + "..."
                    if cls._calc_bytes(payload) > 3072:
                        active_guardrails.pop()
                        payload["active_guardrails"] = active_guardrails

            # Stage 5: Unconditional recent_context truncation
            if cls._calc_bytes(payload) > 3072 and payload.get("recent_context"):
                excess = cls._calc_bytes(payload) - 3072 + 10
                cur_context = payload["recent_context"]
                if len(cur_context) > excess:
                    payload["recent_context"] = cur_context[:max(0, len(cur_context) - excess)] + "..."
                else:
                    payload["recent_context"] = ""

            # Stage 6: Final safety net for multi-byte Unicode or any remaining bytes
            while cls._calc_bytes(payload) > 3072:
                if payload.get("recent_context"):
                    cur_ctx = payload["recent_context"]
                    payload["recent_context"] = cur_ctx[:max(0, len(cur_ctx) - 20)]
                    continue
                if payload.get("active_guardrails"):
                    payload["active_guardrails"] = []
                    continue
                if payload.get("preferences"):
                    payload["preferences"] = []
                    continue
                if payload.get("gaps"):
                    payload["gaps"] = []
                    continue
                if payload.get("decisions"):
                    d = payload["decisions"][0]
                    if d.get("rationale"):
                        d["rationale"] = d["rationale"][:max(0, len(d["rationale"]) - 20)]
                        continue
                    if d.get("decision"):
                        d["decision"] = d["decision"][:max(0, len(d["decision"]) - 20)]
                        continue
                    if d.get("title"):
                        d["title"] = d["title"][:max(0, len(d["title"]) - 20)]
                        continue
                    payload["decisions"] = []
                    continue
                break

        return payload

    @classmethod
    def serialize_with_evidence(
        cls,
        project: str,
        decisions: list[dict[str, Any]],
        preferences: list[dict[str, Any]],
        guardrails: list[str],
        edges: list[dict[str, Any]],
        evidence_hashes: list[str],
        source_refs: list[dict[str, Any]],
        recent_context: str | None = None,
        gaps: list[str] | None = None,
        limit: int = 5,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        base = cls.serialize_slim(
            project=project,
            decisions=decisions,
            preferences=preferences,
            guardrails=guardrails,
            recent_context=recent_context,
            gaps=gaps,
            limit=limit,
            cursor=cursor,
        )

        sanitized_edges = []
        for e in edges:
            sanitized_edges.append({
                "src_id": str(e.get("src_id") or ""),
                "rel_type": str(e.get("rel_type") or ""),
                "dst_id": str(e.get("dst_id") or ""),
                "provenance_hash": str(e.get("provenance_hash") or ""),
            })

        sanitized_hashes = [str(h) for h in evidence_hashes if str(h).strip()]
        # Deterministic dedup preserving order
        deduped_hashes = list(dict.fromkeys(sanitized_hashes))

        sanitized_refs = []
        for s in source_refs:
            if isinstance(s, dict):
                sanitized_refs.append(dict(s))
            elif isinstance(s, str):
                sanitized_refs.append({"locator": s})

        base["schema_version"] = "lbrain_evidence_context.v1"
        base["edges"] = sanitized_edges
        base["evidence_hashes"] = deduped_hashes
        base["source_refs"] = sanitized_refs
        return base

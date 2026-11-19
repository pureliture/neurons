"""Trial-only reader: real PG authority cards + real resolver join.

No production data, no secrets, no body text.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterable

import psycopg

from .graph_first_resolver import GraphFirstResolver

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_brain_trial_authority_cards (
    memory_id TEXT NOT NULL,
    project TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    currentness TEXT NOT NULL DEFAULT 'current',
    lifecycle_state TEXT NOT NULL DEFAULT 'accepted',
    steward_approved INTEGER NOT NULL DEFAULT 1,
    valid_from TEXT NOT NULL DEFAULT '',
    valid_to TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL DEFAULT 'legacy',
    PRIMARY KEY (project, memory_id)
)
"""


class PgAuthorityCardStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(_SCHEMA)

    def upsert_card(
        self,
        *,
        memory_id: str,
        project: str,
        content_hash: str,
        provenance: str = "legacy",
        currentness: str = "current",
    ) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            connection.execute(
                """
                INSERT INTO llm_brain_trial_authority_cards
                    (memory_id, project, content_hash, provenance, currentness)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (project, memory_id) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    provenance = excluded.provenance,
                    currentness = excluded.currentness
                """,
                (memory_id, project, content_hash, provenance, currentness),
            )

    def list_authorized_cards(
        self,
        *,
        project: str,
        memory_ids: list[str],
        as_of: str | None = None,
        limit: int = 50,
        steward_only: bool = True,
    ) -> list[dict[str, Any]]:
        if not memory_ids:
            return []
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            rows = connection.execute(
                """
                SELECT memory_id, content_hash, currentness, lifecycle_state,
                       valid_from, valid_to, provenance
                FROM llm_brain_trial_authority_cards
                WHERE project = %s
                  AND memory_id = ANY(%s)
                  AND currentness = 'current'
                  AND lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                  AND (%s = FALSE OR steward_approved = 1)
                  AND (valid_from = '' OR valid_from <= %s)
                  AND (valid_to = '' OR valid_to >= %s)
                ORDER BY memory_id
                LIMIT %s
                """,
                (project, list(memory_ids), bool(steward_only), as_of or "",
                 as_of or "", int(limit)),
            ).fetchall()
        return [
            {
                "memory_id": row[0],
                "content_hash": row[1],
                "currentness": row[2],
                "lifecycle_state": row[3],
                "valid_from": row[4],
                "valid_to": row[5],
                "provenance": row[6],
            }
            for row in rows
        ]

    def find_authorized_by_content_hash(
        self, *, project: str, content_hashes: list[str], as_of: str = ""
    ) -> list[dict[str, str]]:
        """Return only already-approved current cards matching source hashes."""
        if not content_hashes:
            return []
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            rows = connection.execute(
                """
                SELECT memory_id, content_hash
                FROM llm_brain_trial_authority_cards
                WHERE project = %s
                  AND content_hash = ANY(%s)
                  AND currentness = 'current'
                  AND lifecycle_state IN ('accepted', 'human_accepted', 'auto_accepted')
                  AND steward_approved = 1
                  AND (%s = '' OR valid_from = '' OR valid_from <= %s)
                  AND (%s = '' OR valid_to = '' OR valid_to >= %s)
                ORDER BY memory_id, content_hash
                """,
                (project, list(content_hashes), as_of, as_of, as_of, as_of),
            ).fetchall()
        return [
            {"authority_memory_id": str(row[0]), "content_hash": str(row[1])}
            for row in rows
        ]


class FullInputReader:
    def __init__(self, dsn: str) -> None:
        self.store = PgAuthorityCardStore(dsn)
        self._resolver = GraphFirstResolver.__new__(GraphFirstResolver)
        self._resolver._store = self.store

    def resolve(
        self,
        project: str,
        facts: Iterable[Any],
        *,
        eligible_shadow_groups: Iterable[str] = (),
    ) -> dict[str, Any]:
        eligible = set(eligible_shadow_groups)
        selected = []
        provenance: dict[str, set[str]] = {}
        for fact in facts:
            group = str(fact.payload.get("shadow_group") or "")
            if group and group not in eligible:
                continue
            selected.append(fact)
            source = "shadow" if group else "legacy"
            for item in fact.payload.get("authority_sources", []):
                mid = str(item.get("authority_memory_id") or "")
                provenance.setdefault(mid, set()).add(source)
        cards, status = self._resolver._join_graph_candidates(
            SimpleNamespace(project=project, as_of=None), tuple(selected)
        )
        merged: dict[str, dict[str, Any]] = {}
        for card in cards or []:
            mid = str(card.get("memory_id") or "")
            if not mid:
                continue
            sources = set(provenance.get(mid, {str(card.get("provenance") or "legacy")}))
            if mid not in merged:
                merged[mid] = {**card, "provenance": "+".join(sorted(sources)),
                               "provenance_sources": sorted(sources)}
            else:
                existing = set(merged[mid]["provenance_sources"]) | sources
                merged[mid]["provenance_sources"] = sorted(existing)
                merged[mid]["provenance"] = "+".join(sorted(existing))
        shadow_cards = sum(
            1 for card in merged.values() if "shadow" in card["provenance_sources"]
        )
        legacy_cards = sum(
            1 for card in merged.values() if "legacy" in card["provenance_sources"]
        )
        return {
            "cards": list(merged.values()),
            "status": status,
            "shadow_cards": shadow_cards,
            "legacy_cards": legacy_cards,
        }

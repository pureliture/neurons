"""Trial-only durable chunk state on a real isolated Postgres database.

Not a production path. No bodies, prompts, or secrets are stored.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

import psycopg


class FencingRejected(RuntimeError):
    """A stale lease attempted to complete a chunk."""



class LeaseConflict(RuntimeError):
    """A live lease is already held by another worker."""


class LeaseExpired(RuntimeError):
    """The lease expired before completion."""

class DuplicateRejected(RuntimeError):
    """A chunk key was re-completed with a different verified payload."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_brain_full_input_chunk_trial (
    chunk_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project TEXT NOT NULL,
    session_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    map_digest TEXT NOT NULL,
    output_digest TEXT,
    receipt TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    owner TEXT,
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    fence_token INTEGER NOT NULL DEFAULT 0
)
"""


class PgChunkState:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            connection.execute(_SCHEMA)
            connection.execute(
                "ALTER TABLE llm_brain_full_input_chunk_trial "
                "ALTER COLUMN lease_until TYPE DOUBLE PRECISION"
            )

    def register_plan(
        self,
        *,
        run_id: str,
        project: str,
        session_hash: str,
        policy_version: str,
        source_hash: str,
        map_digest: str,
        chunk_keys: Iterable[str],
    ) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            for key in chunk_keys:
                connection.execute(
                    """
                    INSERT INTO llm_brain_full_input_chunk_trial
                        (chunk_key, run_id, project, session_hash, policy_version,
                         source_hash, map_digest, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'planned')
                    ON CONFLICT (chunk_key) DO NOTHING
                    """,
                    (key, run_id, project, session_hash, policy_version,
                     source_hash, map_digest),
                )

    def claim(self, *, key: str, owner: str, lease_seconds: float = 30.0) -> int:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            now = float(connection.execute(
                "SELECT EXTRACT(EPOCH FROM now())"
            ).fetchone()[0])
            row = connection.execute(
                """
                UPDATE llm_brain_full_input_chunk_trial
                SET fence_token = fence_token + 1,
                    status = CASE WHEN status = 'completed' THEN status ELSE 'claimed' END,
                    owner = %s,
                    lease_until = %s,
                    attempts = attempts + 1
                WHERE chunk_key = %s
                  AND (status = 'completed' OR status = 'planned'
                       OR status = 'superseded' OR owner = %s
                       OR lease_until <= %s)
                RETURNING fence_token
                """,
                (owner, now + float(lease_seconds), key, owner, now),
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT 1 FROM llm_brain_full_input_chunk_trial WHERE chunk_key = %s",
                    (key,),
                ).fetchone()
                if exists is None:
                    raise KeyError("unknown chunk key")
                raise LeaseConflict("live lease held by another worker")
        return int(row[0])

    def complete(
        self,
        *,
        key: str,
        fence_token: int,
        output_digest: str,
        write_receipt: str,
        owner: str | None = None,
    ) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT fence_token, status, output_digest, receipt, owner, lease_until
                    FROM llm_brain_full_input_chunk_trial WHERE chunk_key = %s FOR UPDATE
                    """,
                    (key,),
                ).fetchone()
                if row is None:
                    raise KeyError("unknown chunk key")
                current_token, status, old_digest, old_receipt, lease_owner, lease_until = row
                if int(fence_token) != int(current_token):
                    raise FencingRejected("stale fence token")
                if old_digest and old_receipt:
                    if old_digest == output_digest and old_receipt == write_receipt:
                        return
                    raise DuplicateRejected("chunk already completed with another payload")
                if status != "completed":
                    if owner is not None and str(lease_owner or "") != owner:
                        raise LeaseConflict("completion owner does not hold the lease")
                    live = float(connection.execute(
                        "SELECT EXTRACT(EPOCH FROM now())"
                    ).fetchone()[0])
                    if float(lease_until or 0) <= live:
                        raise LeaseExpired("lease expired before completion")
                connection.execute(
                    """
                    UPDATE llm_brain_full_input_chunk_trial
                    SET status = 'completed', output_digest = %s, receipt = %s,
                        lease_until = 0
                    WHERE chunk_key = %s
                    """,
                    (output_digest, write_receipt, key),
                )

    def is_complete(self, key: str) -> bool:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            row = connection.execute(
                "SELECT status, receipt FROM llm_brain_full_input_chunk_trial WHERE chunk_key = %s",
                (key,),
            ).fetchone()
        return bool(row and row[0] == "completed" and row[1])

    def all_verified(self, keys: Iterable[str]) -> bool:
        keys = tuple(keys)
        return bool(keys) and all(self.is_complete(key) for key in keys)

    def completed_chunks(self, project: str, policy_version: str) -> list[str]:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            rows = connection.execute(
                """
                SELECT chunk_key FROM llm_brain_full_input_chunk_trial
                WHERE project = %s AND policy_version = %s AND status = 'completed'
                ORDER BY chunk_key
                """,
                (project, policy_version),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def pending_keys(self, keys: Iterable[str]) -> list[str]:
        return [key for key in keys if not self.is_complete(key)]

    def force_expire(self, key: str) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            connection.execute(
                "UPDATE llm_brain_full_input_chunk_trial SET lease_until = 0 WHERE chunk_key = %s",
                (key,),
            )

    def is_session_complete(
        self,
        *,
        project: str,
        policy_version: str,
        source_hash: str,
        map_digest: str,
        chunk_keys: Iterable[str],
    ) -> bool:
        keys = tuple(chunk_keys)
        if not keys:
            return False
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            rows = connection.execute(
                """
                SELECT status, source_hash, map_digest, receipt, project, policy_version
                FROM llm_brain_full_input_chunk_trial WHERE chunk_key = ANY(%s)
                """,
                (list(keys),),
            ).fetchall()
        if len(rows) != len(keys):
            return False
        return all(
            row[0] == "completed"
            and row[1] == source_hash
            and row[2] == map_digest
            and bool(row[3])
            and row[4] == project
            and row[5] == policy_version
            for row in rows
        )

    def record_chunk(
        self,
        *,
        key: str,
        project: str,
        policy_version: str,
        source_hash: str,
        map_digest: str,
        output_digest: str,
        receipt: str,
    ) -> None:
        token = self.claim(key=key, owner="cli-trial")
        self.complete(key=key, fence_token=token, owner="cli-trial",
                      output_digest=output_digest, write_receipt=receipt)

    def supersede_keys(self, keys: Iterable[str]) -> None:
        """Mark pending results unusable after a source-revision fence failure."""
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            for key in keys:
                connection.execute(
                    """
                    UPDATE llm_brain_full_input_chunk_trial
                    SET status = 'superseded', receipt = NULL, lease_until = 0
                    WHERE chunk_key = %s
                    """,
                    (key,),
                )


    def is_complete_many(
        self,
        keys: list[str],
        *,
        project: str | None = None,
        policy_version: str | None = None,
        source_hash: str | None = None,
    ) -> set[str]:
        if not keys:
            return set()
        clauses = ["chunk_key = ANY(%s)", "status = 'completed'", "receipt IS NOT NULL"]
        params: list[Any] = [keys]
        if project is not None:
            clauses.append("project = %s")
            params.append(project)
        if policy_version is not None:
            clauses.append("policy_version = %s")
            params.append(policy_version)
        if source_hash is not None:
            clauses.append("source_hash = %s")
            params.append(source_hash)
        query = (
            "SELECT chunk_key FROM llm_brain_full_input_chunk_trial WHERE "
            + " AND ".join(clauses)
        )
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return {str(row[0]) for row in rows}

    def claim_many(
        self, keys: list[str], *, owner: str, lease_seconds: float = 30.0
    ) -> dict[str, int]:
        if not keys:
            return {}
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            with connection.transaction():
                now = float(connection.execute(
                    "SELECT EXTRACT(EPOCH FROM now())"
                ).fetchone()[0])
                rows = connection.execute(
                    """
                    UPDATE llm_brain_full_input_chunk_trial
                    SET fence_token = fence_token + 1,
                        status = CASE WHEN status = 'completed' THEN status ELSE 'claimed' END,
                        owner = %s,
                        lease_until = %s,
                        attempts = attempts + 1
                    WHERE chunk_key = ANY(%s)
                      AND (status = 'completed' OR status = 'planned'
                           OR status = 'superseded' OR owner = %s
                           OR lease_until <= %s)
                    RETURNING chunk_key, fence_token
                    """,
                    (owner, now + float(lease_seconds), keys, owner, now),
                ).fetchall()
                result = {str(row[0]): int(row[1]) for row in rows}
                if len(result) != len(set(keys)):
                    raise LeaseConflict("live lease held by another worker")
        return result

    def complete_many(
        self, entries: list[dict[str, Any]], *, owner: str
    ) -> None:
        if not entries:
            return
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            with connection.transaction():
                for entry in entries:
                    key = str(entry["key"])
                    row = connection.execute(
                        """
                        SELECT fence_token, status, output_digest, receipt, owner, lease_until
                        FROM llm_brain_full_input_chunk_trial WHERE chunk_key = %s FOR UPDATE
                        """,
                        (key,),
                    ).fetchone()
                    if row is None:
                        raise KeyError("unknown chunk key")
                    current_token, status, old_digest, old_receipt, lease_owner, lease_until = row
                    if int(entry["fence_token"]) != int(current_token):
                        raise FencingRejected("stale fence token")
                    if str(lease_owner or "") != owner:
                        raise LeaseConflict("completion owner does not hold the lease")
                    if old_digest and old_receipt:
                        if (old_digest == entry["output_digest"]
                                and old_receipt == entry["write_receipt"]):
                            continue
                        raise DuplicateRejected("chunk already completed with another payload")
                    if status != "completed":
                        live = float(connection.execute(
                            "SELECT EXTRACT(EPOCH FROM now())"
                        ).fetchone()[0])
                        if float(lease_until or 0) <= live:
                            raise LeaseExpired("lease expired before completion")
                    connection.execute(
                        """
                        UPDATE llm_brain_full_input_chunk_trial
                        SET status = 'completed', output_digest = %s, receipt = %s,
                            lease_until = 0
                        WHERE chunk_key = %s
                        """,
                        (entry["output_digest"], entry["write_receipt"], key),
                    )

    def renew_lease(
        self, keys: list[str], tokens: dict[str, int], *, owner: str,
        lease_seconds: float = 30.0
    ) -> None:
        if not keys:
            return
        with psycopg.connect(self._dsn, autocommit=True) as connection:
            with connection.transaction():
                now = float(connection.execute(
                    "SELECT EXTRACT(EPOCH FROM now())"
                ).fetchone()[0])
                for key in keys:
                    row = connection.execute(
                        "SELECT fence_token, owner, status FROM "
                        "llm_brain_full_input_chunk_trial WHERE chunk_key = %s FOR UPDATE",
                        (key,),
                    ).fetchone()
                    if (row is None or int(row[0]) != int(tokens[key])
                            or str(row[1] or "") != owner):
                        raise FencingRejected("lease renewal rejected")
                    if row[2] == "completed":
                        continue
                    connection.execute(
                        "UPDATE llm_brain_full_input_chunk_trial SET lease_until = %s "
                        "WHERE chunk_key = %s",
                        (now + float(lease_seconds), key),
                    )

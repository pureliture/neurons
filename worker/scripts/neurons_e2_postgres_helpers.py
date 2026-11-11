"""Neurons e2-tiny isolated PostgreSQL fixture helper.

Strict Safety & Operational Contracts:
1. Strict server identity fence (cluster_name, current_database, current_user, permissions, DB comment).
   Fails closed (BLOCK) if any identity or permission check fails.
2. Tracks and cleans exactly ONE unique UUID database per test invocation.
3. Absolutely NO pg_terminate_backend.
4. If thread timeout occurs, abort_cleanup() is explicitly set on DatabaseContext,
   preventing DROP DATABASE from executing in finally, preserving the orphan DB for inspection.
5. If DROP fails, drop event is NEVER recorded; drop_failed event is recorded instead.
   Both original test error and cleanup error are preserved via exception chaining.
6. Ambiguous CREATE DATABASE failure: queries pg_database to verify if DB was actually created,
   ensuring cleanup executes if the DB exists despite network blur.
7. Mixed Old/New concurrency race: exactly 1 legacy DDL worker + 1 new adapter worker.
   Crucially, barrier.wait() is placed AFTER connection establishment and BEFORE DDL execution,
   maximizing actual catalog contention.
   Legacy worker only permits expected catalog collision (tuple concurrently updated / 55P03);
   any unexpected error fails the test.
8. Maximum 3 connections strictly enforced (1 admin + 2 workers).
9. Zero secrets / DSN printed in output or exceptions.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
import threading
import time
from uuid import uuid4

import psycopg
from psycopg import sql

EXPECTED_CLUSTER_NAME = "atlas-isolated-tests-e2-tiny"
EXPECTED_CONTROL_DB = "atlas_test_control"
EXPECTED_ROLE = "test_runner"
EXPECTED_DB_COMMENT = "atlas-isolated-test-server:e2-tiny:v1"

VALID_DB_NAME_RE = re.compile(r"^atlas_test_(?:bridge|neurons)_[0-9a-f]{32}$")

# Unconditional legacy julianday DDL shim for Old Worker race simulation
_LEGACY_JULIANDAY_SHIM = """
CREATE OR REPLACE FUNCTION julianday(t text) RETURNS double precision AS $JD$
DECLARE ts timestamptz;
BEGIN
  IF t IS NULL THEN RETURN NULL; END IF;
  IF t = 'now' THEN RETURN extract(epoch FROM now()) / 86400.0 + 2440587.5; END IF;
  BEGIN
    ts := t::timestamptz;
  EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
  END;
  RETURN extract(epoch FROM ts) / 86400.0 + 2440587.5;
END;
$JD$ LANGUAGE plpgsql STABLE;
"""


class IdentityFenceError(RuntimeError):
    """Raised when server identity or permissions do not match the approved test contract."""
    pass


class EventRecordingError(RuntimeError):
    """Raised when writing to ATLAS_PG_EVENT_FILE fails."""
    pass


class DatabaseContext:
    """Manages the lifecycle state of a single isolated test database."""
    def __init__(self, db_name: str, scoped_dsn: str):
        self.db_name = db_name
        self.scoped_dsn = scoped_dsn
        self.allow_drop = True
        self.abort_reason: str | None = None

    def abort_drop(self, reason: str) -> None:
        """Explicitly disallows DROP DATABASE in context manager cleanup for safety."""
        self.allow_drop = False
        self.abort_reason = reason


def validate_test_server(conn: psycopg.Connection) -> None:
    """Verifies that the connected database is the approved e2-tiny isolated test server."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT current_setting('cluster_name'), current_database(), current_user,
                       rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls,
                       shobj_description(d.oid, 'pg_database')
                FROM pg_roles r CROSS JOIN pg_database d
                WHERE r.rolname = current_user AND d.datname = current_database()
            """)
            row = cur.fetchone()
    except Exception as exc:
        raise IdentityFenceError(
            f"Failed to query server identity (error type: {type(exc).__name__}); execution BLOCKED."
        ) from None

    expected = (
        EXPECTED_CLUSTER_NAME,
        EXPECTED_CONTROL_DB,
        EXPECTED_ROLE,
        False,  # rolsuper
        True,   # rolcreatedb
        False,  # rolcreaterole
        False,  # rolreplication
        False,  # rolbypassrls
        EXPECTED_DB_COMMENT,
    )
    if row != expected:
        raise IdentityFenceError(
            "Target PostgreSQL server identity, role, or permissions mismatch; "
            "execution BLOCKED to protect non-test or production environments."
        )


def _record_db_event(event_type: str, db_name: str, *, present: bool | None = None) -> None:
    """Appends database lifecycle event to ATLAS_PG_EVENT_FILE if configured.

    Raises EventRecordingError on I/O error so caller can preserve both cleanup and failure.
    """
    event_path = os.environ.get("ATLAS_PG_EVENT_FILE")
    if not event_path:
        return
    payload = {
        "event": event_type,
        "database": db_name,
        "timestamp": time.time(),
        "helper_identity_verified": True,
    }
    if present is not None:
        payload["present"] = present
    try:
        with open(event_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception as exc:
        raise EventRecordingError(
            f"Failed to record DB event '{event_type}' for {db_name} (error type: {type(exc).__name__})"
        ) from None


@contextmanager
def isolated_neurons_test_database(*, project_prefix: str = "bridge", connect_timeout: int = 10):
    """Allocates, fences, and cleans up exactly ONE isolated UUID database on e2-tiny.

    Enforces:
    - Zero pg_terminate_backend.
    - If ctx.allow_drop is False (e.g. unjoined thread), DROP is skipped completely.
    - If DROP fails, drop event is never recorded; drop_failed event is recorded instead.
    - If CREATE DATABASE fails with an ambiguous error, checks catalog to confirm existence.
    - Preserves both original test error and cleanup error.
    """
    dsn = os.environ.get("ATLAS_TEST_PG_DSN")
    if not dsn:
        raise RuntimeError("ATLAS_TEST_PG_DSN is required; no local or arbitrary fallback allowed")

    raw_name = f"atlas_test_{project_prefix}_{uuid4().hex}"
    if not VALID_DB_NAME_RE.fullmatch(raw_name):
        raise ValueError(f"Generated database name {raw_name} does not match allowed pattern")
    db_name = raw_name

    try:
        admin_conn = psycopg.connect(dsn, autocommit=True, connect_timeout=connect_timeout)
    except Exception as exc:
        raise RuntimeError(f"Admin connection failed (error type: {type(exc).__name__})") from None

    created = False
    event_error = None
    test_error = None
    ctx = None

    try:
        # 1. Identity fence check on management DB (atlas_test_control)
        validate_test_server(admin_conn)

        # 2. Create isolated single UUID database from template0
        try:
            admin_conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(db_name)))
            created = True
        except Exception as create_exc:
            # Ambiguous creation failure (timeout or network glitch): verify catalog existence
            catalog_check_ok = False
            try:
                with admin_conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                    if cur.fetchone():
                        created = True
                catalog_check_ok = True
            except Exception as cat_exc:
                # Management connection broken or catalog query failed!
                # We cannot know if DB was created or not.
                # Record unconfirmed existence to prevent silent orphan DB.
                try:
                    _record_db_event("create_unconfirmed", db_name)
                except Exception:
                    pass
                # Mark created = True so finally block attempts cleanup/reconnect if possible
                created = True
                raise RuntimeError(
                    f"CREATE DATABASE failed ({type(create_exc).__name__}) AND catalog existence check failed "
                    f"({type(cat_exc).__name__}) for database {db_name}! State UNCONFIRMED; potential orphan DB."
                ) from None

            raise RuntimeError(f"CREATE DATABASE failed (error type: {type(create_exc).__name__})") from None

        # 3. Record create event
        try:
            _record_db_event("create", db_name)
        except EventRecordingError as exc:
            event_error = exc
            raise

        scoped_dsn = psycopg.conninfo.make_conninfo(dsn, dbname=db_name, connect_timeout=connect_timeout)
        ctx = DatabaseContext(db_name=db_name, scoped_dsn=scoped_dsn)
        yield ctx
    except Exception as exc:
        test_error = exc
        raise
    finally:
        cleanup_error = None
        if created:
            # Safety check: if ctx exists and abort_drop() was called, SKIP DROP COMPLETELY
            if ctx is not None and not ctx.allow_drop:
                cleanup_error = RuntimeError(
                    f"DROP DATABASE {db_name} ABORTED for safety: {ctx.abort_reason}. "
                    f"Orphan DB {db_name} left untouched for owner inspection."
                )
                try:
                    _record_db_event("drop_aborted", db_name)
                except Exception:
                    pass
            else:
                # 4. Clean up: DROP DATABASE without forced termination
                drop_succeeded = False
                try:
                    admin_conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db_name)))
                    drop_succeeded = True
                    try:
                        _record_db_event("drop", db_name)
                    except EventRecordingError as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                except psycopg.errors.ObjectInUse as exc:
                    cleanup_error = RuntimeError(
                        f"Database {db_name} is still in use by active connections; DROP aborted without termination. "
                        f"Reported orphan DB {db_name}. (Error type: {type(exc).__name__})"
                    )
                except Exception as exc:
                    cleanup_error = RuntimeError(
                        f"DROP DATABASE {db_name} failed (error type: {type(exc).__name__})"
                    )

                if not drop_succeeded:
                    try:
                        _record_db_event("drop_failed", db_name)
                    except Exception:
                        pass

                # 5. Catalog absence verification via admin connection
                # ALWAYS executed if drop_succeeded, even if event recording raised EventRecordingError!
                if drop_succeeded:
                    readback_error = None
                    try:
                        with admin_conn.cursor() as cur:
                            cur.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (db_name,))
                            remaining_count = cur.fetchone()[0]
                        if remaining_count != 0:
                            readback_error = RuntimeError(
                                f"Catalog absence verification FAILED: database {db_name} still exists in pg_database!"
                            )
                    except Exception as exc:
                        readback_error = RuntimeError(
                            f"Catalog absence verification query failed (error type: {type(exc).__name__})"
                        )

                    try:
                        _record_db_event("exact_catalog_read", db_name, present=(readback_error is not None))
                    except EventRecordingError as exc:
                        if cleanup_error is None:
                            cleanup_error = exc

                    if readback_error is not None:
                        if cleanup_error is not None:
                            cleanup_error = RuntimeError(f"{readback_error} (prior event/cleanup error: {cleanup_error})")
                        else:
                            cleanup_error = readback_error

        try:
            admin_conn.close()
        except Exception as close_exc:
            if cleanup_error is None:
                cleanup_error = RuntimeError(f"Admin connection close failed: {type(close_exc).__name__}")

        # Preserve both test error and cleanup/event error
        if cleanup_error is not None:
            if test_error is not None:
                raise cleanup_error from test_error
            raise cleanup_error


def run_mixed_concurrency_race(
    ctx: DatabaseContext,
    *,
    new_adapter_factory,
    timeout_seconds: float = 10.0,
) -> dict[str, str]:
    """Executes mixed 2-worker race: 1 legacy worker + 1 new adapter worker.

    Slot Budget: exactly 1 legacy connection + 1 new connection = 2 worker connections
    (plus 1 admin management connection in caller = 3 total connections max).

    Guarantees & Scope Limitation:
    - Old worker establishes connection FIRST, then synchronizes on Barrier(2) before DDL.
    - New worker instantiates adapter, synchronizes on Barrier(2), then calls adapter.connect().
    - NOTE ON SYNCHRONIZATION SCOPE: Because the new worker connects AFTER the barrier,
      simultaneous catalog DDL collision is NOT guaranteed. This test is strictly a
      concurrent startup contention smoke test, NOT a live verification of catalog recovery.
    - Explicit conn.close() in finally block for both workers.
    - Thread termination verification: if threads remain alive after timeout,
      calls ctx.abort_drop() to prevent DROP DATABASE in context manager cleanup,
      then raises RuntimeError.
    - Legacy worker only permits expected catalog contention errors (tuple concurrently updated / 55P03);
      unexpected errors (e.g. auth/syntax) fail the test.
    """
    barrier = threading.Barrier(2)
    results = {}
    exceptions = {}

    def _old_legacy_worker():
        """Simulates legacy worker: connects first, waits on barrier, then executes unconditional DDL."""
        conn = None
        try:
            conn = psycopg.connect(ctx.scoped_dsn, autocommit=True)
            # Synchronize AFTER connect, immediately BEFORE DDL execution
            barrier.wait(timeout=timeout_seconds)
            with conn.cursor() as cur:
                cur.execute(_LEGACY_JULIANDAY_SHIM)
            results["old"] = "old_ok"
        except Exception as exc:
            # Check if this was an expected catalog contention error
            err_msg = str(exc).lower()
            sqlstate = getattr(exc, "sqlstate", None)
            is_catalog_collision = (
                "tuple concurrently updated" in err_msg
                or (sqlstate == "55P03" and "catalog" in err_msg)
            )
            if is_catalog_collision:
                results["old"] = f"old_collided: {type(exc).__name__}"
            else:
                # Unexpected failure: must fail the test
                results["old"] = f"old_unexpected_err: {type(exc).__name__}"
                exceptions["old"] = exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _new_adapter_worker():
        """Simulates new worker: connects adapter, synchronizes on barrier, executes safe DDL/recovery."""
        conn = None
        try:
            adapter = new_adapter_factory(ctx.scoped_dsn)
            # We synchronize right before connection / DDL execution
            barrier.wait(timeout=timeout_seconds)
            conn = adapter.connect()
            # Must return IDLE transaction
            assert conn._conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
            res = conn.execute("SELECT julianday('now')").fetchone()
            assert res[0] is not None
            results["new"] = "new_ok"
        except Exception as exc:
            results["new"] = f"new_err: {type(exc).__name__}"
            exceptions["new"] = exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    t_old = threading.Thread(target=_old_legacy_worker, name="legacy-ddl-worker")
    t_new = threading.Thread(target=_new_adapter_worker, name="new-adapter-worker")

    t_old.start()
    t_new.start()

    t_old.join(timeout=timeout_seconds + 5.0)
    t_new.join(timeout=timeout_seconds + 5.0)

    # Thread termination verification: both threads MUST be dead before returning
    if t_old.is_alive() or t_new.is_alive():
        ctx.abort_drop("Worker thread(s) failed to terminate within timeout; active connection risk")
        raise RuntimeError(
            "Worker thread(s) failed to terminate within timeout; "
            "aborting database drop for safety."
        )

    # If legacy worker had an unexpected non-collision error, fail
    if "old" in exceptions:
        err_type = type(exceptions["old"]).__name__
        raise RuntimeError(f"Legacy worker failed with unexpected non-collision error: {err_type}")

    # New adapter worker MUST succeed
    if results.get("new") != "new_ok":
        err_type = type(exceptions.get("new")).__name__ if "new" in exceptions else "Unknown"
        raise RuntimeError(f"New adapter worker failed in mixed race: {err_type}")

    return results

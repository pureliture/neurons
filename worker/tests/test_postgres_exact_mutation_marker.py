from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import re

import pytest


_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _contract(*, tables=("public.authority", "public.ledger"), writer_roles=("neurons_writer",)):
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerContract,
    )

    return PostgresExactMutationMarkerContract(
        schema_generation="sha256:" + "1" * 64,
        in_scope_tables=tables,
        writer_roles=writer_roles,
        marker_owner_role="neurons_marker_owner",
        audit_reader_role="neurons_marker_reader",
        advisory_lock_key=7_211_740_091,
        approved_privileged_roles=("postgres_bootstrap",),
        privileged_credential_inventory_anchor_hash="sha256:" + "8" * 64,
    )


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


def _valid_trigger_rows(contract):
    rows = []
    for table in contract.in_scope_tables:
        schema, name = table.split(".")
        rows.extend(
            (
                {
                    "table_schema": schema,
                    "table_name": name,
                    "trigger_name": "authority_ledger_exact_marker_before_v1",
                    "trigger_enabled": "A",
                    "trigger_type": 62,
                    "function_schema": "public",
                    "function_name": "authority_ledger_exact_mutation_before_v1",
                    "function_body_hash": contract.source_function_hashes[
                        "authority_ledger_exact_mutation_before_v1"
                    ],
                },
                {
                    "table_schema": schema,
                    "table_name": name,
                    "trigger_name": "authority_ledger_exact_marker_after_v1",
                    "trigger_enabled": "A",
                    "trigger_type": 60,
                    "function_schema": "public",
                    "function_name": "authority_ledger_exact_mutation_after_v1",
                    "function_body_hash": contract.source_function_hashes[
                        "authority_ledger_exact_mutation_after_v1"
                    ],
                },
            )
        )
    return rows


def _valid_function_rows(contract):
    return [
        {
            "function_schema": "public",
            "function_name": name,
            "function_body_hash": body_hash,
            "is_security_definer": True,
            "function_owner": contract.marker_owner_role,
            "identity_arguments": "",
            "function_kind": "f",
            "volatility": "v",
            "parallel_mode": "u",
            "language_name": (
                "plpgsql"
                if name
                in {
                    "authority_ledger_exact_mutation_before_v1",
                    "authority_ledger_exact_mutation_after_v1",
                }
                else "sql"
            ),
            "function_config": "search_path=pg_catalog, public",
        }
        for name, body_hash in sorted(contract.source_function_hashes.items())
    ]


def _valid_role_rows(contract):
    rows = []
    role_specs = (
        (contract.marker_owner_role, "owner", False, True, True, True),
        (contract.audit_reader_role, "audit", True, False, False, False),
        *(
            (role, "writer", True, False, True, False)
            for role in contract.writer_roles
        ),
    )
    for table in contract.in_scope_tables:
        schema, name = table.split(".")
        for role, kind, can_login, owns, can_write, can_assume_owner in role_specs:
            rows.append(
                {
                    "role_name": role,
                    "role_kind": kind,
                    "table_schema": schema,
                    "table_name": name,
                    "role_exists": True,
                    "is_superuser": False,
                    "is_replication": False,
                    "is_bypass_rls": False,
                    "can_create_db": False,
                    "can_create_role": False,
                    "can_login": can_login,
                    "has_inherited_roles": False,
                    "owns_table": owns,
                    "can_insert": can_write,
                    "can_update": can_write,
                    "can_delete": can_write,
                    "can_truncate": can_write,
                    "can_references": owns,
                    "can_trigger": owns,
                    "can_create_schema": False,
                    "can_set_replication_role": False,
                    "can_assume_owner": can_assume_owner,
                }
            )
    return rows


def _valid_privileged_role_rows(contract):
    return [
        {"role_name": role}
        for role in sorted(contract.approved_privileged_roles)
    ]


class _ValidMarkerConnection:
    dialect = "postgres"

    def __init__(self, contract, *, event_position=41):
        self.contract = contract
        self.event_position = event_position
        self.statements = []
        self.privileged_credential_inventory_anchor_hash = (
            contract.privileged_credential_inventory_anchor_hash
        )

    def execute(self, sql):
        self.statements.append(sql)
        if " AS acquired" in sql:
            return _RowsResult([{"acquired": True}])
        if " AS released" in sql:
            return _RowsResult([{"released": True}])
        if "postgres_exact_marker:session" in sql:
            return _RowsResult(
                [
                    {
                        "audit_role_matches": True,
                        "transaction_read_only": True,
                        "replication_role_origin": True,
                    }
                ]
            )
        if "postgres_exact_marker:parameter_acl" in sql:
            return _RowsResult([{"parameter_acl_supported": True}])
        if "postgres_exact_marker:projection" in sql:
            return _RowsResult(
                [
                    {
                        "schema_generation": self.contract.schema_generation,
                        "event_position": self.event_position,
                        "chain_hash": "sha256:" + "2" * 64,
                        "coverage_hash": self.contract.expected_coverage_hash,
                    }
                ]
            )
        if "postgres_exact_marker:triggers" in sql:
            return _RowsResult(_valid_trigger_rows(self.contract))
        if "postgres_exact_marker:functions" in sql:
            return _RowsResult(_valid_function_rows(self.contract))
        if "postgres_exact_marker:roles" in sql:
            return _RowsResult(_valid_role_rows(self.contract))
        if "postgres_exact_marker:privileged_roles" in sql:
            return _RowsResult(_valid_privileged_role_rows(self.contract))
        if "postgres_exact_marker:unregistered_writers" in sql:
            return _RowsResult([])
        raise AssertionError(f"unexpected SQL: {sql}")

def test_contract_coverage_hash_is_canonical_and_public_safe():
    first = _contract()
    reordered = _contract(
        tables=("public.ledger", "public.authority"),
        writer_roles=("neurons_writer",),
    )

    assert first.expected_coverage_hash == reordered.expected_coverage_hash
    assert _SHA256_PATTERN.fullmatch(first.expected_coverage_hash)
    assert "authority" not in first.expected_coverage_hash
    assert "ledger" not in first.expected_coverage_hash


def test_source_owned_contract_registry_exactly_covers_all_schema_fragments():
    from agent_knowledge.postgres_exact_mutation_marker import (
        SOURCE_OWNED_AUTHORITY_LEDGER_TABLE_REGISTRY,
        build_source_owned_postgres_exact_marker_contract,
    )

    contract = build_source_owned_postgres_exact_marker_contract(
        schema_generation="sha256:" + "1" * 64,
        writer_roles=("neurons_writer",),
        marker_owner_role="neurons_marker_owner",
        audit_reader_role="neurons_marker_reader",
        advisory_lock_key=7_211_740_091,
        approved_privileged_roles=("postgres_bootstrap",),
        privileged_credential_inventory_anchor_hash="sha256:" + "8" * 64,
    )
    source_root = Path(__file__).parents[1] / "lib" / "agent_knowledge"
    fragment_paths = {
        "ledger": source_root / "ledger.py",
        "llm_brain_core.ledger_adapter": (
            source_root / "llm_brain_core" / "ledger_adapter.py"
        ),
    }
    actual_by_fragment = {
        fragment: {
            f"public.{name}"
            for name in re.findall(
                r"CREATE TABLE IF NOT EXISTS\s+([a-z_][a-z0-9_]*)",
                path.read_text(encoding="utf-8"),
            )
        }
        for fragment, path in fragment_paths.items()
    }
    expected_by_fragment = {
        fragment: set(tables)
        for fragment, tables in SOURCE_OWNED_AUTHORITY_LEDGER_TABLE_REGISTRY.items()
    }

    assert expected_by_fragment == actual_by_fragment
    assert set(contract.in_scope_tables) == set().union(*actual_by_fragment.values())
    assert "public.schema_migrations" in contract.in_scope_tables
    assert "public.object_authority_states" in contract.in_scope_tables
    assert "public.llm_brain_session_memory_artifacts" in contract.in_scope_tables
    assert "public.llm_brain_graph_projection_state" in contract.in_scope_tables


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_generation", "generation-1"),
        ("in_scope_tables", ()),
        ("in_scope_tables", ("public.ledger", "public.ledger")),
        ("in_scope_tables", ("public.ledger; DROP TABLE marker",)),
        ("writer_roles", ()),
        ("writer_roles", ("neurons_writer", "neurons_writer")),
        ("writer_roles", ("neurons-writer",)),
        ("marker_owner_role", "PUBLIC"),
        ("audit_reader_role", "neurons reader"),
        ("approved_privileged_roles", ()),
        ("approved_privileged_roles", ("postgres_bootstrap", "postgres_bootstrap")),
        ("privileged_credential_inventory_anchor_hash", "not-a-hash"),
        ("advisory_lock_key", 2**63),
        ("advisory_lock_key", -(2**63) - 1),
    ),
)
def test_contract_rejects_malformed_or_ambiguous_coverage(field, value):
    with pytest.raises(ValueError, match="PostgreSQL exact marker contract"):
        replace(_contract(), **{field: value})


def test_activation_sql_installs_transaction_exact_statement_marker_and_fence():
    sql = _contract().render_activation_sql()
    normalized = " ".join(sql.split()).lower()

    assert "create table if not exists public.authority_ledger_exact_mutation_marker_v1" in normalized
    assert "schema_generation text not null" in normalized
    assert "event_position bigint not null" in normalized
    assert "chain_hash text not null" in normalized
    assert "coverage_hash text not null" in normalized
    assert "pg_advisory_xact_lock_shared(7211740091)" in normalized
    assert normalized.count(
        "before insert or update or delete or truncate on"
    ) == 2
    assert normalized.count(
        "after insert or update or delete or truncate on"
    ) == 2
    assert normalized.count("for each statement") == 4
    assert normalized.count("enable always trigger") == 4
    assert "select event_position, chain_hash, schema_generation" in normalized
    assert "for update" in normalized
    assert "event_position = next_position" in normalized
    assert "9223372036854775807" in normalized
    assert "sha256" in normalized
    assert "tg_table_schema" in normalized
    assert "tg_table_name" in normalized
    assert "tg_op" in normalized
    assert "create or replace view public.authority_ledger_exact_mutation_marker_projection_v1" in normalized
    assert "pg_catalog.pg_parameter_acl" in normalized
    assert "pg_catalog.has_parameter_privilege" in normalized
    assert "server_version_num" in normalized
    assert "postgres_bootstrap" in normalized
    assert "postgresql exact marker privileged role prerequisite failed" in normalized

    assert "for each row" not in normalized
    assert "pg_stat_" not in normalized
    assert "txid" not in normalized
    assert "xmin" not in normalized
    assert "count(" not in normalized


def test_activation_sql_preflights_exact_owner_writer_split_before_any_mutation():
    contract = _contract()
    sql = contract.render_activation_sql()
    normalized = " ".join(sql.split()).lower()

    preflight_start = normalized.index("do $activation_preflight$")
    preflight_end = normalized.index("$activation_preflight$;", preflight_start)
    first_mutation = normalized.index(
        "create table if not exists public.authority_ledger_exact_mutation_marker_v1"
    )

    assert normalized.startswith("begin;")
    assert preflight_start < preflight_end < first_mutation
    assert normalized.endswith("commit;")
    for mutation_sql in (
        "create table",
        "alter table",
        "insert into",
        "create or replace function",
        "revoke all",
        "grant execute",
        "drop trigger",
        "create trigger",
        "create or replace view",
        "alter view",
    ):
        assert normalized.index(mutation_sql) > preflight_end
    assert "session_user" in normalized
    assert "rolsuper" in normalized
    assert "rolreplication" in normalized
    assert "rolbypassrls" in normalized
    assert "rolcreatedb" in normalized
    assert "rolcreaterole" in normalized
    assert "rolcanlogin" in normalized
    assert "pg_catalog.pg_auth_members" in normalized
    assert "pg_catalog.has_schema_privilege" in normalized
    assert "pg_catalog.has_table_privilege" in normalized
    for privilege in ("insert", "update", "delete", "truncate"):
        assert f"table_row.oid, '{privilege}'" in normalized
    assert "'trigger'" in normalized
    assert "'references'" in normalized
    assert "table_row.relowner <> owner_role.oid" in normalized
    assert "postgresql exact marker operator prerequisite failed" in normalized
    assert "postgresql exact marker owner prerequisite failed" in normalized
    assert "postgresql exact marker table prerequisite failed" in normalized
    assert "postgresql exact marker writer prerequisite failed" in normalized
    assert "postgresql exact marker audit reader prerequisite failed" in normalized


def test_activation_sql_never_auto_provisions_or_elevates_source_roles():
    contract = _contract()
    normalized = " ".join(contract.render_activation_sql().split()).lower()

    assert "create role" not in normalized
    assert "alter role" not in normalized
    assert "grant insert" not in normalized
    assert "grant update" not in normalized
    assert "grant delete" not in normalized
    assert "grant truncate" not in normalized
    for table in contract.in_scope_tables:
        assert f"alter table {table} owner" not in normalized


def test_rendered_function_body_bytes_match_coverage_hashes_exactly():
    contract = _contract()
    sql = contract.render_activation_sql()
    rendered_bodies = {
        name: body
        for name, body in re.findall(
            r"CREATE OR REPLACE FUNCTION public\.([a-z0-9_]+)\(\).*?"
            r"AS \$exact_marker\$(.*?)\$exact_marker\$;",
            sql,
            flags=re.DOTALL,
        )
    }

    assert set(rendered_bodies) == set(contract.source_function_hashes)
    assert {
        name: "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
        for name, body in rendered_bodies.items()
    } == contract.source_function_hashes
    assert rendered_bodies[
        "authority_ledger_exact_mutation_before_v1"
    ].rstrip().endswith("END;")
    assert rendered_bodies[
        "authority_ledger_exact_mutation_after_v1"
    ].rstrip().endswith("END;")

    marker_state_body = re.search(
        r"DO \$marker_state\$(.*?)\$marker_state\$;",
        sql,
        flags=re.DOTALL,
    )
    assert marker_state_body is not None
    assert marker_state_body.group(1).rstrip().endswith("END;")


def test_activation_sql_keeps_marker_in_product_transaction_and_leaks_no_row_state():
    normalized = " ".join(_contract().render_activation_sql().split()).lower()

    assert "security definer" in normalized
    assert "revoke all on function public.authority_ledger_exact_mutation_before_v1() from public" in normalized
    assert "revoke all on function public.authority_ledger_exact_mutation_after_v1() from public" in normalized
    assert "grant select on table public.authority_ledger_exact_mutation_marker_projection_v1" in normalized
    assert "grant execute on function public.try_authority_ledger_exact_audit_fence_v1()" in normalized
    assert "grant execute on function public.release_authority_ledger_exact_audit_fence_v1()" in normalized

    assert normalized.startswith("begin;")
    assert normalized.endswith("commit;")
    assert normalized.count("begin;") == 1
    assert normalized.count("commit;") == 1
    assert "dblink" not in normalized
    assert "row_count" not in normalized
    assert "row_id" not in normalized
    assert "primary_key" not in normalized
    assert "query_text" not in normalized
    assert "transaction_id" not in normalized


def test_reader_fails_closed_before_marker_read_when_a_writer_holds_the_fence():
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerError,
        PostgresExactMutationMarkerReader,
    )

    class Result:
        def fetchall(self):
            return [{"acquired": False}]

    class Connection:
        dialect = "postgres"
        privileged_credential_inventory_anchor_hash = "sha256:" + "8" * 64

        def __init__(self):
            self.statements = []

        def execute(self, sql):
            self.statements.append(sql)
            return Result()

    connection = Connection()

    with pytest.raises(PostgresExactMutationMarkerError, match="fence is unavailable"):
        PostgresExactMutationMarkerReader(_contract()).acquire_audit_fence(connection)

    assert len(connection.statements) == 1
    assert "try_authority_ledger_exact_audit_fence_v1" in connection.statements[0]
    assert "projection" not in connection.statements[0]


def test_reader_requires_external_privileged_credential_inventory_anchor_before_sql():
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerError,
        PostgresExactMutationMarkerReader,
    )

    contract = _contract()
    connection = _ValidMarkerConnection(contract)
    connection.privileged_credential_inventory_anchor_hash = "sha256:" + "9" * 64

    with pytest.raises(PostgresExactMutationMarkerError, match="credential inventory"):
        PostgresExactMutationMarkerReader(contract).acquire_audit_fence(connection)

    assert connection.statements == []


def test_fence_context_releases_once_on_the_same_session():
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerReader,
    )

    class Result:
        def __init__(self, row):
            self._row = row

        def fetchall(self):
            return [self._row]

    class Connection:
        dialect = "postgres"
        privileged_credential_inventory_anchor_hash = "sha256:" + "8" * 64

        def __init__(self):
            self.statements = []

        def execute(self, sql):
            self.statements.append(sql)
            if "try_authority" in sql:
                return Result({"acquired": True})
            if "release_authority" in sql:
                return Result({"released": True})
            raise AssertionError(f"unexpected SQL: {sql}")

    connection = Connection()
    lease = PostgresExactMutationMarkerReader(_contract()).acquire_audit_fence(
        connection
    )

    with lease as active_lease:
        assert active_lease is lease
        assert lease.is_active is True

    assert lease.is_active is False
    assert len(connection.statements) == 2
    assert "try_authority_ledger_exact_audit_fence_v1" in connection.statements[0]
    assert "release_authority_ledger_exact_audit_fence_v1" in connection.statements[1]


def test_fenced_read_returns_only_sanitized_exact_coverage_evidence():
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerReader,
    )

    contract = _contract()
    connection = _ValidMarkerConnection(contract, event_position=41)

    with PostgresExactMutationMarkerReader(contract).acquire_audit_fence(
        connection
    ) as fence:
        state = fence.read_marker()

    evidence = state.as_evidence()
    assert evidence == {
        "plane": "authority_ledger",
        "generation_hash": contract.schema_generation,
        "event_position_hash": state.event_position_hash,
        "marker_hash": "sha256:" + "2" * 64,
        "in_flight_count": 0,
        "in_flight_status": "clear",
        "coverage_hash": contract.expected_coverage_hash,
        "coverage_status": "validated",
        "read_scope_status": "read_only",
        "reset_or_decrease_count": 0,
        "read_call_count": 1,
    }
    assert _SHA256_PATTERN.fullmatch(state.event_position_hash)
    serialized = repr(evidence)
    assert "event_position" not in evidence
    assert 41 not in evidence.values()
    assert "public.authority" not in serialized
    assert "neurons_writer" not in serialized

    read_statements = connection.statements[1:-1]
    assert len(read_statements) == 8
    assert sum("postgres_exact_marker:projection" in sql for sql in read_statements) == 1
    assert all("pg_stat_" not in sql.lower() for sql in read_statements)


def test_pre_and_post_snapshots_share_one_fence_but_each_report_one_read():
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerReader,
    )

    contract = _contract()
    connection = _ValidMarkerConnection(contract)

    with PostgresExactMutationMarkerReader(contract).acquire_audit_fence(
        connection
    ) as fence:
        pre = fence.read_marker().as_evidence()
        post = fence.read_marker().as_evidence()

    assert pre["read_call_count"] == 1
    assert post["read_call_count"] == 1
    assert sum(" AS acquired" in sql for sql in connection.statements) == 1
    assert sum(" AS released" in sql for sql in connection.statements) == 1
    assert sum(
        "postgres_exact_marker:projection" in sql for sql in connection.statements
    ) == 2


def test_fence_context_releases_in_finally_when_audit_body_fails():
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerReader,
    )

    class AuditBodyError(RuntimeError):
        pass

    contract = _contract()
    connection = _ValidMarkerConnection(contract)

    with pytest.raises(AuditBodyError):
        with PostgresExactMutationMarkerReader(contract).acquire_audit_fence(
            connection
        ):
            raise AuditBodyError("bounded audit failed")

    assert sum(" AS acquired" in sql for sql in connection.statements) == 1
    assert sum(" AS released" in sql for sql in connection.statements) == 1


@pytest.mark.parametrize(
    "projection_rows",
    (
        [],
        [
            {
                "schema_generation": "sha256:" + "1" * 64,
                "event_position": 1,
                "chain_hash": "sha256:" + "2" * 64,
                "coverage_hash": "sha256:" + "3" * 64,
            },
            {
                "schema_generation": "sha256:" + "1" * 64,
                "event_position": 1,
                "chain_hash": "sha256:" + "2" * 64,
                "coverage_hash": "sha256:" + "3" * 64,
            },
        ],
        [
            {
                "schema_generation": "sha256:" + "9" * 64,
                "event_position": 1,
                "chain_hash": "sha256:" + "2" * 64,
                "coverage_hash": "sha256:" + "3" * 64,
            }
        ],
        [
            {
                "schema_generation": "sha256:" + "1" * 64,
                "event_position": 2**63 - 1,
                "chain_hash": "sha256:" + "2" * 64,
                "coverage_hash": "sha256:" + "3" * 64,
            }
        ],
        [
            {
                "schema_generation": "sha256:" + "1" * 64,
                "event_position": -1,
                "chain_hash": "sha256:" + "2" * 64,
                "coverage_hash": "sha256:" + "3" * 64,
            }
        ],
        [
            {
                "schema_generation": "sha256:" + "1" * 64,
                "event_position": True,
                "chain_hash": "sha256:" + "2" * 64,
                "coverage_hash": "sha256:" + "3" * 64,
            }
        ],
        [
            {
                "schema_generation": "sha256:" + "1" * 64,
                "event_position": 1,
                "chain_hash": "not-a-hash",
                "coverage_hash": "sha256:" + "3" * 64,
            }
        ],
    ),
)
def test_fenced_read_rejects_malformed_mismatched_or_exhausted_marker(
    projection_rows,
):
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerError,
        PostgresExactMutationMarkerReader,
    )

    contract = _contract()

    class Connection(_ValidMarkerConnection):
        def execute(self, sql):
            if "postgres_exact_marker:projection" in sql:
                self.statements.append(sql)
                rows = []
                for row in projection_rows:
                    candidate = dict(row)
                    if candidate.get("coverage_hash") == "sha256:" + "3" * 64:
                        candidate["coverage_hash"] = contract.expected_coverage_hash
                    rows.append(candidate)
                return _RowsResult(rows)
            return super().execute(sql)

    with pytest.raises(PostgresExactMutationMarkerError):
        with PostgresExactMutationMarkerReader(contract).acquire_audit_fence(
            Connection(contract)
        ) as fence:
            fence.read_marker()


@pytest.mark.parametrize(
    "drift",
    (
        "trigger_missing",
        "trigger_disabled",
        "trigger_extra",
        "function_body",
        "function_config",
        "function_overload",
        "role_missing",
        "role_superuser",
        "role_bypass_rls",
        "role_createdb",
        "role_createrole",
        "role_inherited",
        "role_schema_create",
        "role_trigger",
        "role_references",
        "role_parameter_bypass",
        "role_owner",
        "role_bypass",
        "privileged_role_missing",
        "privileged_role_extra",
        "unregistered_writer",
        "unregistered_privileged_admin",
        "unsupported_parameter_acl",
        "unsafe_session",
    ),
)
def test_fenced_read_rejects_catalog_coverage_or_role_bypass_drift(drift):
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerError,
        PostgresExactMutationMarkerReader,
    )

    contract = _contract()

    class Connection(_ValidMarkerConnection):
        def execute(self, sql):
            if "postgres_exact_marker:triggers" in sql:
                self.statements.append(sql)
                rows = _valid_trigger_rows(contract)
                if drift == "trigger_missing":
                    rows.pop()
                elif drift == "trigger_disabled":
                    rows[0] = {**rows[0], "trigger_enabled": "D"}
                elif drift == "trigger_extra":
                    rows.append(dict(rows[0]))
                return _RowsResult(rows)
            if "postgres_exact_marker:functions" in sql:
                self.statements.append(sql)
                rows = _valid_function_rows(contract)
                if drift == "function_body":
                    rows[0] = {
                        **rows[0],
                        "function_body_hash": "sha256:" + "9" * 64,
                    }
                elif drift == "function_config":
                    rows[0] = {**rows[0], "function_config": "search_path=public"}
                elif drift == "function_overload":
                    rows.append({**rows[0], "identity_arguments": "text"})
                return _RowsResult(rows)
            if "postgres_exact_marker:roles" in sql:
                self.statements.append(sql)
                rows = _valid_role_rows(contract)
                if drift == "role_missing":
                    rows.pop()
                elif drift == "role_superuser":
                    rows[0] = {**rows[0], "is_superuser": True}
                elif drift == "role_bypass_rls":
                    rows[0] = {**rows[0], "is_bypass_rls": True}
                elif drift == "role_createdb":
                    rows[0] = {**rows[0], "can_create_db": True}
                elif drift == "role_createrole":
                    rows[0] = {**rows[0], "can_create_role": True}
                elif drift == "role_inherited":
                    rows[0] = {**rows[0], "has_inherited_roles": True}
                elif drift == "role_schema_create":
                    rows[0] = {**rows[0], "can_create_schema": True}
                elif drift == "role_trigger":
                    writer_index = next(
                        index
                        for index, row in enumerate(rows)
                        if row["role_kind"] == "writer"
                    )
                    rows[writer_index] = {
                        **rows[writer_index],
                        "can_trigger": True,
                    }
                elif drift == "role_references":
                    writer_index = next(
                        index
                        for index, row in enumerate(rows)
                        if row["role_kind"] == "writer"
                    )
                    rows[writer_index] = {
                        **rows[writer_index],
                        "can_references": True,
                    }
                elif drift == "role_parameter_bypass":
                    writer_index = next(
                        index
                        for index, row in enumerate(rows)
                        if row["role_kind"] == "writer"
                    )
                    rows[writer_index] = {
                        **rows[writer_index],
                        "can_set_replication_role": True,
                    }
                elif drift == "role_owner":
                    writer_index = next(
                        index
                        for index, row in enumerate(rows)
                        if row["role_kind"] == "writer"
                    )
                    rows[writer_index] = {**rows[writer_index], "owns_table": True}
                elif drift == "role_bypass":
                    writer_index = next(
                        index
                        for index, row in enumerate(rows)
                        if row["role_kind"] == "writer"
                    )
                    rows[writer_index] = {
                        **rows[writer_index],
                        "can_assume_owner": True,
                    }
                return _RowsResult(rows)
            if "postgres_exact_marker:unregistered_writers" in sql:
                self.statements.append(sql)
                if drift in {
                    "unregistered_writer",
                    "unregistered_privileged_admin",
                }:
                    return _RowsResult(
                        [
                            {
                                "role_name": "unknown_writer",
                                "table_schema": "public",
                                "table_name": "ledger",
                            }
                        ]
                    )
                return _RowsResult([])
            if "postgres_exact_marker:privileged_roles" in sql:
                self.statements.append(sql)
                rows = _valid_privileged_role_rows(contract)
                if drift == "privileged_role_missing":
                    rows.clear()
                elif drift == "privileged_role_extra":
                    rows.append({"role_name": "unknown_admin"})
                return _RowsResult(rows)
            if (
                "postgres_exact_marker:parameter_acl" in sql
                and drift == "unsupported_parameter_acl"
            ):
                self.statements.append(sql)
                return _RowsResult([{"parameter_acl_supported": False}])
            if "postgres_exact_marker:session" in sql and drift == "unsafe_session":
                self.statements.append(sql)
                return _RowsResult(
                    [
                        {
                            "audit_role_matches": True,
                            "transaction_read_only": False,
                            "replication_role_origin": True,
                        }
                    ]
                )
            return super().execute(sql)

    with pytest.raises(PostgresExactMutationMarkerError):
        with PostgresExactMutationMarkerReader(contract).acquire_audit_fence(
            Connection(contract)
        ) as fence:
            fence.read_marker()


@pytest.mark.parametrize("release_mode", ("not_held", "connection_lost"))
def test_fence_release_failure_is_fail_closed_and_not_retried(release_mode):
    from agent_knowledge.postgres_exact_mutation_marker import (
        PostgresExactMutationMarkerError,
        PostgresExactMutationMarkerReader,
    )

    class Connection:
        dialect = "postgres"
        privileged_credential_inventory_anchor_hash = "sha256:" + "8" * 64

        def __init__(self):
            self.release_calls = 0

        def execute(self, sql):
            if " AS acquired" in sql:
                return _RowsResult([{"acquired": True}])
            if " AS released" in sql:
                self.release_calls += 1
                if release_mode == "connection_lost":
                    raise ConnectionError("connection closed")
                return _RowsResult([{"released": False}])
            raise AssertionError(f"unexpected SQL: {sql}")

    connection = Connection()
    fence = PostgresExactMutationMarkerReader(_contract()).acquire_audit_fence(
        connection
    )

    with pytest.raises(PostgresExactMutationMarkerError):
        fence.release()

    assert fence.is_active is False
    assert connection.release_calls == 1
    with pytest.raises(PostgresExactMutationMarkerError, match="not active"):
        fence.release()
    assert connection.release_calls == 1

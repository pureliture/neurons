"""PostgreSQL ``authority_ledger`` exact mutation marker source contract.

The module is deliberately independent from a live PostgreSQL driver.  It owns the
canonical contract, activation SQL, and read-only fence/marker verification boundary;
callers provide a PostgreSQL-compatible connection.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType


_CONTRACT_VERSION = "postgres_authority_ledger_exact_mutation_marker.v1"
_IDENTIFIER_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SIGNED_BIGINT_MIN = -(2**63)
_SIGNED_BIGINT_MAX = 2**63 - 1
_MARKER_TABLE = "authority_ledger_exact_mutation_marker_v1"
_PROJECTION_VIEW = "authority_ledger_exact_mutation_marker_projection_v1"
_BEFORE_FUNCTION = "authority_ledger_exact_mutation_before_v1"
_AFTER_FUNCTION = "authority_ledger_exact_mutation_after_v1"
_TRY_FENCE_FUNCTION = "try_authority_ledger_exact_audit_fence_v1"
_RELEASE_FENCE_FUNCTION = "release_authority_ledger_exact_audit_fence_v1"
_BEFORE_TRIGGER = "authority_ledger_exact_marker_before_v1"
_AFTER_TRIGGER = "authority_ledger_exact_marker_after_v1"
_LEDGER_SCHEMA_TABLES = (
    "public.auto_recall_audit",
    "public.backfill_sources",
    "public.context_pack_items",
    "public.context_packs",
    "public.dirty_project_memory",
    "public.dirty_session_memory",
    "public.eval_queries",
    "public.eval_runs",
    "public.index_targets",
    "public.ingest_attempts",
    "public.knowledge_items",
    "public.llm_brain_feedback_records",
    "public.llm_brain_memory_cards",
    "public.llm_brain_projection_jobs",
    "public.llm_brain_source_refs",
    "public.memory_candidates",
    "public.memory_card_evidence",
    "public.memory_cards",
    "public.memory_gc_audit",
    "public.native_memory_mirror",
    "public.object_authority_decisions",
    "public.object_authority_states",
    "public.object_review_proposals",
    "public.profile_facts",
    "public.project_memory_active_snapshots",
    "public.provider_source_contracts",
    "public.qdrant_collections",
    "public.reference_corpus_bundles",
    "public.reference_corpus_document_chunks",
    "public.reference_corpus_document_snapshots",
    "public.reference_corpus_document_sources",
    "public.reference_corpus_document_versions",
    "public.reference_corpus_extraction_runs",
    "public.reference_corpus_freshness_checks",
    "public.retrieval_audit",
    "public.scheduler_runs",
    "public.schema_migrations",
    "public.session_memory_active_snapshots",
    "public.session_memory_coverage_edges",
    "public.session_memory_terminal_skipped_audit",
    "public.tool_evidence_summaries",
    "public.transcript_chunks",
    "public.transcript_sessions",
    "public.transcript_tool_events",
    "public.transcript_turns",
    "public.transcript_validation_files",
)
_LEDGER_ADAPTER_SCHEMA_TABLES = (
    "public.llm_brain_graph_projection_state",
    "public.llm_brain_session_memory_artifacts",
    "public.llm_brain_source_refs",
)
SOURCE_OWNED_AUTHORITY_LEDGER_TABLE_REGISTRY = MappingProxyType(
    {
        "ledger": _LEDGER_SCHEMA_TABLES,
        "llm_brain_core.ledger_adapter": _LEDGER_ADAPTER_SCHEMA_TABLES,
    }
)
SOURCE_OWNED_AUTHORITY_LEDGER_TABLES = tuple(
    sorted(
        {
            table
            for tables in SOURCE_OWNED_AUTHORITY_LEDGER_TABLE_REGISTRY.values()
            for table in tables
        }
    )
)


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class PostgresExactMutationMarkerContract:
    """Source-approved PostgreSQL marker and writer-coverage contract."""

    schema_generation: str
    in_scope_tables: tuple[str, ...]
    writer_roles: tuple[str, ...]
    marker_owner_role: str
    audit_reader_role: str
    advisory_lock_key: int
    approved_privileged_roles: tuple[str, ...]
    privileged_credential_inventory_anchor_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.schema_generation, str) or not _SHA256_PATTERN.fullmatch(
            self.schema_generation
        ):
            _invalid_contract()
        tables = _validated_unique_names(self.in_scope_tables, qualified=True)
        writers = _validated_unique_names(self.writer_roles, qualified=False)
        privileged_roles = _validated_unique_names(
            self.approved_privileged_roles,
            qualified=False,
        )
        if not tables or not writers or not privileged_roles:
            _invalid_contract()
        for role in (self.marker_owner_role, self.audit_reader_role):
            if not _is_identifier(role) or role.casefold() == "public":
                _invalid_contract()
        role_set = (
            set(writers)
            | set(privileged_roles)
            | {self.marker_owner_role, self.audit_reader_role}
        )
        if len(role_set) != len(writers) + len(privileged_roles) + 2:
            _invalid_contract()
        if not _is_sha256(self.privileged_credential_inventory_anchor_hash):
            _invalid_contract()
        if (
            isinstance(self.advisory_lock_key, bool)
            or not isinstance(self.advisory_lock_key, int)
            or not _SIGNED_BIGINT_MIN
            <= self.advisory_lock_key
            <= _SIGNED_BIGINT_MAX
        ):
            _invalid_contract()
        object.__setattr__(self, "in_scope_tables", tuple(sorted(tables)))
        object.__setattr__(self, "writer_roles", tuple(sorted(writers)))
        object.__setattr__(
            self,
            "approved_privileged_roles",
            tuple(sorted(privileged_roles)),
        )

    @property
    def expected_coverage_hash(self) -> str:
        payload = {
            "advisory_lock_key": self.advisory_lock_key,
            "audit_reader_role": self.audit_reader_role,
            "contract_version": _CONTRACT_VERSION,
            "in_scope_tables": sorted(self.in_scope_tables),
            "marker_owner_role": self.marker_owner_role,
            "approved_privileged_roles": sorted(self.approved_privileged_roles),
            "privileged_credential_inventory_anchor_hash": (
                self.privileged_credential_inventory_anchor_hash
            ),
            "schema_generation": self.schema_generation,
            "source_function_hashes": self.source_function_hashes,
            "writer_roles": sorted(self.writer_roles),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return _sha256(encoded)

    @property
    def source_function_hashes(self) -> dict[str, str]:
        return {
            name: _sha256(source.encode("utf-8"))
            for name, source in sorted(self._function_sources().items())
        }

    def render_activation_sql(self) -> str:
        """Render one operator-only transaction with a mutation-free preflight."""

        owner = _quote_identifier(self.marker_owner_role)
        reader = _quote_identifier(self.audit_reader_role)
        coverage_hash = self.expected_coverage_hash
        initial_chain_hash = _sha256(
            (
                f"{_CONTRACT_VERSION}\x1finit\x1f{self.schema_generation}"
                f"\x1f{coverage_hash}"
            ).encode("utf-8")
        )
        functions = self._function_sources()
        statements = [
            f"""
CREATE TABLE IF NOT EXISTS public.{_MARKER_TABLE} (
  singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton IS TRUE),
  schema_generation text NOT NULL
    CHECK (schema_generation ~ '^sha256:[0-9a-f]{{64}}$'),
  event_position bigint NOT NULL CHECK (event_position >= 0),
  chain_hash text NOT NULL CHECK (chain_hash ~ '^sha256:[0-9a-f]{{64}}$'),
  coverage_hash text NOT NULL CHECK (coverage_hash ~ '^sha256:[0-9a-f]{{64}}$')
);
ALTER TABLE public.{_MARKER_TABLE} OWNER TO {owner};
REVOKE ALL ON TABLE public.{_MARKER_TABLE} FROM PUBLIC;
INSERT INTO public.{_MARKER_TABLE} (
  singleton, schema_generation, event_position, chain_hash, coverage_hash
) VALUES (
  TRUE, '{self.schema_generation}', 0, '{initial_chain_hash}', '{coverage_hash}'
)
ON CONFLICT (singleton) DO NOTHING;
DO $marker_state$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM public.{_MARKER_TABLE}
    WHERE singleton IS TRUE
      AND schema_generation = '{self.schema_generation}'
      AND coverage_hash = '{coverage_hash}'
      AND event_position >= 0
      AND chain_hash ~ '^sha256:[0-9a-f]{{64}}$'
  ) THEN
    RAISE EXCEPTION 'PostgreSQL exact marker state mismatch';
  END IF;
END;
$marker_state$;
""".strip()
        ]
        statements.extend(
            (
                _render_trigger_function(_BEFORE_FUNCTION, functions[_BEFORE_FUNCTION], owner),
                _render_trigger_function(_AFTER_FUNCTION, functions[_AFTER_FUNCTION], owner),
                _render_boolean_function(
                    _TRY_FENCE_FUNCTION, functions[_TRY_FENCE_FUNCTION], owner
                ),
                _render_boolean_function(
                    _RELEASE_FENCE_FUNCTION,
                    functions[_RELEASE_FENCE_FUNCTION],
                    owner,
                ),
                f"""
REVOKE ALL ON FUNCTION public.{_BEFORE_FUNCTION}() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.{_AFTER_FUNCTION}() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.{_TRY_FENCE_FUNCTION}() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.{_RELEASE_FENCE_FUNCTION}() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.{_TRY_FENCE_FUNCTION}() TO {reader};
GRANT EXECUTE ON FUNCTION public.{_RELEASE_FENCE_FUNCTION}() TO {reader};
""".strip(),
            )
        )
        for table in self.in_scope_tables:
            quoted_table = _quote_qualified_identifier(table)
            statements.append(
                f"""
DROP TRIGGER IF EXISTS {_BEFORE_TRIGGER} ON {quoted_table};
CREATE TRIGGER {_BEFORE_TRIGGER}
BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON {quoted_table}
FOR EACH STATEMENT EXECUTE FUNCTION public.{_BEFORE_FUNCTION}();
ALTER TABLE {quoted_table} ENABLE ALWAYS TRIGGER {_BEFORE_TRIGGER};
DROP TRIGGER IF EXISTS {_AFTER_TRIGGER} ON {quoted_table};
CREATE TRIGGER {_AFTER_TRIGGER}
AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON {quoted_table}
FOR EACH STATEMENT EXECUTE FUNCTION public.{_AFTER_FUNCTION}();
ALTER TABLE {quoted_table} ENABLE ALWAYS TRIGGER {_AFTER_TRIGGER};
""".strip()
            )
        statements.append(
            f"""
CREATE OR REPLACE VIEW public.{_PROJECTION_VIEW}
WITH (security_barrier = true) AS
SELECT schema_generation, event_position, chain_hash, coverage_hash
FROM public.{_MARKER_TABLE}
WHERE singleton IS TRUE;
ALTER VIEW public.{_PROJECTION_VIEW} OWNER TO {owner};
REVOKE ALL ON TABLE public.{_PROJECTION_VIEW} FROM PUBLIC;
GRANT SELECT ON TABLE public.{_PROJECTION_VIEW} TO {reader};
""".strip()
        )
        return "\n\n".join(
            (
                "BEGIN;",
                self._render_activation_preflight_sql(),
                *statements,
                "COMMIT;",
            )
        ) + "\n"

    def _render_activation_preflight_sql(self) -> str:
        """Require an operator-provisioned owner/writer split before any DDL."""

        owner = self.marker_owner_role
        reader = self.audit_reader_role
        writer_values = ", ".join(f"('{role}')" for role in self.writer_roles)
        privileged_values = ", ".join(
            f"('{role}')" for role in self.approved_privileged_roles
        )
        privileged_names = ", ".join(
            f"'{role}'" for role in self.approved_privileged_roles
        )
        table_values = ", ".join(
            f"('{table.split('.')[0]}', '{table.split('.')[1]}')"
            for table in self.in_scope_tables
        )
        registered_roles = ", ".join(
            f"'{role}'"
            for role in (
                owner,
                reader,
                *self.writer_roles,
                *self.approved_privileged_roles,
            )
        )
        return f"""
/* postgres_exact_marker:activation_preflight */
DO $activation_preflight$
DECLARE
  owner_role pg_catalog.pg_roles%ROWTYPE;
  audit_role pg_catalog.pg_roles%ROWTYPE;
  writer_role pg_catalog.pg_roles%ROWTYPE;
  privileged_role pg_catalog.pg_roles%ROWTYPE;
  table_row record;
  expected_table record;
  expected_writer record;
  expected_privileged record;
BEGIN
  IF current_setting('server_version_num')::integer < 150000
    OR pg_catalog.to_regclass('pg_catalog.pg_parameter_acl') IS NULL
    OR pg_catalog.to_regprocedure(
      'pg_catalog.has_parameter_privilege(oid,text,text)'
    ) IS NULL
  THEN
    RAISE EXCEPTION 'PostgreSQL exact marker parameter ACL prerequisite failed';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles AS operator_role
    WHERE operator_role.rolname = session_user
      AND operator_role.rolsuper IS TRUE
      AND operator_role.rolname IN ({privileged_names})
  )
    OR current_setting('transaction_read_only') <> 'off'
    OR current_setting('session_replication_role') <> 'origin'
  THEN
    RAISE EXCEPTION 'PostgreSQL exact marker operator prerequisite failed';
  END IF;

  FOR expected_privileged IN
    SELECT * FROM (VALUES {privileged_values}) AS approved_role(role_name)
  LOOP
    SELECT * INTO privileged_role
    FROM pg_catalog.pg_roles
    WHERE rolname = expected_privileged.role_name;
    IF NOT FOUND OR NOT (
      privileged_role.rolsuper
      OR privileged_role.rolreplication
      OR privileged_role.rolbypassrls
      OR privileged_role.rolcreatedb
      OR privileged_role.rolcreaterole
      OR pg_catalog.has_parameter_privilege(
        privileged_role.oid, 'session_replication_role', 'SET'
      )
    )
    THEN
      RAISE EXCEPTION 'PostgreSQL exact marker privileged role prerequisite failed';
    END IF;
  END LOOP;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles AS role_row
    WHERE role_row.rolname NOT IN ({privileged_names})
      AND (
        role_row.rolsuper
        OR role_row.rolreplication
        OR role_row.rolbypassrls
        OR role_row.rolcreatedb
        OR role_row.rolcreaterole
        OR pg_catalog.has_parameter_privilege(
          role_row.oid, 'session_replication_role', 'SET'
        )
      )
  )
  THEN
    RAISE EXCEPTION 'PostgreSQL exact marker privileged role prerequisite failed';
  END IF;

  SELECT * INTO owner_role
  FROM pg_catalog.pg_roles
  WHERE rolname = '{owner}';
  IF NOT FOUND
    OR owner_role.rolsuper
    OR owner_role.rolreplication
    OR owner_role.rolbypassrls
    OR owner_role.rolcreatedb
    OR owner_role.rolcreaterole
    OR owner_role.rolcanlogin
    OR pg_catalog.has_parameter_privilege(
      owner_role.oid, 'session_replication_role', 'SET'
    )
    OR pg_catalog.has_schema_privilege(owner_role.oid, 'public', 'CREATE')
    OR EXISTS (
      SELECT 1 FROM pg_catalog.pg_auth_members
      WHERE member = owner_role.oid
    )
  THEN
    RAISE EXCEPTION 'PostgreSQL exact marker owner prerequisite failed';
  END IF;

  SELECT * INTO audit_role
  FROM pg_catalog.pg_roles
  WHERE rolname = '{reader}';
  IF NOT FOUND
    OR audit_role.rolsuper
    OR audit_role.rolreplication
    OR audit_role.rolbypassrls
    OR audit_role.rolcreatedb
    OR audit_role.rolcreaterole
    OR NOT audit_role.rolcanlogin
    OR pg_catalog.has_parameter_privilege(
      audit_role.oid, 'session_replication_role', 'SET'
    )
    OR pg_catalog.has_schema_privilege(audit_role.oid, 'public', 'CREATE')
    OR pg_catalog.pg_has_role(audit_role.oid, owner_role.oid, 'MEMBER')
    OR EXISTS (
      SELECT 1 FROM pg_catalog.pg_auth_members
      WHERE member = audit_role.oid
    )
  THEN
    RAISE EXCEPTION 'PostgreSQL exact marker audit reader prerequisite failed';
  END IF;

  FOR expected_table IN
    SELECT * FROM (VALUES {table_values}) AS source_table(table_schema, table_name)
  LOOP
    SELECT table_class.oid, table_class.relowner, table_class.relkind
      INTO table_row
    FROM pg_catalog.pg_class AS table_class
    JOIN pg_catalog.pg_namespace AS table_ns
      ON table_ns.oid = table_class.relnamespace
    WHERE table_ns.nspname = expected_table.table_schema
      AND table_class.relname = expected_table.table_name;
    IF NOT FOUND
      OR table_row.relkind NOT IN ('r', 'p')
      OR table_row.relowner <> owner_role.oid
    THEN
      RAISE EXCEPTION 'PostgreSQL exact marker table prerequisite failed';
    END IF;
    IF pg_catalog.has_table_privilege(
         audit_role.oid, table_row.oid, 'INSERT,UPDATE,DELETE,TRUNCATE'
       )
      OR pg_catalog.has_table_privilege(audit_role.oid, table_row.oid, 'TRIGGER')
      OR pg_catalog.has_table_privilege(audit_role.oid, table_row.oid, 'REFERENCES')
    THEN
      RAISE EXCEPTION 'PostgreSQL exact marker audit reader prerequisite failed';
    END IF;
  END LOOP;

  FOR expected_writer IN
    SELECT * FROM (VALUES {writer_values}) AS source_writer(role_name)
  LOOP
    SELECT * INTO writer_role
    FROM pg_catalog.pg_roles
    WHERE rolname = expected_writer.role_name;
    IF NOT FOUND
      OR writer_role.rolsuper
      OR writer_role.rolreplication
      OR writer_role.rolbypassrls
      OR writer_role.rolcreatedb
      OR writer_role.rolcreaterole
      OR NOT writer_role.rolcanlogin
      OR pg_catalog.has_parameter_privilege(
        writer_role.oid, 'session_replication_role', 'SET'
      )
      OR pg_catalog.has_schema_privilege(writer_role.oid, 'public', 'CREATE')
      OR pg_catalog.pg_has_role(writer_role.oid, owner_role.oid, 'MEMBER')
      OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members
        WHERE member = writer_role.oid
      )
    THEN
      RAISE EXCEPTION 'PostgreSQL exact marker writer prerequisite failed';
    END IF;
    FOR expected_table IN
      SELECT * FROM (VALUES {table_values}) AS source_table(table_schema, table_name)
    LOOP
      SELECT table_class.oid, table_class.relowner, table_class.relkind
        INTO table_row
      FROM pg_catalog.pg_class AS table_class
      JOIN pg_catalog.pg_namespace AS table_ns
        ON table_ns.oid = table_class.relnamespace
      WHERE table_ns.nspname = expected_table.table_schema
        AND table_class.relname = expected_table.table_name;
      IF table_row.relowner = writer_role.oid
        OR NOT pg_catalog.has_table_privilege(
          writer_role.oid, table_row.oid, 'INSERT'
        )
        OR NOT pg_catalog.has_table_privilege(
          writer_role.oid, table_row.oid, 'UPDATE'
        )
        OR NOT pg_catalog.has_table_privilege(
          writer_role.oid, table_row.oid, 'DELETE'
        )
        OR NOT pg_catalog.has_table_privilege(
          writer_role.oid, table_row.oid, 'TRUNCATE'
        )
        OR pg_catalog.has_table_privilege(writer_role.oid, table_row.oid, 'TRIGGER')
        OR pg_catalog.has_table_privilege(writer_role.oid, table_row.oid, 'REFERENCES')
      THEN
        RAISE EXCEPTION 'PostgreSQL exact marker writer prerequisite failed';
      END IF;
    END LOOP;
  END LOOP;

  IF EXISTS (
    SELECT 1
    FROM (VALUES {table_values}) AS expected_table(table_schema, table_name)
    JOIN pg_catalog.pg_namespace AS table_ns
      ON table_ns.nspname = expected_table.table_schema
    JOIN pg_catalog.pg_class AS table_row
      ON table_row.relnamespace = table_ns.oid
     AND table_row.relname = expected_table.table_name
    CROSS JOIN pg_catalog.pg_roles AS role_row
    WHERE role_row.rolname NOT IN ({registered_roles})
      AND pg_catalog.has_table_privilege(
        role_row.oid, table_row.oid, 'INSERT,UPDATE,DELETE,TRUNCATE'
      )
  ) OR EXISTS (
    SELECT 1
    FROM (VALUES {table_values}) AS expected_table(table_schema, table_name)
    JOIN pg_catalog.pg_namespace AS table_ns
      ON table_ns.nspname = expected_table.table_schema
    JOIN pg_catalog.pg_class AS table_row
      ON table_row.relnamespace = table_ns.oid
     AND table_row.relname = expected_table.table_name
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(
        table_row.relacl,
        pg_catalog.acldefault('r', table_row.relowner)
      )
    ) AS privilege_row
    WHERE privilege_row.grantee = 0
      AND privilege_row.privilege_type IN (
        'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'
      )
  ) OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles AS role_row
    WHERE role_row.rolsuper IS FALSE
      AND role_row.rolname NOT IN ({registered_roles})
      AND pg_catalog.has_parameter_privilege(
        role_row.oid, 'session_replication_role', 'SET'
      )
  ) OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_parameter_acl AS parameter_row
    CROSS JOIN LATERAL pg_catalog.aclexplode(parameter_row.paracl)
      AS privilege_row
    WHERE parameter_row.parname = 'session_replication_role'
      AND privilege_row.grantee = 0
      AND privilege_row.privilege_type = 'SET'
  )
  THEN
    RAISE EXCEPTION 'PostgreSQL exact marker writer prerequisite failed';
  END IF;
END;
$activation_preflight$;
""".strip()

    def _function_sources(self) -> dict[str, str]:
        before_source = f"""
BEGIN
  IF TG_LEVEL <> 'STATEMENT' OR TG_OP NOT IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE') THEN
    RAISE EXCEPTION 'PostgreSQL exact marker BEFORE trigger contract mismatch';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock_shared({self.advisory_lock_key});
  RETURN NULL;
END;
""".strip()
        after_source = f"""
DECLARE
  previous_position bigint;
  previous_chain_hash text;
  marker_generation text;
  next_position bigint;
BEGIN
  IF TG_LEVEL <> 'STATEMENT' OR TG_OP NOT IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE') THEN
    RAISE EXCEPTION 'PostgreSQL exact marker AFTER trigger contract mismatch';
  END IF;
  SELECT event_position, chain_hash, schema_generation
    INTO STRICT previous_position, previous_chain_hash, marker_generation
  FROM public.{_MARKER_TABLE}
  WHERE singleton IS TRUE
  FOR UPDATE;
  IF previous_position >= {_SIGNED_BIGINT_MAX} THEN
    RAISE EXCEPTION USING
      ERRCODE = '22003',
      MESSAGE = 'PostgreSQL exact marker event position overflow';
  END IF;
  next_position := previous_position + 1;
  UPDATE public.{_MARKER_TABLE}
  SET event_position = next_position,
      chain_hash = 'sha256:' || pg_catalog.encode(
        pg_catalog.sha256(
          pg_catalog.convert_to(
            pg_catalog.concat_ws(
              E'\\x1f',
              '{_CONTRACT_VERSION}',
              previous_chain_hash,
              next_position::text,
              TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME,
              pg_catalog.lower(TG_OP),
              marker_generation
            ),
            'UTF8'
          )
        ),
        'hex'
      )
  WHERE singleton IS TRUE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'PostgreSQL exact marker singleton is unavailable';
  END IF;
  RETURN NULL;
END;
""".strip()
        return {
            _BEFORE_FUNCTION: before_source,
            _AFTER_FUNCTION: after_source,
            _TRY_FENCE_FUNCTION: (
                f"SELECT pg_catalog.pg_try_advisory_lock({self.advisory_lock_key})"
            ),
            _RELEASE_FENCE_FUNCTION: (
                f"SELECT pg_catalog.pg_advisory_unlock({self.advisory_lock_key})"
            ),
        }


def build_source_owned_postgres_exact_marker_contract(
    *,
    schema_generation: str,
    writer_roles: tuple[str, ...],
    marker_owner_role: str,
    audit_reader_role: str,
    advisory_lock_key: int,
    approved_privileged_roles: tuple[str, ...],
    privileged_credential_inventory_anchor_hash: str,
) -> PostgresExactMutationMarkerContract:
    """Bind production coverage to every source-owned ledger table."""

    return PostgresExactMutationMarkerContract(
        schema_generation=schema_generation,
        in_scope_tables=SOURCE_OWNED_AUTHORITY_LEDGER_TABLES,
        writer_roles=writer_roles,
        marker_owner_role=marker_owner_role,
        audit_reader_role=audit_reader_role,
        advisory_lock_key=advisory_lock_key,
        approved_privileged_roles=approved_privileged_roles,
        privileged_credential_inventory_anchor_hash=(
            privileged_credential_inventory_anchor_hash
        ),
    )


class PostgresExactMutationMarkerError(RuntimeError):
    """Fail-closed PostgreSQL exact marker/fence contract failure."""


@dataclass(frozen=True)
class PostgresExactMutationMarkerState:
    """Public-safe projection; the raw event position never leaves the reader."""

    event_position_hash: str
    generation_hash: str
    marker_hash: str
    coverage_hash: str
    read_call_count: int

    def as_evidence(self) -> dict[str, object]:
        return {
            "plane": "authority_ledger",
            "generation_hash": self.generation_hash,
            "event_position_hash": self.event_position_hash,
            "marker_hash": self.marker_hash,
            "in_flight_count": 0,
            "in_flight_status": "clear",
            "coverage_hash": self.coverage_hash,
            "coverage_status": "validated",
            "read_scope_status": "read_only",
            "reset_or_decrease_count": 0,
            "read_call_count": self.read_call_count,
        }


class PostgresExactMutationMarkerReader:
    """Acquire the fixed session fence before exposing any marker read surface."""

    _ACQUIRE_SQL = f"SELECT public.{_TRY_FENCE_FUNCTION}() AS acquired"

    def __init__(self, contract: PostgresExactMutationMarkerContract) -> None:
        if not isinstance(contract, PostgresExactMutationMarkerContract):
            raise TypeError("PostgreSQL exact marker contract is required")
        self._contract = contract

    def acquire_audit_fence(self, connection: object) -> "PostgresExactMutationMarkerFence":
        if getattr(connection, "dialect", "") != "postgres":
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker connection is unavailable"
            )
        if (
            getattr(
                connection,
                "privileged_credential_inventory_anchor_hash",
                None,
            )
            != self._contract.privileged_credential_inventory_anchor_hash
        ):
            raise PostgresExactMutationMarkerError(
                "PostgreSQL privileged credential inventory is unavailable"
            )
        try:
            rows = connection.execute(self._ACQUIRE_SQL).fetchall()  # type: ignore[attr-defined]
            acquired = _exact_boolean_row(rows, "acquired")
        except PostgresExactMutationMarkerError:
            raise
        except Exception as exc:
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker fence acquisition failed"
            ) from exc
        if not acquired:
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker fence is unavailable"
            )
        return PostgresExactMutationMarkerFence(self._contract, connection)


class PostgresExactMutationMarkerFence:
    _RELEASE_SQL = f"SELECT public.{_RELEASE_FENCE_FUNCTION}() AS released"
    _SESSION_SQL = """
/* postgres_exact_marker:session */
SELECT current_user = '{audit_role}' AS audit_role_matches,
       current_setting('transaction_read_only') = 'on' AS transaction_read_only,
       current_setting('session_replication_role') = 'origin'
         AS replication_role_origin
"""
    _PARAMETER_ACL_SQL = """
/* postgres_exact_marker:parameter_acl */
SELECT current_setting('server_version_num')::integer >= 150000
         AND pg_catalog.to_regclass('pg_catalog.pg_parameter_acl') IS NOT NULL
         AND pg_catalog.to_regprocedure(
           'pg_catalog.has_parameter_privilege(oid,text,text)'
         ) IS NOT NULL
         AS parameter_acl_supported
"""
    _PROJECTION_SQL = f"""
/* postgres_exact_marker:projection */
SELECT schema_generation, event_position, chain_hash, coverage_hash
FROM public.{_PROJECTION_VIEW}
LIMIT 2
"""
    _TRIGGER_SQL = f"""
/* postgres_exact_marker:triggers */
SELECT table_ns.nspname AS table_schema,
       table_class.relname AS table_name,
       trigger_row.tgname AS trigger_name,
       trigger_row.tgenabled AS trigger_enabled,
       trigger_row.tgtype AS trigger_type,
       function_ns.nspname AS function_schema,
       function_row.proname AS function_name,
       'sha256:' || pg_catalog.encode(
         pg_catalog.sha256(pg_catalog.convert_to(function_row.prosrc, 'UTF8')),
         'hex'
       ) AS function_body_hash
FROM pg_catalog.pg_trigger AS trigger_row
JOIN pg_catalog.pg_class AS table_class
  ON table_class.oid = trigger_row.tgrelid
JOIN pg_catalog.pg_namespace AS table_ns
  ON table_ns.oid = table_class.relnamespace
JOIN pg_catalog.pg_proc AS function_row
  ON function_row.oid = trigger_row.tgfoid
JOIN pg_catalog.pg_namespace AS function_ns
  ON function_ns.oid = function_row.pronamespace
WHERE trigger_row.tgisinternal IS FALSE
  AND (
    trigger_row.tgname IN ('{_BEFORE_TRIGGER}', '{_AFTER_TRIGGER}')
    OR (
      function_ns.nspname = 'public'
      AND function_row.proname IN ('{_BEFORE_FUNCTION}', '{_AFTER_FUNCTION}')
    )
  )
ORDER BY table_schema, table_name, trigger_name
"""
    _FUNCTION_SQL = f"""
/* postgres_exact_marker:functions */
SELECT function_ns.nspname AS function_schema,
       function_row.proname AS function_name,
       'sha256:' || pg_catalog.encode(
         pg_catalog.sha256(pg_catalog.convert_to(function_row.prosrc, 'UTF8')),
         'hex'
       ) AS function_body_hash,
       function_row.prosecdef AS is_security_definer,
       function_owner.rolname AS function_owner,
       pg_catalog.pg_get_function_identity_arguments(function_row.oid)
         AS identity_arguments,
       function_row.prokind AS function_kind,
       function_row.provolatile AS volatility,
       function_row.proparallel AS parallel_mode,
       function_language.lanname AS language_name,
       COALESCE(
         pg_catalog.array_to_string(function_row.proconfig, E'\\x1f'),
         ''
       ) AS function_config
FROM pg_catalog.pg_proc AS function_row
JOIN pg_catalog.pg_namespace AS function_ns
  ON function_ns.oid = function_row.pronamespace
JOIN pg_catalog.pg_roles AS function_owner
  ON function_owner.oid = function_row.proowner
JOIN pg_catalog.pg_language AS function_language
  ON function_language.oid = function_row.prolang
WHERE function_ns.nspname = 'public'
  AND function_row.proname IN (
    '{_BEFORE_FUNCTION}', '{_AFTER_FUNCTION}',
    '{_TRY_FENCE_FUNCTION}', '{_RELEASE_FENCE_FUNCTION}'
  )
ORDER BY function_name, identity_arguments
"""

    def __init__(
        self,
        contract: PostgresExactMutationMarkerContract,
        connection: object,
    ) -> None:
        self._contract = contract
        self._connection = connection
        self._active = True

    @property
    def is_active(self) -> bool:
        return self._active

    def release(self) -> None:
        if not self._active:
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker fence is not active"
            )
        try:
            result = self._connection.execute(self._RELEASE_SQL)  # type: ignore[attr-defined]
            rows = result.fetchall()
            released = _exact_boolean_row(rows, "released")
        except PostgresExactMutationMarkerError:
            raise
        except Exception as exc:
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker fence release failed"
            ) from exc
        finally:
            self._active = False
        if not released:
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker fence release was incomplete"
            )

    def read_marker(self) -> PostgresExactMutationMarkerState:
        if not self._active:
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker fence is not active"
            )
        try:
            session_rows = self._execute_rows(
                self._SESSION_SQL.format(
                    audit_role=self._contract.audit_reader_role
                )
            )
            _validate_session_rows(session_rows)
            parameter_acl_rows = self._execute_rows(self._PARAMETER_ACL_SQL)
            _validate_parameter_acl_rows(parameter_acl_rows)
            privileged_role_rows = self._execute_rows(
                self._render_privileged_role_sql()
            )
            _validate_privileged_role_rows(
                privileged_role_rows,
                self._contract,
            )
            projection_rows = self._execute_rows(self._PROJECTION_SQL)
            trigger_rows = self._execute_rows(self._TRIGGER_SQL)
            function_rows = self._execute_rows(self._FUNCTION_SQL)
            role_rows = self._execute_rows(self._render_role_sql())
            unregistered_rows = self._execute_rows(
                self._render_unregistered_writer_sql()
            )
            marker = _validate_projection_rows(projection_rows, self._contract)
            _validate_trigger_rows(trigger_rows, self._contract)
            _validate_function_rows(function_rows, self._contract)
            _validate_role_rows(role_rows, self._contract)
            if unregistered_rows:
                raise PostgresExactMutationMarkerError(
                    "PostgreSQL exact marker writer coverage is invalid"
                )
        except PostgresExactMutationMarkerError:
            raise
        except Exception as exc:
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker validation failed"
            ) from exc
        position_hash = _sha256(
            (
                f"{_CONTRACT_VERSION}\x1fauthority_ledger\x1f"
                f"{marker['schema_generation']}\x1f{marker['event_position']}"
            ).encode("utf-8")
        )
        return PostgresExactMutationMarkerState(
            event_position_hash=position_hash,
            generation_hash=marker["schema_generation"],
            marker_hash=marker["chain_hash"],
            coverage_hash=marker["coverage_hash"],
            read_call_count=1,
        )

    def _execute_rows(self, sql: str) -> list[object]:
        rows = self._connection.execute(sql).fetchall()  # type: ignore[attr-defined]
        if not isinstance(rows, list):
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker response is malformed"
            )
        return rows

    def _render_role_sql(self) -> str:
        role_values = [(self._contract.marker_owner_role, "owner")]
        role_values.append((self._contract.audit_reader_role, "audit"))
        role_values.extend((role, "writer") for role in self._contract.writer_roles)
        roles_sql = ", ".join(
            f"('{role}', '{kind}')" for role, kind in role_values
        )
        tables_sql = ", ".join(
            f"('{table.split('.')[0]}', '{table.split('.')[1]}')"
            for table in self._contract.in_scope_tables
        )
        owner = self._contract.marker_owner_role
        return f"""
/* postgres_exact_marker:roles */
WITH expected_roles(role_name, role_kind) AS (VALUES {roles_sql}),
     expected_tables(table_schema, table_name) AS (VALUES {tables_sql})
SELECT expected_roles.role_name,
       expected_roles.role_kind,
       expected_tables.table_schema,
       expected_tables.table_name,
       role_row.oid IS NOT NULL AS role_exists,
       COALESCE(role_row.rolsuper, FALSE) AS is_superuser,
       COALESCE(role_row.rolreplication, FALSE) AS is_replication,
       COALESCE(role_row.rolbypassrls, FALSE) AS is_bypass_rls,
       COALESCE(role_row.rolcreatedb, FALSE) AS can_create_db,
       COALESCE(role_row.rolcreaterole, FALSE) AS can_create_role,
       COALESCE(role_row.rolcanlogin, FALSE) AS can_login,
       COALESCE(EXISTS (
         SELECT 1
         FROM pg_catalog.pg_auth_members AS membership_row
         WHERE membership_row.member = role_row.oid
       ), FALSE) AS has_inherited_roles,
       COALESCE(table_row.relowner = role_row.oid, FALSE) AS owns_table,
       COALESCE(pg_catalog.has_table_privilege(
         role_row.oid, table_row.oid, 'INSERT'
       ), FALSE) AS can_insert,
       COALESCE(pg_catalog.has_table_privilege(
         role_row.oid, table_row.oid, 'UPDATE'
       ), FALSE) AS can_update,
       COALESCE(pg_catalog.has_table_privilege(
         role_row.oid, table_row.oid, 'DELETE'
       ), FALSE) AS can_delete,
       COALESCE(pg_catalog.has_table_privilege(
         role_row.oid, table_row.oid, 'TRUNCATE'
       ), FALSE) AS can_truncate,
       COALESCE(pg_catalog.has_table_privilege(
         role_row.oid, table_row.oid, 'REFERENCES'
       ), FALSE) AS can_references,
       COALESCE(pg_catalog.has_table_privilege(
         role_row.oid, table_row.oid, 'TRIGGER'
       ), FALSE) AS can_trigger,
       COALESCE(pg_catalog.has_schema_privilege(
         role_row.oid, 'public', 'CREATE'
       ), FALSE) AS can_create_schema,
       COALESCE(pg_catalog.has_parameter_privilege(
         role_row.oid, 'session_replication_role', 'SET'
       ), FALSE) AS can_set_replication_role,
       CASE
         WHEN expected_roles.role_kind = 'owner' THEN TRUE
         WHEN role_row.oid IS NULL OR owner_role.oid IS NULL THEN FALSE
         ELSE pg_catalog.pg_has_role(role_row.oid, owner_role.oid, 'MEMBER')
       END AS can_assume_owner
FROM expected_roles
CROSS JOIN expected_tables
LEFT JOIN pg_catalog.pg_roles AS role_row
  ON role_row.rolname = expected_roles.role_name
LEFT JOIN pg_catalog.pg_namespace AS table_ns
  ON table_ns.nspname = expected_tables.table_schema
LEFT JOIN pg_catalog.pg_class AS table_row
  ON table_row.relnamespace = table_ns.oid
 AND table_row.relname = expected_tables.table_name
LEFT JOIN pg_catalog.pg_roles AS owner_role
  ON owner_role.rolname = '{owner}'
ORDER BY table_schema, table_name, role_kind, role_name
"""

    def _render_privileged_role_sql(self) -> str:
        return """
/* postgres_exact_marker:privileged_roles */
SELECT role_row.rolname AS role_name
FROM pg_catalog.pg_roles AS role_row
WHERE role_row.rolsuper
   OR role_row.rolreplication
   OR role_row.rolbypassrls
   OR role_row.rolcreatedb
   OR role_row.rolcreaterole
   OR pg_catalog.has_parameter_privilege(
     role_row.oid, 'session_replication_role', 'SET'
   )
ORDER BY role_name
"""

    def _render_unregistered_writer_sql(self) -> str:
        expected_roles = (
            self._contract.marker_owner_role,
            self._contract.audit_reader_role,
            *self._contract.writer_roles,
            *self._contract.approved_privileged_roles,
        )
        roles_sql = ", ".join(f"'{role}'" for role in expected_roles)
        tables_sql = ", ".join(
            f"('{table.split('.')[0]}', '{table.split('.')[1]}')"
            for table in self._contract.in_scope_tables
        )
        return f"""
/* postgres_exact_marker:unregistered_writers */
WITH expected_tables(table_schema, table_name) AS (VALUES {tables_sql})
SELECT role_row.rolname AS role_name,
       expected_tables.table_schema,
       expected_tables.table_name
FROM expected_tables
JOIN pg_catalog.pg_namespace AS table_ns
  ON table_ns.nspname = expected_tables.table_schema
JOIN pg_catalog.pg_class AS table_row
  ON table_row.relnamespace = table_ns.oid
 AND table_row.relname = expected_tables.table_name
CROSS JOIN pg_catalog.pg_roles AS role_row
WHERE role_row.rolname NOT IN ({roles_sql})
  AND (
    pg_catalog.has_table_privilege(role_row.oid, table_row.oid, 'INSERT')
    OR pg_catalog.has_table_privilege(role_row.oid, table_row.oid, 'UPDATE')
    OR pg_catalog.has_table_privilege(role_row.oid, table_row.oid, 'DELETE')
    OR pg_catalog.has_table_privilege(role_row.oid, table_row.oid, 'TRUNCATE')
  )
UNION ALL
SELECT '<public>' AS role_name,
       expected_tables.table_schema,
       expected_tables.table_name
FROM expected_tables
JOIN pg_catalog.pg_namespace AS table_ns
  ON table_ns.nspname = expected_tables.table_schema
JOIN pg_catalog.pg_class AS table_row
  ON table_row.relnamespace = table_ns.oid
 AND table_row.relname = expected_tables.table_name
CROSS JOIN LATERAL pg_catalog.aclexplode(
  COALESCE(
    table_row.relacl,
    pg_catalog.acldefault('r', table_row.relowner)
  )
) AS privilege_row
WHERE privilege_row.grantee = 0
  AND privilege_row.privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
UNION ALL
SELECT role_row.rolname AS role_name,
       '<parameter>' AS table_schema,
       'session_replication_role' AS table_name
FROM pg_catalog.pg_roles AS role_row
WHERE role_row.rolsuper IS FALSE
  AND role_row.rolname NOT IN ({roles_sql})
  AND pg_catalog.has_parameter_privilege(
    role_row.oid, 'session_replication_role', 'SET'
  )
UNION ALL
SELECT '<public>' AS role_name,
       '<parameter>' AS table_schema,
       'session_replication_role' AS table_name
FROM pg_catalog.pg_parameter_acl AS parameter_row
CROSS JOIN LATERAL pg_catalog.aclexplode(parameter_row.paracl)
  AS privilege_row
WHERE parameter_row.parname = 'session_replication_role'
  AND privilege_row.grantee = 0
  AND privilege_row.privilege_type = 'SET'
"""

    def __enter__(self) -> "PostgresExactMutationMarkerFence":
        if not self._active:
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker fence is not active"
            )
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        self.release()
        return False


def _exact_boolean_row(rows: object, key: str) -> bool:
    if not isinstance(rows, list) or len(rows) != 1:
        raise PostgresExactMutationMarkerError(
            "PostgreSQL exact marker response is malformed"
        )
    row = rows[0]
    try:
        if set(row.keys()) != {key}:  # type: ignore[union-attr]
            raise TypeError
        value = row[key]  # type: ignore[index]
    except (AttributeError, KeyError, TypeError):
        raise PostgresExactMutationMarkerError(
            "PostgreSQL exact marker response is malformed"
        ) from None
    if not isinstance(value, bool):
        raise PostgresExactMutationMarkerError(
            "PostgreSQL exact marker response is malformed"
        )
    return value


def _validate_session_rows(rows: list[object]) -> None:
    values = _exact_rows(
        rows,
        (
            "audit_role_matches",
            "transaction_read_only",
            "replication_role_origin",
        ),
    )
    if len(values) != 1 or any(value is not True for value in values[0].values()):
        raise PostgresExactMutationMarkerError(
            "PostgreSQL exact marker read session is unsafe"
        )


def _validate_parameter_acl_rows(rows: list[object]) -> None:
    values = _exact_rows(rows, ("parameter_acl_supported",))
    if len(values) != 1 or values[0]["parameter_acl_supported"] is not True:
        raise PostgresExactMutationMarkerError(
            "PostgreSQL exact marker parameter ACL is unsupported"
        )


def _validate_privileged_role_rows(
    rows: list[object],
    contract: PostgresExactMutationMarkerContract,
) -> None:
    values = _exact_rows(rows, ("role_name",))
    actual = [row["role_name"] for row in values]
    if (
        any(not isinstance(role, str) for role in actual)
        or len(actual) != len(set(actual))
        or set(actual) != set(contract.approved_privileged_roles)
    ):
        raise PostgresExactMutationMarkerError(
            "PostgreSQL exact marker privileged role coverage is invalid"
        )


def _validate_projection_rows(
    rows: list[object], contract: PostgresExactMutationMarkerContract
) -> dict[str, object]:
    values = _exact_rows(
        rows,
        ("schema_generation", "event_position", "chain_hash", "coverage_hash"),
    )
    if len(values) != 1:
        raise PostgresExactMutationMarkerError(
            "PostgreSQL exact marker singleton is malformed"
        )
    marker = values[0]
    position = marker["event_position"]
    if (
        marker["schema_generation"] != contract.schema_generation
        or isinstance(position, bool)
        or not isinstance(position, int)
        or not 0 <= position < _SIGNED_BIGINT_MAX
        or not _is_sha256(marker["chain_hash"])
        or marker["coverage_hash"] != contract.expected_coverage_hash
    ):
        raise PostgresExactMutationMarkerError(
            "PostgreSQL exact marker singleton is malformed"
        )
    return marker


def _validate_trigger_rows(
    rows: list[object], contract: PostgresExactMutationMarkerContract
) -> None:
    keys = (
        "table_schema",
        "table_name",
        "trigger_name",
        "trigger_enabled",
        "trigger_type",
        "function_schema",
        "function_name",
        "function_body_hash",
    )
    expected = []
    for table in contract.in_scope_tables:
        schema, name = table.split(".")
        expected.extend(
            (
                {
                    "table_schema": schema,
                    "table_name": name,
                    "trigger_name": _BEFORE_TRIGGER,
                    "trigger_enabled": "A",
                    "trigger_type": 62,
                    "function_schema": "public",
                    "function_name": _BEFORE_FUNCTION,
                    "function_body_hash": contract.source_function_hashes[
                        _BEFORE_FUNCTION
                    ],
                },
                {
                    "table_schema": schema,
                    "table_name": name,
                    "trigger_name": _AFTER_TRIGGER,
                    "trigger_enabled": "A",
                    "trigger_type": 60,
                    "function_schema": "public",
                    "function_name": _AFTER_FUNCTION,
                    "function_body_hash": contract.source_function_hashes[
                        _AFTER_FUNCTION
                    ],
                },
            )
        )
    _validate_exact_row_set(rows, keys, expected, "trigger")


def _validate_function_rows(
    rows: list[object], contract: PostgresExactMutationMarkerContract
) -> None:
    keys = (
        "function_schema",
        "function_name",
        "function_body_hash",
        "is_security_definer",
        "function_owner",
        "identity_arguments",
        "function_kind",
        "volatility",
        "parallel_mode",
        "language_name",
        "function_config",
    )
    expected = [
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
                if name in {_BEFORE_FUNCTION, _AFTER_FUNCTION}
                else "sql"
            ),
            "function_config": "search_path=pg_catalog, public",
        }
        for name, body_hash in contract.source_function_hashes.items()
    ]
    _validate_exact_row_set(rows, keys, expected, "function")


def _validate_role_rows(
    rows: list[object], contract: PostgresExactMutationMarkerContract
) -> None:
    keys = (
        "role_name",
        "role_kind",
        "table_schema",
        "table_name",
        "role_exists",
        "is_superuser",
        "is_replication",
        "is_bypass_rls",
        "can_create_db",
        "can_create_role",
        "can_login",
        "has_inherited_roles",
        "owns_table",
        "can_insert",
        "can_update",
        "can_delete",
        "can_truncate",
        "can_references",
        "can_trigger",
        "can_create_schema",
        "can_set_replication_role",
        "can_assume_owner",
    )
    role_specs = (
        (contract.marker_owner_role, "owner", False, True, True, True),
        (contract.audit_reader_role, "audit", True, False, False, False),
        *((role, "writer", True, False, True, False) for role in contract.writer_roles),
    )
    expected = []
    for table in contract.in_scope_tables:
        schema, name = table.split(".")
        for role, kind, can_login, owns, can_write, can_assume_owner in role_specs:
            expected.append(
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
    _validate_exact_row_set(rows, keys, expected, "role")


def _validate_exact_row_set(
    rows: list[object],
    keys: tuple[str, ...],
    expected: list[dict[str, object]],
    label: str,
) -> None:
    values = _exact_rows(rows, keys)
    try:
        actual_rows = [tuple(row[key] for key in keys) for row in values]
        expected_rows = [tuple(row[key] for key in keys) for row in expected]
        valid = len(actual_rows) == len(set(actual_rows)) and set(actual_rows) == set(
            expected_rows
        )
    except TypeError:
        valid = False
    if not valid:
        raise PostgresExactMutationMarkerError(
            f"PostgreSQL exact marker {label} coverage is invalid"
        )


def _exact_rows(
    rows: list[object], keys: tuple[str, ...]
) -> list[dict[str, object]]:
    result = []
    expected_keys = set(keys)
    for row in rows:
        try:
            if set(row.keys()) != expected_keys:  # type: ignore[union-attr]
                raise TypeError
            result.append({key: row[key] for key in keys})  # type: ignore[index]
        except (AttributeError, KeyError, TypeError):
            raise PostgresExactMutationMarkerError(
                "PostgreSQL exact marker response is malformed"
            ) from None
    return result


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256_PATTERN.fullmatch(value))


def _validated_unique_names(values: object, *, qualified: bool) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        _invalid_contract()
    names = tuple(values)
    if len(names) != len(set(names)):
        _invalid_contract()
    for name in names:
        if not isinstance(name, str):
            _invalid_contract()
        parts = name.split(".") if qualified else [name]
        if len(parts) != (2 if qualified else 1) or not all(
            _is_identifier(part) for part in parts
        ):
            _invalid_contract()
    return names


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER_PATTERN.fullmatch(value))


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_qualified_identifier(value: str) -> str:
    return ".".join(_quote_identifier(part) for part in value.split("."))


def _render_trigger_function(name: str, source: str, owner: str) -> str:
    return f"""
CREATE OR REPLACE FUNCTION public.{name}() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $exact_marker${source}$exact_marker$;
ALTER FUNCTION public.{name}() OWNER TO {owner};
""".strip()


def _render_boolean_function(name: str, source: str, owner: str) -> str:
    return f"""
CREATE OR REPLACE FUNCTION public.{name}() RETURNS boolean
LANGUAGE sql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $exact_marker${source}$exact_marker$;
ALTER FUNCTION public.{name}() OWNER TO {owner};
""".strip()


def _invalid_contract() -> None:
    raise ValueError("PostgreSQL exact marker contract is malformed or ambiguous")

"""CouchDB-backed transcript/tool-evidence source store.

This package owns the migration of ``transcript-memory`` from a RetiredIndexBridge dataset
to a CouchDB-backed source/evidence plane (design:
``specs/couchdb-transcript-migration/design.md``).

Plane ownership contract (M1):

- CouchDB owns the *source/evidence* plane: ``transcript_session``,
  ``conversation_chunk``, ``tool_evidence_bundle``, ``coverage_manifest``,
  ``projection_state``, ``retention_manifest`` documents.
- RetiredIndexBridge owns *only* the derived ``session-memory`` recall surface after
  cutover. ``transcript-memory`` is a retired RetiredIndexBridge profile and is never a
  CouchDB-source doc type nor a valid post-cutover projection target.
- No raw path, raw id, secret, or un-redacted transcript body crosses into a
  CouchDB-source document. Hashes only.
"""

from .document_model import (  # noqa: F401
    COUCHDB_SOURCE_OWNER,
    COUCHDB_SOURCE_SCHEMA_VERSION,
    COUCHDB_OWNED_DOC_TYPES,
    RETIRED_INDEX_BRIDGE_RECALL_PROFILE,
    RETIRED_RETIRED_INDEX_BRIDGE_PROFILE,
    OwnershipViolation,
    ProjectionStatus,
    RetentionTier,
    SourceDocType,
    SourceRedactionLeak,
    assert_couchdb_owned,
    assert_no_secret_like_metadata,
    assert_index_target_allowed,
    assert_source_text_clean,
    build_conversation_chunk_document,
    build_coverage_hash,
    build_coverage_manifest_document,
    build_projection_state_document,
    build_retention_manifest_document,
    build_session_id_hash,
    build_source_locator_hash,
    build_tool_evidence_bundle_document,
    build_transcript_session_document,
    conversation_chunk_doc_id,
    coverage_manifest_doc_id,
    projection_state_doc_id,
    retention_manifest_doc_id,
    session_doc_id,
    sha256_hash,
    tool_evidence_bundle_doc_id,
)
from .source_store import (  # noqa: F401
    CouchDBSourceStore,
    InMemoryCouchDBSourceStore,
    SourceStoreConflict,
    SourceStoreError,
    StoredRevision,
    merge_transcript_session_documents,
    payload_hash,
    validate_for_write,
)
from .couchdb_http_store import (  # noqa: F401
    CouchDBError,
    CouchDBHttpSourceStore,
)
from .project_authority import (  # noqa: F401
    ProjectAuthorityInput,
    ProjectAuthoritySource,
    ProjectResolution,
    resolve_project,
)
from .historical_import import (  # noqa: F401
    PROVIDER_LANES,
    ImportResult,
    ImportStatus,
    ProviderLane,
    SourceLocator,
    import_historical_source,
    import_historical_sources,
)
from .tool_evidence_bundler import (  # noqa: F401
    build_tool_evidence_bundle_documents,
    store_tool_evidence_bundles,
)
from .session_memory_materializer import (  # noqa: F401
    MaterializedSessionMemory,
    RecordingSessionMemoryProjector,
    SessionMemoryProjector,
    materialize_and_project,
    materialize_session_memory,
    project_session_memory,
    update_coverage_with_tool_evidence,
)
from .shadow_cutover import (  # noqa: F401
    LIVE_CUTOVER_PROVIDERS,
    ComparisonSink,
    CutoverNotReady,
    CutoverPhase,
    RecordingComparisonSink,
    ShadowCoordinator,
    ShadowObservation,
)
from .retirement_verifier import (  # noqa: F401
    RetirementReadiness,
    SessionExpectation,
    SessionRetirementVerdict,
    verify_retirement,
    verify_session_retirement,
)
from .retention import (  # noqa: F401
    RetentionDecision,
    RetentionInput,
    RetentionPolicy,
    apply_retention,
    plan_retention,
)

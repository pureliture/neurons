"""Delivery-safe public-safe / hashing / validation helpers.

These were originally housed in ``llm_brain_core._util``, but importing them from
there triggers the heavy ``llm_brain_core`` package ``__init__`` (graphiti, neo4j,
ontology, ...), which is NOT present in the ingress/delivery worker's vendored lib
subset. Shared delivery and projection helpers live here at the top level where
the only dependencies are ``agent_knowledge.redaction`` and
``agent_knowledge.session_memory.transcript_model`` (both already vendored in the
worker; ``session_memory`` uses lazy exports so importing ``transcript_model`` does
not drag heavy brain modules).

``llm_brain_core._util`` re-exports from here for backwards compatibility.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
from typing import Any

from agent_knowledge.redaction import redact_public_ingress_text
from agent_knowledge.session_memory.transcript_model import bound_text

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_:-]{2,160}$")
PRIVATE_OUTPUT_RE = re.compile(
    r"(/Users/|~/|/private/|/Volumes/|[A-Za-z]:\\|\\\\[A-Za-z0-9_.-]+|\bBearer\s+|\braw[_ -]?transcript\b)",
    re.IGNORECASE,
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"\b[A-Z0-9_]*(TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)\b\s*[:=]",
    re.IGNORECASE,
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_payload(value: Any) -> str:
    return sha256_text(stable_json(value))


def short_hash(value: Any, *, length: int = 16) -> str:
    return hash_payload(value).split(":", 1)[1][:length]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def list_or_empty(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def require_non_empty(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def require_sha256(value: str, field: str) -> str:
    text = require_non_empty(value, field)
    if not SHA256_RE.fullmatch(text):
        raise ValueError(f"{field} must be sha256:<64 hex>")
    return text


def require_opaque_id(value: str, field: str) -> str:
    text = require_non_empty(value, field)
    if not OPAQUE_ID_RE.fullmatch(text):
        raise ValueError(f"{field} must be an opaque id")
    ensure_public_safe(text, field)
    return text


def public_safe_text(value: str, *, max_chars: int = 65536) -> str:
    normalized = " ".join(str(value or "").split())
    return bound_text(redact_public_ingress_text(normalized), max_chars)


def _scan_public_safe_text(text: str, field: str) -> None:
    if PRIVATE_OUTPUT_RE.search(text) or SECRET_ASSIGNMENT_RE.search(text):
        raise ValueError(f"{field} contains private or raw content")


def ensure_public_safe(value: Any, field: str = "value") -> None:
    """Reject private/raw content in ``value`` (a leak guard for stored/emitted data).

    For containers, every key and leaf string is scanned with its RAW string form
    (recursively) -- NOT the JSON-serialized blob. Scanning the JSON blob caused
    false positives: ``json.dumps`` escapes real newlines as ``\\n`` and other control
    chars with backslashes, so a benign body line ending in ``word:`` followed by a
    newline serialized to ``word:\\n`` and matched the Windows-path alternative
    ``[A-Za-z]:\\`` in ``PRIVATE_OUTPUT_RE``. Per-value raw scanning still catches
    real ``/Users/`` / ``Bearer`` / ``C:\\path`` / UNC ``\\\\host`` / ``API_KEY:``
    content (and now scans dict keys too), while dropping escape-artifact matches.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            _scan_public_safe_text(str(key), field)
            ensure_public_safe(item, field)
    elif isinstance(value, (list, tuple)):
        for item in value:
            ensure_public_safe(item, field)
    else:
        # str(value) (not ``value or ""``) so falsy scalars like 0/False are scanned
        # as their real text rather than collapsed to "".
        _scan_public_safe_text(str(value) if value is not None else "", field)


def public_dict(value: dict[str, Any], *, field: str = "value") -> dict[str, Any]:
    ensure_public_safe(value, field)
    return value


_MASKED_PRIVATE_TOKEN = "[mirror-redacted]"
_MASK_PATH_TOKEN_RE = re.compile(
    r"\S*(?:/Users/|~/|/private/|/Volumes/|[A-Za-z]:\\|\\\\[A-Za-z0-9_.-]+|raw[_ -]?transcript)\S*",
    re.IGNORECASE,
)
_MASK_BEARER_TOKEN_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_MASK_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b[A-Z0-9_]*(?:TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)\b\s*[:=]\s*\S*",
    re.IGNORECASE,
)


def public_safe_mask_body(body: str) -> str:
    """Mask residual private, path, and secret tokens in a projection body."""
    from .rag_ingress.server_runtime import _leak_patterns

    text = redact_public_ingress_text(str(body or ""))
    for _name, regex in _leak_patterns():
        text = regex.sub(_MASKED_PRIVATE_TOKEN, text)
    text = _MASK_SECRET_ASSIGNMENT_RE.sub(_MASKED_PRIVATE_TOKEN, text)
    text = _MASK_BEARER_TOKEN_RE.sub(_MASKED_PRIVATE_TOKEN, text)
    return _MASK_PATH_TOKEN_RE.sub(_MASKED_PRIVATE_TOKEN, text)


_MAX_CAUSE_CHAIN_DEPTH = 4
_MAX_PARAM_NORMALIZE_LEN = 96
_HTTP_SEGMENT_RE = re.compile(r"^http_([1-5][0-9]{2})$")
_PARAM_INDEX_SEGMENT_RE = re.compile(r"(?:\[\d+\]|\.\d+(?=\.|$))")


class SafeErrorCode(StrEnum):
    """Closed allowlist of machine-readable error codes emitted in diagnostics."""

    INVALID_JSON_SCHEMA = "invalid_json_schema"
    INVALID_REQUEST_ERROR = "invalid_request_error"
    INVALID_VALUE = "invalid_value"
    INVALID_TYPE = "invalid_type"
    INVALID_API_KEY = "invalid_api_key"
    INVALID_MODEL = "invalid_model"
    INVALID_PROMPT = "invalid_prompt"
    INVALID_RESPONSE_FORMAT = "invalid_response_format"
    MODEL_NOT_FOUND = "model_not_found"
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    MAX_TOKENS_EXCEEDED = "max_tokens_exceeded"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    REQUEST_TOO_LARGE = "request_too_large"
    STRING_ABOVE_MAX_LENGTH = "string_above_max_length"
    ARRAY_ABOVE_MAX_LENGTH = "array_above_max_length"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    INSUFFICIENT_QUOTA = "insufficient_quota"
    BILLING_HARD_LIMIT_REACHED = "billing_hard_limit_reached"
    CONTENT_FILTER = "content_filter"
    JSON_VALIDATE_FAILED = "json_validate_failed"
    RESPONSE_FORMAT_UNSUPPORTED = "response_format_unsupported"
    UNSUPPORTED_PARAMETER = "unsupported_parameter"
    MISSING_REQUIRED_PARAMETER = "missing_required_parameter"
    MISSING_SESSION_ID = "MissingSessionID"
    MISSING_SESSION_ID_SNAKE = "missing_session_id"
    SERVER_ERROR = "server_error"
    SERVICE_UNAVAILABLE = "service_unavailable"
    OVERLOADED = "overloaded"
    REQUEST_TIMEOUT = "request_timeout"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    NO_USABLE_INDEX = "no_usable_index"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    BAD_REQUEST = "bad_request"
    FILE_EXISTS = "file_exists"
    PRECONDITION_FAILED = "precondition_failed"
    COUCHDB_RESPONSE_TOO_LARGE = "couchdb_response_too_large"
    CAPACITY_EXCEEDED = "capacity_exceeded"
    INPUT_TOO_LARGE = "input_too_large"
    INTERNAL_ERROR = "internal_error"
    INVALID_QUERY = "invalid_query"
    INVALID_REQUEST = "invalid_request"
    MODEL_UNAVAILABLE = "model_unavailable"
    SDK_TRANSPORT_ERROR = "sdk_transport_error"
    SOURCE_REVISION_CURRENTNESS_LIMIT_EXCEEDED = "source_revision_currentness_limit_exceeded"
    SOURCE_REVISION_SNAPSHOT_CHANGED_RETRY = "source_revision_snapshot_changed_retry"
    UNKNOWN_MODEL = "unknown_model"
    UNSUPPORTED_BRAIN_ID = "unsupported_brain_id"
    NEO_SYNTAX_ERROR = "Neo.ClientError.Statement.SyntaxError"
    NEO_CONSTRAINT_FAILED = "Neo.ClientError.Schema.ConstraintValidationFailed"
    NEO_UNAUTHORIZED = "Neo.ClientError.Security.Unauthorized"
    NEO_DEADLOCK_DETECTED = "Neo.TransientError.Transaction.DeadlockDetected"
    NEO_LOCK_CLIENT_STOPPED = "Neo.TransientError.Transaction.LockClientStopped"
    UNKNOWN_ERROR_CODE = "unknown_error_code"


class SafeErrorType(StrEnum):
    """Closed allowlist of upstream error category types emitted in diagnostics."""

    INVALID_REQUEST_ERROR = "invalid_request_error"
    AUTHENTICATION_ERROR = "authentication_error"
    PERMISSION_ERROR = "permission_error"
    NOT_FOUND_ERROR = "not_found_error"
    CONFLICT_ERROR = "conflict_error"
    UNPROCESSABLE_ENTITY_ERROR = "unprocessable_entity_error"
    RATE_LIMIT_ERROR = "rate_limit_error"
    API_ERROR = "api_error"
    SERVER_ERROR = "server_error"
    SERVICE_UNAVAILABLE_ERROR = "service_unavailable_error"
    TIMEOUT_ERROR = "timeout_error"
    CONNECTION_ERROR = "connection_error"
    CONTENT_POLICY_VIOLATION = "content_policy_violation"
    TOKENS = "tokens"
    REQUESTS = "requests"
    UNKNOWN_ERROR_TYPE = "unknown_error_type"


class SafeReasonCode(StrEnum):
    """Closed allowlist of internal reason_code tokens emitted in diagnostics."""

    GRAPH_SOURCE_SNAPSHOT_CHANGED = "graph_source_snapshot_changed"
    GRAPH_SELECTION_DUPLICATE_SESSION = "graph_selection_duplicate_session"
    GRAPH_SELECTION_PROJECT_SCOPE_EXCEEDED = "graph_selection_project_scope_exceeded"
    INCONSISTENT_HERMES_PROJECT_SCOPE = "inconsistent_hermes_project_scope"
    PROJECTION_FAILED = "projection_failed"
    ALREADY_RUNNING = "already_running"
    EMPTY_RESULT = "empty_result"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    ERROR = "error"
    SOURCE_REVISION_BOUNDED_GET_UNAVAILABLE = "source_revision_bounded_get_unavailable"
    SOURCE_REVISION_COMPACT_BYTES_EXCEEDED = "source_revision_compact_bytes_exceeded"
    SOURCE_REVISION_CURRENTNESS_LIMIT_EXCEEDED = "source_revision_currentness_limit_exceeded"
    SOURCE_REVISION_MANIFEST_BYTES_EXCEEDED = "source_revision_manifest_bytes_exceeded"
    SOURCE_REVISION_MANIFEST_MEMBERS_EXCEEDED = "source_revision_manifest_members_exceeded"
    SOURCE_REVISION_PAGE_BYTES_EXCEEDED = "source_revision_page_bytes_exceeded"
    SOURCE_REVISION_POINTER_BYTES_EXCEEDED = "source_revision_pointer_bytes_exceeded"
    SOURCE_REVISION_ROW_LIMIT_EXCEEDED = "source_revision_row_limit_exceeded"
    SOURCE_REVISION_SNAPSHOT_CHANGED_RETRY = "source_revision_snapshot_changed_retry"
    AGGREGATE_MERGE_Requires_SESSION = "aggregate_merge_requires_transcript_session"
    TRANSCRIPT_SESSION_DOCUMENT_ID_CHANGED = "transcript_session_document_id_changed"
    TRANSCRIPT_SESSION_PROJECT_CHANGED = "transcript_session_project_changed"
    CONTENT_HASH_MISMATCH = "content_hash_mismatch"
    PG_JOURNAL_QUARANTINED = "pg_journal_quarantined"
    GROUP_QUARANTINED = "group_quarantined"
    EPOCH_MISMATCH = "epoch_mismatch"
    LEASE_MISMATCH = "lease_mismatch"
    GROUP_MISMATCH = "group_mismatch"
    TEXT_CHANGED = "text_changed"
    INPUT_HASH_CHANGED = "input_hash_changed"
    DELETED_TOMBSTONE = "deleted_tombstone"
    TARGET_MISSING = "target_missing"
    FENCE_MISSING = "fence_missing"
    UNKNOWN_REASON_CODE = "unknown_reason_code"


class SafeErrorParam(StrEnum):
    """Closed allowlist of upstream error parameter identifiers emitted in diagnostics."""

    MODEL = "model"
    MESSAGES = "messages"
    MESSAGES_ITEM = "messages[]"
    MESSAGES_ITEM_CONTENT = "messages[].content"
    MESSAGES_ITEM_ROLE = "messages[].role"
    RESPONSE_FORMAT = "response_format"
    RESPONSE_FORMAT_TYPE = "response_format.type"
    RESPONSE_FORMAT_JSON_SCHEMA = "response_format.json_schema"
    RESPONSE_FORMAT_JSON_SCHEMA_NAME = "response_format.json_schema.name"
    RESPONSE_FORMAT_JSON_SCHEMA_SCHEMA = "response_format.json_schema.schema"
    RESPONSE_FORMAT_JSON_SCHEMA_STRICT = "response_format.json_schema.strict"
    JSON_SCHEMA = "json_schema"
    SCHEMA = "schema"
    TOOLS = "tools"
    TOOLS_ITEM = "tools[]"
    TOOLS_ITEM_FUNCTION = "tools[].function"
    TOOLS_ITEM_FUNCTION_PARAMETERS = "tools[].function.parameters"
    TOOL_CHOICE = "tool_choice"
    MAX_TOKENS = "max_tokens"
    MAX_COMPLETION_TOKENS = "max_completion_tokens"
    TEMPERATURE = "temperature"
    TOP_P = "top_p"
    REASONING_EFFORT = "reasoning_effort"
    INPUT = "input"
    PROMPT = "prompt"
    DIMENSIONS = "dimensions"
    ENCODING_FORMAT = "encoding_format"
    STREAM = "stream"
    STOP = "stop"
    SEED = "seed"
    N = "n"
    USER = "user"
    METADATA = "metadata"
    X_OPENCODE_SESSION = "x-opencode-session"
    UNKNOWN_ERROR_PARAM = "unknown_error_param"


class SafeErrorStep(StrEnum):
    """Closed allowlist of Graphiti prompt / graph projection execution steps."""

    ENSURE_GRAPH_INDICES = "ensure_graph_indices"
    EPISODE_EXISTS_CHECK = "episode_exists_check"
    ENTITY_EXTRACTED_CHECK = "entity_extracted_check"
    EPISODIC_DIRECT_SAVE = "episodic_direct_save"
    EPISODIC_PRE_SAVE = "episodic_pre_save"
    EPISODIC_RESTORE = "episodic_restore"
    EXTRACT_NODES_JSON = "extract_nodes.extract_json"
    EXTRACT_NODES_MESSAGE = "extract_nodes.extract_message"
    EXTRACT_NODES_TEXT = "extract_nodes.extract_text"
    EXTRACT_NODES_CLASSIFY = "extract_nodes.classify_nodes"
    EXTRACT_NODES_ATTRIBUTES = "extract_nodes.extract_attributes"
    EXTRACT_NODES_SUMMARY = "extract_nodes.extract_summary"
    EXTRACT_NODES_SUMMARIES_BATCH = "extract_nodes.extract_summaries_batch"
    EXTRACT_NODES_SUMMARIES_FROM_EPISODES = "extract_nodes.extract_entity_summaries_from_episodes"
    DEDUPE_NODES_NODE = "dedupe_nodes.node"
    DEDUPE_NODES_NODES = "dedupe_nodes.nodes"
    DEDUPE_NODES_NODE_LIST = "dedupe_nodes.node_list"
    EXTRACT_EDGES_EDGE = "extract_edges.edge"
    EXTRACT_EDGES_ATTRIBUTES = "extract_edges.extract_attributes"
    EXTRACT_EDGES_TIMESTAMPS = "extract_edges.extract_timestamps"
    EXTRACT_EDGES_TIMESTAMPS_BATCH = "extract_edges.extract_timestamps_batch"
    DEDUPE_EDGES_RESOLVE = "dedupe_edges.resolve_edge"
    COMBINED_EXTRACT_MESSAGE = "extract_nodes_and_edges.extract_message"
    SUMMARIZE_NODES_PAIR = "summarize_nodes.summarize_pair"
    SUMMARIZE_NODES_DESCRIPTION = "summarize_nodes.summary_description"
    SUMMARIZE_SAGAS = "summarize_sagas.summarize_saga"
    POSTCHECK_REDACTION = "postcheck_redaction"
    UNKNOWN_ERROR_STEP = "unknown_error_step"


class SafeErrorClass(StrEnum):
    """Closed allowlist of exception class names emitted in diagnostics."""

    EXCEPTION = "Exception"
    RUNTIME_ERROR = "RuntimeError"
    VALUE_ERROR = "ValueError"
    TYPE_ERROR = "TypeError"
    KEY_ERROR = "KeyError"
    INDEX_ERROR = "IndexError"
    ATTRIBUTE_ERROR = "AttributeError"
    LOOKUP_ERROR = "LookupError"
    ASSERTION_ERROR = "AssertionError"
    ARITHMETIC_ERROR = "ArithmeticError"
    OVERFLOW_ERROR = "OverflowError"
    ZERO_DIVISION_ERROR = "ZeroDivisionError"
    OS_ERROR = "OSError"
    IO_ERROR = "IOError"
    FILE_NOT_FOUND_ERROR = "FileNotFoundError"
    PERMISSION_ERROR = "PermissionError"
    TIMEOUT_ERROR = "TimeoutError"
    CONNECTION_ERROR = "ConnectionError"
    CONNECTION_RESET_ERROR = "ConnectionResetError"
    CONNECTION_REFUSED_ERROR = "ConnectionRefusedError"
    CONNECTION_ABORTED_ERROR = "ConnectionAbortedError"
    BROKEN_PIPE_ERROR = "BrokenPipeError"
    EOF_ERROR = "EOFError"
    MEMORY_ERROR = "MemoryError"
    NAME_ERROR = "NameError"
    IMPORT_ERROR = "ImportError"
    MODULE_NOT_FOUND_ERROR = "ModuleNotFoundError"
    SYNTAX_ERROR = "SyntaxError"
    SYSTEM_ERROR = "SystemError"
    BUFFER_ERROR = "BufferError"
    NOT_IMPLEMENTED_ERROR = "NotImplementedError"
    RECURSION_ERROR = "RecursionError"
    STOP_ITERATION = "StopIteration"
    UNICODE_ERROR = "UnicodeError"
    UNICODE_DECODE_ERROR = "UnicodeDecodeError"
    UNICODE_ENCODE_ERROR = "UnicodeEncodeError"
    JSON_DECODE_ERROR = "JSONDecodeError"
    # OpenAI / LLM SDK
    API_ERROR = "APIError"
    API_STATUS_ERROR = "APIStatusError"
    API_CONNECTION_ERROR = "APIConnectionError"
    API_TIMEOUT_ERROR = "APITimeoutError"
    API_RESPONSE_VALIDATION_ERROR = "APIResponseValidationError"
    BAD_REQUEST_ERROR = "BadRequestError"
    AUTHENTICATION_ERROR = "AuthenticationError"
    PERMISSION_DENIED_ERROR = "PermissionDeniedError"
    NOT_FOUND_ERROR = "NotFoundError"
    CONFLICT_ERROR = "ConflictError"
    UNPROCESSABLE_ENTITY_ERROR = "UnprocessableEntityError"
    RATE_LIMIT_ERROR = "RateLimitError"
    INTERNAL_SERVER_ERROR = "InternalServerError"
    LENGTH_FINISH_REASON_ERROR = "LengthFinishReasonError"
    CONTENT_FILTER_FINISH_REASON_ERROR = "ContentFilterFinishReasonError"
    # HTTPX
    HTTP_ERROR = "HTTPError"
    REQUEST_ERROR = "RequestError"
    TRANSPORT_ERROR = "TransportError"
    TIMEOUT_EXCEPTION = "TimeoutException"
    CONNECT_TIMEOUT = "ConnectTimeout"
    READ_TIMEOUT = "ReadTimeout"
    WRITE_TIMEOUT = "WriteTimeout"
    POOL_TIMEOUT = "PoolTimeout"
    NETWORK_ERROR = "NetworkError"
    CONNECT_ERROR = "ConnectError"
    READ_ERROR = "ReadError"
    WRITE_ERROR = "WriteError"
    CLOSE_ERROR = "CloseError"
    PROXY_ERROR = "ProxyError"
    UNSUPPORTED_PROTOCOL = "UnsupportedProtocol"
    PROTOCOL_ERROR = "ProtocolError"
    LOCAL_PROTOCOL_ERROR = "LocalProtocolError"
    REMOTE_PROTOCOL_ERROR = "RemoteProtocolError"
    DECODING_ERROR = "DecodingError"
    TOO_MANY_REDIRECTS = "TooManyRedirects"
    HTTP_STATUS_ERROR = "HTTPStatusError"
    INVALID_URL = "InvalidURL"
    COOKIE_CONFLICT = "CookieConflict"
    STREAM_ERROR = "StreamError"
    # Neo4j / Graphiti
    NEO4J_ERROR = "Neo4jError"
    TRANSIENT_ERROR = "TransientError"
    CLIENT_ERROR = "ClientError"
    DATABASE_ERROR = "DatabaseError"
    SERVICE_UNAVAILABLE = "ServiceUnavailable"
    SESSION_EXPIRED = "SessionExpired"
    AUTH_ERROR = "AuthError"
    CONFIGURATION_ERROR = "ConfigurationError"
    GRAPHITI_ERROR = "GraphitiError"
    NODE_NOT_FOUND_ERROR = "NodeNotFoundError"
    EDGE_NOT_FOUND_ERROR = "EdgeNotFoundError"
    EDGES_NOT_FOUND_ERROR = "EdgesNotFoundError"
    GROUPS_EDGES_NOT_FOUND_ERROR = "GroupsEdgesNotFoundError"
    GROUPS_NODES_NOT_FOUND_ERROR = "GroupsNodesNotFoundError"
    SEARCH_RERANKER_ERROR = "SearchRerankerError"
    BATCH_EXTRACTION_ERROR = "BatchExtractionError"
    EMPTY_RESPONSE_ERROR = "EmptyResponseError"
    EXTRACTION_ERROR = "ExtractionError"
    EXTRACTION_RESPONSE_ERROR = "ExtractionResponseError"
    EXTRACTION_TRANSPORT_ERROR = "ExtractionTransportError"
    ENTITY_TYPE_VALIDATION_ERROR = "EntityTypeValidationError"
    GROUP_ID_VALIDATION_ERROR = "GroupIdValidationError"
    NODE_LABEL_VALIDATION_ERROR = "NodeLabelValidationError"
    # Repository domain exceptions
    APPROVAL_ERROR = "ApprovalError"
    CANARY_EXECUTION_ERROR = "CanaryExecutionError"
    CANDIDATE_INTAKE_CONFIG_ERROR = "CandidateIntakeConfigError"
    CANONICAL_EMBEDDING_AGGREGATION_ERROR = "CanonicalEmbeddingAggregationError"
    CANONICAL_EMBEDDING_INPUT_ERROR = "CanonicalEmbeddingInputError"
    COMMUNITY_DISABLED_IN_LOCAL_MODE_ERROR = "CommunityDisabledInLocalModeError"
    COUCHDB_ERROR = "CouchDBError"
    COUCHDB_RESPONSE_TOO_LARGE = "CouchDBResponseTooLarge"
    CUTOVER_NOT_READY = "CutoverNotReady"
    DELIVERY_DEFERRED = "DeliveryDeferred"
    DELIVERY_OUTCOME_UNCERTAIN = "DeliveryOutcomeUncertain"
    EMBEDDING_CHUNKS_PENDING = "EmbeddingChunksPending"
    EMBEDDING_PROVIDER_UNAVAILABLE = "EmbeddingProviderUnavailable"
    EVENT_VALIDATION_ERROR = "EventValidationError"
    GRAPH_OPERATION_CONFLICT_QUARANTINED_ERROR = "GraphOperationConflictQuarantinedError"
    GROUP_FENCE_BUSY_ERROR = "GroupFenceBusyError"
    GROUP_FENCE_ERROR = "GroupFenceError"
    GROUP_QUARANTINED_ERROR = "GroupQuarantinedError"
    INCONSISTENT_HERMES_PROJECT_SCOPE = "InconsistentHermesProjectScope"
    INGRESS_ENQUEUE_REJECTED = "IngressEnqueueRejected"
    INGRESS_ENQUEUE_UNREACHABLE = "IngressEnqueueUnreachable"
    INVALID_PROJECTION_TEXT = "InvalidProjectionText"
    LOCAL_EMBEDDING_API_ERROR = "LocalEmbeddingApiError"
    LOCAL_EMBEDDING_CLIENT_ERROR = "LocalEmbeddingClientError"
    LOCAL_EMBEDDING_MODEL_INTEGRITY_ERROR = "LocalEmbeddingModelIntegrityError"
    LOCAL_GROUP_RESOLUTION_BLOCKED_ERROR = "LocalGroupResolutionBlockedError"
    LOCAL_GROUP_WRITE_BLOCKED_ERROR = "LocalGroupWriteBlockedError"
    MODEL_CONNECTOR_CONFIG_ERROR = "ModelConnectorConfigError"
    NATIVE_EXACT_MUTATION_MARKER_ERROR = "NativeExactMutationMarkerError"
    OWNERSHIP_VIOLATION = "OwnershipViolation"
    PAID_EMBEDDING_FALLBACK_BLOCKED_ERROR = "PaidEmbeddingFallbackBlockedError"
    PERMISSION_AUDIT_MARKER_ENVIRONMENT_ERROR = "PermissionAuditMarkerEnvironmentError"
    PERMISSION_AUDIT_MARKER_RUNTIME_ERROR = "PermissionAuditMarkerRuntimeError"
    PERMISSION_AUDIT_STORE_ERROR = "PermissionAuditStoreError"
    POLICY_VIOLATION = "PolicyViolation"
    POSTGRES_EXACT_MUTATION_MARKER_ERROR = "PostgresExactMutationMarkerError"
    PRODUCT_MARKER_ACTIVATION_CLI_ERROR = "ProductMarkerActivationCliError"
    REFUSAL_ERROR = "RefusalError"
    RETIRED_INDEX_BRIDGE_API_ERROR = "RetiredIndexBridgeApiError"
    RUNTIME_BUILD_IDENTITY_ERROR = "RuntimeBuildIdentityError"
    SECRET_LIKE_METADATA_ERROR = "SecretLikeMetadataError"
    SOURCE_CONTRACT_VIOLATION = "SourceContractViolation"
    SOURCE_REDACTION_LEAK = "SourceRedactionLeak"
    SOURCE_REVISION_CURRENTNESS_LIMIT_ERROR = "SourceRevisionCurrentnessLimitError"
    SOURCE_REVISION_RESOLUTION_ERROR = "SourceRevisionResolutionError"
    SOURCE_REVISION_SNAPSHOT_CHANGED_ERROR = "SourceRevisionSnapshotChangedError"
    SOURCE_STORE_CONFLICT = "SourceStoreConflict"
    SOURCE_STORE_ERROR = "SourceStoreError"
    SOURCE_STORE_RESPONSE_TOO_LARGE = "SourceStoreResponseTooLarge"
    STALE_OWNER_REJECTED = "StaleOwnerRejected"
    STATE_DB_ERROR = "StateDBError"
    STEWARD_PERMISSION_ERROR = "StewardPermissionError"
    TEMPORAL_SELECTOR_ERROR = "TemporalSelectorError"
    VALIDATION_REJECTED = "ValidationRejected"
    UNCLASSIFIED_ERROR = "UnclassifiedError"


_KNOWN_ERROR_CODES: frozenset[str] = frozenset(
    member.value for member in SafeErrorCode if member is not SafeErrorCode.UNKNOWN_ERROR_CODE
)
_KNOWN_ERROR_TYPES: frozenset[str] = frozenset(
    member.value for member in SafeErrorType if member is not SafeErrorType.UNKNOWN_ERROR_TYPE
)
_KNOWN_REASON_CODES: frozenset[str] = frozenset(
    member.value for member in SafeReasonCode if member is not SafeReasonCode.UNKNOWN_REASON_CODE
)
_KNOWN_ERROR_CLASSES: frozenset[str] = frozenset(
    member.value for member in SafeErrorClass if member is not SafeErrorClass.UNCLASSIFIED_ERROR
)
_KNOWN_ERROR_PARAMS: frozenset[str] = frozenset(
    member.value for member in SafeErrorParam if member is not SafeErrorParam.UNKNOWN_ERROR_PARAM
)
_KNOWN_ERROR_STEPS: frozenset[str] = frozenset(
    member.value for member in SafeErrorStep if member is not SafeErrorStep.UNKNOWN_ERROR_STEP
)


def sanitize_http_status(value: object) -> int | None:
    """Return a valid HTTP status code int (100..599) or None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


def classify_safe_error_code(value: object) -> str:
    """Map an error code to a closed SafeErrorCode value; unknown non-empty -> generic."""
    if not isinstance(value, str):
        return ""
    token = value.strip()
    if not token:
        return ""
    if token in _KNOWN_ERROR_CODES:
        return token
    return SafeErrorCode.UNKNOWN_ERROR_CODE.value


def classify_safe_error_type(value: object) -> str:
    """Map an error type to a closed SafeErrorType value; unknown non-empty -> generic."""
    if not isinstance(value, str):
        return ""
    token = value.strip()
    if not token:
        return ""
    if token in _KNOWN_ERROR_TYPES:
        return token
    return SafeErrorType.UNKNOWN_ERROR_TYPE.value


def classify_safe_reason_code(value: object) -> str:
    """Map an explicit reason_code to a closed SafeReasonCode value; unknown non-empty -> generic."""
    if not isinstance(value, str):
        return ""
    token = value.strip()
    if not token:
        return ""
    if token in _KNOWN_REASON_CODES:
        return token
    return SafeReasonCode.UNKNOWN_REASON_CODE.value


def classify_safe_error_param(value: object) -> str:
    """Map an upstream error param to a closed SafeErrorParam value; unknown non-empty -> generic."""
    if not isinstance(value, str):
        return ""
    token = value.strip()
    if not token:
        return ""
    if token in _KNOWN_ERROR_PARAMS:
        return token
    if len(token) <= _MAX_PARAM_NORMALIZE_LEN:
        normalized = _PARAM_INDEX_SEGMENT_RE.sub("[]", token)
        if normalized in _KNOWN_ERROR_PARAMS:
            return normalized
    return SafeErrorParam.UNKNOWN_ERROR_PARAM.value


def classify_safe_error_step(value: object) -> str:
    """Map an execution/prompt step to a closed SafeErrorStep value; unknown non-empty -> generic."""
    if not isinstance(value, str):
        return ""
    token = value.strip()
    if not token:
        return ""
    if token in _KNOWN_ERROR_STEPS:
        return token
    return SafeErrorStep.UNKNOWN_ERROR_STEP.value


def classify_safe_error_class(value: object) -> str:
    """Map an exception instance or class name to a closed SafeErrorClass value."""
    if isinstance(value, BaseException):
        name = type(value).__name__
    elif isinstance(value, str):
        name = value.strip()
    else:
        return ""
    if not name:
        return ""
    if name in _KNOWN_ERROR_CLASSES:
        return name
    return SafeErrorClass.UNCLASSIFIED_ERROR.value


def sanitize_structured_reason_code(value: object) -> str:
    """Validate a structured reason_code against closed enums; unknown -> generic."""
    if not isinstance(value, str):
        return ""
    token = value.strip()
    if not token:
        return ""
    if token in _KNOWN_REASON_CODES or token in _KNOWN_ERROR_CLASSES:
        return token
    parts = token.split(":")
    if 1 <= len(parts) <= 3 and parts[0] in _KNOWN_ERROR_CLASSES:
        valid = True
        for seg in parts[1:]:
            if _HTTP_SEGMENT_RE.fullmatch(seg):
                continue
            if seg in _KNOWN_ERROR_CODES or seg in _KNOWN_ERROR_TYPES:
                continue
            valid = False
            break
        if valid:
            return ":".join(parts)
    return SafeReasonCode.UNKNOWN_REASON_CODE.value


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < _MAX_CAUSE_CHAIN_DEPTH:
        ident = id(current)
        if ident in seen:
            break
        seen.add(ident)
        chain.append(current)
        next_exc = current.__cause__ if current.__cause__ is not None else current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None
    return chain


def _extract_body_raw_field(body: object, field_name: str) -> object:
    if not isinstance(body, Mapping):
        return None
    nested = body.get("error")
    if isinstance(nested, Mapping) and nested.get(field_name) is not None:
        return nested.get(field_name)
    return body.get(field_name)


def extract_safe_error_diagnostics(exc: BaseException) -> dict[str, Any]:
    """Extract strictly closed-allowlist status/code tokens without reading raw messages."""
    error_class = classify_safe_error_class(exc) or SafeErrorClass.UNCLASSIFIED_ERROR.value
    explicit_reason = classify_safe_reason_code(getattr(exc, "reason_code", None))
    http_status: int | None = None
    error_code = ""
    error_type = ""
    error_param = ""
    error_step = ""

    for item in _iter_exception_chain(exc):
        if http_status is None:
            http_status = sanitize_http_status(getattr(item, "status_code", None))
        if http_status is None:
            response = getattr(item, "response", None)
            http_status = sanitize_http_status(getattr(response, "status_code", None))
        if http_status is None:
            raw_code_attr = getattr(item, "code", None)
            if isinstance(raw_code_attr, int) and not isinstance(raw_code_attr, bool):
                http_status = sanitize_http_status(raw_code_attr)

        if not error_code or error_code == SafeErrorCode.UNKNOWN_ERROR_CODE.value:
            candidate_code = ""
            raw_code = getattr(item, "code", None)
            if isinstance(raw_code, str):
                candidate_code = classify_safe_error_code(raw_code)
            if not candidate_code:
                candidate_code = classify_safe_error_code(
                    _extract_body_raw_field(getattr(item, "body", None), "code")
                )
            if candidate_code and (
                not error_code or candidate_code != SafeErrorCode.UNKNOWN_ERROR_CODE.value
            ):
                error_code = candidate_code

        if not error_type or error_type == SafeErrorType.UNKNOWN_ERROR_TYPE.value:
            candidate_type = ""
            raw_type = getattr(item, "type", None)
            if isinstance(raw_type, str):
                candidate_type = classify_safe_error_type(raw_type)
            if not candidate_type:
                candidate_type = classify_safe_error_type(
                    _extract_body_raw_field(getattr(item, "body", None), "type")
                )
            if candidate_type and (
                not error_type or candidate_type != SafeErrorType.UNKNOWN_ERROR_TYPE.value
            ):
                error_type = candidate_type

        if not error_param or error_param == SafeErrorParam.UNKNOWN_ERROR_PARAM.value:
            candidate_param = ""
            raw_param = getattr(item, "param", None)
            if isinstance(raw_param, str):
                candidate_param = classify_safe_error_param(raw_param)
            if not candidate_param:
                candidate_param = classify_safe_error_param(
                    _extract_body_raw_field(getattr(item, "body", None), "param")
                )
            if candidate_param and (
                not error_param or candidate_param != SafeErrorParam.UNKNOWN_ERROR_PARAM.value
            ):
                error_param = candidate_param

        if not error_step or error_step == SafeErrorStep.UNKNOWN_ERROR_STEP.value:
            candidate_step = classify_safe_error_step(getattr(item, "error_step", None))
            if candidate_step and (
                not error_step or candidate_step != SafeErrorStep.UNKNOWN_ERROR_STEP.value
            ):
                error_step = candidate_step

    if explicit_reason and http_status is None:
        reason_code = explicit_reason
    else:
        if error_code and error_code != SafeErrorCode.UNKNOWN_ERROR_CODE.value:
            detail = error_code
        elif error_type and error_type != SafeErrorType.UNKNOWN_ERROR_TYPE.value:
            detail = error_type
        else:
            detail = error_code or error_type or explicit_reason
        parts = [error_class]
        if http_status is not None:
            parts.append(f"http_{http_status}")
        if detail and detail != error_class:
            parts.append(detail)
        reason_code = ":".join(parts)

    diagnostics: dict[str, Any] = {
        "error_class": error_class,
        "reason_code": reason_code,
    }
    if http_status is not None:
        diagnostics["http_status"] = http_status
    if error_code:
        diagnostics["error_code"] = error_code
    if error_type:
        diagnostics["error_type"] = error_type
    if error_param:
        diagnostics["error_param"] = error_param
    if error_step:
        diagnostics["error_step"] = error_step
    return diagnostics


def format_safe_reason_code(exc: BaseException) -> str:
    return str(extract_safe_error_diagnostics(exc)["reason_code"])

from __future__ import annotations

TOOL_NAME = "knowledge.search"
BRAIN_QUERY_TOOL_NAME = "brain.query"
BRAIN_RESOLVE_TOOL_NAME = "brain.resolve"
BRAIN_CONTEXT_RESOLVE_TOOL_NAME = "brain_context_resolve"
BRAIN_MEMORY_SEARCH_TOOL_NAME = "brain_memory_search"
BRAIN_INCIDENT_SEARCH_TOOL_NAME = "brain_incident_search"
BRAIN_DRIFT_EXPLAIN_TOOL_NAME = "brain_drift_explain"
BRAIN_PERSONA_GET_TOOL_NAME = "brain_persona_get"
BRAIN_PERSONA_CHECK_TOOL_NAME = "brain_persona_check"
BRAIN_EVIDENCE_GET_TOOL_NAME = "brain_evidence_get"


def list_tools() -> list[dict]:
    return [
        {
            "name": TOOL_NAME,
            "description": "Search server-owned RAGFlow-backed neuron knowledge.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "filters": {"type": "object", "additionalProperties": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 10},
                    "include_private": {"type": "boolean", "default": False},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_QUERY_TOOL_NAME,
            "description": "use brain: query accepted/current neuron memory by brain_id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "brain_id": {"type": "string"},
                    "query": {"type": "string"},
                    "mode": {"type": "string", "enum": ["latest"], "default": "latest"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 8},
                },
                "required": ["brain_id", "query"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_RESOLVE_TOOL_NAME,
            "description": "Resolve available /project/<project> brain_id candidates.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
            "description": "Resolve the current LLM-Brain ContextPack from canonical artifacts/cards plus derived graph status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string"},
                    "branch": {"type": "string"},
                    "current_files": {"type": "array", "items": {"type": "string"}, "default": []},
                    "current_request": {"type": "string"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                },
                "required": ["repository", "branch", "current_request"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_MEMORY_SEARCH_TOOL_NAME,
            "description": "Search accepted/current LLM-Brain memory with derived graph results labeled separately.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "project": {"type": "string"},
                    "repository": {"type": "string"},
                    "card_types": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_INCIDENT_SEARCH_TOOL_NAME,
            "description": "Search prior incidents, attempts, fixes, verifications, and do-not-apply cases.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symptom": {"type": "string"},
                    "project": {"type": "string"},
                    "repository": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                },
                "required": ["symptom"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_DRIFT_EXPLAIN_TOOL_NAME,
            "description": "Explain design, persona, or project assumption drift from canonical memory cards.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "project": {"type": "string"},
                    "repository": {"type": "string"},
                },
                "required": ["subject"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_PERSONA_GET_TOOL_NAME,
            "description": "Return persona facts from accepted/current LLM-Brain memory.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "repository": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_PERSONA_CHECK_TOOL_NAME,
            "description": "Check a plan against accepted persona facts and return aligned/conflict/drift status.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "plan": {"type": "string"},
                    "project": {"type": "string"},
                    "repository": {"type": "string"},
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_EVIDENCE_GET_TOOL_NAME,
            "description": "Resolve a SourceRef/SpanRef through the LLM-Brain evidence policy without exposing raw private paths.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source_ref_id": {"type": "string"},
                    "requesting_device_id_hash": {"type": "string"},
                    "span_ref_id": {"type": "string"},
                    "approval_ref": {"type": "string"},
                    "expected_content_hash": {"type": "string"},
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536, "default": 4096},
                    "redaction_profile": {"type": "string", "default": "public_safe"},
                },
                "required": ["source_ref_id", "requesting_device_id_hash"],
                "additionalProperties": False,
            },
        },
    ]

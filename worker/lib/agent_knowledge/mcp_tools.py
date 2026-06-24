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
            "description": "서버가 소유한 RAGFlow 기반 neuron 지식을 검색한다.",
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
            "description": "brain_id 기준으로 승인된 최신 neuron memory를 질의한다.",
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
            "description": "사용 가능한 /project/<project> brain_id 후보를 찾는다.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_CONTEXT_RESOLVE_TOOL_NAME,
            "description": "canonical artifact/card와 파생 graph 상태로 현재 LLM-Brain ContextPack을 만든다.",
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
            "description": "승인된 최신 LLM-Brain memory를 검색하고 파생 graph 결과는 별도 표시한다.",
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
            "description": "이전 incident, 시도, 수정, 검증, 적용 금지 사례를 검색한다.",
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
            "description": "canonical memory card 기준으로 설계, persona, project 가정의 drift를 설명한다.",
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
            "description": "승인된 최신 LLM-Brain memory에서 persona fact를 반환한다.",
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
            "description": "계획을 승인된 persona fact와 비교해 aligned/conflict/drift 상태를 반환한다.",
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
            "description": "raw private path를 노출하지 않고 LLM-Brain evidence policy로 SourceRef/SpanRef를 해석한다.",
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

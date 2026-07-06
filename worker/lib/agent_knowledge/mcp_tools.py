from __future__ import annotations

from dataclasses import dataclass

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
BRAIN_OBJECTS_QUERY_TOOL_NAME = "brain_objects_query"
BRAIN_OBJECT_EXPLAIN_TOOL_NAME = "brain_object_explain"
BRAIN_CORPUS_STATUS_TOOL_NAME = "brain_corpus_status"
BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME = "brain_corpus_ingest_plan"
BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME = "brain_source_to_candidate_graph"
BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME = "brain_candidate_review_edit"
BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME = "brain_approval_board_decide"
BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME = "brain_object_proposal_create"
BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME = "brain_object_decision_commit"
BRAIN_REVIEW_PROPOSALS_TOOL_NAME = "brain_review_proposals"

# Brain Steward — agent-facing, proposal-only memory management surface.
MEMORY_AUTHORITY_PACK_READ_TOOL_NAME = "memory_authority_pack_read"
MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME = "memory_review_queue_list"
MEMORY_CANDIDATE_CREATE_TOOL_NAME = "memory_candidate_create"
MEMORY_STALE_MARK_TOOL_NAME = "memory_stale_mark"
MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME = "memory_supersede_propose"
MEMORY_CANDIDATE_APPROVE_TOOL_NAME = "memory_candidate_approve"
MEMORY_CANDIDATE_REJECT_TOOL_NAME = "memory_candidate_reject"
MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME = "memory_candidate_auto_accept"
MEMORY_SUPERSEDE_COMMIT_TOOL_NAME = "memory_supersede_commit"
MEMORY_STALE_COMMIT_TOOL_NAME = "memory_stale_commit"

STEWARD_RESTRICTED_TOOL_NAMES = (
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
    MEMORY_CANDIDATE_REJECT_TOOL_NAME,
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
    MEMORY_SUPERSEDE_COMMIT_TOOL_NAME,
    MEMORY_STALE_COMMIT_TOOL_NAME,
)
_TOOL_REGISTRY_CACHE: dict[str, dict] | None = None


@dataclass(frozen=True)
class ToolContract:
    name: str
    description: str
    input_schema: dict
    dispatch_owner: str

    def to_tool(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


_DISPATCH_OWNER_BY_TOOL_NAME = {
    TOOL_NAME: "legacy_search",
    BRAIN_QUERY_TOOL_NAME: "jsonrpc_brain",
    BRAIN_RESOLVE_TOOL_NAME: "jsonrpc_brain",
    BRAIN_CONTEXT_RESOLVE_TOOL_NAME: "jsonrpc_brain",
    BRAIN_MEMORY_SEARCH_TOOL_NAME: "jsonrpc_brain",
    BRAIN_INCIDENT_SEARCH_TOOL_NAME: "jsonrpc_brain",
    BRAIN_DRIFT_EXPLAIN_TOOL_NAME: "jsonrpc_brain",
    BRAIN_PERSONA_GET_TOOL_NAME: "jsonrpc_brain",
    BRAIN_PERSONA_CHECK_TOOL_NAME: "jsonrpc_brain",
    BRAIN_EVIDENCE_GET_TOOL_NAME: "jsonrpc_brain",
    BRAIN_OBJECTS_QUERY_TOOL_NAME: "jsonrpc_brain",
    BRAIN_OBJECT_EXPLAIN_TOOL_NAME: "jsonrpc_brain",
    BRAIN_CORPUS_STATUS_TOOL_NAME: "jsonrpc_brain",
    BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME: "jsonrpc_brain",
    BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME: "jsonrpc_brain",
    BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME: "jsonrpc_brain",
    BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME: "jsonrpc_brain",
    BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME: "jsonrpc_brain",
    BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME: "jsonrpc_brain",
    BRAIN_REVIEW_PROPOSALS_TOOL_NAME: "jsonrpc_brain",
    MEMORY_AUTHORITY_PACK_READ_TOOL_NAME: "brain_steward",
    MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME: "brain_steward",
    MEMORY_CANDIDATE_CREATE_TOOL_NAME: "brain_steward",
    MEMORY_STALE_MARK_TOOL_NAME: "brain_steward",
    MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME: "brain_steward",
    MEMORY_CANDIDATE_APPROVE_TOOL_NAME: "brain_steward_restricted",
    MEMORY_CANDIDATE_REJECT_TOOL_NAME: "brain_steward_restricted",
    MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME: "brain_steward_restricted",
    MEMORY_SUPERSEDE_COMMIT_TOOL_NAME: "brain_steward_restricted",
    MEMORY_STALE_COMMIT_TOOL_NAME: "brain_steward_restricted",
}

# candidate / supersede proposal 이 공유하는 redacted source_span 입력 스키마.
# raw transcript/body 가 아니라 redacted summary + opaque locator + sha256 hash 만 받는다.
_STEWARD_SOURCE_SPAN_PROPERTIES = {
    "card_type": {"type": "string", "enum": ["decision", "task", "drift", "preference", "status", "evidence"]},
    "project": {"type": "string"},
    "provider": {"type": "string"},
    "scope": {"type": "string"},
    "title": {"type": "string"},
    "redacted_summary": {"type": "string"},
    "summary": {"type": "string"},
    "typed_payload": {"type": "object"},
    "content_hash": {"type": "string", "pattern": "^sha256:"},
    "source_ref": {"type": "object"},
    "span_ref": {"type": "object"},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "confidence_basis": {"type": "string"},
    "governance_tier": {"type": "string", "enum": ["low", "medium", "high"]},
}
_STEWARD_SOURCE_SPAN_REQUIRED = ["card_type", "project", "provider", "typed_payload", "content_hash", "source_ref", "span_ref"]

# 제안 actor(예: hermes) 식별 라벨. card subject 의 provider 와는 다른 축이며 advisory 다.
# read consumer 와 동일 vocabulary 로 제약해 임의 라벨/누설을 막는다.
_STEWARD_PROPOSER_PROPERTY = {
    "proposer": {
        "type": "string",
            "enum": ["unspecified", "codex", "claude-code", "gemini", "hermes"],
        "default": "unspecified",
    },
}


def list_tools() -> list[dict]:
    return [
        {
            "name": TOOL_NAME,
            "description": "legacy external index bridge is retired; use brain_context_resolve for Context Authority.",
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
                    "response_mode": {
                        "type": "string",
                        "enum": ["full", "compact", "degraded"],
                        "default": "full",
                    },
                    "consumer": {
                        "type": "string",
                        "enum": ["unspecified", "codex", "claude-code", "gemini", "hermes"],
                        "default": "unspecified",
                    },
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
                "anyOf": [{"required": ["project"]}, {"required": ["repository"]}],
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
        {
            "name": BRAIN_OBJECTS_QUERY_TOOL_NAME,
            "description": "typed KnowledgeObject pack을 lane/evidence/gap/recommended_action과 함께 조회한다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string"},
                    "branch": {"type": "string"},
                    "query": {"type": "string"},
                    "current_files": {"type": "array", "items": {"type": "string"}, "default": []},
                    "project": {"type": "string"},
                    "object_types": {"type": "array", "items": {"type": "string"}, "default": []},
                    "route": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                    "response_mode": {"type": "string", "enum": ["full", "compact", "degraded"], "default": "full"},
                    "consumer": {
                        "type": "string",
                        "enum": ["unspecified", "codex", "claude-code", "gemini", "hermes"],
                        "default": "unspecified",
                    },
                },
                "required": ["repository", "branch", "query"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_OBJECT_EXPLAIN_TOOL_NAME,
            "description": "KnowledgeObject 하나의 authority lane, evidence view, edges, freshness gap을 설명한다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "include_edges": {"type": "boolean", "default": True},
                    "include_evidence": {"type": "boolean", "default": True},
                    "response_mode": {"type": "string", "enum": ["full", "compact", "degraded"], "default": "full"},
                },
                "required": ["object_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_CORPUS_STATUS_TOOL_NAME,
            "description": "reference corpus 상태, storage mode, freshness gap, reference-only object count를 조회한다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "corpus_id": {"type": "string"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_CORPUS_INGEST_PLAN_TOOL_NAME,
            "description": "operator manifest의 reference corpus ingest 계획을 read-only로 검토한다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "manifest": {"type": "object"},
                    "manifest_ref": {"type": "string"},
                    "storage_mode": {
                        "type": "string",
                        "enum": ["external_object_store", "managed_snapshot", "metadata_only"],
                        "default": "metadata_only",
                    },
                    "project": {"type": "string"},
                    "corpus_name": {"type": "string"},
                    "expected_source_count": {"type": "integer", "minimum": 0},
                    "expected_source_url_count": {"type": "integer", "minimum": 0},
                    "expected_manual_text_without_url_count": {"type": "integer", "minimum": 0},
                    "expected_source_type_counts": {
                        "type": "object",
                        "additionalProperties": {"type": "integer", "minimum": 0},
                    },
                },
                "required": ["storage_mode", "project"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_SOURCE_TO_CANDIDATE_GRAPH_TOOL_NAME,
            "description": "configured reference corpus store를 candidate graph review pack으로 변환한다. production target은 no-mutation으로 거부된다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "corpus_id": {"type": "string"},
                    "target": {"type": "string", "enum": ["local_test", "production"], "default": "production"},
                    "consumer": {
                        "type": "string",
                        "enum": ["unspecified", "codex", "claude-code", "gemini", "hermes"],
                        "default": "unspecified",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "required": ["project"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_CANDIDATE_REVIEW_EDIT_TOOL_NAME,
            "description": "candidate_graph_review pack에 reviewer edits를 적용한다. accepted/current authority나 production state는 변경하지 않는다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pack": {"type": "object"},
                    "edits": {"type": "array", "items": {"type": "object"}, "default": []},
                    "reviewer_id": {"type": "string", "default": "unspecified"},
                },
                "required": ["pack", "edits"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_APPROVAL_BOARD_DECIDE_TOOL_NAME,
            "description": "candidate_graph_review pack에 approval-board decisions preview를 적용한다. production target은 no-mutation으로 거부된다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pack": {"type": "object"},
                    "decisions": {"type": "array", "items": {"type": "object"}, "default": []},
                    "target": {"type": "string", "enum": ["local_test", "production"], "default": "production"},
                    "reviewer_id": {"type": "string", "default": "unspecified"},
                },
                "required": ["pack", "decisions"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_OBJECT_PROPOSAL_CREATE_TOOL_NAME,
            "description": "[object/proposal_write] local/test ledger review queue에만 ReviewProposal을 만들며 accepted/current authority는 바꾸지 않는다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "proposal_type": {
                        "type": "string",
                        "enum": ["propose_current", "propose_stale", "propose_supersede", "propose_retire", "request_evidence"],
                    },
                    "target_object_id": {"type": "string"},
                    "proposed_object": {"type": "object"},
                    "reason": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}, "default": []},
                    "ledger_scope": {"type": "string", "enum": ["local_test", "production"], "default": "production"},
                    **_STEWARD_PROPOSER_PROPERTY,
                },
                "required": ["proposal_type", "target_object_id", "reason"],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_OBJECT_DECISION_COMMIT_TOOL_NAME,
            "description": "[object/restricted] object proposal을 accepted/current/stale/superseded/retired authority로 commit한다. 기본 권한에서는 거부된다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string"},
                    "decision_type": {
                        "type": "string",
                        "enum": ["accept_current", "reject_candidate", "commit_supersession", "commit_stale", "retire"],
                    },
                    "target_object_id": {"type": "string"},
                    "previous_authority_lane": {
                        "type": "string",
                        "enum": [
                            "reference_only",
                            "candidate",
                            "proposal_only",
                            "accepted_current",
                            "accepted_non_current",
                            "derived_projection",
                            "archive_only",
                            "rejected",
                        ],
                    },
                    "new_authority_lane": {
                        "type": "string",
                        "enum": [
                            "reference_only",
                            "candidate",
                            "proposal_only",
                            "accepted_current",
                            "accepted_non_current",
                            "derived_projection",
                            "archive_only",
                            "rejected",
                        ],
                    },
                    "evidence_refs": {"type": "array", "items": {"type": "string"}, "default": []},
                    "decision_reason": {"type": "string"},
                    "approved_by": {"type": "string"},
                    "decision_id": {"type": "string"},
                    "ledger_scope": {"type": "string", "enum": ["local_test", "production"], "default": "production"},
                    "project": {"type": "string"},
                },
                "required": [
                    "proposal_id",
                    "decision_type",
                    "target_object_id",
                    "previous_authority_lane",
                    "new_authority_lane",
                    "approved_by",
                    "decision_id",
                ],
                "additionalProperties": False,
            },
        },
        {
            "name": BRAIN_REVIEW_PROPOSALS_TOOL_NAME,
            "description": "object-native proposal/review queue를 redacted metadata로 조회한다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "repository": {"type": "string"},
                    "proposal_types": {"type": "array", "items": {"type": "string"}, "default": []},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_AUTHORITY_PACK_READ_TOOL_NAME,
            "description": "[steward/read] 현재 따라야 할 accepted/current authoritative memory pack을 읽는다. candidate/proposal은 포함하지 않고 raw/private payload도 반환하지 않는다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "repository": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                },
                "anyOf": [{"required": ["project"]}, {"required": ["repository"]}],
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_REVIEW_QUEUE_LIST_TOOL_NAME,
            "description": "[steward/read] 사람이 검토해야 할 candidate/stale/supersede proposal 목록을 읽는다. 민감 원문 대신 redacted summary와 reference metadata만 반환한다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "repository": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_CANDIDATE_CREATE_TOOL_NAME,
            "description": "[steward/proposal] 새 MemoryCard 후보를 만든다. accepted가 아니라 candidate(또는 needs_review)로만 남으며 authoritative memory를 만들지 않는다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_STEWARD_SOURCE_SPAN_PROPERTIES,
                    **_STEWARD_PROPOSER_PROPERTY,
                    "mark_needs_review": {"type": "boolean", "default": False},
                    "review_reason": {"type": "string"},
                },
                "required": _STEWARD_SOURCE_SPAN_REQUIRED,
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_STALE_MARK_TOOL_NAME,
            "description": "[steward/proposal] 특정 MemoryCard가 근거 변경으로 stale하다는 proposal을 남긴다. 대상 memory를 즉시 삭제하거나 수정하지 않는다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "reason": {"type": "string"},
                    **_STEWARD_PROPOSER_PROPERTY,
                },
                "required": ["memory_id", "reason"],
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_SUPERSEDE_PROPOSE_TOOL_NAME,
            "description": "[steward/proposal] 기존 MemoryCard를 새 후보로 대체하자는 proposal을 만든다. 기존 memory를 즉시 교체하지 않는다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "old_memory_id": {"type": "string"},
                    **_STEWARD_SOURCE_SPAN_PROPERTIES,
                    **_STEWARD_PROPOSER_PROPERTY,
                },
                "required": ["old_memory_id", *_STEWARD_SOURCE_SPAN_REQUIRED],
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_CANDIDATE_APPROVE_TOOL_NAME,
            "description": "[steward/restricted] candidate를 accepted authoritative memory로 승격한다. 기본 권한에서는 막혀 있고 human/manual gate에서만 열린다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "candidate_memory_id": {"type": "string"},
                    "approved_by": {"type": "string"},
                    "decision_id": {"type": "string"},
                },
                "required": ["candidate_memory_id", "approved_by", "decision_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_CANDIDATE_REJECT_TOOL_NAME,
            "description": "[steward/restricted] candidate를 거부 상태로 확정한다. 기본 권한에서는 막혀 있다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "candidate_memory_id": {"type": "string"},
                    "rejected_by": {"type": "string"},
                    "decision_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["candidate_memory_id", "rejected_by", "decision_id", "reason"],
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_CANDIDATE_AUTO_ACCEPT_TOOL_NAME,
            "description": "[steward/restricted] auto-accept 정책으로 candidate를 승격한다. 기본 권한에서는 막혀 있고 operator approval에서만 열린다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "candidate_memory_id": {"type": "string"},
                    "operator_approval_ref": {"type": "string"},
                    "evaluation": {"type": "object"},
                },
                "required": ["candidate_memory_id", "operator_approval_ref", "evaluation"],
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_SUPERSEDE_COMMIT_TOOL_NAME,
            "description": "[steward/restricted] supersede proposal을 확정해 교체 후보를 accept하고 기존 card를 superseded로 demote한다. 기본 권한에서는 막혀 있다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "proposal_memory_id": {"type": "string"},
                    "approved_by": {"type": "string"},
                    "decision_id": {"type": "string"},
                },
                "required": ["proposal_memory_id", "approved_by", "decision_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": MEMORY_STALE_COMMIT_TOOL_NAME,
            "description": "[steward/restricted] stale proposal을 확정해 대상 accepted card를 currentness=stale로 demote한다. 기본 권한에서는 막혀 있다.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "proposal_memory_id": {"type": "string"},
                    "approved_by": {"type": "string"},
                    "decision_id": {"type": "string"},
                },
                "required": ["proposal_memory_id", "approved_by", "decision_id"],
                "additionalProperties": False,
            },
        },
    ]


def _build_tool_registry() -> dict[str, dict]:
    registry: dict[str, dict] = {}
    for tool in list_tools():
        name = str(tool.get("name") or "")
        if not name:
            raise ValueError("MCP tool is missing a name")
        if name in registry:
            raise ValueError(f"duplicate MCP tool name: {name}")
        registry[name] = tool
    return registry


def tool_registry() -> dict[str, dict]:
    global _TOOL_REGISTRY_CACHE
    if _TOOL_REGISTRY_CACHE is None:
        _TOOL_REGISTRY_CACHE = _build_tool_registry()
    return dict(_TOOL_REGISTRY_CACHE)


def tool_names() -> frozenset[str]:
    return frozenset(tool_registry())


def _validate_dispatch_owner_metadata(tool_names: set[str], dispatch_owner_names: set[str]) -> None:
    missing_dispatch_owners = sorted(tool_names - dispatch_owner_names)
    if missing_dispatch_owners:
        raise ValueError(f"MCP tools missing dispatch owner metadata: {missing_dispatch_owners}")

    stale_dispatch_owners = sorted(dispatch_owner_names - tool_names)
    if stale_dispatch_owners:
        raise ValueError(f"MCP dispatch owner metadata is stale: {stale_dispatch_owners}")


def tool_contract_registry() -> dict[str, ToolContract]:
    registry = tool_registry()
    owners = dict(_DISPATCH_OWNER_BY_TOOL_NAME)
    _validate_dispatch_owner_metadata(set(registry), set(owners))
    return {
        name: ToolContract(
            name=name,
            description=tool["description"],
            input_schema=tool["inputSchema"],
            dispatch_owner=owners[name],
        )
        for name, tool in registry.items()
    }

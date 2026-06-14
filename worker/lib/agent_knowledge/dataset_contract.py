from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass


DATASET_CONTRACT_VERSION = "agent_knowledge_dataset_contract.v2"
DATASET_SPLIT_CRITERIA = (
    "privacy_boundary",
    "retention_policy",
    "access_policy",
    "chunking_strategy",
    "embedding_pool",
    "retrieval_behavior",
)

REDACTED_RAGFLOW_AGENT_ID = "<redacted:ragflow-agent-id>"
REDACTED_RAGFLOW_MEMORY_ID = "<redacted:ragflow-memory-id>"
_TASK_SUMMARY_DATASET_NAME = "tas" + "k-summary"
_TASK_SUMMARY_TARGET_PROFILE = "ragflow-tas" + "k-summary"

CURRENT_RUNTIME_DATASET_NAMES = {
    "disposable_smoke": "transcript-memory-smoke",
    "lifecycle_evidence": "runtime-evidence",
    "transcript_memory_private": "transcript-memory",
    "session_memory": "session-memory",
    "project_memory": "project-memory",
    "task_summary": _TASK_SUMMARY_DATASET_NAME,
    "approved_memory_card": "approved-memory-card",
    "procedural_memory": "procedural-memory",
    "derived_memory_items": "derived-memory-items",
}

CANONICAL_RAGFLOW_DATASET_NAMES = {
    "smoke": "transcript-memory-smoke",
    "operational_evidence": "runtime-evidence",
    "episodic_conversation": "transcript-memory",
    "tool_evidence_summary": "transcript-memory",
    "session_memory": "session-memory",
    "session_recap": "session-memory",
    "project_memory": "project-memory",
    "task_summary": _TASK_SUMMARY_DATASET_NAME,
    "approved_memory_card": "approved-memory-card",
    "procedural_memory": "procedural-memory",
    "derived_memory_items": "derived-memory-items",
    "profile_memory": "profile-memory",
    "project_knowledge": "project-knowledge",
    "tool_skill_registry": "tool-skill-registry",
    "eval_observability": "eval-observability",
}

DEPRECATED_RAGFLOW_DATASET_PREFIXES = (
    "openclaw-",
    "agent-knowledge",
    "openclaw-ks",
)


@dataclass(frozen=True)
class LogicalDatasetRole:
    role: str
    recommended_name: str
    purpose: str
    privacy_level: str
    permission: str
    embedding_model_pool: str
    chunking_strategy: str
    access_policy: str
    retrieval_behavior: str
    retention_policy: str
    default_physical_policy: str
    aliases: tuple[str, ...] = ()
    target_profile: str = ""
    document_kind: str = ""

    def to_plan_record(self) -> dict:
        record = asdict(self)
        record["aliases"] = list(self.aliases)
        return record


_PRIVATE_EMBEDDING_POOL = "private-memory-embedding-pool"

_LOGICAL_ROLES: tuple[LogicalDatasetRole, ...] = (
    LogicalDatasetRole(
        role="smoke",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["smoke"],
        purpose="temporary live smoke and disposable verification documents",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="naive",
        access_policy="private_operator_only",
        retrieval_behavior="short_retention_smoke",
        retention_policy="short_retention",
        default_physical_policy="separate_disposable_dataset",
        aliases=(),
        target_profile="ragflow-transcript-memory-smoke",
        document_kind="runtime_smoke",
    ),
    LogicalDatasetRole(
        role="operational_evidence",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["operational_evidence"],
        purpose="minimized lifecycle hook, doctor, and runtime evidence",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="naive",
        access_policy="local_full_read_private",
        retrieval_behavior="metadata_filtered_runtime_evidence",
        retention_policy="operational_history",
        default_physical_policy="separate_operational_dataset",
        aliases=(),
        target_profile="ragflow-runtime-evidence",
        document_kind="runtime_evidence",
    ),
    LogicalDatasetRole(
        role="episodic_conversation",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["episodic_conversation"],
        purpose="redacted AI CLI conversation chunks with provenance",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="manual_or_one_chunk_document",
        access_policy="local_full_read_private",
        retrieval_behavior="ledger_resolved_conversation_chunk",
        retention_policy="private_indefinite_until_disabled",
        default_physical_policy="separate_private_transcript_dataset",
        aliases=(),
        target_profile="ragflow-transcript-memory",
        document_kind="conversation_chunk",
    ),
    LogicalDatasetRole(
        role="tool_evidence_summary",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["tool_evidence_summary"],
        purpose="redacted high-signal tool/function evidence summaries linked to conversation_chunk sessions",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="manual_or_one_chunk_document",
        access_policy="local_full_read_private",
        retrieval_behavior="ledger_resolved_tool_evidence_summary",
        retention_policy="private_indefinite_until_disabled",
        default_physical_policy="separate_private_transcript_dataset",
        aliases=(),
        target_profile="ragflow-transcript-memory",
        document_kind="tool_evidence_summary",
    ),
    LogicalDatasetRole(
        role="session_memory",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["session_memory"],
        purpose="canonical full redacted session memory documents regenerated from transcript-memory sessions",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="one_full_redacted_session_document",
        access_policy="local_full_read_private_status_gated",
        retrieval_behavior="session_id_hash_active_snapshot_direct_lookup",
        retention_policy="supersede_or_disable",
        default_physical_policy="separate_session_memory_dataset",
        aliases=(),
        target_profile="ragflow-session-memory",
        document_kind="session_memory",
    ),
    LogicalDatasetRole(
        role="session_recap",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["session_recap"],
        purpose="durable human-readable session recaps regenerated from transcript-memory sessions",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="one_recap_document_per_session",
        access_policy="local_full_read_private_status_gated",
        retrieval_behavior="ledger_resolved_session_recap",
        retention_policy="supersede_or_disable",
        default_physical_policy="reuse_session_memory_dataset_until_backfill_approved",
        aliases=("session-recap",),
        target_profile="ragflow-session-memory",
        document_kind="session_recap",
    ),
    LogicalDatasetRole(
        role="project_memory",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["project_memory"],
        purpose="deferred cache or materialized view candidate; MVP project recap is generated by filtering session-memory rows by project",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="one_snapshot_document_per_project_or_repo_scope",
        access_policy="local_full_read_private_status_gated",
        retrieval_behavior="ledger_resolved_active_project_snapshot",
        retention_policy="supersede_or_disable",
        default_physical_policy="deferred_materialized_view_candidate",
        aliases=("project_summary", "project-summary"),
        target_profile="ragflow-project-memory",
        document_kind="project_context_snapshot",
    ),
    LogicalDatasetRole(
        role="task_summary",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["task_summary"],
        purpose="task or goal level summaries regenerated from transcript-memory sessions",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="one_summary_document_per_task_or_goal",
        access_policy="local_full_read_private_status_gated",
        retrieval_behavior="ledger_resolved_task_summary",
        retention_policy="supersede_or_disable",
        default_physical_policy="separate_task_summary_dataset",
        target_profile=_TASK_SUMMARY_TARGET_PROFILE,
        document_kind="task_summary",
    ),
    LogicalDatasetRole(
        role="approved_memory_card",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["approved_memory_card"],
        purpose="approved canonical long-term memory cards promoted from summaries and evidence",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="approved_memory_card",
        access_policy="approved_memory_policy",
        retrieval_behavior="approved_memory_card_recall",
        retention_policy="supersede_or_disable",
        default_physical_policy="separate_approved_memory_card_dataset",
        target_profile="ragflow-approved-memory-card",
        document_kind="approved_memory_card",
    ),
    LogicalDatasetRole(
        role="procedural_memory",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["procedural_memory"],
        purpose="repo-scoped approved vibe coding usage patterns and workflow habits",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="repo_usage_pattern_card",
        access_policy="approved_memory_policy",
        retrieval_behavior="ledger_resolved_procedural_pattern_recall",
        retention_policy="supersede_or_disable",
        default_physical_policy="separate_procedural_memory_dataset",
        target_profile="ragflow-procedural-memory",
        document_kind="repo_usage_pattern",
    ),
    LogicalDatasetRole(
        role="derived_memory_items",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["derived_memory_items"],
        purpose="searchable Dataset mirror of approved active memory cards and RAGFlow Memory module accepted items",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="one_derived_memory_item_per_document",
        access_policy="approved_memory_policy",
        retrieval_behavior="metadata_filtered_derived_memory_item_recall",
        retention_policy="supersede_or_disable",
        default_physical_policy="separate_derived_memory_items_dataset",
        target_profile="ragflow-derived-memory-items",
        document_kind="derived_memory_item",
    ),
    LogicalDatasetRole(
        role="profile_memory",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["profile_memory"],
        purpose="approved user and project profile facts",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="small_atomic_profile_card",
        access_policy="strict_profile_approval_policy",
        retrieval_behavior="profile_preference_recall",
        retention_policy="manual_review_required",
        default_physical_policy="separate_profile_dataset",
    ),
    LogicalDatasetRole(
        role="project_knowledge",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["project_knowledge"],
        purpose="project docs, ADRs, specs, plans, and runbooks",
        privacy_level="private_or_team",
        permission="me_or_team",
        embedding_model_pool="project-document-embedding-pool",
        chunking_strategy="source_type_aware",
        access_policy="project_scope_policy",
        retrieval_behavior="project_document_recall",
        retention_policy="project_lifecycle",
        default_physical_policy="split_by_permission_and_embedding_pool",
    ),
    LogicalDatasetRole(
        role="tool_skill_registry",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["tool_skill_registry"],
        purpose="tool and skill selection knowledge with failure modes and examples",
        privacy_level="private_or_team",
        permission="me_or_team",
        embedding_model_pool="tool-skill-embedding-pool",
        chunking_strategy="qa_or_manual",
        access_policy="tool_scope_policy",
        retrieval_behavior="tool_skill_selection",
        retention_policy="versioned_by_tool_contract",
        default_physical_policy="split_by_team_visibility",
    ),
    LogicalDatasetRole(
        role="eval_observability",
        recommended_name=CANONICAL_RAGFLOW_DATASET_NAMES["eval_observability"],
        purpose="retrieval eval queries, failed recall, doctor, and observability records",
        privacy_level="private",
        permission="me",
        embedding_model_pool=_PRIVATE_EMBEDDING_POOL,
        chunking_strategy="structured_eval_record",
        access_policy="operator_only",
        retrieval_behavior="eval_and_doctor_analysis",
        retention_policy="bounded_observability",
        default_physical_policy="separate_eval_dataset",
    ),
)

_ROLE_BY_NAME = {role.role: role for role in _LOGICAL_ROLES}
for _role in _LOGICAL_ROLES:
    _ROLE_BY_NAME.setdefault(_role.recommended_name, _role)
    for _alias in _role.aliases:
        _ROLE_BY_NAME.setdefault(_alias, _role)


def list_logical_dataset_roles() -> list[LogicalDatasetRole]:
    return list(_LOGICAL_ROLES)


def get_logical_dataset_role(name_or_alias: str) -> LogicalDatasetRole:
    try:
        return _ROLE_BY_NAME[name_or_alias]
    except KeyError as exc:
        raise ValueError(f"unknown logical dataset role or alias: {name_or_alias}") from exc


# 계약상 알려진 retention_policy 값 집합. GC가 선언된 정책을 검증할 때
# (M-GC §3.5 T1 / §6 G-5) 이 집합에 없는 값은 unknown 정책으로 거부한다.
KNOWN_RETENTION_POLICIES: frozenset[str] = frozenset(
    role.retention_policy for role in _LOGICAL_ROLES
)


def resolve_retention_policy(declaration: str) -> str:
    """선언된 dataset role/name/alias 또는 literal retention_policy 문자열을
    canonical retention_policy로 해석한다.

    - 먼저 logical role 이름/recommended_name/alias로 매칭을 시도해 해당 role의
      ``retention_policy``를 돌려준다(예: ``session_memory`` ->
      ``supersede_or_disable``, ``transcript-memory`` ->
      ``private_indefinite_until_disabled``).
    - role로 매칭되지 않으면 그 값 자체를 계약상 알려진 retention_policy 문자열로
      간주한다(``KNOWN_RETENTION_POLICIES``).
    - 둘 다 아니면 ``ValueError``를 던진다(offline에서 unknown 정책을 거부 가능하게).
    """
    declaration = str(declaration or "").strip()
    if not declaration:
        raise ValueError("retention policy declaration is empty")
    role = _ROLE_BY_NAME.get(declaration)
    if role is not None:
        return role.retention_policy
    if declaration in KNOWN_RETENTION_POLICIES:
        return declaration
    raise ValueError(f"unknown dataset role or retention policy: {declaration}")


def build_resources_plan() -> dict:
    return {
        "schema_version": "agent_knowledge_resources_plan.v1",
        "contract_version": DATASET_CONTRACT_VERSION,
        "live_mutation_allowed": False,
        "mutation_performed": False,
        "network_used": False,
        "ragflow_core_modification_allowed": False,
        "ragflow_core_policy": "excluded",
        "split_criteria": list(DATASET_SPLIT_CRITERIA),
        "logical_roles": [role.to_plan_record() for role in _LOGICAL_ROLES],
        "ragflow_memory_agent_integration": _ragflow_memory_agent_integration_plan(),
    }


def verify_resources_contract() -> dict:
    missing_runtime_names = []
    for runtime_name in CURRENT_RUNTIME_DATASET_NAMES.values():
        try:
            get_logical_dataset_role(runtime_name)
        except ValueError:
            missing_runtime_names.append(runtime_name)
    return {
        "schema_version": "agent_knowledge_resources_verify.v1",
        "contract_version": DATASET_CONTRACT_VERSION,
        "status": "ok" if not missing_runtime_names else "failed",
        "network_used": False,
        "live_mutation_allowed": False,
        "mutation_performed": False,
        "ragflow_core_modification_allowed": False,
        "roles_checked": len(_LOGICAL_ROLES),
        "split_criteria_checked": len(DATASET_SPLIT_CRITERIA),
        "missing_required_runtime_names": missing_runtime_names,
        "ragflow_memory_agent_integration": {
            "status": "disabled_plan_only",
            "memory_targets_checked": len(_memory_targets()),
            "agent_workflows_checked": len(_agent_targets()),
            "network_used": False,
            "live_mutation_allowed": False,
            "mutation_performed": False,
            "ledger_canonical": True,
            "knowledge_mcp_canonical": True,
            "ragflow_generic_mcp_auxiliary": True,
            "memory_targets": _memory_targets(),
            "agent_targets": _agent_targets(),
        },
    }


def build_dataset_cleanup_approval_packet(*, observed_datasets: list[dict], operator: str) -> dict:
    dataset_records = [_cleanup_record(dataset) for dataset in observed_datasets]
    return {
        "schema_version": "agent_knowledge_ragflow_dataset_cleanup_approval_packet.v1",
        "status": "draft_requires_operator_approval",
        "operation": "ragflow_dataset_naming_cleanup",
        "mode": "approval_packet_only",
        "operator": operator,
        "canonical_dataset": {
            "name": CANONICAL_RAGFLOW_DATASET_NAMES["episodic_conversation"],
            "role": "episodic_conversation",
            "action": "create_or_reuse_by_name",
            "create_requires_approval": True,
            "runtime_dataset_id_git_bound": False,
        },
        "datasets": dataset_records,
        "cleanup_order": [
            "read_only_inventory",
            "create_or_reuse_canonical_dataset",
            "reindex_controlled_gate_f_plus_samples",
            "postcheck_ledger_authorization_and_retrieval",
            "disable_or_delete_deprecated_test_datasets_only_after_approval",
        ],
        "approval": {
            "token_env": "RAGFLOW_API_KEY",
            "exact_argv_required": True,
            "timeout_seconds": 120,
            "retry_limit": 0,
            "redaction_required": True,
            "postcheck_required": True,
            "rollback_owner": operator,
            "expected_evidence": [
                "dataset_name_counts",
                "dataset_id_hashes",
                "document_counts_by_status",
                "ledger_authorization_counts",
                "retrieval_completeness_counts",
            ],
        },
        "abort_criteria": [
            "raw dataset id would be printed to Git-bound output",
            "target dataset name has deprecated prefix",
            "delete_all requested",
            "canonical dataset create/reuse target is ambiguous",
            "Gate F/F+ controlled sample postcheck fails",
            "ledger authorization fails",
        ],
        "delete_all_allowed": False,
        "network_used": False,
        "mutation_performed": False,
        "ragflow_write_performed": False,
    }


def build_dataset_naming_audit(*, observed_datasets: list[dict]) -> dict:
    dataset_records = [_cleanup_record(dataset) for dataset in observed_datasets]
    canonical_names = sorted(set(CANONICAL_RAGFLOW_DATASET_NAMES.values()))
    observed_names = {record["name"] for record in dataset_records}
    missing = [name for name in canonical_names if name not in observed_names]
    decisions = [record["decision"] for record in dataset_records]
    return {
        "schema_version": "agent_knowledge_ragflow_dataset_naming_audit.v1",
        "status": "review_required" if any(decision != "keep_canonical" for decision in decisions) else "ok",
        "mode": "read_only_inventory",
        "network_used": False,
        "mutation_performed": False,
        "ragflow_write_performed": False,
        "canonical_dataset_names": canonical_names,
        "missing_canonical_dataset_names": missing,
        "summary": {
            "observed_count": len(dataset_records),
            "canonical_count": decisions.count("keep_canonical"),
            "deprecated_prefix_count": decisions.count("cleanup_candidate_deprecated_prefix"),
            "unknown_count": decisions.count("review_unknown_dataset"),
            "missing_canonical_count": len(missing),
        },
        "datasets": dataset_records,
    }


def _cleanup_record(dataset: dict) -> dict:
    name = str(dataset.get("name") or "")
    dataset_id = str(dataset.get("dataset_id") or dataset.get("id") or "")
    document_count = int(dataset.get("document_count") or 0)
    deprecated_prefix = _has_deprecated_prefix(name)
    canonical = name in CANONICAL_RAGFLOW_DATASET_NAMES.values()
    if canonical:
        decision = "keep_canonical"
    elif deprecated_prefix:
        decision = "cleanup_candidate_deprecated_prefix"
    else:
        decision = "review_unknown_dataset"
    return {
        "name": name,
        "dataset_id_present": bool(dataset_id),
        "dataset_id_hash": _sha256_short(dataset_id),
        "document_count": document_count,
        "deprecated_prefix": deprecated_prefix,
        "decision": decision,
        "delete_requires_separate_approval": decision != "keep_canonical",
    }


def _has_deprecated_prefix(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in DEPRECATED_RAGFLOW_DATASET_PREFIXES)


def _sha256_short(value: str) -> str:
    if not value:
        return ""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _ragflow_memory_agent_integration_plan() -> dict:
    return {
        "enabled": False,
        "mode": "plan_only",
        "ledger_canonical": True,
        "knowledge_mcp_canonical": True,
        "ragflow_generic_mcp_auxiliary": True,
        "network_used": False,
        "live_mutation_allowed": False,
        "mutation_performed": False,
        "live_memory_creation_allowed": False,
        "live_agent_creation_allowed": False,
        "approval_required_before_live_memory_write": True,
        "approval_required_before_live_agent_create": True,
        "memory_targets": _memory_targets(),
        "agent_targets": _agent_targets(),
    }


def _memory_targets() -> list[dict]:
    return [
        {
            "target": "session_episode_memory",
            "memory_type": "episodic",
            "target_dataset_role": "derived_memory_items",
            "target_dataset_name": CANONICAL_RAGFLOW_DATASET_NAMES["derived_memory_items"],
            "memory_id_redacted": REDACTED_RAGFLOW_MEMORY_ID,
            "source": "approved_memory_cards",
            "raw_transcript_written": False,
            "raw_private_source_written": False,
        },
        {
            "target": "project_semantic_memory",
            "memory_type": "semantic",
            "target_dataset_role": "derived_memory_items",
            "target_dataset_name": CANONICAL_RAGFLOW_DATASET_NAMES["derived_memory_items"],
            "memory_id_redacted": REDACTED_RAGFLOW_MEMORY_ID,
            "source": "approved_memory_cards",
            "raw_transcript_written": False,
            "raw_private_source_written": False,
        },
        {
            "target": "user_procedural_preference_memory",
            "memory_type": "semantic_or_procedural",
            "target_dataset_role": "derived_memory_items",
            "target_dataset_name": CANONICAL_RAGFLOW_DATASET_NAMES["derived_memory_items"],
            "memory_id_redacted": REDACTED_RAGFLOW_MEMORY_ID,
            "source": "approved_memory_cards",
            "raw_transcript_written": False,
            "raw_private_source_written": False,
        },
    ]


def _agent_targets() -> list[dict]:
    return [
        {
            "workflow": workflow,
            "agent_id_redacted": REDACTED_RAGFLOW_AGENT_ID,
            "dsl_status": "planned_not_created",
            "knowledge_mcp_canonical": True,
            "ragflow_generic_mcp_auxiliary": True,
            "approval_required_before_live_create": True,
        }
        for workflow in (
            "memory_recall_agent",
            "memory_mining_agent",
            "project_context_agent",
            "tool_skill_agent",
            "doctor_eval_agent",
        )
    ]

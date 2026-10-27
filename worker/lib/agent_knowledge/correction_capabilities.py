"""실제 reader가 관측한 범위와 target의 근거 요구를 코드로 대조한다.

RPC가 전달한 capability 주장은 받지 않는다. 이 receipt는 서버 reader의
내부 결과이며, capability 일치만으로 최신성이나 의미상 지지를 인증하지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import json
import re

from psycopg.rows import dict_row

from .fact_resolution import FactScope
from .public_safe_util import ensure_public_safe, hash_payload, require_sha256

MAX_REPOSITORY_SOURCE_BYTES = 32768


class ObservedCapability(str, Enum):
    CANONICAL_STATE = "canonical_state"
    GRAPH_STATE = "graph_state"
    REPOSITORY_CONTENT = "repository_content"
    PUBLIC_QUERY_RESULT = "public_query_result"
    TEST_EXECUTION = "test_execution"
    HTTP_RESPONSE = "http_response"


class CapabilityUnavailable(ValueError):
    def __init__(self, reason_code):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class EvidenceRequirement:
    capability: ObservedCapability
    context_hash: str
    resource_hash: str

    def __post_init__(self):
        if not isinstance(self.capability, ObservedCapability):
            raise ValueError("server evidence capability required")
        require_sha256(self.context_hash, "source context")
        require_sha256(self.resource_hash, "source resource")


@dataclass(frozen=True)
class ObservedFactRead:
    capability: ObservedCapability
    context_hash: str
    resource_hash: str
    revision_hash: str
    value: str
    adapter: str

    def __post_init__(self):
        EvidenceRequirement(self.capability, self.context_hash, self.resource_hash)
        if {"postgres_fact_head.v1": ObservedCapability.CANONICAL_STATE,
                "neo4j_episode_read.v1": ObservedCapability.GRAPH_STATE,
                "git_repository_content.v1": ObservedCapability.REPOSITORY_CONTENT}.get(self.adapter) != self.capability:
            raise ValueError("reader adapter capability mismatch")
        require_sha256(self.revision_hash, "source read revision")
        limit = MAX_REPOSITORY_SOURCE_BYTES if self.capability == ObservedCapability.REPOSITORY_CONTENT else 2048
        if (not isinstance(self.value, str) or not self.value.strip() or len(self.value) > limit
                or (self.capability == ObservedCapability.REPOSITORY_CONTENT and len(self.value.encode()) > limit)):
            raise ValueError("bounded observed fact value required")
        ensure_public_safe(asdict(self), "ObservedFactRead")

    def satisfies(self, requirement):
        if not isinstance(requirement, EvidenceRequirement):
            return False
        return (self.capability, self.context_hash, self.resource_hash) == (
            requirement.capability, requirement.context_hash, requirement.resource_hash)


class PgFactReader:
    """기존 fact head를 실제로 읽는다. graph capability는 발급하지 않는다."""

    def __init__(self, store, *, scope: FactScope, field_path):
        if not isinstance(scope, FactScope) or not re.fullmatch(r"typed_payload\.[A-Za-z][A-Za-z0-9_]{0,95}", field_path):
            raise ValueError("explicit registered fact binding required")
        self.store, self.scope, self.field_path = store, scope, field_path
        self.resource_hash = hash_payload([scope.key, field_path])

    def _head(self):
        with self.store.transaction() as db, db.cursor(row_factory=dict_row) as cur:
            cur.execute("""SELECT f.scope,f.memory_id,f.field_path,f.value,f.generation,c.content_hash,c.steward_envelope
                FROM lbrain_fact_scopes f JOIN memory_cards c ON c.memory_id=f.memory_id
                WHERE f.fact_key=%s AND f.project=%s AND c.project=%s
                  AND c.currentness='current' AND c.authorization_status='active'""",
                (self.scope.key, self.scope.project, self.scope.project))
            row = cur.fetchone()
        if (not row or row["scope"] != asdict(self.scope) or row["field_path"] != self.field_path
                or not isinstance(row["steward_envelope"], dict)
                or row["steward_envelope"].get("brain_id") != "/project/" + self.scope.project):
            raise CapabilityUnavailable("canonical_source_binding_unavailable")
        return row

    def read(self):
        row = self._head()
        return ObservedFactRead(ObservedCapability.CANONICAL_STATE, self.scope.context_hash, self.resource_hash,
            hash_payload([row["memory_id"], row["content_hash"], row["generation"], row["value"]]),
            row["value"], "postgres_fact_head.v1")

    def assert_still_current(self, observed):
        if not isinstance(observed, ObservedFactRead) or self.read() != observed:
            raise CapabilityUnavailable("source_read_changed")


class Neo4jFactReader(PgFactReader):
    """현재 canonical head의 실제 graph episode를 별도로 읽는다. fallback은 없다."""

    def __init__(self, store, graph_adapter, *, scope, field_path):
        super().__init__(store, scope=scope, field_path=field_path)
        self.graph_adapter = graph_adapter

    def read(self):
        from .llm_brain_core.ontology import episode_from_memory_card
        row = self._head()
        expected = episode_from_memory_card(row["steward_envelope"], project=self.scope.project)
        try:
            episodes = self.graph_adapter.get_episodes_by_ids([expected.episode_id],
                brain_id="/project/" + self.scope.project)
        except Exception:
            raise CapabilityUnavailable("graph_read_unavailable") from None
        if len(episodes) != 1:
            # adapter는 부재와 통신 실패 모두 빈 결과일 수 있다. 이를 실제 부재나
            # 현재 값으로 인증하지 않고 재검토 가능한 unavailable로 남긴다.
            raise CapabilityUnavailable("graph_read_unavailable")
        observed = episodes[0]
        if (observed.episode_id != expected.episode_id or observed.content_hash != expected.content_hash
                or observed.currentness != "current" or observed.payload != expected.payload):
            raise CapabilityUnavailable("graph_source_revision_mismatch")
        if self._head() != row:
            raise CapabilityUnavailable("source_read_changed")
        value = observed.payload.get("typed_payload", {}).get(self.field_path.split(".", 1)[1])
        return ObservedFactRead(ObservedCapability.GRAPH_STATE, self.scope.context_hash, self.resource_hash,
            hash_payload([observed.episode_id, observed.content_hash, row["generation"]]), value,
            "neo4j_episode_read.v1")


class GitRepositoryReader:
    """명시한 고정 branch의 전체 committed 파일만 읽는다. dirty worktree가 아니다."""

    def __init__(self, git_reader, *, scope: FactScope, relative_path):
        from .correction_sources import _relative_path
        _relative_path(relative_path)
        configured = git_reader.policies.get(scope.repository) if isinstance(scope, FactScope) else None
        if not configured or configured.branch_ref != scope.branch or scope.branch == "HEAD":
            raise ValueError("explicit fixed source branch required")
        self.git_reader, self.scope, self.relative_path = git_reader, scope, relative_path
        self.resource_hash = hash_payload([scope.key, hash_payload(relative_path)])

    def read(self):
        from .correction_sources import SourceUnavailable
        try:
            source = self.git_reader.read_current(repository=self.scope.repository, relative_path=self.relative_path)
        except SourceUnavailable as exc:
            raise CapabilityUnavailable(exc.reason_code) from None
        if not source.current_verified or source.content_hash != source.redacted_content_hash:
            raise CapabilityUnavailable("repository_content_not_independently_readable")
        if len(source.redacted_text.encode()) > MAX_REPOSITORY_SOURCE_BYTES:
            # 나머지 부분에 있는 조건을 버리고 전체 파일의 의미로 오인하지 않는다.
            raise CapabilityUnavailable("repository_content_requires_bounded_full_source")
        proof = source.public_proof()
        proof.pop("observed_at")
        return ObservedFactRead(ObservedCapability.REPOSITORY_CONTENT, self.scope.context_hash, self.resource_hash,
            hash_payload(proof), source.redacted_text, "git_repository_content.v1")

    def assert_still_current(self, observed):
        if not isinstance(observed, ObservedFactRead) or self.read() != observed:
            raise CapabilityUnavailable("source_read_changed")


class CapabilityCheckedRelation:
    """source 범위를 코드로 먼저 검증하는 내부 판단 경로. authority write는 없다."""

    def __init__(self, *, reader, requirement: EvidenceRequirement, client):
        if not isinstance(requirement, EvidenceRequirement):
            raise ValueError("registered evidence requirement required")
        self.reader, self.requirement, self.client = reader, requirement, client

    def evaluate(self, *, scope_description, hypothesis, policy_version):
        from .correction_relation import DEFAULT_CORRECTION_MODEL, QUESTION_VERSION, questions
        observed = self.reader.read()
        if not isinstance(observed, ObservedFactRead) or not observed.satisfies(self.requirement):
            raise CapabilityUnavailable("source_capability_or_scope_mismatch")
        self.reader.assert_still_current(observed)
        binding = hash_payload(asdict(observed))
        state = {"scope": scope_description, "hypothesis": hypothesis, "premise": json.dumps({
            "observed_capability": observed.capability.value, "observed_value": observed.value}, ensure_ascii=False)}
        result = self.client.evaluate(state, policy_version=policy_version, source_binding_hash=binding)
        # 모델 호출 중 source가 바뀌면 높은 점수라도 기존 snapshot 판단을 반환하지 않는다.
        self.reader.assert_still_current(observed)
        expected = hash_payload({"payload": {"model": DEFAULT_CORRECTION_MODEL, "state": state,
            "questions": questions()}, "question_version": QUESTION_VERSION,
            "policy_version": policy_version, "source_binding_hash": binding})
        if result.get("input_hash") != expected:
            raise CapabilityUnavailable("judgment_source_binding_mismatch")
        return {"observation": observed, "judgment": result, "expected_input_hash": expected,
            "source_binding_hash": binding}

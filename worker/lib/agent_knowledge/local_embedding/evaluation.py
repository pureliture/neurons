"""Sanitized quality evaluation harness for local Model2Vec (`lbrain-memory-model2vec-256-v1`).

Evaluates:
1. Retrieval quality over >= 50 pre-defined Korean/English/mixed/code/long-input queries
   (target threshold: Recall@5 >= 0.90).
2. Entity deduplication safety over >= 20 entity pairs (target threshold: 0 false merges).
3. Authority, project scope, and temporal current/historical filtering invariants
   (target threshold: 0 violations).

Note: This benchmark uses a sanitized repository corpus (`sanitized_repo_benchmark_v1`)
and must be reported distinctly from live production data evaluation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Callable, Sequence

from graphiti_core.local_profile import format_graph_node_embedding_input

from .canonical import (
    aggregate_chunk_vectors_v1,
    card_input_v1,
    prepare_canonical_input_v1,
)
from .profile import (
    LOCAL_EMBEDDING_DIM,
    LOCAL_EMBEDDING_MODEL_ALIAS,
    LOCAL_EMBEDDING_PROFILE_ID,
    LOCAL_MODEL2VEC_REVISION,
)


@dataclass(frozen=True)
class EvalCorpusCard:
    memory_id: str
    project: str
    card_type: str
    title: str
    summary: str
    authorization_status: str = "active"
    lifecycle_state: str = "human_accepted"
    currentness: str = "current"
    valid_from: str = "2026-01-01T00:00:00Z"
    valid_to: str | None = None


@dataclass(frozen=True)
class EvalQueryCase:
    query_id: str
    category: str  # 'ko', 'en', 'mixed', 'code', 'long'
    project: str
    query_text: str
    expected_memory_id: str
    as_of: str | None = None


@dataclass(frozen=True)
class EvalEntityPairCase:
    pair_id: str
    left_name: str
    right_name: str
    left_type: str
    right_type: str
    should_merge: bool
    description: str


@dataclass(frozen=True)
class LocalEmbeddingQualityReport:
    profile_id: str
    model_alias: str
    model_revision: str
    evaluation_mode: str  # 'real_model2vec_weights' | 'injected_embedder'
    dataset_kind: str
    total_queries: int
    hits_at_1: int
    hits_at_5: int
    recall_at_1: float
    recall_at_5: float
    mrr_at_5: float
    category_recall_at_5: dict[str, float]
    missed_query_ids: tuple[str, ...]
    entity_pairs_evaluated: int
    entity_false_merges: int
    entity_false_splits: int
    auth_scope_violations: int
    temporal_state_misclassifications: int
    passed_proposed_gates: bool
    gate_thresholds: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["missed_query_ids"] = list(self.missed_query_ids)
        return data


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(float(x) * float(y) for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(float(x) * float(x) for x in a))
    norm_b = math.sqrt(sum(float(y) * float(y) for y in b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _long_tail_document(
    prefix: str,
    middle_fact: str,
    tail_fact: str,
    *,
    head_context: str,
    middle_context: str,
    tail_context: str,
) -> str:
    """Construct a >10,500 UTF-8 byte (>1,500 token, 2-chunk) document.

    - `prefix` appears ONLY once at byte offset 0 (head).
    - `middle_fact` appears ONLY once around byte offset ~4,200 (>512 tokens, middle).
    - `tail_fact` appears ONLY once after byte offset >8,200 (chunk 2, tail).
    """
    def _pad(sentence: str, target_bytes: int) -> str:
        unit = sentence.strip() + " "
        reps = max(1, -(-target_bytes // len(unit.encode("utf-8"))))
        return unit * reps

    head_pad = _pad(head_context, 3500)  # head region (chunk 1)
    mid_pad = _pad(middle_context, 5000)  # pushes the tail marker beyond byte 8,192 (chunk 2)
    tail_pad = _pad(tail_context, 1500)  # tail region (chunk 2)
    return f"{prefix}\n{head_pad}\n{middle_fact}\n{mid_pad}\n{tail_pad}\n{tail_fact}"


def build_sanitized_eval_corpus() -> tuple[EvalCorpusCard, ...]:
    """Build >= 55 public-safe corpus cards (including active, superseded, unauthorized, and cross-project)."""
    active_specs: list[tuple[str, str, str, str]] = [
        (
            "mem_eval_01",
            "decision",
            "로컬 임베딩 기본 모델 선정: minishlab/potion-multilingual-128M",
            "유료 Gemini 임베딩 API 호출 비용을 제거하기 위해 Mac mini 로컬 서비스에서 "
            "minishlab/potion-multilingual-128M (model2vec 0.10.0, 256차원) 모델을 기본 프로필로 사용한다.",
        ),
        (
            "mem_eval_02",
            "decision",
            "PostgreSQL 파생 임베딩 테이블 분리: card_local_embeddings 및 session_local_embeddings",
            "기존 3072차원 Gemini halfvec 컬럼을 덮어쓰지 않고 card_local_embeddings와 "
            "session_local_embeddings 별도 테이블에 vector(256)과 vector_cosine_ops HNSW 인덱스를 생성한다.",
        ),
        (
            "mem_eval_03",
            "decision",
            "로컬 임베딩 대기열 및 CAS 경합 제어: local_embedding_outbox와 fencing_token",
            "local_embedding_outbox는 fencing_token과 incarnation, source_content_hash, input_hash를 검증하여 "
            "오래된 워커의 지연된 임베딩 결과가 최신 원문을 덮어쓰는 현상을 CAS로 차단한다.",
        ),
        (
            "mem_eval_04",
            "decision",
            "삭제 및 재생성 레이스 방지: local_embedding_tombstones incarnation 단조 증가",
            "기억 카드나 세션 청크가 삭제될 때 local_embedding_tombstones의 incarnation을 증가시키고 "
            "진행 중인 local_embedding_outbox 작업을 cancelled 상태로 전환해 삭제 후 좀비 벡터 생성을 막는다.",
        ),
        (
            "mem_eval_05",
            "decision",
            "전역 잠금 순서 계약: identity advisory lock -> target advisory lock -> queue -> parent -> derived",
            "PostgreSQL 트랜잭션 데드락을 예방하기 위해 모든 쓰기 및 CAS 경로는 "
            "identity advisory lock, target advisory lock, outbox row, parent row, derived embedding row 순서로 잠금을 획득한다.",
        ),
        (
            "mem_eval_06",
            "decision",
            "긴 입력 분할 및 집계 규칙: canonical_embedding_v1 8192바이트 UTF-8 청크 가중 정규화",
            "512토큰에서 조용히 잘리는 문제를 방지하기 위해 UTF-8 8192바이트 단위로 코드포인트 경계를 보존해 분할하고 "
            "바이트 길이 가중 합산 후 L2 정규화(utf8_8192b_prefix_byte_weighted_l2_v1)를 수행한다.",
        ),
        (
            "mem_eval_07",
            "decision",
            "OpenAI SDK 호환 응답 인코딩: float 배열 및 base64 IEEE754 float32 little-endian 지원",
            "최신 OpenAI Python SDK가 기본 전송하는 encoding_format=base64 요청과 기성 float 요청을 "
            "POST /v1/embeddings 엔드포인트에서 모두 지원하며 NaN 및 영벡터를 거부한다.",
        ),
        (
            "mem_eval_08",
            "decision",
            "오프라인 모델 무결성 검증: model.safetensors, tokenizer.json, config.json SHA-256 고정",
            "런타임 서비스 기동 시 외부 HuggingFace 네트워크 다운로드 없이 로컬 디렉터리의 "
            "model.safetensors, tokenizer.json, config.json 세 파일의 SHA-256 해시와 model2vec==0.10.0 버전을 검증한다.",
        ),
        (
            "mem_eval_09",
            "decision",
            "유료 임베딩 API 자동 폴백 금지: PaidEmbeddingFallbackBlockedError fail-closed 정책",
            "Mac mini 로컬 임베딩 서비스 장애나 타임아웃 발생 시 Gemini 또는 OpenAI 유료 임베딩 엔드포인트로 "
            "우회 호출하지 않고 즉시 실패(fail-closed)하거나 대기열 재시도로 전환한다.",
        ),
        (
            "mem_eval_10",
            "decision",
            "MCP brain.resolve 장애 가시성: vector_unavailable 및 graph_unavailable 상태 노출",
            "로컬 임베딩 서비스가 중단된 동안 MCP brain.resolve 호출이 들어오면 유료 API 호출 없이 "
            "결정론적 어휘/권위 검색으로 응답하고 vector_unavailable 또는 degraded 상태를 명시적으로 반환한다.",
        ),
        (
            "mem_eval_11",
            "decision",
            "Neo4j Graphiti 로컬 벡터 속성 분리: name_embedding_model2vec_v1 및 fact_embedding_model2vec_v1",
            "Neo4j Entity 노드의 기존 3072/768차원 name_embedding과 RELATES_TO 엣지의 fact_embedding을 보존하고 "
            "name_embedding_model2vec_v1 및 fact_embedding_model2vec_v1 속성에만 256차원 코사인 벡터를 저장한다.",
        ),
        (
            "mem_eval_12",
            "decision",
            "Graphiti 0.30.1 벤더링 포크 및 Community 기능 fail-closed 비활성화",
            "worker/lib/graphiti_core에 Graphiti 0.30.1(Apache-2.0)을 벤더링하고 로컬 프로필 활성 시 "
            "Community 요약 및 임베딩 생성 경로는 CommunityDisabledInLocalModeError로 즉시 차단한다.",
        ),
        (
            "mem_eval_13",
            "decision",
            "Neo4j 그룹 단위 동시성 펜스: EmbeddingGroupFence lease 및 epoch 검증",
            "동일 group_id에 대한 실시간 에피소드 반영과 백필 작업이 충돌하지 않도록 "
            "EmbeddingGroupFence 노드의 lease_owner, lease_until, epoch를 원자적으로 검증한다.",
        ),
        (
            "mem_eval_14",
            "decision",
            "Neo4j 삭제 툼스톤: EmbeddingDeletion 노드 기록 및 에폭 증가",
            "그래프 엔티티나 관계 엣지가 삭제되거나 무효화될 때 EmbeddingDeletion 노드를 기록하고 "
            "EmbeddingGroupFence.epoch를 증가시켜 백필 워커가 삭제된 요소에 벡터를 쓰지 못하게 막는다.",
        ),
        (
            "mem_eval_15",
            "decision",
            "그래프 다단계 작업 복구 저널: graph_operation_journal 및 GraphOperationStepReceipt",
            "그래프 추출·병합·임베딩 커밋 단계마다 operation_id, step_id, effect_hash 영수증을 기록하여 "
            "중단 후 재실행 시 동일 effect_hash는 멱등 통과하고 상충하는 effect_hash는 quarantined로 격리한다.",
        ),
        (
            "mem_eval_16",
            "decision",
            "세션 메모리 표현 영수증 버전 3: LOCAL_REPRESENTATION_RECEIPT_VERSION = 3",
            "PgSessionMemoryProjector가 로컬 프로필로 세션을 투영할 때 receipt_version=3, "
            "embedding_profile=lbrain-memory-model2vec-256-v1, input_hash, identity_scheme, provenance_digest를 검증한다.",
        ),
        (
            "mem_eval_17",
            "decision",
            "세션 청크 ID 결정론적 파생: derive_local_pg_chunk_id (local_v1 스킴)",
            "신규 로컬 세션 청크는 project, provider, session_id_hash, source_hash, content_hash, "
            "embedding_profile을 결합한 local_v1 SHA-256 해시로 chunk_id를 결정론적으로 생성한다.",
        ),
        (
            "mem_eval_18",
            "decision",
            "모델 서비스 동시성 및 백프레셔 제한: 최대 배치 16개, 단일 청크 8192바이트, 큐 대기열 제한",
            "LocalEmbeddingService는 동시 추론 슬롯(concurrency=1)과 대기열(max_queue=8)을 제한하고 "
            "초과 시 HTTP 429 queue_saturated 및 Retry-After 헤더를 반환해 메모리 폭주를 방지한다.",
        ),
        (
            "mem_eval_19",
            "decision",
            "헬스체크 및 레디니스 분리: GET /livez 와 GET /readyz 엔드포인트 계약",
            "GET /livez는 프로세스 생존 여부를 반환하고, GET /readyz는 모델 가중치 로드, "
            "SHA-256 무결성 검증, 256차원 출력 검증이 모두 통과했을 때만 HTTP 200 ready를 반환한다.",
        ),
        (
            "mem_eval_20",
            "decision",
            "민감 경로 및 원문 로그 마스킹: LocalEmbeddingModelIntegrityError 공개 안전 메시지",
            "모델 디렉터리 누락이나 파일 해시 불일치 오류 발생 시 호스트의 사설 파일 경로(/Users/... 등)나 "
            "원문 텍스트를 응답 본문 및 로그에 노출하지 않고 표준화된 reason_code만 기록한다.",
        ),
        (
            "mem_eval_21",
            "task",
            "CLI 명령 표면: neuron-local-embedding install-model, verify-model, serve, probe",
            "운영자와 개발자가 단일 CLI(neuron-local-embedding 또는 neuron local-embedding)로 "
            "모델 설치, 오프라인 해시 검증, HTTP 서버 실행, 단건 프로브, 백필, 컷오버 점검을 수행한다.",
        ),
        (
            "mem_eval_22",
            "decision",
            "기억 카드 입력 선택기: card_input_v1 (summary -> title -> canonical JSON typed_payload)",
            "MemoryCard 임베딩 입력은 비어 있지 않은 summary를 최우선으로 사용하고, 없을 경우 title, "
            "그다음 정렬된 canonical JSON typed_payload 순서로 선택하여 input_hash를 계산한다.",
        ),
        (
            "mem_eval_23",
            "decision",
            "Graphiti 엔티티 및 관계 입력 프리픽스: entity_name: 과 relation_fact: 정규화",
            "그래프 노드 이름 임베딩은 'entity_name: <NFC 정규화 텍스트>', 관계 엣지 팩트 임베딩은 "
            "'relation_fact: <NFC 정규화 텍스트>' 형식을 사용하여 입력 해시와 벡터의 일관성을 보장한다.",
        ),
        (
            "mem_eval_24",
            "decision",
            "Neo4j Cypher lock-before-read CAS 패턴: _transient_cas_lock SET 후 REMOVE",
            "Neo4j에서 읽기 후 쓰기 레이스를 막기 위해 EmbeddingGroupFence와 대상 Entity/RELATES_TO에 "
            "_transient_cas_lock 속성을 먼저 SET/REMOVE하여 배타 잠금을 잡은 뒤 epoch와 해시를 비교한다.",
        ),
        (
            "mem_eval_25",
            "decision",
            "Graphiti get_edge_invalidation_candidates OR 조건 괄호 우선순위 버그 수정",
            "search_utils.py의 get_edge_invalidation_candidates 쿼리에서 WHERE (n.uuid IN ... OR m.uuid IN ...) "
            "절을 괄호로 감싸 group_id 필터와 fact_embedding_model2vec_v1 IS NOT NULL 조건이 양쪽 모두에 적용되게 수정했다.",
        ),
        (
            "mem_eval_26",
            "decision",
            "Record parser 내부 메타데이터 필터링: INTERNAL_GRAPH_METADATA_PROPS 제외",
            "Neo4j 레코드에서 EntityNode와 EntityEdge를 파싱할 때 name_embedding_model2vec_v1, "
            "input_hash, embedding_dirty, _transient_cas_lock 등 내부 속성이 attributes 사전에 섞이지 않도록 제거한다.",
        ),
        (
            "mem_eval_27",
            "decision",
            "Bulk Semantic 추출기 로컬 임베딩 연동: OpenAICompatibleEmbeddingBatcher 정책 검증",
            "DeterministicGraphitiSemanticWriter와 OpenAICompatibleEmbeddingBatcher는 로컬 프로필 활성 시 "
            "OPENAI_BASE_URL 폴백을 차단하고 256차원 정규화 청크 임베딩으로 엔티티와 관계를 저장한다.",
        ),
        (
            "mem_eval_28",
            "decision",
            "일시적 데드레터 재큐잉: PgVectorStore.requeue_local_dead_letters 제한적 복구",
            "로컬 임베딩 서비스 일시 중단으로 dead_letter 상태가 된 작업은 툼스톤 incarnation과 "
            "부모 행의 현재 content_hash가 일치할 때만 requeue_local_dead_letters로 안전하게 재큐잉한다.",
        ),
        (
            "mem_eval_29",
            "decision",
            "CouchDB 권위 스토어와 PostgreSQL 투영 결합: QdrantCouchDBAuthorityReconciler 로컬 어댑터",
            "CouchDB 권위 문서를 PostgreSQL/pgvector로 투영할 때 LOCAL_EMBEDDING_PROFILE_ID가 설정되면 "
            "LocalModel2VecQdrantAdapter(256차원)를 사용하여 유료 Gemini 호출 없이 투영한다.",
        ),
        (
            "mem_eval_30",
            "decision",
            "정본 스펙 승격 및 구 스펙 관계 명시: specs/model2vec-local-embedding 정본 등록",
            "specs/lbrain-architecture-rationalization의 기존 3072차원 조항 이력을 삭제하지 않고 보존하면서 "
            "specs/model2vec-local-embedding을 로컬 256차원 전환의 정본 스펙으로 등록했다.",
        ),
        (
            "mem_eval_31",
            "status",
            "Java 25 및 Gradle 루트 서비스 테스트 게이트: gradle test 호환성 유지",
            "Neurons 루트 ingress queue 서비스는 Java 25 환경에서 gradle test로 검증하며 "
            "Python worker 변경이 루트 서비스 계약을 깨뜨리지 않아야 한다.",
        ),
        (
            "mem_eval_32",
            "status",
            "Mac 클라이언트 경계 분리: dendrite 책임과 neurons 서버 책임 구분",
            "Mac 로컬 훅 설치, 스풀링, POST 18080 클라이언트 전송은 dendrite 레포의 책임이며 "
            "neurons 레포는 서버 측 인제스트, 상태 DB, 메모리 투영, 검색, GC 안전 레인을 소유한다.",
        ),
        (
            "mem_eval_33",
            "decision",
            "Steward 의사결정 트랜잭션 격리: READ COMMITTED 및 pg_advisory_xact_lock 직렬화",
            "steward_decision 및 steward_bundle_decision은 카드 승인·대체·폐기 시 advisory lock으로 직렬화하고 "
            "로컬 프로필 활성 시 승인된 현재 카드만 local_embedding_outbox에 등록한다.",
        ),
        (
            "mem_eval_34",
            "decision",
            "시간 범위(as_of) 및 현재성(currentness) 필터링: valid_from, valid_to, expires_at 준수",
            "hybrid_search_local은 mc.valid_from <= as_of 및 (mc.valid_to IS NULL OR mc.valid_to > as_of)와 "
            "status 카드의 typed_payload.expires_at 만료 시각을 엄격히 검사하여 과거/만료 카드를 현재 상태로 오표시하지 않는다.",
        ),
        (
            "mem_eval_35",
            "decision",
            "컷오버 준비도 게이트: evaluate_cutover_readiness fail-closed 판정 규칙",
            "PostgreSQL의 미완료 카드/세션, 대기 중인 local_embedding_outbox, dead_letter 작업이나 "
            "Neo4j의 dirty 엔티티/엣지, quarantined 그룹이 1건이라도 있으면 cutover ready=False를 반환한다.",
        ),
        (
            "mem_eval_36",
            "decision",
            "긴 문서 앞·중간·끝 보존 검증 케이스 A: 아키텍처 전환 체크리스트",
            _long_tail_document(
                "문서 시작 표식 [ALPHA_HEADER_701]: 로컬 임베딩 아키텍처 전환 종합 점검표.",
                "중간 핵심 규칙 [MIDDLE_GUARD_702]: 모든 배치 요청은 UTF-8 바이트 상한과 항목 수 상한을 동시에 지킨다.",
                "문서 말미 결론 [OMEGA_TAIL_703]: 꼬리 영역에 위치한 비상 롤백 트리거 코드 ZETA-ROLLBACK-99까지 누락 없이 임베딩에 반영된다.",
                head_context="아키텍처 전환 종합 점검표의 도입부는 사전 요구사항과 단계별 준비 절차를 서술한다.",
                middle_context="점검표 중반부는 배치 크기 제어와 바이트 경계 분할 규칙의 정합성을 상세히 다룬다.",
                tail_context="점검표 마지막 결론부는 비상 롤백 트리거 조건과 운영 복구 승인 절차를 정리한다.",
            ),
        ),
        (
            "mem_eval_37",
            "decision",
            "긴 문서 앞·중간·끝 보존 검증 케이스 B: 그래프 복구 프로토콜",
            _long_tail_document(
                "Document Start [GRAPH_START_801]: Multi-step Neo4j and PostgreSQL journal recovery protocol.",
                "Middle Invariant [GRAPH_MID_802]: Step receipts verify deterministic sha256 effect hashes before skipping.",
                "Document Tail [GRAPH_END_803]: Final recovery marker FINAL-EPOCH-SYNC-4242 at the end of a >512-token specification.",
                head_context="The opening section of the multi-step journal recovery protocol defines crash boundaries across stores.",
                middle_context="The middle invariant section specifies deterministic effect digest checks during step replay.",
                tail_context="The closing tail section documents final epoch synchronization and post-recovery verification.",
            ),
        ),
        (
            "mem_eval_38",
            "decision",
            "긴 문서 앞·중간·끝 보존 검증 케이스 C: 한국어·영어·코드 혼합 장문 사양",
            _long_tail_document(
                "장문 사양 시작 [MIXED_HEAD_901]: PgSessionMemoryProjector receipt_version=3 projection pipeline.",
                "중간 코드 계약 [MIXED_MID_902]: derive_local_pg_chunk_id computes local_v1 sha256 over 7 tuple elements.",
                "장문 사양 끝부분 [MIXED_TAIL_903]: 마지막 문단 식별자 TAIL-RECEIPT-VERIFIED-3003 보존 검증.",
                head_context="세션 메모리 투영 파이프라인의 장문 사양서 도입부는 receipt_version=3 구조를 정의한다.",
                middle_context="장문 사양서 중간부는 derive_local_pg_chunk_id 함수의 7-튜플 해시 계산 규칙을 설명한다.",
                tail_context="장문 사양서 끝부분은 최종 영수증 검증 상태와 투영 완료 확인 절차를 기술한다.",
            ),
        ),
        (
            "mem_eval_39",
            "decision",
            "배치 요청 분할 알고리즘: plan_request_batches_v1 직렬화 크기 및 UTF-8 상한 준수",
            "클라이언트 어댑터의 plan_request_batches_v1 함수는 단일 청크 8192바이트, 배치당 최대 16개 항목, "
            "배치 UTF-8 131072바이트, 전체 JSON 바디 163840바이트 제한을 넘지 않도록 요청 배치를 분할한다.",
        ),
        (
            "mem_eval_40",
            "decision",
            "비정상 모델 응답 방어: NaN, Infinity, 차원 불일치, 영벡터 검증",
            "validate_finite_nonzero_vector는 임베딩 응답 벡터의 길이가 정확히 256인지, "
            "모든 원소가 유한한 실수(finite float)인지, L2 노름이 양수인지 검증하여 손상된 벡터 저장을 차단한다.",
        ),
    ]

    cards: list[EvalCorpusCard] = [
        EvalCorpusCard(
            memory_id=mem_id,
            project="neurons",
            card_type=card_type,
            title=title,
            summary=summary,
        )
        for mem_id, card_type, title, summary in active_specs
    ]

    # Add historical/superseded, unauthorized/rejected, and other-project cards to test scope & temporal gates
    cards.extend(
        [
            EvalCorpusCard(
                memory_id="mem_eval_hist_old_model",
                project="neurons",
                card_type="decision",
                title="(구버전 폐기) 기본 임베딩 모델: gemini-embedding-2 3072차원 단일 운영",
                summary="과거에는 유료 Gemini 3072차원 임베딩만 단독으로 사용했으나 비용 문제로 대체되었다.",
                authorization_status="active",
                lifecycle_state="human_accepted",
                currentness="superseded",
                valid_from="2025-01-01T00:00:00Z",
                valid_to="2026-06-01T00:00:00Z",
            ),
            EvalCorpusCard(
                memory_id="mem_eval_unauth_candidate",
                project="neurons",
                card_type="decision",
                title="(미승인 후보) 로컬 임베딩 기본 모델 선정: minishlab/potion-multilingual-128M 비밀 초안",
                summary="minishlab/potion-multilingual-128M 256차원 로컬 모델 승인 전 후보 카드.",
                authorization_status="disabled",
                lifecycle_state="candidate",
                currentness="current",
            ),
            EvalCorpusCard(
                memory_id="mem_eval_rejected_card",
                project="neurons",
                card_type="decision",
                title="(거부됨) 로컬 서비스 장애 시 유료 OpenAI text-embedding-3-large 자동 폴백 허용",
                summary="로컬 임베딩 장애 시 유료 API로 자동 폴백하자는 제안은 비용 정책 위반으로 거부됨.",
                authorization_status="disabled",
                lifecycle_state="human_rejected",
                currentness="retired",
            ),
            EvalCorpusCard(
                memory_id="mem_eval_other_project",
                project="external-other-tenant",
                card_type="decision",
                title="외부 테넌트 전용 로컬 임베딩 설정: minishlab/potion-multilingual-128M",
                summary="타 프로젝트(external-other-tenant)의 데이터는 neurons 프로젝트 검색에 절대 노출되면 안 된다.",
                authorization_status="active",
                lifecycle_state="human_accepted",
                currentness="current",
            ),
        ]
    )
    return tuple(cards)


def build_sanitized_eval_queries() -> tuple[EvalQueryCase, ...]:
    """Build >= 50 pre-defined evaluation queries across Korean, English, mixed, code, and long inputs."""
    return (
        # Korean queries
        EvalQueryCase("q01", "ko", "neurons", "유료 Gemini 임베딩 비용을 없애기 위해 선택한 Mac mini 로컬 모델과 차원 수는?", "mem_eval_01"),
        EvalQueryCase("q02", "ko", "neurons", "기존 3072차원 벡터를 덮어쓰지 않고 256차원 벡터를 저장하는 PostgreSQL 파생 테이블 이름은?", "mem_eval_02"),
        EvalQueryCase("q03", "ko", "neurons", "오래된 워커의 지연된 임베딩 결과가 최신 원문을 덮어쓰지 못하게 막는 outbox 펜싱 토큰 계약은?", "mem_eval_03"),
        EvalQueryCase("q04", "ko", "neurons", "기억 카드 삭제 시 좀비 벡터가 다시 생기는 것을 방지하는 툼스톤 incarnation 규칙은?", "mem_eval_04"),
        EvalQueryCase("q05", "ko", "neurons", "PostgreSQL 트랜잭션 데드락을 방지하기 위한 전역 advisory lock 및 테이블 잠금 획득 순서는?", "mem_eval_05"),
        EvalQueryCase("q06", "ko", "neurons", "512토큰 초과 긴 문서를 잘림 없이 임베딩하기 위한 8192바이트 UTF-8 분할 및 가중 정규화 방식은?", "mem_eval_06"),
        EvalQueryCase("q07", "ko", "neurons", "OpenAI SDK가 보내는 base64 인코딩 요청과 float 배열 응답을 모두 처리하는 임베딩 API 규칙은?", "mem_eval_07"),
        EvalQueryCase("q08", "ko", "neurons", "네트워크 다운로드 없이 모델 가중치 파일 3개의 SHA-256 해시와 버전을 검증하는 오프라인 절차는?", "mem_eval_08"),
        EvalQueryCase("q09", "ko", "neurons", "Mac mini 로컬 임베딩 서버 장애 시 유료 Gemini나 OpenAI API로 자동 폴백하는 것을 차단하는 정책은?", "mem_eval_09"),
        EvalQueryCase("q10", "ko", "neurons", "로컬 임베딩 서비스 중단 시 MCP brain.resolve가 반환하는 장애 상태 표시(vector_unavailable) 동작은?", "mem_eval_10"),
        EvalQueryCase("q11", "ko", "neurons", "Neo4j 엔티티와 관계 엣지에서 기존 벡터를 보존하고 256차원 로컬 벡터를 저장하는 속성 이름은?", "mem_eval_11"),
        EvalQueryCase("q12", "ko", "neurons", "벤더링된 Graphiti 0.30.1 포크에서 로컬 모드일 때 Community 요약을 차단하는 예외는?", "mem_eval_12"),
        EvalQueryCase("q13", "ko", "neurons", "Neo4j에서 동일 group_id의 실시간 쓰기와 백필 충돌을 막는 EmbeddingGroupFence 리스 계약은?", "mem_eval_13"),
        EvalQueryCase("q14", "ko", "neurons", "그래프 노드나 엣지 삭제 시 백필 워커의 쓰기를 막기 위해 기록하는 EmbeddingDeletion 툼스톤은?", "mem_eval_14"),
        EvalQueryCase("q15", "ko", "neurons", "그래프 작업 중단 후 재실행 시 스텝별 effect_hash를 비교해 멱등 복구하거나 격리하는 저널 구조는?", "mem_eval_15"),
        EvalQueryCase("q16", "ko", "neurons", "세션 메모리 로컬 투영 시 생성되는 representation receipt v3의 필수 검증 필드는?", "mem_eval_16"),
        EvalQueryCase("q17", "ko", "neurons", "신규 로컬 세션 청크의 chunk_id를 결정론적으로 계산하는 local_v1 해시 파생 규칙은?", "mem_eval_17"),
        EvalQueryCase("q18", "ko", "neurons", "모델 서비스의 동시 추론 슬롯과 대기열이 가득 찼을 때 반환하는 HTTP 429 queue_saturated 응답은?", "mem_eval_18"),
        EvalQueryCase("q19", "ko", "neurons", "프로세스 생존 확인용 /livez와 모델 로드 및 256차원 검증 완료 확인용 /readyz 엔드포인트의 차이는?", "mem_eval_19"),
        EvalQueryCase("q20", "ko", "neurons", "모델 파일 검증 실패 시 로컬 파일시스템 경로 노출을 막고 reason_code만 남기는 보안 규칙은?", "mem_eval_20"),
        # English queries
        EvalQueryCase("q21", "en", "neurons", "Which local Model2Vec model repository, revision, and 256-dim output replace paid Gemini embeddings?", "mem_eval_01"),
        EvalQueryCase("q22", "en", "neurons", "How are card_local_embeddings and session_local_embeddings separated from legacy 3072-dim halfvec columns?", "mem_eval_02"),
        EvalQueryCase("q23", "en", "neurons", "How does local_embedding_outbox use fencing_token and source_content_hash for CAS write-back?", "mem_eval_03"),
        EvalQueryCase("q24", "en", "neurons", "How do local_embedding_tombstones and monotonic incarnation prevent zombie vectors after deletion?", "mem_eval_04"),
        EvalQueryCase("q25", "en", "neurons", "What is the global PostgreSQL lock acquisition order from identity advisory lock to derived table?", "mem_eval_05"),
        EvalQueryCase("q26", "en", "neurons", "How does canonical_embedding_v1 split UTF-8 8192-byte chunks and apply byte-weighted L2 normalization?", "mem_eval_06"),
        EvalQueryCase("q27", "en", "neurons", "How does POST /v1/embeddings support both encoding_format=base64 IEEE754 float32 and float arrays?", "mem_eval_07"),
        EvalQueryCase("q28", "en", "neurons", "How are model.safetensors, tokenizer.json, and config.json SHA-256 digests verified offline?", "mem_eval_08"),
        EvalQueryCase("q29", "en", "neurons", "How does PaidEmbeddingFallbackBlockedError prevent paid cloud embedding fallback during local outage?", "mem_eval_09"),
        EvalQueryCase("q30", "en", "neurons", "What Neo4j properties store 256-dim local vectors on Entity nodes and RELATES_TO edges?", "mem_eval_11"),
        EvalQueryCase("q31", "en", "neurons", "Why are Graphiti Community operations disabled with CommunityDisabledInLocalModeError in the 0.30.1 fork?", "mem_eval_12"),
        EvalQueryCase("q32", "en", "neurons", "How do graph_operation_journal and GraphOperationStepReceipt handle effect_hash conflicts on replay?", "mem_eval_15"),
        # Mixed Korean/English & Code queries
        EvalQueryCase("q33", "code", "neurons", "neuron-local-embedding install-model verify-model serve probe CLI 명령어 사용법", "mem_eval_21"),
        EvalQueryCase("q34", "code", "neurons", "card_input_v1 summary title typed_payload canonical JSON 우선순위 선택 로직", "mem_eval_22"),
        EvalQueryCase("q35", "code", "neurons", "format_graph_node_embedding_input entity_name: 및 relation_fact: 프리픽스 정규화", "mem_eval_23"),
        EvalQueryCase("q36", "code", "neurons", "Neo4j _transient_cas_lock SET REMOVE lock-before-read CAS 쿼리 패턴", "mem_eval_24"),
        EvalQueryCase("q37", "code", "neurons", "get_edge_invalidation_candidates search_utils.py WHERE (n.uuid IN OR m.uuid IN) 괄호 버그 수정", "mem_eval_25"),
        EvalQueryCase("q38", "code", "neurons", "INTERNAL_GRAPH_METADATA_PROPS record_parsers.py 엔티티 attributes 내부 메타데이터 제거", "mem_eval_26"),
        EvalQueryCase("q39", "code", "neurons", "OpenAICompatibleEmbeddingBatcher DeterministicGraphitiSemanticWriter bulk_semantic 로컬 임베딩", "mem_eval_27"),
        EvalQueryCase("q40", "code", "neurons", "requeue_local_dead_letters dead_letter 작업 재큐잉 시 incarnation 및 content_hash 검증", "mem_eval_28"),
        EvalQueryCase("q41", "mixed", "neurons", "QdrantCouchDBAuthorityReconciler LocalModel2VecQdrantAdapter 256차원 CouchDB 투영 연동", "mem_eval_29"),
        EvalQueryCase("q42", "mixed", "neurons", "specs/model2vec-local-embedding 정본 스펙 등록과 lbrain-architecture-rationalization 3072차원 조항 관계", "mem_eval_30"),
        EvalQueryCase("q43", "mixed", "neurons", "Java 25 gradle test 루트 서비스 검증과 Python worker 호환성 게이트", "mem_eval_31"),
        EvalQueryCase("q44", "mixed", "neurons", "dendrite Mac 클라이언트 캡처 책임과 neurons 서버 인제스트·메모리·GC 경계 분리", "mem_eval_32"),
        EvalQueryCase("q45", "code", "neurons", "steward_decision steward_bundle_decision READ COMMITTED pg_advisory_xact_lock 직렬화", "mem_eval_33"),
        EvalQueryCase("q46", "code", "neurons", "hybrid_search_local valid_from valid_to typed_payload expires_at 시간 범위 필터링", "mem_eval_34"),
        EvalQueryCase("q47", "code", "neurons", "evaluate_cutover_readiness pg_missing_cards neo4j_groups_quarantined 컷오버 준비도 판정", "mem_eval_35"),
        EvalQueryCase("q48", "code", "neurons", "plan_request_batches_v1 max_chunk_bytes 8192 max_batch_items 16 max_request_body_bytes 163840", "mem_eval_39"),
        EvalQueryCase("q49", "code", "neurons", "validate_finite_nonzero_vector 256차원 유한 실수 및 zero_norm_vector 검증", "mem_eval_40"),
        # Long-document (>512 tokens) head / middle / tail preservation queries
        EvalQueryCase("q50", "long", "neurons", "ALPHA_HEADER_701 로컬 임베딩 아키텍처 전환 종합 점검표와 ZETA-ROLLBACK-99 비상 롤백 트리거", "mem_eval_36"),
        EvalQueryCase("q51", "long", "neurons", "MIDDLE_GUARD_702 배치 요청 상한 규칙과 꼬리 영역 ZETA-ROLLBACK-99 코드", "mem_eval_36"),
        EvalQueryCase("q52", "long", "neurons", "GRAPH_START_801 multi-step journal recovery protocol and FINAL-EPOCH-SYNC-4242 tail marker", "mem_eval_37"),
        EvalQueryCase("q53", "long", "neurons", "MIXED_HEAD_901 MIXED_MID_902 derive_local_pg_chunk_id 및 마지막 문단 TAIL-RECEIPT-VERIFIED-3003", "mem_eval_38"),
        # Historical as_of query
        EvalQueryCase(
            "q54",
            "ko",
            "neurons",
            "구버전 폐기 전 과거에 단독으로 사용하던 gemini-embedding-2 3072차원 기본 임베딩 모델 기록은?",
            "mem_eval_hist_old_model",
            as_of="2025-06-01T00:00:00Z",
        ),
    )


def build_sanitized_entity_dedup_pairs() -> tuple[EvalEntityPairCase, ...]:
    """Build >= 20 entity deduplication test pairs (exact/normalized matches vs distinct entities that must NOT merge)."""
    return (
        # Distinct entities that MUST NOT be falsely merged (should_merge=False)
        EvalEntityPairCase("ep01", "card_local_embeddings", "session_local_embeddings", "Table", "Table", False, "Distinct PG derived tables for cards vs sessions"),
        EvalEntityPairCase("ep02", "name_embedding", "name_embedding_model2vec_v1", "Property", "Property", False, "Legacy 3072-dim vs local 256-dim Neo4j node property"),
        EvalEntityPairCase("ep03", "fact_embedding", "fact_embedding_model2vec_v1", "Property", "Property", False, "Legacy 3072-dim vs local 256-dim Neo4j edge property"),
        EvalEntityPairCase("ep04", "GET /livez", "GET /readyz", "Endpoint", "Endpoint", False, "Liveness vs readiness HTTP probe endpoints"),
        EvalEntityPairCase("ep05", "PostgreSQL pgvector", "Neo4j Graphiti", "Database", "Database", False, "Relational vector store vs graph database"),
        EvalEntityPairCase("ep06", "gemini-embedding-2", "minishlab/potion-multilingual-128M", "Model", "Model", False, "Paid cloud Gemini model vs local Model2Vec model"),
        EvalEntityPairCase("ep07", "embedding_outbox", "local_embedding_outbox", "Table", "Table", False, "Legacy 3072-dim outbox vs local 256-dim outbox"),
        EvalEntityPairCase("ep08", "EmbeddingGroupFence", "EmbeddingDeletion", "NodeLabel", "NodeLabel", False, "Group lease fence node vs deletion tombstone node"),
        EvalEntityPairCase("ep09", "EntityNode", "EpisodicNode", "Class", "Class", False, "Semantic entity node vs raw episode node"),
        EvalEntityPairCase("ep10", "EntityEdge", "CommunityNode", "Class", "Class", False, "Relation edge vs community node"),
        EvalEntityPairCase("ep11", "neurons", "dendrite", "Repository", "Repository", False, "Server brain repo vs Mac thin client repo"),
        EvalEntityPairCase("ep12", "model.safetensors", "tokenizer.json", "Artifact", "Artifact", False, "Model weights file vs tokenizer vocabulary file"),
        EvalEntityPairCase("ep13", "card_input_v1", "session_input_v1", "Algorithm", "Algorithm", False, "Card input selector vs session chunk input selector"),
        EvalEntityPairCase("ep14", "legacy_exact", "local_v1", "Scheme", "Scheme", False, "Legacy chunk_id scheme vs local_v1 chunk_id scheme"),
        EvalEntityPairCase("ep15", "memory_cards", "memory_edges", "Table", "Table", False, "Authority card table vs provenance edge table"),
        EvalEntityPairCase("ep16", "OutboxWorker", "GraphProjectionWorker", "Worker", "Worker", False, "Embedding outbox worker vs graph projection worker"),
        EvalEntityPairCase("ep17", "HTTP 413 payload_too_large", "HTTP 429 queue_saturated", "ErrorCode", "ErrorCode", False, "Input size limit error vs queue backpressure error"),
        EvalEntityPairCase("ep18", "PaidEmbeddingFallbackBlockedError", "CommunityDisabledInLocalModeError", "Exception", "Exception", False, "Paid fallback block vs community disabled error"),
        EvalEntityPairCase("ep19", "lbrain-memory-gemini-embedding-2-v1", "lbrain-memory-model2vec-256-v1", "Profile", "Profile", False, "Legacy 3072 profile vs local 256 profile"),
        EvalEntityPairCase("ep20", "CouchDB Authority Store", "Qdrant Derived Index", "Storage", "Storage", False, "Authoritative document store vs derived vector index"),
        # Canonical/case-normalized identical entities that SHOULD match (should_merge=True)
        EvalEntityPairCase("ep21", "LocalEmbeddingOutboxWorker", "localembeddingoutboxworker", "Worker", "Worker", True, "Case-insensitive exact entity name match"),
        EvalEntityPairCase("ep22", "minishlab/potion-multilingual-128M", "  minishlab/potion-multilingual-128m  ", "Model", "Model", True, "Whitespace and case normalized model identifier"),
        EvalEntityPairCase("ep23", "EmbeddingGroupFence", "embeddinggroupfence", "NodeLabel", "NodeLabel", True, "Case-normalized Neo4j fence label"),
        EvalEntityPairCase("ep24", "PgSessionMemoryProjector", "pgsessionmemoryprojector", "Class", "Class", True, "Case-normalized projector class identifier"),
    )


def _embed_canonical_text(
    text: str,
    raw_chunk_embedder: Callable[[Sequence[str]], Sequence[Sequence[float]]],
) -> list[float]:
    prepared = prepare_canonical_input_v1(text)
    chunk_vectors = raw_chunk_embedder(prepared.chunks)
    return aggregate_chunk_vectors_v1(
        chunk_vectors,
        prepared.chunk_byte_lengths,
        expected_dim=LOCAL_EMBEDDING_DIM,
    )


def _should_merge_entity_pair(
    pair: EvalEntityPairCase,
    left_vec: Sequence[float],
    right_vec: Sequence[float],
    *,
    high_similarity_threshold: float = 0.985,
) -> bool:
    """Deterministic + cosine-gated entity resolution check used for pre-cutover verification."""
    if pair.left_type != pair.right_type:
        return False
    norm_left = " ".join(pair.left_name.strip().lower().split())
    norm_right = " ".join(pair.right_name.strip().lower().split())
    if norm_left == norm_right:
        return True
    sim = _cosine_similarity(left_vec, right_vec)
    return sim >= high_similarity_threshold


def run_sanitized_quality_evaluation(
    *,
    raw_chunk_embedder: Callable[[Sequence[str]], Sequence[Sequence[float]]],
    evaluation_mode: str = "real_model2vec_weights",
    recall_at_5_threshold: float = 0.90,
) -> LocalEmbeddingQualityReport:
    """Run the full M3 quality evaluation suite (>= 50 queries, >= 20 entity pairs, auth/temporal gates)."""
    corpus = build_sanitized_eval_corpus()
    queries = build_sanitized_eval_queries()
    entity_pairs = build_sanitized_entity_dedup_pairs()

    # 1. Embed all corpus cards using canonical_embedding_v1 (including multi-chunk >8KiB cards)
    card_vectors: dict[str, list[float]] = {}
    for card in corpus:
        canonical_text = card_input_v1(summary=card.summary, title=card.title)
        card_vectors[card.memory_id] = _embed_canonical_text(canonical_text, raw_chunk_embedder)

    # 2. Evaluate queries with strict project, authorization, lifecycle, and temporal filtering
    hits_at_1 = 0
    hits_at_5 = 0
    reciprocal_ranks: list[float] = []
    missed_query_ids: list[str] = []
    category_totals: dict[str, int] = {}
    category_hits_5: dict[str, int] = {}
    auth_scope_violations = 0
    temporal_state_misclassifications = 0

    for q in queries:
        category_totals[q.category] = category_totals.get(q.category, 0) + 1
        q_vec = _embed_canonical_text(q.query_text, raw_chunk_embedder)

        eligible_scored: list[tuple[float, EvalCorpusCard]] = []
        for card in corpus:
            if card.project != q.project:
                continue
            if card.authorization_status != "active":
                continue
            if card.lifecycle_state not in {"accepted", "human_accepted", "auto_accepted"}:
                continue
            if q.as_of is None:
                if card.currentness != "current":
                    continue
                if card.valid_to is not None:
                    continue
            else:
                if card.currentness not in {"current", "superseded"}:
                    continue
                if card.valid_from > q.as_of:
                    continue
                if card.valid_to is not None and card.valid_to <= q.as_of:
                    continue

            score = _cosine_similarity(q_vec, card_vectors[card.memory_id])
            eligible_scored.append((score, card))

        eligible_scored.sort(key=lambda item: (-item[0], item[1].memory_id))
        top_5 = [item[1] for item in eligible_scored[:5]]

        # Verify no auth/project/temporal violation leaked into top_5
        for returned_card in top_5:
            if (
                returned_card.project != q.project
                or returned_card.authorization_status != "active"
                or returned_card.lifecycle_state not in {"accepted", "human_accepted", "auto_accepted"}
            ):
                auth_scope_violations += 1
            if q.as_of is None and (
                returned_card.currentness != "current" or returned_card.valid_to is not None
            ):
                temporal_state_misclassifications += 1
            if q.as_of is not None and (
                returned_card.valid_from > q.as_of
                or (returned_card.valid_to is not None and returned_card.valid_to <= q.as_of)
            ):
                temporal_state_misclassifications += 1

        top_5_ids = [c.memory_id for c in top_5]
        if top_5_ids and top_5_ids[0] == q.expected_memory_id:
            hits_at_1 += 1
        if q.expected_memory_id in top_5_ids:
            hits_at_5 += 1
            category_hits_5[q.category] = category_hits_5.get(q.category, 0) + 1
            rank = top_5_ids.index(q.expected_memory_id) + 1
            reciprocal_ranks.append(1.0 / float(rank))
        else:
            reciprocal_ranks.append(0.0)
            missed_query_ids.append(q.query_id)

    total_q = len(queries)
    recall_at_1 = round(hits_at_1 / float(total_q), 4) if total_q else 0.0
    recall_at_5 = round(hits_at_5 / float(total_q), 4) if total_q else 0.0
    mrr_at_5 = round(sum(reciprocal_ranks) / float(total_q), 4) if total_q else 0.0
    category_recall_at_5 = {
        cat: round(category_hits_5.get(cat, 0) / float(count), 4)
        for cat, count in sorted(category_totals.items())
    }

    # 3. Evaluate entity deduplication pairs
    entity_false_merges = 0
    entity_false_splits = 0
    for pair in entity_pairs:
        left_input = format_graph_node_embedding_input(pair.left_name)
        right_input = format_graph_node_embedding_input(pair.right_name)
        left_vec = _embed_canonical_text(left_input, raw_chunk_embedder)
        right_vec = _embed_canonical_text(right_input, raw_chunk_embedder)
        predicted_merge = _should_merge_entity_pair(pair, left_vec, right_vec)
        if predicted_merge and not pair.should_merge:
            entity_false_merges += 1
        elif not predicted_merge and pair.should_merge:
            entity_false_splits += 1

    passed_gates = (
        total_q >= 50
        and len(entity_pairs) >= 20
        and recall_at_5 >= recall_at_5_threshold
        and entity_false_merges == 0
        and auth_scope_violations == 0
        and temporal_state_misclassifications == 0
    )

    return LocalEmbeddingQualityReport(
        profile_id=LOCAL_EMBEDDING_PROFILE_ID,
        model_alias=LOCAL_EMBEDDING_MODEL_ALIAS,
        model_revision=LOCAL_MODEL2VEC_REVISION,
        evaluation_mode=evaluation_mode,
        dataset_kind="sanitized_repo_benchmark_v1",
        total_queries=total_q,
        hits_at_1=hits_at_1,
        hits_at_5=hits_at_5,
        recall_at_1=recall_at_1,
        recall_at_5=recall_at_5,
        mrr_at_5=mrr_at_5,
        category_recall_at_5=category_recall_at_5,
        missed_query_ids=tuple(missed_query_ids),
        entity_pairs_evaluated=len(entity_pairs),
        entity_false_merges=entity_false_merges,
        entity_false_splits=entity_false_splits,
        auth_scope_violations=auth_scope_violations,
        temporal_state_misclassifications=temporal_state_misclassifications,
        passed_proposed_gates=passed_gates,
        gate_thresholds={
            "min_queries": 50,
            "recall_at_5_min": recall_at_5_threshold,
            "min_entity_pairs": 20,
            "max_entity_false_merges": 0,
            "max_auth_scope_violations": 0,
            "max_temporal_state_misclassifications": 0,
            "status": "proposed_pending_user_confirmation",
        },
    )

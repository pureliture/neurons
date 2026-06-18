"""ledger.py 4-area 책임 경계 manifest (Phase D).

deepdive 아키텍처 리뷰(``docs/architecture/ledger-review-deepdive-20260614.html`` 의
'ledger.py 내부 책임 지도')를 machine-readable SoT로 코드화한다.

audit override(Modular Monolith): 물리 컨테이너 분리는 오버엔지니어링으로 기각되고,
**단일 컨테이너 내부에서 인터페이스/클래스 경계만 엄격히 나누는** 방식이 채택됐다. 따라서
Phase D는 코드 물리 이동이 아니라 *in-process 경계 manifest* 이며, 그 경계는
``worker/eval/ledger_area_boundaries.py`` lint가 강제한다(Phase A seam-invariant lint와
동형). 이 모듈은 런타임 동작을 바꾸지 않는다(순수 선언).

경계 = 4개 책임 영역 + core(schema/connection/migration 인프라). 각 ledger 테이블은
정확히 한 영역에 속한다(전수·배타). 한 영역의 테이블만 만지는 Ledger 메서드는 그 영역에
귀속되고, 두 영역 이상을 가로지르는 메서드는 명시 allowlist(lint의
``FROZEN_CROSS_AREA``)로만 허용돼 신규 경계 위반이 회귀로 잡힌다.
"""

from __future__ import annotations

# 영역 키(코드 식별자) — deepdive Area A~D 에 1:1 대응.
AREA_A = "ingress_status"  # Area A
AREA_B = "gc_safety"  # Area B
AREA_C = "memory_promotion"  # Area C
AREA_D = "native_memory"  # Area D
CORE = "core"  # schema/connection/migration 인프라(cross-area 정당)

AREA_TITLES: dict[str, str] = {
    AREA_A: "Ingress Status Tracking & Queue Management",
    AREA_B: "GC Planning & Auditing (GC Safety Lane)",
    AREA_C: "Session & Project Memory Promotion State Machine",
    AREA_D: "Native Memory & Memory Cards Synchronization",
    CORE: "Schema / connection / migration infrastructure",
}

# 테이블 → 영역 (deepdive '33 Tables Map' + core 인프라 1개 = 34).
AREA_TABLES: dict[str, frozenset[str]] = {
    AREA_A: frozenset({
        "knowledge_items",
        "ingest_attempts",
        "transcript_sessions",
        "transcript_turns",
        "transcript_tool_events",
        "transcript_chunks",
        "transcript_validation_files",
        "provider_source_contracts",
        "backfill_sources",
        "scheduler_runs",
    }),
    AREA_B: frozenset({
        "retrieval_audit",
        "auto_recall_audit",
        "session_memory_terminal_skipped_audit",
        "memory_gc_audit",
    }),
    AREA_C: frozenset({
        "memory_candidates",
        "dirty_session_memory",
        "session_memory_active_snapshots",
        "session_memory_coverage_edges",
        "dirty_project_memory",
        "project_memory_active_snapshots",
    }),
    AREA_D: frozenset({
        "ragflow_datasets",
        "memory_cards",
        "memory_card_evidence",
        "llm_brain_memory_cards",
        "llm_brain_feedback_records",
        "llm_brain_projection_jobs",
        "llm_brain_session_memory_artifacts",
        "llm_brain_source_refs",
        "profile_facts",
        "context_packs",
        "context_pack_items",
        "eval_queries",
        "eval_runs",
        "tool_evidence_summaries",
        "native_memory_mirror",
    }),
    CORE: frozenset({
        "schema_migrations",
    }),
}

# 비-core 영역 순서(보고/시각화용).
AREAS = (AREA_A, AREA_B, AREA_C, AREA_D)


def table_to_area() -> dict[str, str]:
    """table → area 역인덱스. 한 테이블이 두 영역에 중복되면 ValueError(배타성 보증)."""
    index: dict[str, str] = {}
    for area, tables in AREA_TABLES.items():
        for table in tables:
            if table in index:
                raise ValueError(
                    f"table {table!r} 이 {index[table]!r} 와 {area!r} 두 영역에 중복 배정됨"
                )
            index[table] = area
    return index


def all_mapped_tables() -> frozenset[str]:
    return frozenset().union(*AREA_TABLES.values())

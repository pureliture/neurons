"""Recall-time ledger filter for RAGFlow-native memory (Option A, slice1).

이 모듈은 RAGFlow Memory search hits 를 ledger active-set 으로 필터해 **현재 유효한
기억만** 반환한다(superseded / 미등록=미승인 제외). RAGFlow 항목은 건드리지 않는다
(supersede 는 ledger status 만 바꾸는 Option A; 실제 disable 은 후속 Option C reconcile).

서버측 session_id 필터는 동작하지 않으므로(라이브 사실 #5) 항상 클라이언트측 fetch 후
session_tag join 으로 필터한다. join key 는 `session_id == "mem:<statement_id>"`(사실 #3).
content / content_hash 로 추출본을 매칭하지 않는다(패러프레이즈, 사실 #7).

broker seam (follow-up, spec §12.2):
    `filter_active_native_memory` / `recall_active_native_memory` 가 반환하는 dict 는
    `ContextBroker._active_*_items` item shape(`kind` / `currentness` / `policy_reason` /
    `content` / `score`)와 호환된다. broker 깊은 배선(`_active_native_memory_items` 추가,
    `ragflow.search_messages` 바인딩 — broker 의 dataset retrieval `ragflow.retrieve` 와
    다른 호출 —, memory_id 바인딩, over-fetch 루프 broker 통합, MCP 노출)은 **follow-up**
    이다. 이 슬라이스는 broker 코드를 변경하지 않고 호환 seam 만 보장한다.
"""

from __future__ import annotations

from .native_memory_governance import governance_tier
from .native_memory_mirror import NativeMemoryMirrorStore


NATIVE_MEMORY_OVERFETCH_THRESHOLD = 2  # active 노출 수가 이 미만이면 over-fetch 재조회


def filter_active_native_memory(
    hits: list[dict],
    store: NativeMemoryMirrorStore,
    *,
    brain_id: str = "",
) -> list[dict]:
    """search hits(RAGFlow item dict들) → active 기억만, 메타 부착해 반환.

    keep/drop 판정은 session_tag↔active join. 미등록 tag = 미승인 = DROP,
    superseded = DROP. brain_id 가 주어지면 그 brain_id(=`/project/<project>`) 의 active 만
    남긴다(단일 Memory multi-project 혼재 제거, goal.md noisy 제외). 빈 brain_id = 전체
    active(하위호환). 입력 hits 순서 보존. store 예외는 그대로 전파(fail-closed).
    score 는 `hit.get("score")`(없으면 None) — KeyError 금지(사실 #2).

    동일 session_tag 복수 hit(라이브 사실 #3: raw 1건 + 서버측 추출본 semantic/
    procedural/episodic 복수건이 같은 session_id 공유)는 여기서 dedup 하지 않고 전부
    통과시킨다. message_type 기준 선별(예: raw 제외, 추출본 우선)은 broker 책임이다.
    """
    tags = list({hit["session_id"] for hit in hits})
    registered = store.get_by_session_tags(tags)
    kept: list[dict] = []
    for hit in hits:
        session_tag = hit["session_id"]
        row = registered.get(session_tag)
        if row is None or row["status"] != "active":
            continue
        if brain_id and row["brain_id"] != brain_id:
            continue
        kept.append(
            {
                "kind": "native_memory",
                "session_tag": session_tag,
                "brain_id": row["brain_id"],
                "approval_state": "active",
                "policy_reason": "native_memory_active_mirror_match",
                "currentness": "active_native_memory",
                # tier 는 mirror row 의 card_type(miner 어휘)으로만 계산한다.
                # hit.get("message_type")(RAGFlow 어휘: raw/semantic/...)로 계산하지 않는다 — 별개 어휘.
                "tier": governance_tier(row.get("card_type", "")),
                "message_type": hit.get("message_type"),
                "content": hit.get("content"),
                "score": hit.get("score"),
            }
        )
    return kept


def _needs_overfetch(
    filtered: list[dict],
    *,
    threshold: int = NATIVE_MEMORY_OVERFETCH_THRESHOLD,
) -> bool:
    """active 노출 수 < threshold 이면 True → 재조회. orchestration 내부 전용 헬퍼
    (public export 아님). 단독 테스트하지 않고 recall_active_native_memory 로 간접 커버."""
    return len(filtered) < threshold


def _extract_hits(search_result: dict) -> list[dict]:
    """envelope `json.data.chunks` 정상 경로만 추출(probe `_search_hit_count` 선례).
    비정상 shape 는 `[]`. fallback 분기는 비범위.

    라이브 실측(Step1/2): search 정상 응답은 `json.data` 가 **리스트 직접**이다
    (`{"code":0,"data":[...],"message":true}`). 일부 경로는 `json.data.chunks` 형태도 쓰므로
    두 정상 형태를 모두 처리하고, 그 외(비정상 envelope)는 `[]`(fail-closed). reconcile 의
    `_search_items` 와 동일한 해석."""
    data = search_result.get("json") or {}
    if not isinstance(data, dict):
        return []
    inner = data.get("data", {})
    if isinstance(inner, list):
        return inner
    if isinstance(inner, dict):
        chunks = inner.get("chunks", [])
        return chunks if isinstance(chunks, list) else []
    return []


def recall_active_native_memory(
    *,
    ragflow,                       # search_messages 덕타입. 테스트=fake.
    store: NativeMemoryMirrorStore,
    memory_id: str,
    query: str,
    brain_id: str = "",
    base_top_n: int = 10,
    max_top_n: int = 50,
    overfetch_threshold: int = NATIVE_MEMORY_OVERFETCH_THRESHOLD,
) -> list[dict]:
    """search → filter → (포화 시 top_n 확대 1회 재조회) → filter.

    라이브 호출은 주입된 ragflow.search_messages(fake). 서버측 session_id 필터 미사용
    (사실 #5). 재조회는 **최대 1회**, 2차 top_n 은 정확히 `max_top_n`. store 예외는
    그대로 전파(fail-closed; filter_active_native_memory 가 던지는 예외를 흡수하지 않음).
    """
    search_result = ragflow.search_messages(
        query=query, memory_id=memory_id, top_n=base_top_n
    )
    filtered = filter_active_native_memory(_extract_hits(search_result), store, brain_id=brain_id)
    if _needs_overfetch(filtered, threshold=overfetch_threshold) and max_top_n > base_top_n:
        search_result = ragflow.search_messages(
            query=query, memory_id=memory_id, top_n=max_top_n
        )
        filtered = filter_active_native_memory(_extract_hits(search_result), store, brain_id=brain_id)
    return filtered

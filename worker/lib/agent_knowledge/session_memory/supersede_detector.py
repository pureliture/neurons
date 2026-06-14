"""Supersede detector — embedding-cosine candidate recall + LLM judge (decided #2).

Two stages: (1) lenient RAGFlow vector recall over the derived-memory-items mirror pulls
candidate prior cards (recall-oriented; the LLM filters precision), (2) an injected LLM
judge decides supersede/distinct/conflict per (new candidate, prior card). Fail-closed:
only an explicit "supersede" verdict against a still-current ledger card returns a demote
target; distinct/conflict/uncertain return None (no silent auto-supersede; cosine-only and
keyword-only are rejected). similarity_threshold/top_n are policy.v0 defaults, calibrated
against the golden tuning slice.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

# policy.v0 defaults — model-dependent, calibrated against the golden tuning slice.
DEFAULT_SIMILARITY_THRESHOLD = 0.35
DEFAULT_TOP_N = 8


_JUDGE_PROMPT = (
    "You decide whether a NEW memory supersedes an OLD one about the same subject. "
    "Reply with exactly one word: 'supersede' if NEW replaces/updates OLD, 'conflict' if they "
    "contradict without a clear winner, or 'distinct' if they are about different subjects. "
    "Output only the one word."
)


def build_ragflow_judge_fn(ragflow: Any, *, llm_id: str = "") -> Callable[[Mapping[str, Any], Mapping[str, Any]], str]:
    """LLM judge over the RAGFlow chat model: (new, old) -> supersede|conflict|distinct (fail-closed to distinct)."""

    def judge(candidate: Mapping[str, Any], old_card: Mapping[str, Any]) -> str:
        messages = [
            {"role": "system", "content": _JUDGE_PROMPT},
            {"role": "user", "content": f"OLD: {old_card.get('summary') or ''}\nNEW: {candidate.get('summary') or ''}"},
        ]
        try:
            verdict = str(ragflow.chat_completion(messages, llm_id=llm_id) or "").strip().lower()
        except Exception:
            return "distinct"
        if "supersede" in verdict:
            return "supersede"
        if "conflict" in verdict:
            return "conflict"
        return "distinct"

    return judge


def build_supersede_detector(
    *,
    ragflow: Any,
    judge_fn: Callable[[Mapping[str, Any], Mapping[str, Any]], str],
    dataset_id: str,
    project: str,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    top_n: int = DEFAULT_TOP_N,
) -> Callable[[Mapping[str, Any], Any], dict | None]:
    def detect(candidate: Mapping[str, Any], ledger: Any) -> dict | None:
        query = str(candidate.get("summary") or candidate.get("title") or "")
        if not query:
            return None
        try:
            hits = ragflow.retrieve(
                query,
                [dataset_id],
                filters={"project": project} if project else None,
                similarity_threshold=similarity_threshold,
                top_n=top_n,
            )
        except Exception:
            return None
        if not isinstance(hits, list):
            return None
        for hit in hits:
            if not isinstance(hit, Mapping):
                continue
            memory_id = str(hit.get("memory_id") or (hit.get("metadata") or {}).get("memory_id") or "")
            if not memory_id or memory_id == candidate.get("memory_id"):
                continue
            old_card = ledger.get_llm_brain_memory_card(memory_id)
            if not old_card or old_card.get("currentness") != "current":
                continue
            if judge_fn(candidate, old_card) == "supersede":
                return old_card
        return None

    return detect

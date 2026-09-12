"""Fix 4: 공용 terms 모듈 SoT 검증 (docs/specs/lbrain-memory-recall-fix-spec.md)."""

from agent_knowledge.llm_brain_core import terms as terms_mod
from agent_knowledge.llm_brain_core.context import _terms as context_terms
from agent_knowledge.llm_brain_core.graphiti_adapter import _terms as graph_terms


def test_terms_split_korean_and_punctuated_english():
    assert terms_mod.terms("최근 일주일간 개발사항") == ["일주일간", "개발사항"]  # 2글자 '최근'은 cutoff 제외


def test_context_and_graph_adapter_use_shared_tokenizer():
    value = "최근 일주일간 개발사항, hybrid-graph"
    # 이중 구현이 통합됐다면 두 필터가 동일한 토큰 집합을 생성한다.
    assert context_terms(value) == graph_terms(value)


def test_default_min_length_three():
    assert terms_mod.terms("a ab abc") == ["abc"]

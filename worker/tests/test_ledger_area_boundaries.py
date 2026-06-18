"""ledger.py 4-area 경계 lint 테스트 (Phase D).

통과 단언 + 전수·배타 + 합성 위반 검출(falsifiable). seam-invariant 테스트와 동형.
"""

import ledger_area_boundaries as lint
from agent_knowledge import ledger_areas


def test_area_boundaries_pass_on_current_code():
    assert lint.check_area_boundaries() == []


def test_tables_partition_is_total_and_exclusive():
    # 배타: table_to_area 가 중복 없이 역인덱스를 만든다(중복이면 ValueError).
    index = ledger_areas.table_to_area()
    # 전수: ledger.py 가 실제 만드는 테이블 == manifest 매핑 테이블.
    source = lint._ledger_path().read_text(encoding="utf-8")
    created = lint._created_tables(source)
    assert created == set(ledger_areas.all_mapped_tables())
    assert set(index) == created
    # 영역 합집합 == 전체(분할).
    union = set().union(*ledger_areas.AREA_TABLES.values())
    assert union == created


def test_catch_unmapped_table(tmp_path):
    # manifest에 없는 새 테이블이 ledger.py에 생기면 '배정되지 않음' 위반이 나야 한다.
    fake = tmp_path / "ledger.py"
    fake.write_text(
        "class Ledger:\n"
        "    def _initialize(self):\n"
        '        self._x("CREATE TABLE knowledge_items (id TEXT)")\n'
        '        self._x("CREATE TABLE brand_new_widget (id TEXT)")\n',
        encoding="utf-8",
    )
    violations = lint.check_area_boundaries(fake)
    assert any("brand_new_widget" in v and "배정되지 않음" in v for v in violations)


def test_catch_new_cross_area_method(monkeypatch):
    # 실재하는 cross-area 메서드를 allowlist에서 빼면 '새로 가로지름' 위반이 나야 한다.
    reduced = dict(lint.FROZEN_CROSS_AREA)
    reduced.pop("promote_session_memory")
    monkeypatch.setattr(lint, "FROZEN_CROSS_AREA", reduced)
    violations = lint.check_area_boundaries()
    assert any("promote_session_memory" in v and "가로지름" in v for v in violations)


def test_catch_stale_cross_area_allowlist(monkeypatch):
    # cross-area가 아닌(혹은 없는) 메서드를 allowlist에 넣으면 stale 위반이 나야 한다.
    extended = dict(lint.FROZEN_CROSS_AREA)
    extended["no_such_method"] = frozenset({"gc_safety", "native_memory"})
    monkeypatch.setattr(lint, "FROZEN_CROSS_AREA", extended)
    violations = lint.check_area_boundaries()
    assert any("no_such_method" in v and "stale" in v for v in violations)


def test_every_area_has_title():
    for area in (*ledger_areas.AREAS, ledger_areas.CORE):
        assert ledger_areas.AREA_TITLES.get(area)

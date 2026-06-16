"""ledger.py 4-area 책임 경계 lint (Phase D).

``agent_knowledge.ledger_areas`` manifest(테이블 → 4 영역, deepdive 책임 지도의
machine-readable SoT)를 강제한다. audit override(Modular Monolith)대로 코드 물리 이동이
아니라 *in-process 경계*를 lint로 동결한다 — Phase A seam-invariant lint와 동형.

검사:
  1. **전수·배타**: ledger.py 의 모든 ``CREATE TABLE`` 테이블이 manifest 4영역+core 중
     정확히 하나에 배정됐는가(미배정 테이블 = 위반, 신규 테이블이 분류를 강제). 배타성은
     ``ledger_areas.table_to_area()`` 가 보증.
  2. **메서드 귀속**: 각 ``Ledger`` 메서드가 만지는 테이블의 영역 집합을 AST로 산출.
     비-core 영역을 둘 이상 가로지르는 메서드는 ``FROZEN_CROSS_AREA`` allowlist 안에만
     존재해야 한다. 새 cross-area 메서드(=신규 경계 결합)가 생기면 위반으로 잡힌다.
     allowlist에 있으나 더는 cross-area가 아닌 항목은 stale 위반.

product 코드는 이 모듈을 import하지 않는다(eval-only lint).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from agent_knowledge.ledger_areas import (  # noqa: E402
    AREAS,
    AREA_TABLES,
    CORE,
    all_mapped_tables,
    table_to_area,
)

# 두 비-core 영역 이상을 정당하게 가로지르는 Ledger 메서드의 frozen allowlist.
# 값 = 그 메서드가 닿는 비-core 영역 집합(현재 실측, 동결). 신규 cross-area 메서드는
# 여기 없으면 위반 → 4-area 경계를 새로 침범하는 결합이 회귀로 잡힌다.
#
# 현재 9개: ① _initialize = 전 테이블 스키마 부트스트랩(전 영역). ② knowledge_items(A)를
# 허브로 각 영역 테이블과 조인하는 read(get/list memory_card·tool_evidence·project
# candidates). ③ promote_*는 knowledge_items(A) status flip + active snapshot(C). ④
# record_context_pack은 context_pack(D) 기록 시 retrieval_audit(B) 동반.
FROZEN_CROSS_AREA: dict[str, frozenset[str]] = {
    "_initialize": frozenset({"gc_safety", "ingress_status", "memory_promotion", "native_memory"}),
    "get_memory_card": frozenset({"ingress_status", "native_memory"}),
    "get_memory_card_state": frozenset({"ingress_status", "native_memory"}),
    "list_memory_cards_for_eval": frozenset({"ingress_status", "native_memory"}),
    "list_project_memory_indexed_candidates": frozenset({"ingress_status", "native_memory"}),
    "promote_project_memory_snapshot": frozenset({"ingress_status", "memory_promotion"}),
    "promote_session_memory": frozenset({"ingress_status", "memory_promotion"}),
    "record_context_pack": frozenset({"gc_safety", "native_memory"}),
    "upsert_tool_evidence_summary": frozenset({"ingress_status", "native_memory"}),
}


def _ledger_path(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        cand = parent / "lib" / "agent_knowledge" / "ledger.py"
        if cand.is_file():
            return cand
    raise RuntimeError("ledger.py not found")


def _created_tables(source: str) -> set[str]:
    return set(
        re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_]+)", source, re.IGNORECASE)
    )


def _ledger_class(tree: ast.AST) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Ledger":
            return node
    raise RuntimeError("class Ledger not found")


def _method_tables(source: str, method: ast.AST, tables: frozenset[str]) -> set[str]:
    seg = ast.get_source_segment(source, method) or ""
    hit: set[str] = set()
    for table in tables:
        if re.search(rf"\b{re.escape(table)}\b", seg):
            hit.add(table)
    return hit


def classify_methods(source: str, tree: ast.AST) -> dict[str, dict]:
    """method 이름 → {tables, areas(비-core), kind}. kind ∈ core|<area>|cross."""
    t2a = table_to_area()
    all_tables = all_mapped_tables()
    cls = _ledger_class(tree)
    out: dict[str, dict] = {}
    for item in cls.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        hit = _method_tables(source, item, all_tables)
        non_core = {t2a[t] for t in hit if t2a[t] != CORE}
        if not non_core:
            kind = CORE
        elif len(non_core) == 1:
            kind = next(iter(non_core))
        else:
            kind = "cross"
        out[item.name] = {"tables": hit, "areas": non_core, "kind": kind}
    return out


def check_area_boundaries(ledger_path: Path | None = None) -> list[str]:
    """위반 목록을 반환한다(빈 리스트 = 통과)."""
    path = ledger_path or _ledger_path()
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations: list[str] = []

    # 1) 배타성(manifest 자체) + 전수(ledger.py 의 실제 테이블).
    try:
        table_to_area()
    except ValueError as exc:
        violations.append(f"manifest overlap: {exc}")
    created = _created_tables(source)
    mapped = all_mapped_tables()
    for table in sorted(created - mapped):
        violations.append(f"table {table!r} 가 어느 영역에도 배정되지 않음(manifest 갱신 필요)")
    for table in sorted(mapped - created):
        violations.append(f"table {table!r} 가 manifest엔 있으나 ledger.py에 없음(stale)")

    # 2) cross-area 메서드 == FROZEN_CROSS_AREA.
    classified = classify_methods(source, tree)
    cross_now = {name: info["areas"] for name, info in classified.items() if info["kind"] == "cross"}
    for name, areas in sorted(cross_now.items()):
        if name not in FROZEN_CROSS_AREA:
            violations.append(
                f"메서드 {name!r} 이 영역 {sorted(areas)} 를 새로 가로지름(경계 위반, allowlist 미등록)"
            )
        elif set(FROZEN_CROSS_AREA[name]) != set(areas):
            violations.append(
                f"메서드 {name!r} cross-area 영역 변동: frozen={sorted(FROZEN_CROSS_AREA[name])} now={sorted(areas)}"
            )
    for name in sorted(set(FROZEN_CROSS_AREA) - set(cross_now)):
        violations.append(f"메서드 {name!r} 가 allowlist엔 있으나 더는 cross-area 아님(stale)")
    return violations


def _report(ledger_path: Path | None = None) -> None:
    path = ledger_path or _ledger_path()
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    classified = classify_methods(source, tree)
    from collections import Counter

    counts = Counter(info["kind"] for info in classified.values())
    print("=== 영역별 테이블 수 ===")
    for area in (*AREAS, CORE):
        print(f"  {area}: {len(AREA_TABLES[area])}")
    print("=== 메서드 분류 카운트 ===")
    for kind, n in counts.most_common():
        print(f"  {kind}: {n}")
    print("=== cross-area 메서드(allowlist 후보) ===")
    for name, info in sorted(classified.items()):
        if info["kind"] == "cross":
            print(f'    "{name}": frozenset({{{", ".join(repr(a) for a in sorted(info["areas"]))}}}),')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger-area-boundaries")
    parser.add_argument("--report", action="store_true", help="분류 결과 출력(allowlist 시드용)")
    args = parser.parse_args(argv)
    if args.report:
        _report()
        return 0
    violations = check_area_boundaries()
    if violations:
        print("AREA BOUNDARY VIOLATIONS:")
        for v in violations:
            print("  -", v)
        return 1
    print("area boundaries OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

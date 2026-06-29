"""GC Safety Lane seam structural invariants (Phase A lint).

비가역/forbidden RetiredIndexBridge mutation(delete_documents / disable_document /
disable_message / delete_memory)의 직접 호출 사이트를 AST로 열거해 **frozen allowlist**와
대조한다. 새 직접 호출 사이트가 생기면(= seam을 우회한 비가역 op) 위반으로 잡는다.
또한 3개 GC runner가 ``index_client`` 주입 seam을 노출하는지 확인한다.

Phase A 범위 = allowlist 동결 + 주입 seam 존재. Phase A2(S8)에서 delete 사이트가 seam
경유로만 도달하도록 allowlist를 shrink한다. 이 모듈은 product 코드가 import하지 않는다
(eval-only lint).
"""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN_RETIRED_INDEX_BRIDGE_MUTATIONS = frozenset(
    {"delete_documents", "disable_document", "disable_message", "delete_memory"}
)

# 비가역/forbidden RetiredIndexBridge mutation 직접 호출 사이트의 frozen allowlist.
# 키 = agent_knowledge 기준 상대 경로, 값 = 그 파일에서 호출되는 메서드 집합.
# index_client.py(정의)는 스캔에서 제외한다.
#
# A2 shrink: GC 3 스크립트의 ``delete_documents`` 직접 호출이 GC Safety Lane seam
# (``gc_safety_auditor.hard_delete_documents``) 경유로 라우팅됐다. 이제 비가역 RetiredIndexBridge
# 삭제의 직접 호출은 **seam 모듈 한 곳**에만 존재한다. GC 스크립트에 delete_documents가
# 다시 나타나면 allowlist 밖 위반으로 잡힌다(seam 우회 금지). sync_roundtrip/
# native_memory_reconcile의 disable_*는 Phase A 밖(envelope 안)이라 allowlist 유지.
FROZEN_DELETE_ALLOWLIST: dict[str, frozenset[str]] = {
    "session_memory/gc_safety_auditor.py": frozenset({"delete_documents"}),
    "session_memory/sync_roundtrip.py": frozenset({"disable_document"}),
    "session_memory/native_memory_reconcile.py": frozenset({"disable_message"}),
}

# 주입 seam을 노출해야 하는 GC runner: 상대 경로 -> (클래스명, 필수 파라미터).
REQUIRED_INJECTION_SEAMS: dict[str, tuple[str, str]] = {
    "session_memory/session_memory_gc.py": ("SessionMemoryGcRunner", "index_client"),
    "session_memory/transcript_volume_gc.py": ("TranscriptVolumeGcRunner", "index_client"),
    "session_memory/transcript_session_gc.py": ("TranscriptSessionGcRunner", "index_client"),
}


def _agent_knowledge_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        cand = parent / "lib" / "agent_knowledge"
        if cand.is_dir():
            return cand
    raise RuntimeError("agent_knowledge root not found")


def _scan_mutation_sites(root: Path) -> dict[str, set[str]]:
    sites: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if path.name == "index_client.py":
            continue  # 정의이지 호출 사이트가 아님
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in FORBIDDEN_RETIRED_INDEX_BRIDGE_MUTATIONS
            ):
                sites.setdefault(rel, set()).add(node.func.attr)
    return sites


def _check_injection_seams(root: Path) -> list[str]:
    violations: list[str] = []
    for rel, (cls_name, param) in REQUIRED_INJECTION_SEAMS.items():
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        params = [a.arg for a in item.args.args] + [a.arg for a in item.args.kwonlyargs]
                        if param in params:
                            found = True
        if not found:
            violations.append(f"{rel}: {cls_name}.__init__ missing injection seam param '{param}'")
    return violations


def check_seam_invariants(root: Path | None = None) -> list[str]:
    """위반 목록을 반환한다(빈 리스트 = 통과)."""
    root = root or _agent_knowledge_root()
    violations: list[str] = []
    sites = _scan_mutation_sites(root)
    # 1) allowlist 밖의 새 비가역 호출 사이트 금지(= seam 우회 비가역 op).
    for rel, methods in sorted(sites.items()):
        allowed = FROZEN_DELETE_ALLOWLIST.get(rel)
        if allowed is None:
            violations.append(
                f"{rel}: forbidden RetiredIndexBridge mutation {sorted(methods)} outside seam allowlist"
            )
            continue
        extra = methods - set(allowed)
        if extra:
            violations.append(f"{rel}: new forbidden mutation {sorted(extra)} not in allowlist")
    # 2) allowlist에 있으나 코드에서 사라진 사이트(allowlist stale 감지).
    for rel in FROZEN_DELETE_ALLOWLIST:
        if rel not in sites:
            violations.append(f"{rel}: allowlisted delete site missing (allowlist stale?)")
    # 3) GC runner 주입 seam 존재.
    violations.extend(_check_injection_seams(root))
    return violations


def main() -> int:
    violations = check_seam_invariants()
    if violations:
        print("SEAM INVARIANT VIOLATIONS:")
        for v in violations:
            print("  -", v)
        return 1
    print("seam invariants OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

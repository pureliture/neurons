import ledger_seam_invariants as lint


def test_seam_invariants_pass_on_current_code():
    assert lint.check_seam_invariants() == []


def test_seam_invariants_catch_unallowlisted_delete_site(monkeypatch):
    # allowlist에서 한 delete 사이트를 빼면 그 사이트가 '밖' 위반으로 잡혀야 한다(falsifiable).
    reduced = dict(lint.FROZEN_DELETE_ALLOWLIST)
    reduced.pop("session_memory/session_memory_gc.py")
    monkeypatch.setattr(lint, "FROZEN_DELETE_ALLOWLIST", reduced)
    violations = lint.check_seam_invariants()
    assert any(
        "session_memory_gc.py" in v and "outside seam allowlist" in v for v in violations
    )


def test_seam_invariants_catch_missing_injection_seam(monkeypatch):
    # 주입 seam 요구 대상을 존재하지 않는 클래스로 바꾸면 missing 위반이 나야 한다.
    monkeypatch.setattr(
        lint,
        "REQUIRED_INJECTION_SEAMS",
        {"session_memory/session_memory_gc.py": ("NoSuchRunner", "ragflow_client")},
    )
    violations = lint.check_seam_invariants()
    assert any("missing injection seam" in v for v in violations)


def test_seam_invariants_catch_stale_allowlist(monkeypatch):
    # 코드에 없는 사이트를 allowlist에 넣으면 stale 위반이 나야 한다.
    extended = dict(lint.FROZEN_DELETE_ALLOWLIST)
    extended["session_memory/does_not_exist.py"] = frozenset({"delete_documents"})
    monkeypatch.setattr(lint, "FROZEN_DELETE_ALLOWLIST", extended)
    violations = lint.check_seam_invariants()
    assert any("does_not_exist.py" in v and "stale" in v for v in violations)

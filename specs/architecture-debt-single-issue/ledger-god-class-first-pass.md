# Ledger God-Class First Pass

## 상태

- issue: #40
- status: first-pass done
- scope: Ledger area-boundary guard hardening and one private cross-area accessor seam
- live runtime mutation: 없음

## 확인한 결합

1. `Ledger` is still composed through multiple mixins, so the public object remains a broad facade.
2. Existing `ledger_area_boundaries.py` guarded table ownership, cross-area SQL/table access, and mixin stray table access.
3. Two blind spots remained:
   - expected area mixin files could disappear and `_check_mixins` would silently skip them
   - `class Ledger(...)` could drop an expected mixin base while mixin files remained on disk
4. `IngressStatusMixin` also called memory-promotion methods directly through inherited `self`, so a cross-area method call could bypass the table-based lint.

## 적용한 guard

- `ledger_area_boundaries.py` now fails closed when an expected area mixin file is missing.
- `ledger_area_boundaries.py` now verifies that `Ledger` still includes the expected mixin bases.
- `ledger_area_boundaries.py` now blocks direct inherited calls from `ledger_ingress_mixin` into memory-promotion dirty-marking methods.
- `Ledger._memory_promotion_area` provides a private seam for the current behavior-preserving delegation.
- `IngressStatusMixin` uses that private seam when `mark_indexed()` needs to mark session/project memory dirty after a conversation chunk is indexed.

## 검증

- `cd worker && uv run pytest -q tests/test_ledger_area_boundaries.py::test_check_area_boundaries_fails_when_expected_mixin_file_missing`
  - RED: missing mixin file was not reported before the lint change
  - GREEN: missing mixin file is reported
- `cd worker && uv run pytest -q tests/test_ledger_area_boundaries.py::test_check_area_boundaries_fails_when_expected_ledger_base_missing`
  - RED: missing `Ledger` mixin base was not reported before the lint change
  - GREEN: missing base is reported
- `cd worker && uv run python eval/ledger_area_boundaries.py`
  - RED after adding direct-call guard: ingress mixin direct calls were reported
  - GREEN after routing calls through `_memory_promotion_area`
- `cd worker && uv run pytest -q tests/test_ledger_area_boundaries.py tests/test_ledger_core.py tests/test_ledger_transaction.py`
  - 통과
- `cd worker && PYTHONDONTWRITEBYTECODE=1 uv run python -B eval/ledger_area_boundaries.py`
  - 통과
- `cd worker && PYTHONDONTWRITEBYTECODE=1 uv run python -B eval/ledger_seam_invariants.py`
  - 통과

## 남은 리스크

- Multiple inheritance remains in place. This pass does not remove `IngressStatusMixin`, `GcSafetyMixin`, `MemoryPromotionMixin`, or `NativeMemoryMixin` from `Ledger`.
- `_memory_promotion_area` is currently a thin private delegation seam returning `self`; future slices can move implementation behind a dedicated area object.
- Table-string lint still cannot fully model row shape, transaction semantics, or dynamic SQL construction.

"""M6 dual-write activation (env-gated, default-off). No live route / no network."""

from __future__ import annotations

from agent_knowledge.rag_ingress.qdrant_dual_write import (
    MirrorDualWriteBackend,
    build_qdrant_mirror_from_env,
    maybe_wrap_dual_write,
)


class _Primary:
    def submit_document(self, document, *, on_step_complete=None):  # pragma: no cover - not called here
        raise AssertionError("not used")


def test_dual_write_is_off_by_default():
    primary = _Primary()
    # no MIRROR_DUAL_WRITE -> primary returned unchanged
    assert maybe_wrap_dual_write(primary, environ={}) is primary
    # flag present but not "1"
    assert maybe_wrap_dual_write(primary, environ={"MIRROR_DUAL_WRITE": "0"}) is primary


def test_dual_write_on_with_builder_wraps_primary():
    primary = _Primary()
    sentinel_mirror = object()
    wrapped = maybe_wrap_dual_write(
        primary, environ={"MIRROR_DUAL_WRITE": "1"}, mirror_builder=lambda _env: sentinel_mirror
    )
    assert isinstance(wrapped, MirrorDualWriteBackend)
    assert wrapped._primary is primary and wrapped._mirror is sentinel_mirror


def test_dual_write_on_but_no_mirror_configured_fails_safe_to_primary():
    primary = _Primary()
    # flag on, builder returns None (e.g. no QDRANT_URL) -> primary only, no crash
    wrapped = maybe_wrap_dual_write(
        primary, environ={"MIRROR_DUAL_WRITE": "1"}, mirror_builder=lambda _env: None
    )
    assert wrapped is primary


def test_none_primary_stays_none():
    assert maybe_wrap_dual_write(None, environ={"MIRROR_DUAL_WRITE": "1"}) is None


def test_build_qdrant_mirror_from_env_returns_none_without_url():
    assert build_qdrant_mirror_from_env({}) is None
    assert build_qdrant_mirror_from_env({"QDRANT_URL": ""}) is None
    assert (
        build_qdrant_mirror_from_env({"QDRANT_URL": "https://qdrant.invalid"})
        is None
    )


def test_builder_exception_fails_safe_to_primary_not_crash(capsys):
    primary = _Primary()

    def _boom(_env):
        raise RuntimeError("missing embedding model / qdrant-client absent")

    # flag on + builder RAISES (misconfig) -> must NOT propagate; primary-only.
    wrapped = maybe_wrap_dual_write(primary, environ={"MIRROR_DUAL_WRITE": "1"}, mirror_builder=_boom)
    assert wrapped is primary
    # observability: a build error is logged (redaction-safe: status + class only)
    out = capsys.readouterr().out
    assert "mirror_build_error" in out and "RuntimeError" in out

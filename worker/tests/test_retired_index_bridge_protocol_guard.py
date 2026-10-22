"""Guard: the retired external index bridge must not re-enter the active runtime.

The bridge was retired from the `neurons` runtime. What survives is (a) a
backend-neutral adapter boundary and (b) an explicitly-legacy operator tool
package. This module pins the boundary so a later change cannot quietly put the
retired wire protocol, its credentials or its backend option back into a
runtime, deploy or recall path.

Guardrail source: `CLAUDE.md` — "Retired external index bridge credentials are
not part of active runtime configuration."
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "worker" / "lib" / "agent_knowledge"

# Operator-side tooling that may still drive the retired wire protocol. These
# modules are CLI/GC/archive tools whose only backend was the retired bridge;
# none of them is reachable from a default-enabled runtime path.
LEGACY_CLIENT_ALLOWED_MODULES = frozenset(
    {
        "session_memory/cleanup_readiness.py",
        "session_memory/dirty_session_memory_sync.py",
        "session_memory/gc_backup.py",
        "session_memory/native_memory_reconcile.py",
        "session_memory/native_memory_writer.py",
        "session_memory/neuron_session_memory.py",
        "session_memory/session_memory_gc.py",
        "session_memory/transcript_backfill.py",
        "session_memory/transcript_session_gc.py",
        "session_memory/transcript_volume_gc.py",
    }
)

# Deploy/runtime surface that must not carry the retired bridge credentials.
DEPLOY_SURFACE_FILES = (
    "compose.yaml",
    ".env.example",
    "worker/deploy/session-memory/compose.yaml",
)


def _package_modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_legacy_index_client_is_only_imported_by_operator_tooling() -> None:
    offenders: list[str] = []
    for path in _package_modules():
        rel = path.relative_to(PACKAGE_ROOT).as_posix()
        if rel == "legacy/index_client.py" or rel in LEGACY_CLIENT_ALLOWED_MODULES:
            continue
        text = path.read_text(encoding="utf-8")
        if "legacy.index_client" in text or "legacy import index_client" in text:
            offenders.append(rel)
    assert offenders == [], (
        "retired index bridge transport imported outside the legacy operator "
        f"tooling allowlist: {offenders}"
    )


def test_ingress_runtime_does_not_import_the_legacy_transport() -> None:
    offenders: list[str] = []
    for path in sorted((PACKAGE_ROOT / "rag_ingress").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "legacy.index_client" in text or "legacy import index_client" in text:
            offenders.append(path.relative_to(PACKAGE_ROOT).as_posix())
    assert offenders == [], (
        f"the ingress runtime must stay bridge-free: {offenders}"
    )


def test_active_source_carries_no_retired_bridge_wire_protocol() -> None:
    # The distinctive paths of the retired bridge wire protocol. A bare
    # "/api/v1" prefix is too coarse (transcript evidence scanning matches
    # generic URL shapes), so the endpoint shapes are pinned instead.
    retired_protocol_paths = (
        "/api/v1/datasets",
        "/api/v1/retrieval",
        "/api/v1/memories",
        "/api/v1/messages",
        "/api/v1/chat/completions",
    )
    offenders: list[str] = []
    for path in _package_modules():
        rel = path.relative_to(PACKAGE_ROOT).as_posix()
        if rel.startswith("legacy/"):
            continue
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in retired_protocol_paths):
            offenders.append(rel)
    for path in sorted((REPO_ROOT / "src" / "main").rglob("*.java")):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in retired_protocol_paths):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == [], (
        f"retired bridge wire protocol found in active source: {offenders}"
    )


def test_java_retired_bridge_adapter_package_is_gone() -> None:
    package = (
        REPO_ROOT
        / "src"
        / "main"
        / "java"
        / "com"
        / "local"
        / "ragingressqueue"
        / "adapter"
        / "ext"
        / "retired_index_bridge"
    )
    assert not package.exists(), "retired bridge java adapter package still exists"


def test_deploy_surface_has_no_retired_bridge_credentials() -> None:
    offenders: list[str] = []
    for rel in DEPLOY_SURFACE_FILES:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        if "RETIRED_INDEX_BRIDGE" in text:
            offenders.append(rel)
    assert offenders == [], (
        f"retired external index bridge credentials reappeared in the deploy "
        f"surface: {offenders}"
    )


def test_delivery_backend_selector_only_accepts_couchdb() -> None:
    from agent_knowledge.rag_ingress import shadow_worker, state_cli

    assert shadow_worker._DELIVERY_BACKENDS == frozenset({"couchdb"})
    assert not hasattr(state_cli, "_BACKEND_RETIRED_INDEX_BRIDGE")
    assert state_cli._BACKEND_COUCHDB == "couchdb"

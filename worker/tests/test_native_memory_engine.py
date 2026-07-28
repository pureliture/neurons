from __future__ import annotations

import subprocess
import sys

import pytest

from agent_knowledge.ledger import Ledger
from agent_knowledge.session_memory import NativeMemoryEngine as ExportedNativeMemoryEngine
from agent_knowledge.session_memory.native_memory_engine import NativeMemoryEngine
from agent_knowledge.session_memory.native_memory_write_runner import run_native_memory_sync

NATIVE_MEMORY_ENGINE_MODULE = "agent_knowledge.session_memory.native_memory_engine"
NATIVE_MEMORY_WRITE_RUNNER_MODULE = "agent_knowledge.session_memory.native_memory_write_runner"


def test_importing_session_memory_keeps_native_memory_engine_lazy():
    code = (
        "import sys; import agent_knowledge.session_memory as _package; "
        "loaded = set(sys.modules); "
        f"assert {NATIVE_MEMORY_ENGINE_MODULE!r} not in loaded; "
        f"assert {NATIVE_MEMORY_WRITE_RUNNER_MODULE!r} not in loaded"
    )

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_native_memory_engine_is_available_through_lazy_package_export():
    assert ExportedNativeMemoryEngine is NativeMemoryEngine


def test_sync_session_memory_preserves_legacy_dry_run_report(tmp_path):
    expected = run_native_memory_sync(
        ledger=Ledger(tmp_path / "legacy.sqlite3"),
        retired_index_bridge=None,
        memory_id="mem_main",
        dry_run=True,
    )
    actual = NativeMemoryEngine(
        ledger=Ledger(tmp_path / "engine.sqlite3"),
        retired_index_bridge=None,
        memory_id="mem_main",
    ).sync_session_memory(dry_run=True)

    assert actual == expected


def test_sync_session_memory_forwards_the_explicit_contract(monkeypatch):
    forwarded_kwargs = {}
    delegated_report = {"status": "delegated"}

    def fake_run_native_memory_sync(**kwargs):
        forwarded_kwargs.update(kwargs)
        return delegated_report

    monkeypatch.setattr(
        f"{NATIVE_MEMORY_ENGINE_MODULE}.run_native_memory_sync",
        fake_run_native_memory_sync,
    )
    ledger = object()
    engine = NativeMemoryEngine(
        ledger=ledger,
        retired_index_bridge="bridge",
        memory_id="mem_custom",
        agent_id="agent",
        user_id="user",
        batch_limit=17,
        reconcile_top_n=23,
    )

    actual = engine.sync_session_memory(dry_run=False)

    assert actual is delegated_report
    assert forwarded_kwargs == {
        "ledger": ledger,
        "retired_index_bridge": "bridge",
        "memory_id": "mem_custom",
        "agent_id": "agent",
        "user_id": "user",
        "batch_limit": 17,
        "reconcile_top_n": 23,
        "dry_run": False,
    }


def test_sync_session_memory_requires_explicit_dry_run():
    engine = NativeMemoryEngine(
        ledger=object(),
        retired_index_bridge=None,
        memory_id="mem_main",
    )

    with pytest.raises(TypeError):
        engine.sync_session_memory()

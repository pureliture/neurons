from pathlib import Path


def test_build_once_uses_couchdb_native_builder_with_qdrant_default() -> None:
    script = Path("deploy/session-memory/build-once.sh").read_text(encoding="utf-8")

    assert "couchdb-session-memory-build" in script
    assert "agent_knowledge.session_memory.neuron_session_memory" not in script
    assert "SESSION_MEMORY_PROJECTION_BACKEND" in script
    assert "QDRANT_URL" in script
    assert "neurons_mirror_gemini_3072_v1" in script
    assert "RETIRED_INDEX_BRIDGE_API_KEY" not in script


def test_build_once_writes_matching_live_approval_for_couchdb_builder() -> None:
    script = Path("deploy/session-memory/build-once.sh").read_text(encoding="utf-8")

    assert '"operation": "couchdb_session_memory_build"' in script
    assert '"redaction_required": True' in script
    assert '"command": {"argv": argv}' in script
    assert "state/couchdb-build-approval.json" in script


def test_build_once_enforces_process_timeout() -> None:
    script = Path("deploy/session-memory/build-once.sh").read_text(encoding="utf-8")

    assert "SESSION_MEMORY_BUILD_TIMEOUT_SECONDS" in script
    assert "timeout \"${SESSION_MEMORY_BUILD_TIMEOUT_SECONDS:-300}\"" in script


def test_session_memory_image_installs_qdrant_projection_dependencies() -> None:
    dockerfile = Path("Dockerfile.session-memory").read_text(encoding="utf-8")

    assert "qdrant-client>=1.10" in dockerfile
    assert "openai>=1.0" in dockerfile

from __future__ import annotations

from pathlib import Path
import tomllib

import yaml


def test_pyproject_declares_mcp_client_extra_for_operator_capture() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    extras = pyproject["project"]["optional-dependencies"]

    assert "mcp-client" in extras
    assert "mcp>=1.28.0" in extras["mcp-client"]


def test_llm_brain_tools_image_installs_mcp_client_extra() -> None:
    dockerfile = Path("Dockerfile.tools").read_text(encoding="utf-8")

    assert '".[mcp-client]"' in dockerfile
    assert "from mcp import ClientSession" in dockerfile
    assert "streamablehttp_client" in dockerfile
    assert "post_deploy_mcp_capture" in dockerfile
    assert "qdrant-client>=1.10" in dockerfile
    assert "openai>=1.0" in dockerfile
    assert 'CMD ["sleep", "infinity"]' in dockerfile


def test_base_worker_image_does_not_become_mcp_client_image() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert '".[mcp-client]"' not in dockerfile
    assert '".[mcp-http]"' not in dockerfile


def test_compose_llm_brain_tools_uses_tools_dockerfile() -> None:
    compose = yaml.safe_load(Path("../compose.yaml").read_text(encoding="utf-8"))

    build = compose["services"]["llm-brain-tools"]["build"]

    assert build["context"] == "./worker"
    assert build["dockerfile"] == "Dockerfile.tools"

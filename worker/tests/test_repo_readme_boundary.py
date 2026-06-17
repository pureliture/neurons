from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repo_readme_names_neurons_as_server_authority() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "text=neurons" in readme
    assert "text=rag-ingress-queue" not in readme
    assert "`neurons`는 LLM-brain의 server/brain repo" in readme
    assert "Those client responsibilities belong to `dendrite`." in readme


def test_repo_readme_keeps_rag_ingress_as_service_lane_not_repo_identity() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    boundary = readme.split("## Neurons Boundary", 1)[1].split("<!--", 1)[0]
    assert "역사적 `rag-ingress-queue`" in boundary
    assert "ingress service/runtime lane" in boundary
    assert "Mac thin-client가 아니라 server-side authority" in boundary

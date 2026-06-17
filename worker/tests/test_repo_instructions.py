from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_provider_instruction_files_exist_and_preserve_server_boundary() -> None:
    for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "한국어" in text
        assert "server/brain" in text
        assert "dendrite" in text
        assert "RAGFLOW_API_KEY" in text
        assert "GC" in text


def test_agents_assigns_client_surface_to_dendrite() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for client_surface in (
        "provider hook installation on Mac",
        "locator-only local capture spool/outbox",
        "Mac thin shipper ergonomics",
        "`POST 18080` client-side enqueue command surface",
    ):
        assert client_surface in text
    assert "Those client responsibilities belong to `dendrite`." in text


def test_agents_keeps_gc_as_safety_lane() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for phrase in (
        "GC is a safety lane",
        "Dry-run first",
        "coverage proof",
        "backup/rollback evidence",
        "recall regression gate",
    ):
        assert phrase in text

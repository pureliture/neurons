from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_provider_instruction_files_exist_and_preserve_server_boundary() -> None:
    for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "한국어" in text
        assert "server/brain" in text
        assert "dendrite" in text
        assert "active runtime configuration" in text
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


def test_public_private_split_policy_is_documented() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    policy = (ROOT / "docs" / "public-private-separation.md").read_text(
        encoding="utf-8"
    )

    assert "docs/public-private-separation.md" in agents
    assert "docs/public-private-separation.md" in readme
    for phrase in (
        "neurons-ops",
        "raw transcript",
        "real `MemoryCard` ledger",
        "raw `dataset_id`",
        "raw `document_id`",
        ".env.example",
        "sample",
    ):
        assert phrase in policy


# Generic credential SHAPES only -- no real host/alias/secret is encoded here,
# so these guards stay public-safe when the files they scan are published.
_CREDENTIAL_SHAPES = (
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{12,}")),
    ("pem_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("long_hex_secret", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}")),
)


def test_public_private_policy_doc_has_no_credential_shaped_strings() -> None:
    policy = (ROOT / "docs" / "public-private-separation.md").read_text(
        encoding="utf-8"
    )
    hits = {
        name: pat.findall(policy)
        for name, pat in _CREDENTIAL_SHAPES
        if pat.search(policy)
    }
    assert not hits, f"public-safe policy doc has credential-shaped strings: {sorted(hits)}"


def test_public_private_policy_keeps_compose_credential_clause() -> None:
    policy = (ROOT / "docs" / "public-private-separation.md").read_text(
        encoding="utf-8"
    )
    norm = re.sub(r"[`*_]", "", policy).lower()
    assert "compose" in norm
    assert "must not embed real credentials" in norm
    assert "neurons-ops" in norm


def test_root_env_example_is_placeholder_only() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    secret_assign = re.compile(
        r"^[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_?KEY)\s*=\s*(.+)$",
        re.MULTILINE,
    )
    placeholder = re.compile(
        r"^(?:replace-with|changeme|placeholder|your-|example|<|\$\{|dummy|unused)",
        re.IGNORECASE,
    )
    offenders = []
    for match in secret_assign.finditer(env):
        value = match.group(1).split("#", 1)[0].strip()
        if not value:
            continue
        if not placeholder.match(value) or any(
            pat.search(value) for _, pat in _CREDENTIAL_SHAPES
        ):
            offenders.append(match.group(0).strip())
    assert not offenders, f".env.example must hold placeholder secrets only: {offenders}"

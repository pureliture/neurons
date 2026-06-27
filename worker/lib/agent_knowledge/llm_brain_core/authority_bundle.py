from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe, public_safe_text, short_hash
from .models import ContextPack


def build_markdown_authority_bundle(
    pack: ContextPack | Mapping[str, Any],
    *,
    root: str = "context-authority",
) -> dict[str, str]:
    """Render a reviewable Markdown/OKF-style bundle from a ContextPack.

    This is a pure builder: it returns target paths and file bodies but does not
    write to disk. Runtime authority remains in the ContextPack/source stores.
    """

    data = pack.to_dict() if isinstance(pack, ContextPack) else dict(pack)
    authority = data.get("authority") if isinstance(data.get("authority"), Mapping) else {}
    root_path = _clean_root(root)
    files: dict[str, str] = {
        f"{root_path}/index.md": _index_markdown(data, authority),
    }
    for document in _items(authority.get("documents")):
        path = public_safe_text(str(document.get("path") or ""), max_chars=240)
        if not path:
            continue
        files[f"{root_path}/documents/{short_hash(path, length=12)}.md"] = _card_markdown(
            "Document Authority",
            {
                "path": path,
                "status": document.get("status") or "unknown",
                "reason": document.get("reason") or "",
                "confidence": document.get("confidence") or 0,
                "generated_artifact": _is_generated_document(document),
                "archive_proposal_only": bool(document.get("archive_proposal_only", True)),
                "evidence_refs": list(document.get("evidence_refs") or []),
            },
            body=str(document.get("reason") or document.get("status") or ""),
        )
    for contract in _items(authority.get("workflow_contracts")):
        rule = public_safe_text(str(contract.get("rule") or ""), max_chars=360)
        if not rule:
            continue
        name = _slug(str(contract.get("memory_id") or rule), fallback=short_hash(rule, length=12))
        files[f"{root_path}/workflows/{name}.md"] = _card_markdown(
            "Workflow Contract",
            {
                "memory_id": contract.get("memory_id") or "",
                "scope": contract.get("scope") or "project",
                "reason": contract.get("reason") or "",
                "confidence": contract.get("confidence") or 0,
                "auto_update_allowed": bool(contract.get("auto_update_allowed", False)),
                "evidence_refs": list(contract.get("evidence_refs") or []),
                "exceptions": list(contract.get("exceptions") or []),
            },
            body=rule,
        )
    for preference in _items(authority.get("preferences")):
        rule = public_safe_text(str(preference.get("rule") or ""), max_chars=360)
        if not rule:
            continue
        name = _slug(str(preference.get("memory_id") or rule), fallback=short_hash(rule, length=12))
        files[f"{root_path}/preferences/{name}.md"] = _card_markdown(
            "Preference Rule",
            {
                "memory_id": preference.get("memory_id") or "",
                "scope": preference.get("scope") or "global",
                "reason": preference.get("reason") or "",
                "confidence": preference.get("confidence") or 0,
                "currentness": preference.get("currentness") or "unknown",
                "evidence_refs": list(preference.get("evidence_refs") or []),
                "exceptions": list(preference.get("exceptions") or []),
            },
            body=rule,
        )
    for gap in _items(authority.get("evidence_gaps")):
        code = public_safe_text(str(gap.get("code") or ""), max_chars=120)
        if not code:
            continue
        files[f"{root_path}/evidence-gaps/{_slug(code, fallback='gap')}.md"] = _card_markdown(
            "Evidence Gap",
            {
                "code": code,
                "severity": gap.get("severity") or "unknown",
                "next_action": gap.get("next_action") or "",
            },
            body=str(gap.get("next_action") or code),
        )
    ensure_public_safe(files, "markdown_authority_bundle")
    return files


def check_markdown_authority_bundle_drift(
    pack: ContextPack | Mapping[str, Any],
    files: Mapping[str, str],
    *,
    root: str = "context-authority",
) -> dict[str, Any]:
    expected = build_markdown_authority_bundle(pack, root=root)
    actual_paths = set(files)
    expected_paths = set(expected)
    missing = sorted(expected_paths - actual_paths)
    extra = sorted(actual_paths - expected_paths)
    changed = sorted(
        path
        for path in expected_paths & actual_paths
        if str(files.get(path) or "") != expected[path]
    )
    result = {
        "status": "drifted" if missing or extra or changed else "in_sync",
        "missing": missing,
        "extra": extra,
        "changed": changed,
    }
    ensure_public_safe(result, "markdown_authority_bundle_drift")
    return result


def _index_markdown(data: Mapping[str, Any], authority: Mapping[str, Any]) -> str:
    lines = [
        "---",
        f"schema_version: {authority.get('schema_version') or 'unknown'}",
        "generated_from: ContextPack",
        f"brain_id: {data.get('brain_id') or ''}",
        f"documents: {len(_items(authority.get('documents')))}",
        f"workflow_contracts: {len(_items(authority.get('workflow_contracts')))}",
        f"preferences: {len(_items(authority.get('preferences')))}",
        f"evidence_gaps: {len(_items(authority.get('evidence_gaps')))}",
        "---",
        "",
        "# Context Authority Bundle",
        "",
        "Reviewable Markdown projection of the current Context Authority Pack.",
        "",
    ]
    return "\n".join(lines)


def _card_markdown(title: str, fields: Mapping[str, Any], *, body: str) -> str:
    frontmatter = ["---"]
    for key, value in fields.items():
        frontmatter.extend(_yaml_lines(key, value))
    frontmatter.extend(["---", "", f"# {title}", ""])
    text = public_safe_text(body, max_chars=2048)
    if text:
        frontmatter.extend([text, ""])
    return "\n".join(frontmatter)


def _yaml_lines(key: str, value: Any) -> list[str]:
    if isinstance(value, bool):
        return [f"{key}: {'true' if value else 'false'}"]
    if isinstance(value, (list, tuple)):
        if not value:
            return [f"{key}: []"]
        lines = [f"{key}:"]
        lines.extend(f"  - {_yaml_scalar(item)}" for item in value)
        return lines
    return [f"{key}: {_yaml_scalar(value)}"]


def _yaml_scalar(value: Any) -> str:
    text = public_safe_text(str(value if value is not None else ""), max_chars=512)
    if text == "":
        return '""'
    return text


def _items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _is_generated_document(document: Mapping[str, Any]) -> bool:
    path = str(document.get("path") or "").casefold()
    status = str(document.get("status") or "").casefold()
    reason = str(document.get("reason") or "").casefold()
    return status == "generated_companion" or path.endswith(".html") or "generated" in reason


def _clean_root(root: str) -> str:
    value = root.strip().strip("/")
    return _slug(value, fallback="context-authority")


def _slug(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return slug[:120] if slug else fallback

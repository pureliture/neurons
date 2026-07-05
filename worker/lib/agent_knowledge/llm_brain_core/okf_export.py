from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._util import ensure_public_safe, public_safe_text


def build_okf_bundle(packs: Mapping[str, Mapping[str, Any]], *, root: str = "okf") -> dict[str, str]:
    root_path = public_safe_text(root.strip("/"), max_chars=80) or "okf"
    objects: list[Mapping[str, Any]] = []
    edges: list[Mapping[str, Any]] = []
    evidence: list[Mapping[str, Any]] = []
    files: dict[str, str] = {}
    for name, pack in sorted(packs.items()):
        pack_name = public_safe_text(str(name), max_chars=120)
        objects.extend(_items(pack.get("objects")))
        edges.extend(_items(pack.get("edges")))
        evidence.extend(_items(pack.get("evidence")))
        files[f"{root_path}/packs/{pack_name}.md"] = _pack_markdown(pack_name, pack)
    files[f"{root_path}/manifest.yml"] = "\n".join(
        [
            "schema_version: okf_review_bundle.v1",
            "role: export_only_review_companion",
            f"pack_count: {len(packs)}",
            f"object_count: {len(objects)}",
            f"edge_count: {len(edges)}",
            f"evidence_count: {len(evidence)}",
            "",
        ]
    )
    files[f"{root_path}/objects.yml"] = _yaml_items("objects", objects)
    files[f"{root_path}/edges.yml"] = _yaml_items("edges", edges)
    files[f"{root_path}/evidence.yml"] = _yaml_items("evidence", evidence)
    ensure_public_safe(files, "OKFBundle")
    return dict(sorted(files.items()))


def _pack_markdown(name: str, pack: Mapping[str, Any]) -> str:
    lanes = pack.get("lanes") if isinstance(pack.get("lanes"), Mapping) else {}
    lines = [
        "---",
        "schema_version: okf_pack_view.v1",
        f"pack: {_quote(name)}",
        f"route: {_quote(pack.get('route') or name)}",
        "---",
        "",
        f"# {name}",
        "",
    ]
    for lane, items in lanes.items():
        lines.append(f"## {lane}")
        lines.append("")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, Mapping):
                continue
            lines.append(f"- `{item.get('object_id')}` {item.get('title') or item.get('summary') or ''}")
        lines.append("")
    return "\n".join(lines)


def _yaml_items(root: str, items: list[Mapping[str, Any]]) -> str:
    lines = [f"{root}:"]
    if not items:
        lines.append("  []")
        return "\n".join(lines) + "\n"
    for item in items:
        lines.append("  -")
        for key in ("object_id", "object_type", "edge_id", "edge_type", "evidence_id", "evidence_type", "authority_lane", "verification_state", "title"):
            if key in item:
                lines.append(f"    {key}: {_quote(item.get(key))}")
    return "\n".join(lines) + "\n"


def _items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _quote(value: Any) -> str:
    text = public_safe_text(str(value if value is not None else ""), max_chars=512)
    if re.fullmatch(r"[A-Za-z0-9_./:-]+", text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

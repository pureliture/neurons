from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


RUNTIME_BUILD_IDENTITY_SCHEMA = "brain_runtime_build_identity.v1"
DEFAULT_RUNTIME_BUILD_IDENTITY_PATH = Path("/app/build-identity.json")

_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PACKAGED_KEYS = frozenset(
    {"schema_version", "source_commit", "build_content_manifest_hash"}
)
_PROJECTION_FALSE_FIELDS = (
    "production_mutation_performed",
    "protected_values_returned",
    "raw_private_evidence_returned",
    "secret_returned",
    "host_topology_returned",
    "raw_external_ids_returned",
)
_PROJECTION_FIELD_ORDER = (
    "schema_version",
    "source_commit",
    "build_content_manifest_hash",
    *_PROJECTION_FALSE_FIELDS,
)
_PROJECTION_KEYS = frozenset(_PROJECTION_FIELD_ORDER)


class RuntimeBuildIdentityError(ValueError):
    pass


def write_runtime_build_identity(
    *,
    source_commit: str,
    content_root: str | Path,
    output_path: str | Path,
) -> dict[str, str]:
    if _SOURCE_COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeBuildIdentityError("runtime build source commit is invalid")
    root = Path(content_root)
    candidates = [root / "pyproject.toml"]
    lib_root = root / "lib"
    if lib_root.is_dir():
        candidates.extend(
            sorted(
                path
                for path in lib_root.rglob("*")
                if path.is_file() and not _is_generated_build_cache(path, root=root)
            )
        )
    files = []
    for path in candidates:
        if not path.is_file():
            continue
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not files:
        raise RuntimeBuildIdentityError("runtime build content manifest is empty")
    canonical_manifest = json.dumps(
        {"files": files},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    identity = {
        "schema_version": RUNTIME_BUILD_IDENTITY_SCHEMA,
        "source_commit": source_commit,
        "build_content_manifest_hash": "sha256:"
        + hashlib.sha256(canonical_manifest).hexdigest(),
    }
    Path(output_path).write_text(
        json.dumps(identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return identity


def _is_generated_build_cache(path: Path, *, root: Path) -> bool:
    relative_parts = path.relative_to(root).parts
    return (
        path.suffix in {".pyc", ".pyo"}
        or "__pycache__" in relative_parts
        or any(part.endswith(".egg-info") for part in relative_parts)
    )


def load_runtime_build_identity(
    path: str | Path = DEFAULT_RUNTIME_BUILD_IDENTITY_PATH,
) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeBuildIdentityError("packaged runtime build identity is unavailable") from exc
    if not isinstance(value, dict) or set(value) != _PACKAGED_KEYS:
        raise RuntimeBuildIdentityError("packaged runtime build identity is malformed")
    if value.get("schema_version") != RUNTIME_BUILD_IDENTITY_SCHEMA:
        raise RuntimeBuildIdentityError("packaged runtime build identity schema is invalid")
    source_commit = value.get("source_commit")
    if not isinstance(source_commit, str) or _SOURCE_COMMIT_RE.fullmatch(source_commit) is None:
        raise RuntimeBuildIdentityError("packaged runtime build identity commit is invalid")
    manifest_hash = value.get("build_content_manifest_hash")
    if not isinstance(manifest_hash, str) or _SHA256_RE.fullmatch(manifest_hash) is None:
        raise RuntimeBuildIdentityError("packaged runtime build identity hash is invalid")
    return validate_runtime_build_identity_projection(
        {
            "schema_version": RUNTIME_BUILD_IDENTITY_SCHEMA,
            "source_commit": source_commit,
            "build_content_manifest_hash": manifest_hash,
            "production_mutation_performed": False,
            "protected_values_returned": False,
            "raw_private_evidence_returned": False,
            "secret_returned": False,
            "host_topology_returned": False,
            "raw_external_ids_returned": False,
        }
    )


def validate_runtime_build_identity_projection(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != _PROJECTION_KEYS:
        raise RuntimeBuildIdentityError("runtime build identity projection is malformed")
    if value.get("schema_version") != RUNTIME_BUILD_IDENTITY_SCHEMA:
        raise RuntimeBuildIdentityError("runtime build identity projection is malformed")
    source_commit = value.get("source_commit")
    manifest_hash = value.get("build_content_manifest_hash")
    if (
        not isinstance(source_commit, str)
        or _SOURCE_COMMIT_RE.fullmatch(source_commit) is None
        or not isinstance(manifest_hash, str)
        or _SHA256_RE.fullmatch(manifest_hash) is None
        or any(value.get(field) is not False for field in _PROJECTION_FALSE_FIELDS)
    ):
        raise RuntimeBuildIdentityError("runtime build identity projection is malformed")
    return {key: value[key] for key in _PROJECTION_FIELD_ORDER}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="runtime-build-identity")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--content-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    write_runtime_build_identity(
        source_commit=args.source_commit,
        content_root=args.content_root,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

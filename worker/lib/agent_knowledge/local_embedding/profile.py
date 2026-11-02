"""Immutable profile descriptor and model integrity verification for local Model2Vec."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

LOCAL_MODEL2VEC_PROFILE_ID = "lbrain-memory-model2vec-256-v1"
LOCAL_MODEL2VEC_MODEL_ALIAS = "local-model2vec-256-v1"
LOCAL_MODEL2VEC_REPO_ID = "minishlab/potion-multilingual-128M"
LOCAL_MODEL2VEC_REVISION = "73908c3438cf03b6a01bcb9611d62b23d0726f08"
LOCAL_MODEL2VEC_LIBRARY_NAME = "model2vec"
LOCAL_MODEL2VEC_LIBRARY_VERSION = "0.10.0"
LOCAL_MODEL2VEC_DIMENSIONS = 256
LOCAL_EMBEDDING_PROFILE_ID = LOCAL_MODEL2VEC_PROFILE_ID
LOCAL_EMBEDDING_MODEL_ALIAS = LOCAL_MODEL2VEC_MODEL_ALIAS
LOCAL_EMBEDDING_MODEL_REVISION = LOCAL_MODEL2VEC_REVISION
LOCAL_EMBEDDING_DIM = LOCAL_MODEL2VEC_DIMENSIONS
LOCAL_MODEL2VEC_DISTANCE = "cosine"
LOCAL_MODEL2VEC_SQL_TYPE = "vector(256)"
LOCAL_MODEL2VEC_SQL_OPERATOR = "vector_cosine_ops"
LOCAL_MODEL2VEC_INPUT_ALGORITHM = "canonical_embedding_v1"
LOCAL_MODEL2VEC_CARD_INPUT_ALGORITHM = "card_input_v1"
LOCAL_MODEL2VEC_SESSION_INPUT_ALGORITHM = "session_content_markdown_v1"

LOCAL_MODEL2VEC_MAX_CHUNK_BYTES = 8192
LOCAL_MODEL2VEC_MAX_BATCH_ITEMS = 16
LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES = 131072
LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES = 163840
LOCAL_MODEL2VEC_DEFAULT_CONCURRENCY = 1
LOCAL_MODEL2VEC_DEFAULT_MAX_QUEUE = 8
LOCAL_MODEL2VEC_DEFAULT_QUEUE_TIMEOUT_SECONDS = 10.0
LOCAL_MODEL2VEC_DEFAULT_BIND_HOST = "127.0.0.1"
LOCAL_MODEL2VEC_DEFAULT_PORT = 18120
LOCAL_MODEL2VEC_MODEL_DIR_ENV = "NEURON_LOCAL_EMBEDDING_MODEL_DIR"

LOCAL_MODEL2VEC_FILE_SHA256: Mapping[str, str] = {
    "model.safetensors": "14b5eb39cb4ce5666da8ad1f3dc6be4346e9b2d601c073302fa0a31bf7943397",
    "tokenizer.json": "19f1909063da3cfe3bd83a782381f040dccea475f4816de11116444a73e1b6a1",
    "config.json": "595e4cab2093732efd5dbe084fd5c1826b5eea693b73b4c1fd971672867d2e54",
}

LEGACY_GEMINI_PROFILE_V1 = "lbrain-memory-gemini-embedding-2-v1"
LEGACY_GEMINI_PROFILE_V2_BOUNDED8K = "lbrain-memory-gemini-embedding-2-v2-bounded8k"


class LocalEmbeddingModelIntegrityError(RuntimeError):
    """Raised when local Model2Vec files or library version fail integrity checks.

    Messages and error codes are sanitized so private filesystem paths are never leaked.
    """

    def __init__(self, reason_code: str, message: str = "local embedding model unavailable") -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.public_message = message


@dataclass(frozen=True)
class LocalEmbeddingProfileDescriptor:
    """Immutable descriptor binding every parameter of the local Model2Vec profile."""

    profile_id: str = LOCAL_MODEL2VEC_PROFILE_ID
    model_alias: str = LOCAL_MODEL2VEC_MODEL_ALIAS
    model_repository: str = LOCAL_MODEL2VEC_REPO_ID
    model_revision: str = LOCAL_MODEL2VEC_REVISION
    library_name: str = LOCAL_MODEL2VEC_LIBRARY_NAME
    library_version: str = LOCAL_MODEL2VEC_LIBRARY_VERSION
    dimensions: int = LOCAL_MODEL2VEC_DIMENSIONS
    distance: str = LOCAL_MODEL2VEC_DISTANCE
    sql_type: str = LOCAL_MODEL2VEC_SQL_TYPE
    sql_operator: str = LOCAL_MODEL2VEC_SQL_OPERATOR
    normalization: str = "l2_per_chunk_and_byte_weighted_l2"
    encoding: str = "ieee754_float32_le_or_json_float"
    long_input_version: str = "utf8_8192b_prefix_byte_weighted_l2_v1"
    input_algorithm: str = LOCAL_MODEL2VEC_INPUT_ALGORITHM
    input_role: str = "symmetric_passage_and_query"
    file_sha256: Mapping[str, str] = field(
        default_factory=lambda: dict(LOCAL_MODEL2VEC_FILE_SHA256)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "model_alias": self.model_alias,
            "model_repository": self.model_repository,
            "model_revision": self.model_revision,
            "library_name": self.library_name,
            "library_version": self.library_version,
            "dimensions": self.dimensions,
            "distance": self.distance,
            "sql_type": self.sql_type,
            "sql_operator": self.sql_operator,
            "normalization": self.normalization,
            "encoding": self.encoding,
            "long_input_version": self.long_input_version,
            "input_algorithm": self.input_algorithm,
            "input_role": self.input_role,
            "file_sha256": dict(sorted(self.file_sha256.items())),
        }

    def descriptor_digest(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()


LOCAL_MODEL2VEC_PROFILE_DESCRIPTOR = LocalEmbeddingProfileDescriptor()


def is_local_model2vec_profile(profile_or_model: str | None) -> bool:
    value = str(profile_or_model or "").strip()
    return value in {LOCAL_MODEL2VEC_PROFILE_ID, LOCAL_MODEL2VEC_MODEL_ALIAS}


def compute_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_model2vec_library_version(
    *,
    expected_version: str = LOCAL_MODEL2VEC_LIBRARY_VERSION,
) -> str:
    try:
        installed = importlib.metadata.version(LOCAL_MODEL2VEC_LIBRARY_NAME)
    except importlib.metadata.PackageNotFoundError as exc:
        raise LocalEmbeddingModelIntegrityError(
            "library_not_installed",
            "local embedding library is not installed",
        ) from exc
    if installed != expected_version:
        raise LocalEmbeddingModelIntegrityError(
            "library_version_mismatch",
            "local embedding library version mismatch",
        )
    return installed


def verify_model_directory(
    model_dir: str | Path | None,
    *,
    expected_hashes: Mapping[str, str] = LOCAL_MODEL2VEC_FILE_SHA256,
    check_library_version: bool = True,
) -> dict[str, Any]:
    """Verify pinned model files and SHA-256 digests without network access."""

    if check_library_version:
        installed_version = verify_model2vec_library_version()
    else:
        installed_version = LOCAL_MODEL2VEC_LIBRARY_VERSION

    if model_dir is None or not str(model_dir).strip():
        raise LocalEmbeddingModelIntegrityError(
            "model_dir_unconfigured",
            "local embedding model directory is not configured",
        )
    root = Path(str(model_dir).strip())
    if not root.exists() or not root.is_dir():
        raise LocalEmbeddingModelIntegrityError(
            "model_dir_missing",
            "local embedding model directory does not exist",
        )

    verified_files: dict[str, str] = {}
    for filename, expected_sha in sorted(expected_hashes.items()):
        candidate = root / filename
        if not candidate.exists() or not candidate.is_file():
            raise LocalEmbeddingModelIntegrityError(
                f"missing_file:{filename}",
                "required local embedding model file is missing",
            )
        actual_sha = compute_file_sha256(candidate)
        if actual_sha.lower() != expected_sha.lower():
            raise LocalEmbeddingModelIntegrityError(
                f"sha256_mismatch:{filename}",
                "local embedding model file integrity check failed",
            )
        verified_files[filename] = actual_sha.lower()

    return {
        "status": "verified",
        "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
        "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
        "repository": LOCAL_MODEL2VEC_REPO_ID,
        "revision": LOCAL_MODEL2VEC_REVISION,
        "library_version": installed_version,
        "dimensions": LOCAL_MODEL2VEC_DIMENSIONS,
        "files": verified_files,
        "descriptor_digest": LOCAL_MODEL2VEC_PROFILE_DESCRIPTOR.descriptor_digest(),
    }


def install_verified_model(
    target_dir: str | Path,
    *,
    source_dir: str | Path | None = None,
    allow_download: bool = False,
    expected_hashes: Mapping[str, str] = LOCAL_MODEL2VEC_FILE_SHA256,
    check_library_version: bool = True,
) -> dict[str, Any]:
    """Install and verify pinned Model2Vec artifacts into ``target_dir``.

    Network download occurs ONLY when ``allow_download=True`` is explicitly passed
    by the operator install CLI; server startup/readiness never calls this function.
    """

    if check_library_version:
        verify_model2vec_library_version()

    if not str(target_dir or "").strip():
        raise LocalEmbeddingModelIntegrityError(
            "invalid_target_dir",
            "target model directory is required",
        )
    dest = Path(str(target_dir).strip())

    resolved_source: Path | None = None
    if source_dir is not None and str(source_dir).strip():
        resolved_source = Path(str(source_dir).strip())
        if not resolved_source.exists() or not resolved_source.is_dir():
            raise LocalEmbeddingModelIntegrityError(
                "source_dir_missing",
                "source model directory does not exist",
            )
    elif allow_download:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise LocalEmbeddingModelIntegrityError(
                "huggingface_hub_unavailable",
                "huggingface_hub is required for explicit model download",
            ) from exc
        downloaded = snapshot_download(
            repo_id=LOCAL_MODEL2VEC_REPO_ID,
            revision=LOCAL_MODEL2VEC_REVISION,
            allow_patterns=list(expected_hashes.keys()),
        )
        resolved_source = Path(downloaded)
    else:
        raise LocalEmbeddingModelIntegrityError(
            "install_source_required",
            "explicit source_dir or allow_download=True is required to install model files",
        )

    # First verify source files before copying into target_dir.
    verify_model_directory(
        resolved_source,
        expected_hashes=expected_hashes,
        check_library_version=check_library_version,
    )

    dest.mkdir(parents=True, exist_ok=True)
    staged_paths: list[Path] = []
    try:
        for filename in sorted(expected_hashes.keys()):
            src_file = resolved_source / filename
            dst_file = dest / filename
            if src_file.resolve() != dst_file.resolve():
                tmp_file = dest / f".{filename}.tmp.{os.getpid()}"
                shutil.copyfile(src_file, tmp_file)
                os.replace(tmp_file, dst_file)
            staged_paths.append(dst_file)

        verification = verify_model_directory(
            dest,
            expected_hashes=expected_hashes,
            check_library_version=check_library_version,
        )
        manifest_path = dest / "INSTALL_MANIFEST.json"
        manifest_payload = {
            "schema_version": "neurons_local_embedding_install_manifest.v1",
            "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
            "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
            "repository": LOCAL_MODEL2VEC_REPO_ID,
            "revision": LOCAL_MODEL2VEC_REVISION,
            "library_version": verification["library_version"],
            "dimensions": LOCAL_MODEL2VEC_DIMENSIONS,
            "files": verification["files"],
            "descriptor_digest": verification["descriptor_digest"],
        }
        manifest_path.write_text(
            json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return verification
    except Exception:
        for path in staged_paths:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        raise

"""Canonical input preparation, UTF-8 chunking, and vector aggregation (`canonical_embedding_v1`)."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .profile import (
    LOCAL_MODEL2VEC_DIMENSIONS,
    LOCAL_MODEL2VEC_MAX_BATCH_ITEMS,
    LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES,
    LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
    LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES,
    LOCAL_MODEL2VEC_MODEL_ALIAS,
    LOCAL_MODEL2VEC_PROFILE_ID,
)


class CanonicalEmbeddingInputError(ValueError):
    """Raised when an input fails `canonical_embedding_v1` or `card_input_v1` validation."""

    def __init__(self, code: str, message: str = "invalid embedding input") -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class CanonicalEmbeddingAggregationError(ValueError):
    """Raised when chunk vectors cannot be aggregated into a valid normalized vector."""

    def __init__(self, code: str, message: str = "invalid embedding vector aggregation") -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True)
class PreparedCanonicalInput:
    """Prepared input chunks and hash for one logical document."""

    input_hash: str
    chunks: tuple[str, ...]
    chunk_byte_lengths: tuple[int, ...]
    total_utf8_bytes: int


def compute_input_hash(text: str) -> str:
    """Compute `sha256:<64hex>` over the exact UTF-8 bytes prior to chunking."""

    if not isinstance(text, str):
        raise CanonicalEmbeddingInputError("invalid_type", "embedding input must be a string")
    if text == "":
        raise CanonicalEmbeddingInputError("empty_input", "embedding input must not be empty")
    try:
        raw_bytes = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CanonicalEmbeddingInputError("invalid_utf8", "embedding input is not valid UTF-8") from exc
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


def split_utf8_chunks_v1(
    text: str,
    *,
    max_chunk_bytes: int = LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
) -> tuple[str, ...]:
    """Split ``text`` into exact UTF-8 prefixes of at most ``max_chunk_bytes`` bytes.

    No character/whitespace normalization, overlap, or truncation is performed.
    Multi-byte UTF-8 codepoint boundaries are strictly preserved.
    """

    if not isinstance(text, str):
        raise CanonicalEmbeddingInputError("invalid_type", "embedding input must be a string")
    if text == "":
        raise CanonicalEmbeddingInputError("empty_input", "embedding input must not be empty")
    if max_chunk_bytes < 4:
        raise CanonicalEmbeddingInputError("invalid_chunk_limit", "max_chunk_bytes must be >= 4")
    try:
        raw = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CanonicalEmbeddingInputError("invalid_utf8", "embedding input is not valid UTF-8") from exc

    total = len(raw)
    chunks: list[str] = []
    offset = 0
    while offset < total:
        end = min(offset + max_chunk_bytes, total)
        if end < total:
            # Back up if `end` lands on a UTF-8 continuation byte (0b10xxxxxx).
            while end > offset and (raw[end] & 0xC0) == 0x80:
                end -= 1
        if end <= offset:
            raise CanonicalEmbeddingInputError(
                "utf8_boundary_error",
                "unable to advance UTF-8 chunk boundary",
            )
        chunk_str = raw[offset:end].decode("utf-8", errors="strict")
        chunks.append(chunk_str)
        offset = end

    return tuple(chunks)


def prepare_canonical_input_v1(
    text: str,
    *,
    max_chunk_bytes: int = LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
) -> PreparedCanonicalInput:
    """Prepare a document for `canonical_embedding_v1`: compute full hash and UTF-8 chunks."""

    input_hash = compute_input_hash(text)
    chunks = split_utf8_chunks_v1(text, max_chunk_bytes=max_chunk_bytes)
    byte_lengths = tuple(len(chunk.encode("utf-8")) for chunk in chunks)
    return PreparedCanonicalInput(
        input_hash=input_hash,
        chunks=chunks,
        chunk_byte_lengths=byte_lengths,
        total_utf8_bytes=sum(byte_lengths),
    )


def validate_finite_nonzero_vector(
    vector: Sequence[float],
    *,
    expected_dim: int = LOCAL_MODEL2VEC_DIMENSIONS,
) -> list[float]:
    if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes, bytearray)):
        raise CanonicalEmbeddingAggregationError("invalid_vector_type", "vector must be a sequence")
    if len(vector) != expected_dim:
        raise CanonicalEmbeddingAggregationError(
            "dimension_mismatch",
            f"expected {expected_dim} dimensions, got {len(vector)}",
        )
    out: list[float] = []
    norm_sq = 0.0
    for item in vector:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise CanonicalEmbeddingAggregationError("non_numeric_component", "vector component is not numeric")
        val = float(item)
        if not math.isfinite(val):
            raise CanonicalEmbeddingAggregationError("non_finite_component", "vector component is not finite")
        out.append(val)
        norm_sq += val * val
    if norm_sq <= 0.0 or not math.isfinite(norm_sq):
        raise CanonicalEmbeddingAggregationError("zero_norm_vector", "vector norm must be positive and finite")
    return out


def aggregate_chunk_vectors_v1(
    chunk_vectors: Sequence[Sequence[float]],
    chunk_byte_lengths: Sequence[int],
    *,
    expected_dim: int = LOCAL_MODEL2VEC_DIMENSIONS,
) -> list[float]:
    """Aggregate per-chunk vectors under `canonical_embedding_v1`.

    - Single chunk: returns the validated 256-dim vector directly.
    - Multiple chunks: computes the UTF-8 byte-length weighted sum and L2-normalizes.
    """

    if not chunk_vectors or len(chunk_vectors) != len(chunk_byte_lengths):
        raise CanonicalEmbeddingAggregationError(
            "chunk_count_mismatch",
            "chunk vectors and byte lengths must be non-empty and equal length",
        )

    validated = [
        validate_finite_nonzero_vector(vec, expected_dim=expected_dim)
        for vec in chunk_vectors
    ]
    if len(validated) == 1:
        if int(chunk_byte_lengths[0]) <= 0:
            raise CanonicalEmbeddingAggregationError("invalid_chunk_weight", "chunk byte length must be positive")
        return validated[0]

    accum = [0.0] * expected_dim
    for vec, weight_int in zip(validated, chunk_byte_lengths):
        weight = float(weight_int)
        if weight <= 0.0 or not math.isfinite(weight):
            raise CanonicalEmbeddingAggregationError("invalid_chunk_weight", "chunk byte length must be positive")
        for idx, component in enumerate(vec):
            accum[idx] += weight * component

    norm_sq = sum(val * val for val in accum)
    if norm_sq <= 0.0 or not math.isfinite(norm_sq):
        raise CanonicalEmbeddingAggregationError(
            "zero_norm_aggregate",
            "aggregated vector norm is zero or non-finite",
        )
    norm = math.sqrt(norm_sq)
    normalized = [val / norm for val in accum]
    return validate_finite_nonzero_vector(normalized, expected_dim=expected_dim)


def serialize_embedding_request_body(
    chunks: Sequence[str],
    *,
    model: str = LOCAL_MODEL2VEC_MODEL_ALIAS,
    encoding_format: str | None = "float",
    dimensions: int | None = LOCAL_MODEL2VEC_DIMENSIONS,
) -> bytes:
    payload: dict[str, Any] = {
        "model": model,
        "input": list(chunks),
    }
    if encoding_format is not None:
        payload["encoding_format"] = encoding_format
    if dimensions is not None:
        payload["dimensions"] = dimensions
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def plan_request_batches_v1(
    chunks: Sequence[str],
    *,
    model: str = LOCAL_MODEL2VEC_MODEL_ALIAS,
    encoding_format: str | None = "float",
    dimensions: int | None = LOCAL_MODEL2VEC_DIMENSIONS,
    max_chunk_bytes: int = LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
    max_batch_items: int = LOCAL_MODEL2VEC_MAX_BATCH_ITEMS,
    max_batch_utf8_bytes: int = LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES,
    max_request_body_bytes: int = LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES,
) -> list[list[str]]:
    """Split ``chunks`` in order into request batches respecting item, UTF-8, and JSON body limits."""

    if not chunks:
        raise CanonicalEmbeddingInputError("empty_chunks", "at least one chunk is required")

    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_utf8_bytes = 0

    for chunk in chunks:
        if not isinstance(chunk, str) or chunk == "":
            raise CanonicalEmbeddingInputError("invalid_chunk", "chunk must be a non-empty string")
        chunk_bytes = len(chunk.encode("utf-8", errors="strict"))
        if chunk_bytes > max_chunk_bytes:
            raise CanonicalEmbeddingInputError(
                "chunk_too_large",
                "single chunk exceeds per-item UTF-8 byte limit",
            )
        single_body = serialize_embedding_request_body(
            [chunk],
            model=model,
            encoding_format=encoding_format,
            dimensions=dimensions,
        )
        if len(single_body) > max_request_body_bytes:
            raise CanonicalEmbeddingInputError(
                "single_chunk_envelope_too_large",
                "single chunk exceeds maximum JSON request body size",
            )

        if not current_batch:
            current_batch = [chunk]
            current_utf8_bytes = chunk_bytes
            continue

        candidate_batch = current_batch + [chunk]
        candidate_utf8_bytes = current_utf8_bytes + chunk_bytes
        if (
            len(candidate_batch) <= max_batch_items
            and candidate_utf8_bytes <= max_batch_utf8_bytes
            and len(
                serialize_embedding_request_body(
                    candidate_batch,
                    model=model,
                    encoding_format=encoding_format,
                    dimensions=dimensions,
                )
            )
            <= max_request_body_bytes
        ):
            current_batch = candidate_batch
            current_utf8_bytes = candidate_utf8_bytes
        else:
            batches.append(current_batch)
            current_batch = [chunk]
            current_utf8_bytes = chunk_bytes

    if current_batch:
        batches.append(current_batch)
    return batches


def card_input_v1(
    *,
    summary: str | None = None,
    title: str | None = None,
    typed_payload: Mapping[str, Any] | None = None,
) -> str:
    """Canonical source text selector for MemoryCards (`card_input_v1` in D2).

    1. Non-empty ``summary`` if present
    2. Otherwise non-empty ``title`` if present
    3. Otherwise canonical JSON of non-empty ``typed_payload``
    """

    if isinstance(summary, str) and summary.strip() != "":
        return summary
    if isinstance(title, str) and title.strip() != "":
        return title
    if isinstance(typed_payload, Mapping) and len(typed_payload) > 0:
        try:
            serialized = json.dumps(
                dict(typed_payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise CanonicalEmbeddingInputError("invalid_typed_payload", "typed_payload is not canonical JSON") from exc
        if serialized and serialized != "{}":
            return serialized
    raise CanonicalEmbeddingInputError("empty_card_input", "card has no valid summary, title, or typed_payload")


def session_input_v1(content_markdown: str | None) -> str:
    """Canonical source text selector for session chunks (D2)."""

    if not isinstance(content_markdown, str) or content_markdown.strip() == "":
        raise CanonicalEmbeddingInputError("empty_session_input", "session content_markdown must be non-empty")
    return content_markdown


def encode_base64_float32_le(
    vector: Sequence[float],
    *,
    expected_dim: int = LOCAL_MODEL2VEC_DIMENSIONS,
) -> str:
    """Encode 256 finite floats as base64 of IEEE754 float32 little-endian bytes."""

    validated = validate_finite_nonzero_vector(vector, expected_dim=expected_dim)
    packed = struct.pack(f"<{expected_dim}f", *validated)
    return base64.b64encode(packed).decode("ascii")


def decode_base64_float32_le(
    encoded: str,
    *,
    expected_dim: int = LOCAL_MODEL2VEC_DIMENSIONS,
) -> list[float]:
    """Decode base64 IEEE754 float32 little-endian bytes into 256 validated finite floats."""

    if not isinstance(encoded, str) or not encoded:
        raise CanonicalEmbeddingAggregationError("invalid_base64", "base64 embedding payload must be a non-empty string")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise CanonicalEmbeddingAggregationError("invalid_base64", "failed to decode base64 embedding payload") from exc
    if len(raw) != expected_dim * 4:
        raise CanonicalEmbeddingAggregationError(
            "invalid_base64_length",
            f"expected {expected_dim * 4} bytes, got {len(raw)}",
        )
    unpacked = struct.unpack(f"<{expected_dim}f", raw)
    return validate_finite_nonzero_vector(unpacked, expected_dim=expected_dim)


def normalize_canonical_text(text: str | None) -> str:
    value = str(text or "")
    return value if value != "" else " "


def split_into_canonical_chunks(
    text: str,
    *,
    max_chunk_bytes: int = LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
) -> list[str]:
    safe_text = normalize_canonical_text(text)
    return list(split_utf8_chunks_v1(safe_text, max_chunk_bytes=max_chunk_bytes))


def aggregate_chunk_embeddings(
    chunk_vectors: Sequence[Sequence[float]],
    chunk_byte_lengths: Sequence[int] | None = None,
    *,
    expected_dim: int = LOCAL_MODEL2VEC_DIMENSIONS,
) -> list[float]:
    weights = (
        list(chunk_byte_lengths)
        if chunk_byte_lengths is not None
        else [1] * len(chunk_vectors)
    )
    return aggregate_chunk_vectors_v1(
        chunk_vectors,
        weights,
        expected_dim=expected_dim,
    )


def _compat_card_input_v1(
    summary: str | None = None,
    body_or_title: str | None = None,
    *,
    title: str | None = None,
    typed_payload: Mapping[str, Any] | None = None,
) -> str:
    resolved_title = title if title is not None else body_or_title
    return card_input_v1(
        summary=summary,
        title=resolved_title,
        typed_payload=typed_payload,
    )


canonical_card_input_v1 = _compat_card_input_v1
canonical_session_input_v1 = session_input_v1
canonical_input_hash_v1 = compute_input_hash
decode_base64_f32_le = decode_base64_float32_le
encode_base64_f32_le = encode_base64_float32_le


__all__ = [
    "CanonicalEmbeddingAggregationError",
    "CanonicalEmbeddingInputError",
    "LOCAL_MODEL2VEC_PROFILE_ID",
    "PreparedCanonicalInput",
    "aggregate_chunk_embeddings",
    "aggregate_chunk_vectors_v1",
    "canonical_card_input_v1",
    "canonical_input_hash_v1",
    "canonical_session_input_v1",
    "card_input_v1",
    "compute_input_hash",
    "decode_base64_f32_le",
    "decode_base64_float32_le",
    "encode_base64_f32_le",
    "encode_base64_float32_le",
    "normalize_canonical_text",
    "plan_request_batches_v1",
    "prepare_canonical_input_v1",
    "serialize_embedding_request_body",
    "session_input_v1",
    "split_into_canonical_chunks",
    "split_utf8_chunks_v1",
    "validate_finite_nonzero_vector",
]

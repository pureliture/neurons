"""Shared client adapter implementing `canonical_embedding_v1` for all Neurons consumers."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .canonical import (
    CanonicalEmbeddingAggregationError,
    CanonicalEmbeddingInputError,
    PreparedCanonicalInput,
    aggregate_chunk_vectors_v1,
    decode_base64_float32_le,
    plan_request_batches_v1,
    prepare_canonical_input_v1,
    serialize_embedding_request_body,
    validate_finite_nonzero_vector,
)
from .profile import (
    LOCAL_MODEL2VEC_DIMENSIONS,
    LOCAL_MODEL2VEC_MAX_BATCH_ITEMS,
    LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES,
    LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
    LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES,
    LOCAL_MODEL2VEC_MODEL_ALIAS,
    LOCAL_MODEL2VEC_PROFILE_ID,
)

DEFAULT_MAX_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 5.0


class LocalEmbeddingClientError(RuntimeError):
    """Sanitized client error with explicit retryability classification."""

    def __init__(
        self,
        code: str,
        message: str = "local embedding request failed",
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class CanonicalEmbeddingResult:
    """Result of embedding one logical document under `canonical_embedding_v1`."""

    vector: list[float]
    input_hash: str
    chunk_count: int
    prompt_tokens: int
    profile_id: str = LOCAL_MODEL2VEC_PROFILE_ID
    model_alias: str = LOCAL_MODEL2VEC_MODEL_ALIAS


def _normalize_embeddings_endpoint(base_url: str) -> str:
    cleaned = str(base_url or "").strip().rstrip("/")
    if not cleaned:
        raise LocalEmbeddingClientError(
            "missing_base_url",
            "local embedding base_url is required",
            retryable=False,
        )
    if cleaned.endswith("/v1/embeddings"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/embeddings"
    return f"{cleaned}/v1/embeddings"


def _normalize_sdk_base_url(base_url: str) -> str:
    cleaned = str(base_url or "").strip().rstrip("/")
    if not cleaned:
        raise LocalEmbeddingClientError(
            "missing_base_url",
            "local embedding base_url is required",
            retryable=False,
        )
    if cleaned.endswith("/v1/embeddings"):
        return cleaned[: -len("/embeddings")]
    if cleaned.endswith("/v1"):
        return cleaned
    return f"{cleaned}/v1"


def _parse_and_verify_batch_payload(
    payload: Any,
    *,
    expected_count: int,
    expected_model: str = LOCAL_MODEL2VEC_MODEL_ALIAS,
    expected_dim: int = LOCAL_MODEL2VEC_DIMENSIONS,
) -> tuple[list[list[float]], int]:
    if not isinstance(payload, dict):
        raise LocalEmbeddingClientError(
            "invalid_response_envelope",
            "embedding response must be a JSON object",
            retryable=False,
        )
    if payload.get("object") != "list":
        raise LocalEmbeddingClientError(
            "invalid_response_object",
            "embedding response object must be 'list'",
            retryable=False,
        )
    if payload.get("model") != expected_model:
        raise LocalEmbeddingClientError(
            "response_model_mismatch",
            "embedding response model mismatch",
            retryable=False,
        )
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise LocalEmbeddingClientError(
            "response_count_mismatch",
            "embedding response item count mismatch",
            retryable=False,
        )

    by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict) or item.get("object") != "embedding":
            raise LocalEmbeddingClientError(
                "invalid_embedding_item",
                "embedding response item is malformed",
                retryable=False,
            )
        idx = item.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int) or not (0 <= idx < expected_count):
            raise LocalEmbeddingClientError(
                "invalid_embedding_index",
                "embedding response item has invalid index",
                retryable=False,
            )
        if idx in by_index:
            raise LocalEmbeddingClientError(
                "duplicate_embedding_index",
                "embedding response contains duplicate index",
                retryable=False,
            )
        raw_emb = item.get("embedding")
        try:
            if isinstance(raw_emb, str):
                vec = decode_base64_float32_le(raw_emb, expected_dim=expected_dim)
            elif isinstance(raw_emb, list):
                vec = validate_finite_nonzero_vector(raw_emb, expected_dim=expected_dim)
            else:
                raise LocalEmbeddingClientError(
                    "invalid_embedding_payload",
                    "embedding payload must be float list or base64 string",
                    retryable=False,
                )
        except CanonicalEmbeddingAggregationError as exc:
            raise LocalEmbeddingClientError(
                exc.code,
                exc.public_message,
                retryable=False,
            ) from exc
        by_index[idx] = vec

    if len(by_index) != expected_count:
        raise LocalEmbeddingClientError(
            "missing_embedding_index",
            "embedding response is missing expected indices",
            retryable=False,
        )

    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise LocalEmbeddingClientError(
            "missing_usage",
            "embedding response is missing usage object",
            retryable=False,
        )
    prompt_tokens = usage.get("prompt_tokens")
    if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        raise LocalEmbeddingClientError(
            "invalid_usage_tokens",
            "embedding response usage.prompt_tokens is invalid",
            retryable=False,
        )

    ordered = [by_index[i] for i in range(expected_count)]
    return ordered, int(prompt_tokens)


class CanonicalLocalEmbeddingClient:
    """Shared client adapter enforcing `canonical_embedding_v1` across all callers."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "local-model2vec",
        model: str = LOCAL_MODEL2VEC_MODEL_ALIAS,
        profile_id: str = LOCAL_MODEL2VEC_PROFILE_ID,
        dimensions: int = LOCAL_MODEL2VEC_DIMENSIONS,
        encoding_format: str | None = "float",
        timeout_seconds: float = 15.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = 0.25,
        use_openai_sdk: bool = False,
        openai_client: Any | None = None,
        async_openai_client: Any | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if model != LOCAL_MODEL2VEC_MODEL_ALIAS:
            raise ValueError(f"unsupported local embedding model alias: {model}")
        if profile_id != LOCAL_MODEL2VEC_PROFILE_ID:
            raise ValueError(f"unsupported local embedding profile_id: {profile_id}")
        if dimensions != LOCAL_MODEL2VEC_DIMENSIONS:
            raise ValueError(f"unsupported local embedding dimensions: {dimensions}")
        if encoding_format not in {None, "float", "base64"}:
            raise ValueError(f"unsupported encoding_format: {encoding_format}")

        self._base_url = str(base_url or "").strip()
        self._endpoint_url = _normalize_embeddings_endpoint(self._base_url)
        self._sdk_base_url = _normalize_sdk_base_url(self._base_url)
        self._api_key = str(api_key or "local-model2vec")
        self._model = model
        self._profile_id = profile_id
        self._dimensions = dimensions
        self._encoding_format = encoding_format
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self._use_openai_sdk = bool(use_openai_sdk)
        self._openai_client = openai_client
        self._async_openai_client = async_openai_client
        self._sleep_fn = sleep_fn

    def close(self) -> None:
        """Release the lazily created sync OpenAI SDK client, if any (idempotent)."""
        client = self._openai_client
        self._openai_client = None
        closer = getattr(client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover - best-effort resource release
                pass

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def model_alias(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_document(self, text: str) -> CanonicalEmbeddingResult:
        results = self.embed_documents([text])
        return results[0]

    def embed_documents(self, texts: Sequence[str]) -> list[CanonicalEmbeddingResult]:
        if not texts:
            return []

        prepared_docs: list[PreparedCanonicalInput] = []
        all_chunks: list[str] = []
        doc_chunk_Spans: list[tuple[int, int]] = []

        for text in texts:
            try:
                prep = prepare_canonical_input_v1(
                    text,
                    max_chunk_bytes=LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
                )
            except CanonicalEmbeddingInputError as exc:
                raise LocalEmbeddingClientError(
                    exc.code,
                    exc.public_message,
                    retryable=False,
                ) from exc
            start = len(all_chunks)
            all_chunks.extend(prep.chunks)
            end = len(all_chunks)
            prepared_docs.append(prep)
            doc_chunk_Spans.append((start, end))

        try:
            batches = plan_request_batches_v1(
                all_chunks,
                model=self._model,
                encoding_format=self._encoding_format,
                dimensions=self._dimensions,
                max_chunk_bytes=LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
                max_batch_items=LOCAL_MODEL2VEC_MAX_BATCH_ITEMS,
                max_batch_utf8_bytes=LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES,
                max_request_body_bytes=LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES,
            )
        except CanonicalEmbeddingInputError as exc:
            raise LocalEmbeddingClientError(
                exc.code,
                exc.public_message,
                retryable=False,
            ) from exc

        flat_vectors: list[list[float]] = []
        batch_token_counts: list[int] = []
        for batch in batches:
            vecs, tokens = self._send_chunk_batch_sync(batch)
            flat_vectors.extend(vecs)
            batch_token_counts.append(tokens)

        if len(flat_vectors) != len(all_chunks):
            raise LocalEmbeddingClientError(
                "total_chunk_vector_mismatch",
                "received vector count does not match total chunks",
                retryable=False,
            )

        total_tokens = sum(batch_token_counts)
        results: list[CanonicalEmbeddingResult] = []
        for prep, (start, end) in zip(prepared_docs, doc_chunk_Spans):
            doc_vecs = flat_vectors[start:end]
            try:
                aggregated = aggregate_chunk_vectors_v1(
                    doc_vecs,
                    prep.chunk_byte_lengths,
                    expected_dim=self._dimensions,
                )
            except CanonicalEmbeddingAggregationError as exc:
                raise LocalEmbeddingClientError(
                    exc.code,
                    exc.public_message,
                    retryable=False,
                ) from exc
            results.append(
                CanonicalEmbeddingResult(
                    vector=aggregated,
                    input_hash=prep.input_hash,
                    chunk_count=len(prep.chunks),
                    prompt_tokens=total_tokens if len(prepared_docs) == 1 else 0,
                    profile_id=self._profile_id,
                    model_alias=self._model,
                )
            )
        return results

    async def aembed_document(self, text: str) -> CanonicalEmbeddingResult:
        results = await self.aembed_documents([text])
        return results[0]

    async def aembed_documents(self, texts: Sequence[str]) -> list[CanonicalEmbeddingResult]:
        if not texts:
            return []

        prepared_docs: list[PreparedCanonicalInput] = []
        all_chunks: list[str] = []
        doc_chunk_spans: list[tuple[int, int]] = []

        for text in texts:
            try:
                prep = prepare_canonical_input_v1(
                    text,
                    max_chunk_bytes=LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
                )
            except CanonicalEmbeddingInputError as exc:
                raise LocalEmbeddingClientError(
                    exc.code,
                    exc.public_message,
                    retryable=False,
                ) from exc
            start = len(all_chunks)
            all_chunks.extend(prep.chunks)
            end = len(all_chunks)
            prepared_docs.append(prep)
            doc_chunk_spans.append((start, end))

        try:
            batches = plan_request_batches_v1(
                all_chunks,
                model=self._model,
                encoding_format=self._encoding_format,
                dimensions=self._dimensions,
                max_chunk_bytes=LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
                max_batch_items=LOCAL_MODEL2VEC_MAX_BATCH_ITEMS,
                max_batch_utf8_bytes=LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES,
                max_request_body_bytes=LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES,
            )
        except CanonicalEmbeddingInputError as exc:
            raise LocalEmbeddingClientError(
                exc.code,
                exc.public_message,
                retryable=False,
            ) from exc

        flat_vectors: list[list[float]] = []
        batch_token_counts: list[int] = []
        for batch in batches:
            vecs, tokens = await self._send_chunk_batch_async(batch)
            flat_vectors.extend(vecs)
            batch_token_counts.append(tokens)

        if len(flat_vectors) != len(all_chunks):
            raise LocalEmbeddingClientError(
                "total_chunk_vector_mismatch",
                "received vector count does not match total chunks",
                retryable=False,
            )

        total_tokens = sum(batch_token_counts)
        results: list[CanonicalEmbeddingResult] = []
        for prep, (start, end) in zip(prepared_docs, doc_chunk_spans):
            doc_vecs = flat_vectors[start:end]
            try:
                aggregated = aggregate_chunk_vectors_v1(
                    doc_vecs,
                    prep.chunk_byte_lengths,
                    expected_dim=self._dimensions,
                )
            except CanonicalEmbeddingAggregationError as exc:
                raise LocalEmbeddingClientError(
                    exc.code,
                    exc.public_message,
                    retryable=False,
                ) from exc
            results.append(
                CanonicalEmbeddingResult(
                    vector=aggregated,
                    input_hash=prep.input_hash,
                    chunk_count=len(prep.chunks),
                    prompt_tokens=total_tokens if len(prepared_docs) == 1 else 0,
                    profile_id=self._profile_id,
                    model_alias=self._model,
                )
            )
        return results

    def _send_chunk_batch_sync(self, batch: list[str]) -> tuple[list[list[float]], int]:
        attempt = 0
        while True:
            try:
                if self._use_openai_sdk or self._openai_client is not None:
                    return self._call_openai_sdk_sync(batch)
                return self._call_http_sync(batch)
            except LocalEmbeddingClientError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                delay = (
                    exc.retry_after_seconds
                    if exc.retry_after_seconds is not None and exc.retry_after_seconds > 0
                    else self._retry_backoff_seconds * (2**attempt)
                )
                attempt += 1
                if delay > 0:
                    self._sleep_fn(min(delay, MAX_RETRY_DELAY_SECONDS))

    async def _send_chunk_batch_async(self, batch: list[str]) -> tuple[list[list[float]], int]:
        attempt = 0
        while True:
            try:
                if self._use_openai_sdk or self._async_openai_client is not None:
                    return await self._call_openai_sdk_async(batch)
                return await asyncio.to_thread(self._call_http_sync, batch)
            except LocalEmbeddingClientError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                delay = (
                    exc.retry_after_seconds
                    if exc.retry_after_seconds is not None and exc.retry_after_seconds > 0
                    else self._retry_backoff_seconds * (2**attempt)
                )
                attempt += 1
                if delay > 0:
                    await asyncio.sleep(min(delay, MAX_RETRY_DELAY_SECONDS))

    def _call_http_sync(self, batch: list[str]) -> tuple[list[list[float]], int]:
        body = serialize_embedding_request_body(
            batch,
            model=self._model,
            encoding_format=self._encoding_format,
            dimensions=self._dimensions,
        )
        req = urllib.request.Request(
            self._endpoint_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                raw_resp = resp.read()
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc) from exc
        except Exception as exc:
            raise LocalEmbeddingClientError(
                "transport_unavailable",
                "local embedding endpoint is unreachable",
                retryable=True,
            ) from exc

        try:
            parsed = json.loads(raw_resp.decode("utf-8", errors="strict"))
        except Exception as exc:
            raise LocalEmbeddingClientError(
                "malformed_response_json",
                "local embedding endpoint returned invalid JSON",
                retryable=False,
            ) from exc

        return _parse_and_verify_batch_payload(
            parsed,
            expected_count=len(batch),
            expected_model=self._model,
            expected_dim=self._dimensions,
        )

    def _call_openai_sdk_sync(self, batch: list[str]) -> tuple[list[list[float]], int]:
        client = self._openai_client
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=self._api_key,
                base_url=self._sdk_base_url,
                timeout=self._timeout_seconds,
                max_retries=0,
            )
            self._openai_client = client

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": batch,
            "dimensions": self._dimensions,
        }
        if self._encoding_format is not None:
            kwargs["encoding_format"] = self._encoding_format

        try:
            response = client.embeddings.create(**kwargs)
        except Exception as exc:
            raise self._map_sdk_exception(exc) from exc

        return self._verify_sdk_response(response, expected_count=len(batch))

    async def _call_openai_sdk_async(self, batch: list[str]) -> tuple[list[list[float]], int]:
        client = self._async_openai_client
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._sdk_base_url,
                timeout=self._timeout_seconds,
                max_retries=0,
            )
            self._async_openai_client = client

        kwargs: dict[str, Any] = {
            "model": self._model,
            "input": batch,
            "dimensions": self._dimensions,
        }
        if self._encoding_format is not None:
            kwargs["encoding_format"] = self._encoding_format

        try:
            response = await client.embeddings.create(**kwargs)
        except Exception as exc:
            raise self._map_sdk_exception(exc) from exc

        return self._verify_sdk_response(response, expected_count=len(batch))

    def _verify_sdk_response(
        self,
        response: Any,
        *,
        expected_count: int,
    ) -> tuple[list[list[float]], int]:
        if getattr(response, "model", None) != self._model:
            raise LocalEmbeddingClientError(
                "response_model_mismatch",
                "SDK embedding response model mismatch",
                retryable=False,
            )
        data = getattr(response, "data", None)
        if not isinstance(data, list) or len(data) != expected_count:
            raise LocalEmbeddingClientError(
                "response_count_mismatch",
                "SDK embedding response item count mismatch",
                retryable=False,
            )
        by_index: dict[int, list[float]] = {}
        for item in data:
            idx = getattr(item, "index", None)
            if isinstance(idx, bool) or not isinstance(idx, int) or not (0 <= idx < expected_count):
                raise LocalEmbeddingClientError(
                    "invalid_embedding_index",
                    "SDK embedding response item has invalid index",
                    retryable=False,
                )
            if idx in by_index:
                raise LocalEmbeddingClientError(
                    "duplicate_embedding_index",
                    "SDK embedding response contains duplicate index",
                    retryable=False,
                )
            raw_emb = getattr(item, "embedding", None)
            try:
                if isinstance(raw_emb, str):
                    vec = decode_base64_float32_le(raw_emb, expected_dim=self._dimensions)
                elif isinstance(raw_emb, list):
                    vec = validate_finite_nonzero_vector(raw_emb, expected_dim=self._dimensions)
                else:
                    raise LocalEmbeddingClientError(
                        "invalid_embedding_payload",
                        "SDK embedding payload must be float list or base64 string",
                        retryable=False,
                    )
            except CanonicalEmbeddingAggregationError as exc:
                raise LocalEmbeddingClientError(
                    exc.code,
                    exc.public_message,
                    retryable=False,
                ) from exc
            by_index[idx] = vec

        if len(by_index) != expected_count:
            raise LocalEmbeddingClientError(
                "missing_embedding_index",
                "SDK embedding response is missing expected indices",
                retryable=False,
            )
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        return [by_index[i] for i in range(expected_count)], prompt_tokens

    def _map_http_error(self, exc: urllib.error.HTTPError) -> LocalEmbeddingClientError:
        status = int(getattr(exc, "code", 500) or 500)
        retry_after: float | None = None
        if exc.headers is not None:
            raw_ra = exc.headers.get("Retry-After")
            if raw_ra is not None:
                try:
                    retry_after = float(raw_ra)
                except ValueError:
                    retry_after = None
        code = f"http_{status}"
        try:
            body_bytes = exc.read()
            parsed = json.loads(body_bytes.decode("utf-8", errors="ignore"))
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
                err_code = parsed["error"].get("code")
                if isinstance(err_code, str) and err_code:
                    code = err_code
        except Exception:
            pass
        retryable = status in {429, 503}
        return LocalEmbeddingClientError(
            code,
            f"local embedding service returned HTTP {status}",
            status_code=status,
            retryable=retryable,
            retry_after_seconds=retry_after,
        )

    def _map_sdk_exception(self, exc: Exception) -> LocalEmbeddingClientError:
        status = getattr(exc, "status_code", None)
        retry_after: float | None = None
        response = getattr(exc, "response", None)
        if response is not None and hasattr(response, "headers"):
            raw_ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
            if raw_ra is not None:
                try:
                    retry_after = float(raw_ra)
                except ValueError:
                    retry_after = None

        code = "sdk_transport_error"
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err_obj = body.get("error") if "error" in body else body
            if isinstance(err_obj, dict) and isinstance(err_obj.get("code"), str):
                code = err_obj["code"]

        if isinstance(status, int):
            retryable = status in {429, 503}
            if code == "sdk_transport_error":
                code = f"http_{status}"
            return LocalEmbeddingClientError(
                code,
                f"local embedding SDK request failed with HTTP {status}",
                status_code=status,
                retryable=retryable,
                retry_after_seconds=retry_after,
            )

        return LocalEmbeddingClientError(
            "transport_unavailable",
            "local embedding endpoint is unreachable",
            retryable=True,
        )


class LocalModel2VecGraphitiEmbedder:
    """Graphiti-compatible async embedder backed by `CanonicalLocalEmbeddingClient`."""

    def __init__(
        self,
        *,
        canonical_client: CanonicalLocalEmbeddingClient,
        config: Any | None = None,
    ) -> None:
        self._canonical_client = canonical_client
        if config is None:
            from types import SimpleNamespace

            config = SimpleNamespace(
                embedding_model=canonical_client.model_alias,
                embedding_dim=canonical_client.dimensions,
                base_url=getattr(canonical_client, "_base_url", None),
                api_key=None,
            )
        self.config = config

    @property
    def profile_id(self) -> str:
        return self._canonical_client.profile_id

    @property
    def canonical_client(self) -> CanonicalLocalEmbeddingClient:
        return self._canonical_client

    async def create(self, input_data: str | list[str] | Any) -> list[float]:
        if isinstance(input_data, str):
            res = await self._canonical_client.aembed_document(input_data)
            return res.vector
        if isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            res = await self._canonical_client.aembed_document(input_data[0])
            return res.vector
        raise LocalEmbeddingClientError(
            "invalid_request",
            "Graphiti local embedder requires non-empty string input",
            retryable=False,
        )

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        if not input_data_list:
            return []
        results = await self._canonical_client.aembed_documents(input_data_list)
        return [res.vector for res in results]

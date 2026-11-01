"""Local Model2Vec HTTP service (`GET /livez`, `GET /readyz`, `POST /v1/embeddings`)."""

from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .canonical import encode_base64_float32_le, validate_finite_nonzero_vector
from .profile import (
    LOCAL_MODEL2VEC_DEFAULT_BIND_HOST,
    LOCAL_MODEL2VEC_DEFAULT_CONCURRENCY,
    LOCAL_MODEL2VEC_DEFAULT_MAX_QUEUE,
    LOCAL_MODEL2VEC_DEFAULT_PORT,
    LOCAL_MODEL2VEC_DEFAULT_QUEUE_TIMEOUT_SECONDS,
    LOCAL_MODEL2VEC_DIMENSIONS,
    LOCAL_MODEL2VEC_LIBRARY_VERSION,
    LOCAL_MODEL2VEC_MAX_BATCH_ITEMS,
    LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES,
    LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
    LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES,
    LOCAL_MODEL2VEC_MODEL_ALIAS,
    LOCAL_MODEL2VEC_MODEL_DIR_ENV,
    LOCAL_MODEL2VEC_PROFILE_ID,
    LOCAL_MODEL2VEC_REVISION,
    LocalEmbeddingModelIntegrityError,
    verify_model_directory,
)

ALLOWED_EMBEDDING_REQUEST_KEYS = frozenset(
    {"model", "input", "encoding_format", "dimensions", "user"}
)


@dataclass(frozen=True)
class LocalEmbeddingHttpResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]
    content_type: str = "application/json; charset=utf-8"


class LocalEmbeddingApiError(Exception):
    """Structured public-safe API error matching the D1 error envelope contract."""

    def __init__(
        self,
        *,
        status_code: int,
        error_type: str,
        code: str,
        message: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.code = code
        self.public_message = message
        self.retry_after = retry_after

    def to_response(self) -> LocalEmbeddingHttpResponse:
        payload = {
            "error": {
                "message": self.public_message,
                "type": self.error_type,
                "param": None,
                "code": self.code,
            }
        }
        headers: dict[str, str] = {}
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return LocalEmbeddingHttpResponse(
            status_code=self.status_code,
            body=body,
            headers=headers,
        )


@dataclass(frozen=True)
class EncodedBatchResult:
    vectors: list[list[float]]
    prompt_tokens: int


class _DuplicateJsonKeyError(ValueError):
    """Internal signal raised when a JSON object contains duplicate keys."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError("duplicate_json_key")
        result[key] = value
    return result


class _BoundedInferenceGate:
    """Strict FIFO single-inference gate with bounded wait queue and timeout."""

    def __init__(
        self,
        *,
        max_concurrency: int = LOCAL_MODEL2VEC_DEFAULT_CONCURRENCY,
        max_queue_size: int = LOCAL_MODEL2VEC_DEFAULT_MAX_QUEUE,
        queue_timeout_seconds: float = LOCAL_MODEL2VEC_DEFAULT_QUEUE_TIMEOUT_SECONDS,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if max_queue_size < 0:
            raise ValueError("max_queue_size must be >= 0")
        if queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be > 0")
        self._max_concurrency = int(max_concurrency)
        self._max_queue_size = int(max_queue_size)
        self._queue_timeout_seconds = float(queue_timeout_seconds)
        self._cond = threading.Condition(threading.Lock())
        self._active = 0
        self._waiters: deque[list[bool]] = deque()

    def acquire(self) -> None:
        with self._cond:
            if self._active < self._max_concurrency and not self._waiters:
                self._active += 1
                return
            if len(self._waiters) >= self._max_queue_size:
                raise LocalEmbeddingApiError(
                    status_code=429,
                    error_type="rate_limit_error",
                    code="capacity_exceeded",
                    message="embedding service queue capacity exceeded",
                    retry_after=2,
                )
            slot = [False]
            self._waiters.append(slot)
            deadline = time.monotonic() + self._queue_timeout_seconds
            while not slot[0]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        self._waiters.remove(slot)
                    except ValueError:
                        pass
                    if slot[0]:
                        self._release_locked()
                    raise LocalEmbeddingApiError(
                        status_code=429,
                        error_type="rate_limit_error",
                        code="capacity_exceeded",
                        message="embedding service queue wait timed out",
                        retry_after=2,
                    )
                self._cond.wait(timeout=remaining)

    def release(self) -> None:
        with self._cond:
            self._release_locked()

    def _release_locked(self) -> None:
        while self._waiters:
            next_slot = self._waiters.popleft()
            next_slot[0] = True
            self._cond.notify_all()
            return
        if self._active > 0:
            self._active -= 1
        self._cond.notify_all()


class LocalModel2VecEngine:
    """Verified offline-only Model2Vec inference engine."""

    def __init__(
        self,
        *,
        model: Any,
        model_dir: Path | None,
        verification: Mapping[str, Any],
    ) -> None:
        self._model = model
        self._model_dir = model_dir
        self._verification = dict(verification)

    @classmethod
    def from_model_dir(cls, model_dir: str | Path | None) -> LocalModel2VecEngine:
        """Load `StaticModel` from a local directory after SHA-256 verification.

        Never downloads from the network.
        """

        verification = verify_model_directory(model_dir, check_library_version=True)
        resolved_dir = Path(str(model_dir).strip())

        prev_hf_offline = os.environ.get("HF_HUB_OFFLINE")
        prev_tf_offline = os.environ.get("TRANSFORMERS_OFFLINE")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            from model2vec import StaticModel

            model = StaticModel.from_pretrained(resolved_dir)
        except Exception as exc:
            raise LocalEmbeddingModelIntegrityError(
                "model_load_failed",
                "failed to load local embedding model from verified directory",
            ) from exc
        finally:
            if prev_hf_offline is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = prev_hf_offline
            if prev_tf_offline is None:
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
            else:
                os.environ["TRANSFORMERS_OFFLINE"] = prev_tf_offline

        engine = cls(model=model, model_dir=resolved_dir, verification=verification)
        # Self-check dimensions and finite output at load time.
        probe = engine.encode_batch(["readiness_self_check"])
        if len(probe.vectors) != 1 or len(probe.vectors[0]) != LOCAL_MODEL2VEC_DIMENSIONS:
            raise LocalEmbeddingModelIntegrityError(
                "self_check_dimension_mismatch",
                "local embedding model failed startup dimension check",
            )
        return engine

    def check_ready(self) -> dict[str, Any]:
        if self._model_dir is not None:
            verify_model_directory(self._model_dir, check_library_version=True)
        return {
            "ready": True,
            "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
            "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
            "revision": LOCAL_MODEL2VEC_REVISION,
            "library_version": LOCAL_MODEL2VEC_LIBRARY_VERSION,
            "dimensions": LOCAL_MODEL2VEC_DIMENSIONS,
        }

    def encode_batch(self, items: Sequence[str]) -> EncodedBatchResult:
        """Encode a batch of items with `max_length=None, normalize=True` and exact token count."""

        if not items:
            raise ValueError("items must not be empty")

        tokenizer = getattr(self._model, "tokenizer", None)
        if tokenizer is None:
            raise RuntimeError("model tokenizer is unavailable")

        prev_truncation = (
            dict(tokenizer.truncation)
            if getattr(tokenizer, "truncation", None) is not None
            else None
        )
        try:
            tokenizer.no_truncation()
            encodings = tokenizer.encode_batch(list(items), add_special_tokens=False)
            prompt_tokens = sum(len(enc.ids) for enc in encodings)
            raw_vectors = self._model.encode(
                list(items),
                max_length=None,
                normalize=True,
                show_progress_bar=False,
            )
        finally:
            if prev_truncation is not None:
                tokenizer.enable_truncation(**prev_truncation)
            else:
                tokenizer.no_truncation()

        if hasattr(raw_vectors, "tolist"):
            as_list = raw_vectors.tolist()
        else:
            as_list = list(raw_vectors)

        if not as_list or not isinstance(as_list[0], (list, tuple)):
            as_list = [as_list]

        if len(as_list) != len(items):
            raise RuntimeError("encoded vector count mismatch")

        validated_vectors = [
            validate_finite_nonzero_vector(vec, expected_dim=LOCAL_MODEL2VEC_DIMENSIONS)
            for vec in as_list
        ]
        return EncodedBatchResult(
            vectors=validated_vectors,
            prompt_tokens=int(prompt_tokens),
        )


class LocalEmbeddingServiceApplication:
    """HTTP application layer for the local Model2Vec service."""

    def __init__(
        self,
        *,
        engine: LocalModel2VecEngine | None,
        startup_error_code: str | None = None,
        max_concurrency: int = LOCAL_MODEL2VEC_DEFAULT_CONCURRENCY,
        max_queue_size: int = LOCAL_MODEL2VEC_DEFAULT_MAX_QUEUE,
        queue_timeout_seconds: float = LOCAL_MODEL2VEC_DEFAULT_QUEUE_TIMEOUT_SECONDS,
        max_chunk_bytes: int = LOCAL_MODEL2VEC_MAX_CHUNK_BYTES,
        max_batch_items: int = LOCAL_MODEL2VEC_MAX_BATCH_ITEMS,
        max_batch_utf8_bytes: int = LOCAL_MODEL2VEC_MAX_BATCH_UTF8_BYTES,
        max_request_body_bytes: int = LOCAL_MODEL2VEC_MAX_REQUEST_BODY_BYTES,
    ) -> None:
        self._engine = engine
        self._startup_error_code = startup_error_code
        self._gate = _BoundedInferenceGate(
            max_concurrency=max_concurrency,
            max_queue_size=max_queue_size,
            queue_timeout_seconds=queue_timeout_seconds,
        )
        self._max_chunk_bytes = int(max_chunk_bytes)
        self._max_batch_items = int(max_batch_items)
        self._max_batch_utf8_bytes = int(max_batch_utf8_bytes)
        self._max_request_body_bytes = int(max_request_body_bytes)

    @property
    def max_request_body_bytes(self) -> int:
        return self._max_request_body_bytes

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        model_dir: str | Path | None = None,
        max_concurrency: int = LOCAL_MODEL2VEC_DEFAULT_CONCURRENCY,
        max_queue_size: int = LOCAL_MODEL2VEC_DEFAULT_MAX_QUEUE,
        queue_timeout_seconds: float = LOCAL_MODEL2VEC_DEFAULT_QUEUE_TIMEOUT_SECONDS,
    ) -> LocalEmbeddingServiceApplication:
        env = os.environ if environ is None else environ
        selected_dir = (
            str(model_dir).strip()
            if model_dir is not None and str(model_dir).strip()
            else str(env.get(LOCAL_MODEL2VEC_MODEL_DIR_ENV) or "").strip()
        )
        try:
            engine = LocalModel2VecEngine.from_model_dir(selected_dir)
            return cls(
                engine=engine,
                max_concurrency=max_concurrency,
                max_queue_size=max_queue_size,
                queue_timeout_seconds=queue_timeout_seconds,
            )
        except LocalEmbeddingModelIntegrityError as exc:
            return cls(
                engine=None,
                startup_error_code=exc.reason_code,
                max_concurrency=max_concurrency,
                max_queue_size=max_queue_size,
                queue_timeout_seconds=queue_timeout_seconds,
            )
        except Exception:
            return cls(
                engine=None,
                startup_error_code="startup_failed",
                max_concurrency=max_concurrency,
                max_queue_size=max_queue_size,
                queue_timeout_seconds=queue_timeout_seconds,
            )

    def handle_livez(self) -> LocalEmbeddingHttpResponse:
        ready = self._is_ready()
        payload = {
            "object": "service_status",
            "status": "ok",
            "live": True,
            "ready": ready,
            "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
            "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
            "revision": LOCAL_MODEL2VEC_REVISION,
            "dimensions": LOCAL_MODEL2VEC_DIMENSIONS,
        }
        return LocalEmbeddingHttpResponse(
            status_code=200,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={},
        )

    def handle_readyz(self) -> LocalEmbeddingHttpResponse:
        if self._engine is None:
            return self._unavailable_readyz_response()
        try:
            self._engine.check_ready()
        except Exception:
            return self._unavailable_readyz_response()

        payload = {
            "object": "service_status",
            "status": "ok",
            "ready": True,
            "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
            "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
            "revision": LOCAL_MODEL2VEC_REVISION,
            "dimensions": LOCAL_MODEL2VEC_DIMENSIONS,
        }
        return LocalEmbeddingHttpResponse(
            status_code=200,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={},
        )

    def handle_embeddings(self, raw_body: bytes) -> LocalEmbeddingHttpResponse:
        try:
            if len(raw_body) > self._max_request_body_bytes:
                raise LocalEmbeddingApiError(
                    status_code=413,
                    error_type="invalid_request_error",
                    code="input_too_large",
                    message="request body exceeds maximum allowed size",
                )
            items, encoding_format = self._parse_and_validate_request(raw_body)
            if self._engine is None:
                raise LocalEmbeddingApiError(
                    status_code=503,
                    error_type="server_error",
                    code="model_unavailable",
                    message="local embedding model is unavailable",
                    retry_after=2,
                )

            self._gate.acquire()
            try:
                encoded = self._engine.encode_batch(items)
            except LocalEmbeddingApiError:
                raise
            except LocalEmbeddingModelIntegrityError as exc:
                raise LocalEmbeddingApiError(
                    status_code=503,
                    error_type="server_error",
                    code="model_unavailable",
                    message="local embedding model is unavailable",
                    retry_after=2,
                ) from exc
            except Exception as exc:
                raise LocalEmbeddingApiError(
                    status_code=500,
                    error_type="server_error",
                    code="internal_error",
                    message="internal embedding inference error",
                ) from exc
            finally:
                self._gate.release()

            data_items: list[dict[str, Any]] = []
            for idx, vec in enumerate(encoded.vectors):
                if encoding_format == "base64":
                    emb_payload: Any = encode_base64_float32_le(
                        vec,
                        expected_dim=LOCAL_MODEL2VEC_DIMENSIONS,
                    )
                else:
                    emb_payload = vec
                data_items.append(
                    {
                        "object": "embedding",
                        "index": idx,
                        "embedding": emb_payload,
                    }
                )

            response_payload = {
                "object": "list",
                "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
                "data": data_items,
                "usage": {
                    "prompt_tokens": encoded.prompt_tokens,
                    "total_tokens": encoded.prompt_tokens,
                    "billable": False,
                    "cost_usd": 0.0,
                },
            }
            return LocalEmbeddingHttpResponse(
                status_code=200,
                body=json.dumps(
                    response_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8"),
                headers={},
            )
        except LocalEmbeddingApiError as exc:
            return exc.to_response()
        except Exception:
            return LocalEmbeddingApiError(
                status_code=500,
                error_type="server_error",
                code="internal_error",
                message="internal server error",
            ).to_response()

    def _parse_and_validate_request(self, raw_body: bytes) -> tuple[list[str], str]:
        if not raw_body:
            raise LocalEmbeddingApiError(
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_request",
                message="request body must be a valid JSON object",
            )
        try:
            text = raw_body.decode("utf-8", errors="strict")
            parsed = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except _DuplicateJsonKeyError as exc:
            raise LocalEmbeddingApiError(
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_request",
                message="duplicate JSON keys are not allowed",
            ) from exc
        except Exception as exc:
            raise LocalEmbeddingApiError(
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_request",
                message="malformed JSON request body",
            ) from exc

        if not isinstance(parsed, dict):
            raise LocalEmbeddingApiError(
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_request",
                message="request body must be a JSON object",
            )

        unknown_keys = set(parsed.keys()) - ALLOWED_EMBEDDING_REQUEST_KEYS
        if unknown_keys:
            raise LocalEmbeddingApiError(
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_request",
                message="request contains unsupported fields",
            )

        if "model" not in parsed or not isinstance(parsed["model"], str) or not parsed["model"].strip():
            raise LocalEmbeddingApiError(
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_request",
                message="model field is required and must be a string",
            )
        if parsed["model"] != LOCAL_MODEL2VEC_MODEL_ALIAS:
            raise LocalEmbeddingApiError(
                status_code=400,
                error_type="invalid_request_error",
                code="unknown_model",
                message="requested embedding model is not supported",
            )

        encoding_format = "float"
        if "encoding_format" in parsed:
            raw_fmt = parsed["encoding_format"]
            if not isinstance(raw_fmt, str) or raw_fmt not in {"float", "base64"}:
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="encoding_format must be 'float' or 'base64'",
                )
            encoding_format = raw_fmt

        if "dimensions" in parsed:
            raw_dim = parsed["dimensions"]
            if (
                raw_dim is None
                or isinstance(raw_dim, bool)
                or not isinstance(raw_dim, int)
                or raw_dim != LOCAL_MODEL2VEC_DIMENSIONS
            ):
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="dimensions must be omitted or equal to 256",
                )

        if "user" in parsed:
            if not isinstance(parsed["user"], str):
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="user must be a string when provided",
                )

        if "input" not in parsed:
            raise LocalEmbeddingApiError(
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_request",
                message="input field is required",
            )
        raw_input = parsed["input"]
        if isinstance(raw_input, str):
            if raw_input == "":
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="input string must not be empty",
                )
            items = [raw_input]
        elif isinstance(raw_input, list):
            if len(raw_input) == 0:
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="input array must not be empty",
                )
            items = []
            for element in raw_input:
                if not isinstance(element, str) or element == "":
                    raise LocalEmbeddingApiError(
                        status_code=400,
                        error_type="invalid_request_error",
                        code="invalid_request",
                        message="input array must contain only non-empty strings",
                    )
                items.append(element)
        else:
            raise LocalEmbeddingApiError(
                status_code=400,
                error_type="invalid_request_error",
                code="invalid_request",
                message="input must be a non-empty string or array of non-empty strings",
            )

        if len(items) > self._max_batch_items:
            raise LocalEmbeddingApiError(
                status_code=413,
                error_type="invalid_request_error",
                code="input_too_large",
                message="input batch exceeds maximum allowed item count",
            )

        total_utf8_bytes = 0
        for item in items:
            try:
                encoded_item = item.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="input contains invalid UTF-8",
                ) from exc
            item_len = len(encoded_item)
            if item_len > self._max_chunk_bytes:
                raise LocalEmbeddingApiError(
                    status_code=413,
                    error_type="invalid_request_error",
                    code="input_too_large",
                    message="input item exceeds maximum UTF-8 byte limit",
                )
            total_utf8_bytes += item_len
            if total_utf8_bytes > self._max_batch_utf8_bytes:
                raise LocalEmbeddingApiError(
                    status_code=413,
                    error_type="invalid_request_error",
                    code="input_too_large",
                    message="input batch exceeds maximum total UTF-8 byte limit",
                )

        return items, encoding_format

    def _is_ready(self) -> bool:
        if self._engine is None:
            return False
        try:
            self._engine.check_ready()
            return True
        except Exception:
            return False

    def _unavailable_readyz_response(self) -> LocalEmbeddingHttpResponse:
        payload = {
            "object": "service_status",
            "status": "unavailable",
            "ready": False,
            "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
            "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
            "revision": LOCAL_MODEL2VEC_REVISION,
            "dimensions": LOCAL_MODEL2VEC_DIMENSIONS,
            "error": {
                "message": "local embedding model is unavailable",
                "type": "server_error",
                "param": None,
                "code": "model_unavailable",
            },
        }
        return LocalEmbeddingHttpResponse(
            status_code=503,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={"Retry-After": "2"},
        )


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def create_local_embedding_http_server(
    *,
    application: LocalEmbeddingServiceApplication,
    bind_host: str = LOCAL_MODEL2VEC_DEFAULT_BIND_HOST,
    port: int = LOCAL_MODEL2VEC_DEFAULT_PORT,
    allow_non_loopback: bool = False,
    server_factory: Callable[..., ThreadingHTTPServer] = ThreadingHTTPServer,
) -> ThreadingHTTPServer:
    """Create a `ThreadingHTTPServer` bound to loopback by default."""

    if not allow_non_loopback and not _is_loopback_host(bind_host):
        raise ValueError("local embedding service binds to loopback only by default")
    if not 0 <= int(port) <= 65535:
        raise ValueError("invalid port number")

    max_body = application.max_request_body_bytes

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/livez":
                resp = application.handle_livez()
            elif path == "/readyz":
                resp = application.handle_readyz()
            else:
                resp = LocalEmbeddingApiError(
                    status_code=404,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="route not found",
                ).to_response()
            self._write_response(resp)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/v1/embeddings":
                resp = LocalEmbeddingApiError(
                    status_code=404,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="route not found",
                ).to_response()
                self._write_response(resp)
                return

            try:
                body = self._read_bounded_body(max_body)
            except LocalEmbeddingApiError as exc:
                self._write_response(exc.to_response())
                return
            except Exception:
                resp = LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="invalid HTTP request body",
                ).to_response()
                self._write_response(resp)
                return

            resp = application.handle_embeddings(body)
            self._write_response(resp)

        def _read_bounded_body(self, limit: int) -> bytes:
            transfer_encoding = self.headers.get("Transfer-Encoding", "").strip().lower()
            if transfer_encoding == "chunked":
                chunks: list[bytes] = []
                total = 0
                while True:
                    line = self.rfile.readline(64)
                    if not line:
                        raise ValueError("unexpected EOF in chunked body")
                    size_str = line.split(b";", 1)[0].strip()
                    chunk_len = int(size_str, 16)
                    if chunk_len < 0:
                        raise ValueError("negative chunk size")
                    if chunk_len == 0:
                        self.rfile.readline(64)
                        break
                    total += chunk_len
                    if total > limit:
                        raise LocalEmbeddingApiError(
                            status_code=413,
                            error_type="invalid_request_error",
                            code="input_too_large",
                            message="request body exceeds maximum allowed size",
                        )
                    data = self.rfile.read(chunk_len)
                    if len(data) != chunk_len:
                        raise ValueError("truncated chunk")
                    chunks.append(data)
                    crlf = self.rfile.read(2)
                    if crlf != b"\r\n":
                        raise ValueError("invalid chunk terminator")
                return b"".join(chunks)

            lengths = self.headers.get_all("Content-Length", failobj=[])
            if len(lengths) != 1:
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="single Content-Length header is required",
                )
            try:
                content_length = int(lengths[0])
            except ValueError as exc:
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="invalid Content-Length header",
                ) from exc
            if content_length < 0:
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="negative Content-Length is invalid",
                )
            if content_length > limit:
                raise LocalEmbeddingApiError(
                    status_code=413,
                    error_type="invalid_request_error",
                    code="input_too_large",
                    message="request body exceeds maximum allowed size",
                )
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                raise LocalEmbeddingApiError(
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_request",
                    message="incomplete HTTP request body",
                )
            return body

        def _write_response(self, response: LocalEmbeddingHttpResponse) -> None:
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for header_name, header_val in response.headers.items():
                self.send_header(header_name, header_val)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, format: str, *args: object) -> None:
            # Never log request paths, headers, user identifiers, texts, or vectors.
            return

    return server_factory((bind_host, int(port)), Handler)

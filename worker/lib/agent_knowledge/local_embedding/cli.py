"""CLI entrypoints for installing, verifying, and serving the local Model2Vec service."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence

from .profile import (
    LOCAL_MODEL2VEC_DEFAULT_BIND_HOST,
    LOCAL_MODEL2VEC_DEFAULT_MAX_QUEUE,
    LOCAL_MODEL2VEC_DEFAULT_PORT,
    LOCAL_MODEL2VEC_DEFAULT_QUEUE_TIMEOUT_SECONDS,
    LOCAL_MODEL2VEC_DIMENSIONS,
    LOCAL_MODEL2VEC_MODEL_ALIAS,
    LOCAL_MODEL2VEC_MODEL_DIR_ENV,
    LOCAL_MODEL2VEC_PROFILE_ID,
    LOCAL_MODEL2VEC_REPO_ID,
    LOCAL_MODEL2VEC_REVISION,
    LocalEmbeddingModelIntegrityError,
    install_verified_model,
    verify_model_directory,
)
from .service import (
    LocalEmbeddingServiceApplication,
    LocalModel2VecEngine,
    create_local_embedding_http_server,
)


def _resolve_model_dir(arg_value: str | None, environ: Mapping[str, str]) -> str:
    if arg_value is not None and str(arg_value).strip():
        return str(arg_value).strip()
    return str(environ.get(LOCAL_MODEL2VEC_MODEL_DIR_ENV) or "").strip()


def _cmd_install(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(prog="neuron-local-embedding install")
    parser.add_argument("--model-dir", default="", help="target model directory")
    parser.add_argument("--source-dir", default="", help="local verified snapshot directory to copy from")
    parser.add_argument(
        "--download",
        action="store_true",
        help="explicitly download pinned revision from Hugging Face before SHA-256 verification",
    )
    args = parser.parse_args(list(argv))
    target_dir = _resolve_model_dir(args.model_dir, environ)
    try:
        result = install_verified_model(
            target_dir,
            source_dir=args.source_dir or None,
            allow_download=bool(args.download),
        )
    except LocalEmbeddingModelIntegrityError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": exc.reason_code,
                    "message": exc.public_message,
                    "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
                    "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
                    "revision": LOCAL_MODEL2VEC_REVISION,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": "install_failed",
                    "message": "local embedding model installation failed",
                    "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
                    "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
                    "revision": LOCAL_MODEL2VEC_REVISION,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_verify(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(prog="neuron-local-embedding verify")
    parser.add_argument("--model-dir", default="", help="model directory to verify")
    parser.add_argument(
        "--skip-load-probe",
        action="store_true",
        help="verify file SHA-256 digests and library version without loading weights into memory",
    )
    args = parser.parse_args(list(argv))
    target_dir = _resolve_model_dir(args.model_dir, environ)
    try:
        verification = verify_model_directory(target_dir, check_library_version=True)
        if not args.skip_load_probe:
            engine = LocalModel2VecEngine.from_model_dir(target_dir)
            engine.check_ready()
    except LocalEmbeddingModelIntegrityError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": exc.reason_code,
                    "message": exc.public_message,
                    "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
                    "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
                    "revision": LOCAL_MODEL2VEC_REVISION,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": "verify_failed",
                    "message": "local embedding model verification failed",
                    "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
                    "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
                    "revision": LOCAL_MODEL2VEC_REVISION,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(verification, sort_keys=True))
    return 0


def _cmd_serve(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    parser = argparse.ArgumentParser(prog="neuron-local-embedding serve")
    parser.add_argument("--model-dir", default="", help="verified local model directory")
    parser.add_argument("--host", default=LOCAL_MODEL2VEC_DEFAULT_BIND_HOST)
    parser.add_argument("--port", type=int, default=LOCAL_MODEL2VEC_DEFAULT_PORT)
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument("--max-queue-size", type=int, default=LOCAL_MODEL2VEC_DEFAULT_MAX_QUEUE)
    parser.add_argument(
        "--queue-timeout-seconds",
        type=float,
        default=LOCAL_MODEL2VEC_DEFAULT_QUEUE_TIMEOUT_SECONDS,
    )
    args = parser.parse_args(list(argv))
    target_dir = _resolve_model_dir(args.model_dir, environ)

    app = LocalEmbeddingServiceApplication.from_environment(
        environ,
        model_dir=target_dir,
        max_queue_size=args.max_queue_size,
        queue_timeout_seconds=args.queue_timeout_seconds,
    )
    try:
        server = create_local_embedding_http_server(
            application=app,
            bind_host=args.host,
            port=args.port,
            allow_non_loopback=bool(args.allow_non_loopback),
        )
    except ValueError:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": "invalid_server_config",
                    "message": "local embedding server bind configuration is invalid",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _cmd_profile(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="neuron-local-embedding profile")
    parser.parse_args(list(argv))
    print(
        json.dumps(
            {
                "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
                "model": LOCAL_MODEL2VEC_MODEL_ALIAS,
                "repository": LOCAL_MODEL2VEC_REPO_ID,
                "revision": LOCAL_MODEL2VEC_REVISION,
                "dimensions": LOCAL_MODEL2VEC_DIMENSIONS,
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_evaluate(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    from .evaluation import run_sanitized_quality_evaluation

    parser = argparse.ArgumentParser(prog="neuron-local-embedding evaluate")
    parser.add_argument("--model-dir", default="", help="verified local model directory")
    parser.add_argument(
        "--recall-at-5-threshold",
        type=float,
        default=0.90,
        help="minimum Recall@5 threshold (default: 0.90)",
    )
    args = parser.parse_args(list(argv))
    target_dir = _resolve_model_dir(args.model_dir, environ)
    try:
        engine = LocalModel2VecEngine.from_model_dir(target_dir)
        engine.check_ready()
        report = run_sanitized_quality_evaluation(
            raw_chunk_embedder=lambda chunks: engine.encode_batch(list(chunks)).vectors,
            evaluation_mode="real_model2vec_weights",
            recall_at_5_threshold=float(args.recall_at_5_threshold),
        )
    except LocalEmbeddingModelIntegrityError as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": exc.reason_code,
                    "message": exc.public_message,
                    "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": "evaluation_failed",
                    "message": "local embedding quality evaluation failed",
                    "profile_id": LOCAL_MODEL2VEC_PROFILE_ID,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passed_proposed_gates else 1


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if environ is None else environ
    if not raw or raw[0] in {"-h", "--help"}:
        print(
            "usage: neuron-local-embedding <install|verify|serve|profile|evaluate> [args...]\n\n"
            "Local Model2Vec embedding service, verifier, and evaluation CLI for neurons."
        )
        return 0

    sub = raw[0]
    rest = raw[1:]
    if sub == "install":
        return _cmd_install(rest, env)
    if sub == "verify":
        return _cmd_verify(rest, env)
    if sub == "serve":
        return _cmd_serve(rest, env)
    if sub == "profile":
        return _cmd_profile(rest)
    if sub == "evaluate":
        return _cmd_evaluate(rest, env)

    print(f"unknown neuron-local-embedding subcommand: {sub}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

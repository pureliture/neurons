"""Native 호출·응답의 제한된 고정본. 실행의 진위나 현재 세계의 사실 인증이 아니다.

Assistant 문장과 요약을 원래 tool result로 복원하지 않는다. 원래 식별자가
있는 완성된 호출 쌍만 보존하고, 본문·redaction·잘림 범위를 함께 결속한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from ..public_safe_util import ensure_public_safe, hash_payload, sha256_text
from ..redaction import redact_public_ingress_text
from .observation_refs import observed_time

SCHEMA = "native_tool_receipt.v1"
EXTRACTOR = "native-tool-receipt-extractor.v1"
MAX_INPUT_BYTES = 2048
MAX_RESULT_BYTES = 4096
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 100000
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_FIELDS = frozenset(
    {
        "schema_version",
        "provider",
        "session_id_hash",
        "source_event_id_hash",
        "observation_id_hash",
        "source_content_hash",
        "tool_name",
        "arguments_text",
        "arguments_hash",
        "arguments_complete",
        "arguments_redacted",
        "result_text",
        "result_hash",
        "result_complete",
        "result_redacted",
        "result_scope",
        "is_error",
        "call_observed_at",
        "result_observed_at",
        "extractor_version",
        "redaction_version",
        "receipt_hash",
    }
)

MAX_BUNDLE_RECEIPT_BYTES = 65536


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("native_tool_duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("native_tool_nonfinite_json_value")


def _loads(value):
    return json.loads(
        value, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant
    )


def _bound(text, limit):
    redacted = redact_public_ingress_text(text)
    data = redacted.encode()
    return (
        data[:limit].decode("utf-8", errors="ignore"),
        sha256_text(redacted),
        len(data) <= limit,
        redacted != text,
    )


def build(
    *,
    provider,
    session_id,
    call_id,
    tool_name,
    arguments,
    result,
    is_error=False,
    call_observed_at="",
    result_observed_at="",
    result_complete=True,
):
    if (
        provider not in {"codex", "claude", "gemini", "grok", "antigravity"}
        or any(
            not isinstance(s, str) or not s.strip() or len(s) > 512
            for s in (session_id, call_id)
        )
        or not isinstance(tool_name, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", tool_name)
        or not isinstance(result, str)
        or type(is_error) is not bool
        or type(result_complete) is not bool
    ):
        raise ValueError("native_tool_pair_invalid")
    input_text, input_hash, input_complete, input_redacted = _bound(
        _json(arguments), MAX_INPUT_BYTES
    )
    result_text, result_hash, fits, result_redacted = _bound(result, MAX_RESULT_BYTES)
    # Provider가 알린 잘림도 보존한다. provider result 전체와 실제 stdout 전체를
    # 같은 범위로 주장하지 않는다.
    complete = (
        result_complete
        and fits
        and not any(
            marker in result
            for marker in (
                "[truncated]",
                "Warning: truncated output",
                "Output truncated",
                "[output truncated]",
            )
        )
    )
    session = sha256_text(f"{provider}:{session_id}")
    event = sha256_text(f"{provider}:{call_id}")
    receipt = {
        "schema_version": SCHEMA,
        "provider": provider,
        "session_id_hash": session,
        "source_event_id_hash": event,
        "observation_id_hash": sha256_text(f"tool_observation.v1:{session}:{event}"),
        "source_content_hash": hash_payload(
            {
                "tool": tool_name,
                "arguments": arguments,
                "result": result,
                "is_error": is_error,
            }
        ),
        "tool_name": tool_name,
        "arguments_text": input_text,
        "arguments_hash": input_hash,
        "arguments_complete": input_complete,
        "arguments_redacted": input_redacted,
        "result_text": result_text,
        "result_hash": result_hash,
        "result_complete": complete,
        "result_redacted": result_redacted,
        "result_scope": "provider_tool_result",
        "is_error": is_error,
        "call_observed_at": observed_time(call_observed_at),
        "result_observed_at": observed_time(result_observed_at),
        "extractor_version": EXTRACTOR,
        "redaction_version": "redaction.v2",
    }
    receipt["receipt_hash"] = hash_payload(receipt)
    return validate(receipt, provider=provider, session_id_hash=session)


def validate(receipt, *, provider, session_id_hash):
    if (
        not isinstance(receipt, dict)
        or set(receipt) != _FIELDS
        or receipt.get("schema_version") != SCHEMA
        or (receipt.get("provider"), receipt.get("session_id_hash"))
        != (provider, session_id_hash)
        or receipt.get("extractor_version") != EXTRACTOR
        or receipt.get("redaction_version") != "redaction.v2"
        or receipt.get("result_scope") != "provider_tool_result"
    ):
        raise ValueError("native_tool_receipt_binding_invalid")
    for name in (
        "session_id_hash",
        "source_event_id_hash",
        "observation_id_hash",
        "source_content_hash",
        "arguments_hash",
        "result_hash",
        "receipt_hash",
    ):
        if not isinstance(receipt[name], str) or not _HASH.fullmatch(receipt[name]):
            raise ValueError("native_tool_receipt_digest_invalid")
    if receipt["observation_id_hash"] != sha256_text(
        f"tool_observation.v1:{session_id_hash}:{receipt['source_event_id_hash']}"
    ) or receipt["receipt_hash"] != hash_payload(
        {k: v for k, v in receipt.items() if k != "receipt_hash"}
    ):
        raise ValueError("native_tool_receipt_integrity_invalid")
    if not isinstance(receipt["tool_name"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.:/-]{1,128}", receipt["tool_name"]
    ):
        raise ValueError("native_tool_receipt_tool_invalid")
    for text, digest, complete, limit in (
        ("arguments_text", "arguments_hash", "arguments_complete", MAX_INPUT_BYTES),
        ("result_text", "result_hash", "result_complete", MAX_RESULT_BYTES),
    ):
        if (
            not isinstance(receipt[text], str)
            or len(receipt[text].encode()) > limit
            or type(receipt[complete]) is not bool
            or (receipt[complete] and sha256_text(receipt[text]) != receipt[digest])
            or redact_public_ingress_text(receipt[text]) != receipt[text]
        ):
            raise ValueError("native_tool_receipt_content_invalid")
    if any(
        type(receipt[k]) is not bool
        for k in ("is_error", "arguments_redacted", "result_redacted")
    ):
        raise ValueError("native_tool_receipt_error_invalid")
    for name in ("call_observed_at", "result_observed_at"):
        if (
            not isinstance(receipt[name], str)
            or observed_time(receipt[name]) != receipt[name]
        ):
            raise ValueError("native_tool_receipt_time_invalid")
    ensure_public_safe(receipt, "NativeToolReceipt")
    return json.loads(_json(receipt))


def validate_bundle(entries, *, references, provider, session_id_hash):
    if (
        not isinstance(entries, list)
        or len(entries) > 10000
        or len(_json(entries).encode()) > MAX_BUNDLE_RECEIPT_BYTES
    ):
        raise ValueError("native_tool_receipt_bundle_limit")
    refs = {}
    for ref in references:
        refs.setdefault(ref["evidence_id_hash"], []).append(ref)
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"evidence_id_hash", "receipt"}:
            raise ValueError("native_tool_receipt_bundle_invalid")
        key = entry["evidence_id_hash"]
        if not isinstance(key, str) or key not in refs or key in result:
            raise ValueError("native_tool_receipt_reference_invalid")
        receipt = validate(
            entry["receipt"], provider=provider, session_id_hash=session_id_hash
        )
        if any(
            receipt[field] != ref[field]
            for ref in refs[key]
            for field in (
                "observation_id_hash",
                "source_event_id_hash",
                "source_content_hash",
                "extractor_version",
                "redaction_version",
            )
        ):
            raise ValueError("native_tool_receipt_reference_mismatch")
        result[key] = receipt
    if any(
        ref["extractor_version"] == EXTRACTOR and ref["evidence_id_hash"] not in result
        for ref in references
    ):
        raise ValueError("native_tool_receipt_missing")
    return result


def from_bundle(bundle, references):
    entries = bundle.get("native_tool_receipts", [])
    if entries == [] and not any(
        ref["extractor_version"] == EXTRACTOR for ref in references
    ):
        return {}
    result = validate_bundle(
        entries,
        references=references,
        provider=bundle["provider"],
        session_id_hash=bundle["session_id_hash"],
    )
    if entries:
        from .observation_refs import coverage_hash

        expected = coverage_hash(bundle["record_content_hashes"], references, entries)
        if bundle.get("coverage_hash") != expected:
            raise ValueError("native_tool_receipt_coverage_invalid")
    return result


def read_source(path):
    """새 native 경로는 symlink·변경 중인 source·초과 크기를 허용하지 않는다."""
    fd = os.open(
        Path(path),
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE_BYTES:
            raise ValueError("native_tool_source_invalid")
        rows, total = [], 0
        with os.fdopen(os.dup(fd), "rb") as stream:
            while line := stream.readline(MAX_LINE_BYTES + 1):
                total += len(line)
                if (
                    len(line) > MAX_LINE_BYTES
                    or total > MAX_SOURCE_BYTES
                    or len(rows) >= MAX_RECORDS
                ):
                    raise ValueError("native_tool_source_limit")
                if not line.strip():
                    continue
                item = _loads(line)
                if not isinstance(item, dict):
                    raise ValueError("native_tool_source_record_invalid")
                rows.append(item)
        after = os.fstat(fd)
        signature = lambda s: (
            s.st_dev,
            s.st_ino,
            s.st_size,
            s.st_mtime_ns,
            s.st_ctime_ns,
        )
        if signature(before) != signature(after) or total != after.st_size:
            raise ValueError("native_tool_source_changed")
        return rows
    finally:
        os.close(fd)


def _text(value):
    if isinstance(value, str):
        return value, True
    if isinstance(value, list):
        parts = []
        for part in value:
            if (
                not isinstance(part, dict)
                or part.get("type") not in {"text", "output_text", "input_text"}
                or not isinstance(part.get("text"), str)
            ):
                return _json(value), False
            parts.append(part["text"])
        return "\n".join(parts), True
    return _json(value), True


def extract(provider, rows):
    """Provider의 native 구조만 읽는다. Assistant 본문 안의 JSON은 파싱하지 않는다."""
    if provider not in {"codex", "claude", "gemini", "grok", "antigravity"}:
        raise ValueError("native_tool_provider_not_supported")
    sessions = set()
    calls, results = {}, {}

    def put(table, key, item):
        if not isinstance(key, str) or not key or len(key) > 512:
            raise ValueError("native_tool_call_id_required")
        if key in table:
            comparable = lambda d: {
                k: v
                for k, v in d.items()
                if k not in {"call_observed_at", "result_observed_at"}
            }
            if comparable(table[key]) != comparable(item):
                raise ValueError("native_tool_call_collision")
            return
        table[key] = item

    for row in rows:
        time = str(row.get("timestamp") or "")
        if provider == "codex":
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if row.get("type") == "session_meta":
                if payload.get("id"):
                    sessions.add(str(payload["id"]))
                continue
            if row.get("type") != "response_item":
                continue
            kind = payload.get("type")
            key = payload.get("call_id")
            if kind in {"function_call", "custom_tool_call"}:
                args = payload.get("arguments", payload.get("input", {}))
                if kind == "function_call" and isinstance(args, str):
                    args = _loads(args)
                put(
                    calls,
                    key,
                    {
                        "tool_name": payload.get("name"),
                        "arguments": args,
                        "call_observed_at": time,
                    },
                )
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                if "output" not in payload:
                    raise ValueError("native_tool_result_required")
                text, complete = _text(payload["output"])
                put(
                    results,
                    key,
                    {
                        "result": text,
                        "result_complete": complete,
                        "is_error": False,
                        "result_observed_at": time,
                    },
                )
        elif provider == "claude":
            if row.get("sessionId") or row.get("session_id"):
                sessions.add(str(row.get("sessionId") or row["session_id"]))
            message = row.get("message", {})
            if not isinstance(message, dict) or not isinstance(
                message.get("content"), list
            ):
                continue
            role = message.get("role")
            for block in message["content"]:
                if not isinstance(block, dict):
                    continue
                if (
                    row.get("type") == "assistant"
                    and role == "assistant"
                    and block.get("type") == "tool_use"
                ):
                    put(
                        calls,
                        block.get("id"),
                        {
                            "tool_name": block.get("name"),
                            "arguments": block.get("input", {}),
                            "call_observed_at": time,
                        },
                    )
                elif (
                    row.get("type") == "user"
                    and role == "user"
                    and block.get("type") == "tool_result"
                ):
                    if "content" not in block:
                        raise ValueError("native_tool_result_required")
                    text, complete = _text(block["content"])
                    put(
                        results,
                        block.get("tool_use_id"),
                        {
                            "result": text,
                            "result_complete": complete,
                            "is_error": bool(block.get("is_error")),
                            "result_observed_at": time,
                        },
                    )
        elif provider == "gemini":
            if row.get("sessionId") or row.get("session_id"):
                sessions.add(str(row.get("sessionId") or row["session_id"]))
            if row.get("type") not in {"gemini", "model", "assistant"}:
                continue
            for call in row.get("toolCalls", []):
                if not isinstance(call, dict):
                    raise ValueError("native_tool_pair_invalid")
                key = call.get("id")
                status = call.get("status")
                if call.get("name") and "args" in call:
                    put(
                        calls,
                        key,
                        {
                            "tool_name": call["name"],
                            "arguments": call["args"],
                            "call_observed_at": time,
                        },
                    )
                if status in {"pending", "validating", "executing", "running"}:
                    continue
                if status not in {
                    "success",
                    "completed",
                    "error",
                    "failed",
                    "cancelled",
                }:
                    raise ValueError("native_tool_result_incomplete")
                field = next(
                    (f for f in ("result", "resultDisplay") if f in call), None
                )
                if field is None:
                    raise ValueError("native_tool_result_required")
                text, complete = _text(call[field])
                if key not in calls:
                    raise ValueError("native_tool_call_required")
                put(
                    results,
                    key,
                    {
                        "result": text,
                        "result_complete": complete and not bool(call.get("truncated")),
                        "is_error": status in {"error", "failed", "cancelled"},
                        "result_observed_at": time,
                    },
                )
        elif provider == "antigravity":
            identity = (
                row.get("conversationId")
                or row.get("conversation_id")
                or row.get("session_id")
            )
            if identity:
                sessions.add(str(identity))
            for call in row.get("tool_calls", []):
                if not isinstance(call, dict):
                    raise ValueError("native_tool_pair_invalid")
                put(
                    calls,
                    call.get("id"),
                    {
                        "tool_name": call.get("name"),
                        "arguments": call.get("args", {}),
                        "call_observed_at": time,
                    },
                )
            if row.get("type") == "GENERIC" and row.get("tool_call_id"):
                if "content" not in row:
                    raise ValueError("native_tool_result_required")
                text, complete = _text(row["content"])
                put(
                    results,
                    row["tool_call_id"],
                    {
                        "result": text,
                        "result_complete": complete,
                        "is_error": row.get("status")
                        in {"ERROR", "FAILED", "CANCELLED"},
                        "result_observed_at": time,
                    },
                )
        else:
            params = row.get("params", {})
            if not isinstance(params, dict):
                continue
            if params.get("sessionId") or params.get("session_id"):
                sessions.add(str(params.get("sessionId") or params["session_id"]))
            update = params.get("update", {})
            if not isinstance(update, dict):
                continue
            kind = update.get("sessionUpdate")
            key = update.get("toolCallId")
            if kind == "tool_call":
                meta = update.get("_meta", {})
                if not isinstance(meta, dict) or not isinstance(
                    meta.get("x.ai/tool"), dict
                ):
                    raise ValueError("native_tool_call_required")
                meta = meta["x.ai/tool"]
                put(
                    calls,
                    key,
                    {
                        "tool_name": meta.get("name"),
                        "arguments": update.get("rawInput", {}),
                        "call_observed_at": time,
                    },
                )
            elif kind == "tool_call_update" and update.get("status") in {
                "completed",
                "error",
                "failed",
                "cancelled",
            }:
                output = update.get("rawOutput")
                if not isinstance(output, dict):
                    raise ValueError("native_tool_result_required")
                text, complete = _text(
                    output.get("output_for_prompt", output.get("output", output))
                )
                put(
                    results,
                    key,
                    {
                        "result": text,
                        "result_complete": complete
                        and not bool(output.get("truncated")),
                        "is_error": update.get("status")
                        in {"error", "failed", "cancelled"}
                        or output.get("exit_code", 0) != 0,
                        "result_observed_at": time,
                    },
                )
    if len(sessions) != 1 or calls.keys() != results.keys():
        raise ValueError("native_tool_source_unpaired_or_ambiguous")
    session = next(iter(sessions))
    return [
        build(
            provider=provider, session_id=session, call_id=key, **call, **results[key]
        )
        for key, call in calls.items()
    ]


def evidence_records(provider, source_path, *, project):
    """기존 source bundle 경로로 보존할 record를 만든다. 원문은 recall body에 넣지 않는다."""
    from .transcript_model import ToolEvidenceSummaryRecord
    from .transcript_parsers.evidence import _classify_tool_evidence

    records = []
    for index, receipt in enumerate(extract(provider, read_source(source_path))):
        try:
            args = (
                json.loads(receipt["arguments_text"])
                if receipt["arguments_complete"]
                else {}
            )
        except ValueError:
            args = {}
        command = (
            str(args.get("cmd") or args.get("command") or "")
            if isinstance(args, dict)
            else ""
        )
        classified = None
        if receipt["arguments_complete"] and receipt["result_complete"]:
            classified = _classify_tool_evidence(
                receipt["tool_name"],
                command,
                receipt["result_text"],
                receipt["is_error"],
            )
        category, outcome, summary = classified or (
            "native_observation",
            "info",
            "원래 도구 응답은 별도 관측 자료에 보존됨",
        )
        records.append(
            ToolEvidenceSummaryRecord(
                session_id_hash=receipt["session_id_hash"],
                provider=provider,
                project=project,
                category=category,
                outcome=outcome,
                tool_name=receipt["tool_name"],
                command_summary="",
                redacted_summary=summary,
                observed_at=receipt["result_observed_at"],
                evidence_index=index,
                source_event_index=index,
                source_event_id_hash=receipt["source_event_id_hash"],
                source_content_hash=receipt["source_content_hash"],
                extractor_version=EXTRACTOR,
                native_receipt_json=_json(receipt),
            )
        )
    return records

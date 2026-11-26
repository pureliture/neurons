from __future__ import annotations

import json
from pathlib import Path

from agent_knowledge.llm_brain_core.graph_trigger_cli import run_graph_trigger


def test_graph_trigger_dry_run_reports_bounded_plan_without_child_call(tmp_path):
    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        limit=17,
        execute=False,
        graph_main=_exploding_child,
    )

    rendered = json.dumps(report, sort_keys=True)
    assert report["status"] == "dry_run"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False
    assert report["plan"]["bounded"] is True
    assert report["plan"]["limit"] == 17
    assert report["plan"]["child_command"] == "couchdb-graph-project"
    assert str(tmp_path) not in rendered
    assert report["raw_paths_printed"] is False


def test_graph_trigger_execute_calls_graph_project_with_lock_and_outputs_redacted_plan(tmp_path):
    calls: list[list[str]] = []

    def _child(argv):  # type: ignore[no-untyped-def]
        calls.append(list(argv or []))
        print(
            json.dumps(
                {
                    "schema_version": "child.graph",
                    "status": "ok",
                    "run_ref": "sha256:" + "a" * 12,
                    "projection": {"projected": 3, "duplicates": 0, "failed": 0},
                }
            )
        )
        return 0

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        limit=3,
        project="neurons",
        provider="codex",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "ok"
    assert report["mutation_performed"] is True
    assert report["network_used"] is True
    assert report["step"]["report"]["run_ref"] == "sha256:" + "a" * 12
    assert len(calls) == 1
    argv = calls[0]
    assert "--runtime-dir" in argv
    assert "--enable-graph" in argv
    # hot path is episode-only by default: no per-session Graphiti entity extraction.
    assert "--extract-entities" not in argv
    assert ["--limit", "3"] == argv[2:4]
    assert "neurons" not in json.dumps(report, sort_keys=True)


def test_graph_trigger_extract_entities_opt_in_adds_flag(tmp_path):
    calls: list[list[str]] = []

    def _child(argv):  # type: ignore[no-untyped-def]
        calls.append(list(argv or []))
        print(json.dumps({"schema_version": "child.graph", "status": "ok"}))
        return 0

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        extract_entities=True,
        graph_main=_child,
    )

    assert report["status"] == "ok"
    argv = calls[0]
    # debug/manual opt-in restores per-session entity extraction.
    assert "--extract-entities" in argv


def test_graph_trigger_reextract_flag_forwarded_in_both_states(tmp_path):
    calls: list[list[str]] = []

    def _child(argv):  # type: ignore[no-untyped-def]
        calls.append(list(argv or []))
        print(json.dumps({"schema_version": "child.graph", "status": "ok"}))
        return 0

    for reextract in (True, False):
        report = run_graph_trigger(
            ledger_path=tmp_path / "ledger.sqlite3",
            runtime_dir=tmp_path / "runtime",
            execute=True,
            extract_entities=True,
            reextract_entities=reextract,
            graph_main=_child,
        )
        assert report["status"] == "ok"
        assert ("--reextract-entities" in calls[-1]) is reextract


def test_graph_trigger_already_running_is_not_counted_as_mutation(tmp_path):
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(json.dumps({"schema_version": "child.graph", "status": "already_running"}))
        return 0

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "already_running"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_graph_trigger_failed_child_returns_failed_status(tmp_path):
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(json.dumps({"schema_version": "child.graph", "status": "failed"}))
        return 1

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "failed"
    assert report["step"]["exit_code"] == 1


def test_graph_trigger_partial_projection_then_failure_still_reports_side_effects(tmp_path):
    # 일부 projected 후 child가 실패(status=failed, exit 1)해도 그래프 기록 신호가
    # 있으면 mutation/network를 거짓 음성으로 숨기지 않는다.
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(
            json.dumps(
                {
                    "schema_version": "child.graph",
                    "status": "failed",
                    "projection": {"projected": 1, "duplicates": 0, "failed": 2},
                }
            )
        )
        return 1

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "failed"
    assert report["mutation_performed"] is True
    assert report["network_used"] is True


def test_graph_trigger_ok_with_zero_projection_reports_no_mutation(tmp_path):
    # status=ok이지만 projected=0이면 mutation을 거짓 양성으로 보고하지 않는다.
    # duplicates만 있으면 그래프 백엔드에 접촉했으므로 network는 True다.
    def _child(argv):  # type: ignore[no-untyped-def]
        _ = argv
        print(
            json.dumps(
                {
                    "schema_version": "child.graph",
                    "status": "ok",
                    "projection": {"projected": 0, "duplicates": 4, "failed": 0},
                }
            )
        )
        return 0

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_child,
    )

    assert report["status"] == "ok"
    assert report["mutation_performed"] is False
    assert report["network_used"] is True


def _exploding_child(argv):  # type: ignore[no-untyped-def]
    _ = argv
    raise AssertionError("child command should not be called")


def test_graph_trigger_surfaces_sanitized_child_stderr_failure_codes_without_raw_message(
    tmp_path, monkeypatch
):
    import httpx
    import openai
    from agent_knowledge.couchdb_source.couchdb_http_store import CouchDBError
    from agent_knowledge.llm_brain_core import couchdb_projection_cli

    def _failing_store(**_kwargs):
        raise CouchDBError(
            "POST /transcript_source/_find failed: /Users/alice/secret TOKEN=supersecret sk-"
            + "1234567890abcdef",
            status_code=503,
        )

    monkeypatch.setattr(couchdb_projection_cli, "_build_source_store", _failing_store)

    report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        couchdb_url="http://127.0.0.1:5984",
    )

    rendered = json.dumps(report, sort_keys=True)
    assert report["status"] == "failed"
    assert report["step"]["exit_code"] == 1
    assert report["step"]["stderr_present"] is True
    assert report["step"]["report"]["status"] == "failed"
    assert report["step"]["report"]["error_class"] == "CouchDBError"
    assert report["step"]["report"]["http_status"] == 503
    assert report["step"]["report"]["reason_code"] == "CouchDBError:http_503"
    assert "/Users/alice" not in rendered
    assert "supersecret" not in rendered
    assert ("sk-" + "1234567890abcdef") not in rendered
    assert "message" not in report["step"]["report"]

    # Direct uncaught child exception (e.g., BadRequestError) is also caught fail-closed
    # with only sanitized HTTP status and provider error code.
    req = httpx.Request("POST", "https://llm.example.invalid/v1/chat/completions")
    resp = httpx.Response(
        400,
        request=req,
        json={
            "error": {
                "message": "Prompt echo /Users/private Bearer secret_token_value_12345",
                "type": "invalid_request_error",
                "code": "invalid_json_schema",
            }
        },
    )

    def _crashing_child(_argv):
        raise openai.BadRequestError(
            "Error code: 400 - /Users/private sk-" + "secretabcdef123456",
            response=resp,
            body=resp.json(),
        )

    crash_report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_crashing_child,
    )
    crash_rendered = json.dumps(crash_report, sort_keys=True)
    assert crash_report["status"] == "failed"
    assert crash_report["step"]["exit_code"] == 1
    assert crash_report["step"]["error_class"] == "BadRequestError"
    assert crash_report["step"]["http_status"] == 400
    assert crash_report["step"]["error_code"] == "invalid_json_schema"
    assert crash_report["step"]["reason_code"] == "BadRequestError:http_400:invalid_json_schema"
    assert "/Users/private" not in crash_rendered
    assert "secret_token_value_12345" not in crash_rendered
    assert ("sk-" + "secretabcdef123456") not in crash_rendered


def test_error_code_sanitizer_blocks_secrets_paths_cycles_and_untrusted_stderr():
    from agent_knowledge.llm_brain_core.graph_trigger_cli import (
        SafeChildReportStatus,
        parse_sanitized_child_error_report,
    )
    from agent_knowledge.public_safe_util import (
        SafeErrorClass,
        SafeErrorCode,
        SafeErrorType,
        SafeReasonCode,
        classify_safe_error_class,
        classify_safe_error_code,
        classify_safe_error_type,
        classify_safe_reason_code,
        extract_safe_error_diagnostics,
    )

    assert classify_safe_error_code(None) == ""
    assert classify_safe_error_code("") == ""
    assert classify_safe_error_code("   ") == ""
    assert classify_safe_error_type(None) == ""
    assert classify_safe_error_type("") == ""
    assert classify_safe_reason_code(None) == ""
    assert classify_safe_reason_code("") == ""

    for forbidden in (
        "/Users/alice/secret",
        "postgres://u:p@host:5432/db",
        "Bearer abcdefghijklmnop",
        "Basic dXNlcjpwYXNz",
        "sk-" + "1234567890abcdef",
        "ghp_" + "1234567890abcdefghij",
        "eyJ" + "hbGciOiJIUzI1NiJ9.payload",
        "api_key=supersecret",
        "0123456789abcdef0123456789abcdef",
        "a1b2c3d4e5f6g7h8i9j0k1l2",
        "invalid request with spaces",
        "run:7cba8870d6b6",
        "sha256:0123456789abcdef",
    ):
        assert classify_safe_error_code(forbidden) == SafeErrorCode.UNKNOWN_ERROR_CODE
        assert classify_safe_error_type(forbidden) == SafeErrorType.UNKNOWN_ERROR_TYPE
        assert classify_safe_reason_code(forbidden) == SafeReasonCode.UNKNOWN_REASON_CODE

    assert classify_safe_error_code("context_length_exceeded") == "context_length_exceeded"
    assert classify_safe_error_code("invalid_json_schema") == "invalid_json_schema"
    assert (
        classify_safe_error_code("Neo.ClientError.Statement.SyntaxError")
        == "Neo.ClientError.Statement.SyntaxError"
    )
    assert classify_safe_error_type("invalid_request_error") == "invalid_request_error"
    assert classify_safe_reason_code("graph_source_snapshot_changed") == "graph_source_snapshot_changed"
    assert classify_safe_error_class("SourceRevisionResolutionError") == "SourceRevisionResolutionError"

    # Cyclic __cause__ chain terminates safely without reading exception message text.
    exc_a = RuntimeError("/Users/secret TOKEN=leak_a")
    exc_b = ValueError("/Users/secret TOKEN=leak_b")
    exc_a.__cause__ = exc_b
    exc_b.__cause__ = exc_a
    diag = extract_safe_error_diagnostics(exc_a)
    assert diag == {"error_class": "RuntimeError", "reason_code": "RuntimeError"}

    # Untrusted stderr lines or poisoned JSON fields never leak through.
    assert (
        parse_sanitized_child_error_report("Traceback: /Users/secret sk-" + "1234567890abcdef")
        == {}
    )
    assert (
        parse_sanitized_child_error_report(
            json.dumps({"schema_version": "untrusted.v1", "error_class": "ValueError"})
        )
        == {}
    )
    poisoned_stderr = "\n".join(
        [
            "ERROR root: /Users/alice/private Bearer topsecret_token_9999",
            json.dumps(
                {
                    "schema_version": "llm_brain_couchdb_graph_projection.v1",
                    "status": "alice.secret.status",
                    "error_class": "BadRequestError",
                    "reason_code": "sk-" + "poisoned1234567890",
                    "error_code": "/Users/alice/leak",
                    "error_type": "invalid_request_error",
                    "http_status": 400,
                    "message": "RAW PROMPT LEAK /Users/alice",
                    "extra_secret": "postgres://user:pw@db/ledger",
                }
            ),
        ]
    )
    recovered = parse_sanitized_child_error_report(poisoned_stderr)
    assert recovered == {
        "schema_version": "llm_brain_couchdb_graph_projection.v1",
        "status": SafeChildReportStatus.FAILED,
        "error_class": "BadRequestError",
        "reason_code": "BadRequestError:http_400:invalid_request_error",
        "error_code": SafeErrorCode.UNKNOWN_ERROR_CODE,
        "error_type": "invalid_request_error",
        "http_status": 400,
        "raw_paths_printed": False,
    }


def test_closed_allowlist_replaces_regex_passing_secret_and_personal_tokens_with_generic(tmp_path):
    """Counterexample test: tokens crafted to pass regex/prefix denylists are replaced with generic enums."""
    import httpx
    import openai
    import sys
    from agent_knowledge.llm_brain_core.graph_trigger_cli import parse_sanitized_child_error_report
    from agent_knowledge.public_safe_util import (
        SafeErrorClass,
        SafeErrorCode,
        SafeErrorType,
        SafeReasonCode,
        classify_safe_error_class,
        classify_safe_error_code,
        classify_safe_error_type,
        classify_safe_reason_code,
        extract_safe_error_diagnostics,
    )

    # Every one of these adversarial tokens matches `^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`,
    # avoids denylisted prefixes (`sk-`, `ghp_`, `Bearer`, etc.), and includes separators
    # so naive entropy/regex checks would let intentional secrets or personal text pass.
    adversarial_tokens = (
        "alice.kim:diabetes-record",
        "project_secret:codename-apollo",
        "cred.part1-AbCdEf_9876",
        "user-hong-gildong-010-1234-5678",
    )
    forbidden_substrings = ("alice", "diabetes", "apollo", "AbCdEf", "gildong", "010-1234")

    for token in adversarial_tokens:
        assert classify_safe_error_code(token) == SafeErrorCode.UNKNOWN_ERROR_CODE
        assert classify_safe_error_type(token) == SafeErrorType.UNKNOWN_ERROR_TYPE
        assert classify_safe_reason_code(token) == SafeReasonCode.UNKNOWN_REASON_CODE
        assert classify_safe_error_class(token) == SafeErrorClass.UNCLASSIFIED_ERROR

    # 1) Adversarial upstream HTTP body where both `code` and `type` carry personal/secret text.
    req = httpx.Request("POST", "https://llm.example.invalid/v1/chat/completions")
    resp = httpx.Response(
        400,
        request=req,
        json={
            "error": {
                "message": "raw prompt",
                "code": "alice.kim:diabetes-record",
                "type": "project_secret:codename-apollo",
            }
        },
    )
    exc_http = openai.BadRequestError("400", response=resp, body=resp.json())
    diag_http = extract_safe_error_diagnostics(exc_http)
    assert diag_http == {
        "error_class": "BadRequestError",
        "http_status": 400,
        "error_code": "unknown_error_code",
        "error_type": "unknown_error_type",
        "reason_code": "BadRequestError:http_400:unknown_error_code",
    }

    # 2) Dynamic custom exception class + arbitrary `reason_code` attribute carrying personal data.
    DynamicLeakError = type("AliceKimDiabetesLeakError", (Exception,), {})
    exc_custom = DynamicLeakError("sensitive message")
    exc_custom.reason_code = "user-hong-gildong-010-1234-5678"  # type: ignore[attr-defined]
    diag_custom = extract_safe_error_diagnostics(exc_custom)
    assert diag_custom == {
        "error_class": "UnclassifiedError",
        "reason_code": "unknown_reason_code",
    }

    # 3) Child stderr JSON carrying regex-passing secret tokens in every field + end-to-end trigger.
    crafted_stderr = json.dumps(
        {
            "schema_version": "llm_brain_couchdb_graph_projection.v1",
            "status": "cred.part1-AbCdEf_9876",
            "error_class": "AliceKimDiabetesLeakError",
            "reason_code": "BadRequestError:http_400:alice.kim:diabetes-record",
            "error_code": "alice.kim:diabetes-record",
            "error_type": "project_secret:codename-apollo",
            "http_status": 400,
        }
    )
    recovered = parse_sanitized_child_error_report(crafted_stderr)
    assert recovered == {
        "schema_version": "llm_brain_couchdb_graph_projection.v1",
        "status": "failed",
        "error_class": "UnclassifiedError",
        "reason_code": "UnclassifiedError:http_400:unknown_error_code",
        "error_code": "unknown_error_code",
        "error_type": "unknown_error_type",
        "http_status": 400,
        "raw_paths_printed": False,
    }

    def _adversarial_child(_argv):
        sys.stderr.write(crafted_stderr + "\n")
        return 1

    trigger_report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_adversarial_child,
    )
    serialized = json.dumps(trigger_report, sort_keys=True)
    for leak in forbidden_substrings:
        assert leak not in serialized
    assert trigger_report["step"]["error_class"] == "UnclassifiedError"
    assert trigger_report["step"]["error_code"] == "unknown_error_code"
    assert trigger_report["step"]["error_type"] == "unknown_error_type"
    assert trigger_report["step"]["reason_code"] == "UnclassifiedError:http_400:unknown_error_code"


def test_closed_allowlist_error_param_and_step_disambiguate_null_code_http400_without_leaks(tmp_path):
    """Closed SafeErrorParam + SafeErrorStep disambiguate code=None HTTP 400s without raw leaks."""
    import httpx
    import openai
    import sys
    from agent_knowledge.llm_brain_core.graph_trigger_cli import parse_sanitized_child_error_report
    from agent_knowledge.public_safe_util import (
        SafeErrorCode,
        SafeErrorParam,
        SafeErrorStep,
        classify_safe_error_code,
        classify_safe_error_param,
        classify_safe_error_step,
        ensure_public_safe,
        extract_safe_error_diagnostics,
    )

    assert classify_safe_error_param(None) == ""
    assert classify_safe_error_param("") == ""
    assert classify_safe_error_param("   ") == ""
    assert classify_safe_error_step(None) == ""
    assert classify_safe_error_step("") == ""
    assert classify_safe_error_step("   ") == ""

    assert classify_safe_error_param("response_format") == SafeErrorParam.RESPONSE_FORMAT
    assert (
        classify_safe_error_param("response_format.json_schema.schema")
        == SafeErrorParam.RESPONSE_FORMAT_JSON_SCHEMA_SCHEMA
    )
    assert classify_safe_error_param("messages[1].content") == SafeErrorParam.MESSAGES_ITEM_CONTENT
    assert classify_safe_error_param("messages.0.role") == SafeErrorParam.MESSAGES_ITEM_ROLE
    assert classify_safe_error_param("tools[2].function.parameters") == SafeErrorParam.TOOLS_ITEM_FUNCTION_PARAMETERS
    assert classify_safe_error_param("x-opencode-session") == SafeErrorParam.X_OPENCODE_SESSION
    assert classify_safe_error_code("MissingSessionID") == SafeErrorCode.MISSING_SESSION_ID

    assert classify_safe_error_step("extract_nodes.extract_json") == SafeErrorStep.EXTRACT_NODES_JSON
    assert classify_safe_error_step("dedupe_nodes.nodes") == SafeErrorStep.DEDUPE_NODES_NODES
    assert classify_safe_error_step("extract_edges.edge") == SafeErrorStep.EXTRACT_EDGES_EDGE
    assert (
        classify_safe_error_step("extract_nodes.extract_summaries_batch")
        == SafeErrorStep.EXTRACT_NODES_SUMMARIES_BATCH
    )

    for poisoned in (
        "messages[0]:/Users/alice/secret",
        "response_format:Bearer sk-" + "secret123456",
        "alice.kim:diabetes-record",
        "project_secret:codename-apollo",
        "extract_edges.edge:/Users/bob/raw_transcript",
    ):
        assert classify_safe_error_param(poisoned) == SafeErrorParam.UNKNOWN_ERROR_PARAM
        assert classify_safe_error_step(poisoned) == SafeErrorStep.UNKNOWN_ERROR_STEP

    req = httpx.Request("POST", "https://llm.example.invalid/v1/chat/completions")
    resp = httpx.Response(
        400,
        request=req,
        json={
            "error": {
                "message": "Invalid schema at /Users/alice/secret Bearer sk-leak999",
                "type": "invalid_request_error",
                "param": "response_format.json_schema.schema",
                "code": None,
            }
        },
    )
    exc = openai.BadRequestError("400 schema failure", response=resp, body=resp.json())
    exc.error_step = "extract_edges.edge"  # type: ignore[attr-defined]
    diag = extract_safe_error_diagnostics(exc)
    ensure_public_safe(diag, field="null_code_400_diag")
    assert diag == {
        "error_class": "BadRequestError",
        "http_status": 400,
        "error_type": "invalid_request_error",
        "error_param": "response_format.json_schema.schema",
        "error_step": "extract_edges.edge",
        "reason_code": "BadRequestError:http_400:invalid_request_error",
    }

    child_stderr = json.dumps(
        {
            "schema_version": "llm_brain_couchdb_graph_projection.v1",
            "status": "failed",
            **diag,
            "message": "/Users/alice/secret Bearer sk-leak999",
        }
    )
    parsed = parse_sanitized_child_error_report(child_stderr)
    ensure_public_safe(parsed, field="parsed_child_400")
    assert parsed["error_param"] == "response_format.json_schema.schema"
    assert parsed["error_step"] == "extract_edges.edge"

    poisoned_stderr = json.dumps(
        {
            "schema_version": "llm_brain_couchdb_graph_projection.v1",
            "status": "failed",
            "error_class": "BadRequestError",
            "http_status": 400,
            "error_type": "invalid_request_error",
            "error_param": "messages[0]:alice.kim:diabetes-record",
            "error_step": "extract_edges:project_secret:apollo",
        }
    )

    def _poisoned_param_step_child(_argv):
        sys.stderr.write(poisoned_stderr + "\n")
        return 1

    trigger_report = run_graph_trigger(
        ledger_path=tmp_path / "ledger.sqlite3",
        runtime_dir=tmp_path / "runtime",
        execute=True,
        graph_main=_poisoned_param_step_child,
    )
    ensure_public_safe(trigger_report, field="trigger_report_param_step")
    rendered = json.dumps(trigger_report, sort_keys=True)
    assert "alice" not in rendered
    assert "diabetes" not in rendered
    assert "apollo" not in rendered
    assert trigger_report["step"]["error_param"] == "unknown_error_param"
    assert trigger_report["step"]["error_step"] == "unknown_error_step"

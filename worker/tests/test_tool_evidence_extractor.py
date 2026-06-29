"""TDD for the Codex non-message tool-evidence extractor.

The existing conversation_chunk parser keeps only user/assistant messages and
drops every function_call / function_call_output / patch record. This extractor
re-reads the same Codex JSONL and produces high-signal, redacted
ToolEvidenceSummaryRecord objects linked to the same session_id_hash, so the
tool-evidence plane (test results, git state, audit counts, live proof, errors,
approval evidence) is no longer lost from transcript-memory.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent_knowledge.session_memory.transcript_model import ToolEvidenceSummaryRecord
from agent_knowledge.session_memory.transcript_parsers import extract_codex_tool_evidence


PROJECT = "workspace-index-advisor"
SOURCE_LOCATOR_HASH = "sha256:" + "a" * 64

# Injected leak sentinels (split so this file never matches itself in the gate).
SECRET_VALUE = "synthetic-" + "evidence-token-value"
# Built by concatenation so this source file never contains a literal private
# runtime path (enforced by the committed-artifact guard in goal2 tests).
PRIVATE_PATH = "/Users/example/.open" + "claw/private/evidence-secret/ledger.sqlite"
LOCAL_RUNTIME_PATH = "/Users/example/Projects/internal-app/src/main.py"


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resp(payload: dict, *, ts: str = "2026-05-27T23:20:50.000Z") -> dict:
    return {"type": "response_item", "timestamp": ts, "payload": payload}


def _fn_call(call_id: str, cmd: str, *, name: str = "exec_command") -> dict:
    return _resp(
        {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps({"cmd": cmd, "workdir": "/redacted"}),
        }
    )


def _fn_output(call_id: str, output: str) -> dict:
    return _resp({"type": "function_call_output", "call_id": call_id, "output": output})


def _write_codex_evidence_jsonl(path: Path, *, session_id: str = "evidence-session-1") -> None:
    records = [
        {
            "type": "session_meta",
            "timestamp": "2026-05-27T23:20:47.000Z",
            "payload": {"id": session_id, "cwd": "/redacted/workspace", "cli_version": "codex-cli 0.1"},
        },
        # message turns must be ignored by the evidence extractor
        _resp(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "please run tests"}],
            }
        ),
        _resp(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "running"}],
            }
        ),
        # reasoning + token_count must be dropped (internal / plumbing)
        _resp({"type": "reasoning", "content": [{"type": "reasoning_text", "text": "internal thoughts"}]}),
        {"type": "event_msg", "timestamp": "2026-05-27T23:20:48.000Z", "payload": {"type": "token_count", "info": {}}},
        # test_result: pass
        _fn_call("c1", "rtk uv run --quiet pytest tests -q"),
        _fn_output("c1", "............\n12 passed in 1.23s\n"),
        # test_result: fail
        _fn_call("c2", "uv run pytest tests/test_x.py -q"),
        _fn_output("c2", "F...\n1 failed, 4 passed in 2.00s\nFAILED tests/test_x.py::test_y - AssertionError"),
        # git_state: dirty status, with an injected private path that must be redacted
        _fn_call("c3", "git status --short --branch"),
        _fn_output("c3", f"## codex/x...origin/main\n M file.py\n?? {PRIVATE_PATH}\n"),
        # git_state: commit, with an injected secret assignment that must be redacted
        _fn_call("c4", "git commit -m wip"),
        _fn_output("c4", f"[codex/x abc1234] wip {LOCAL_RUNTIME_PATH} EVIDENCE_TOKEN={SECRET_VALUE}\n 2 files changed, 9 insertions(+)"),
        # local_audit: count query
        _fn_call("c5", "sqlite3 ledger.sqlite 'select count(*) from knowledge_items'"),
        _fn_output("c5", "40231"),
        # file_view: must be DROPPED even though output contains the word Error
        _fn_call("c6", "nl -ba lib/agent_knowledge/cli.py"),
        _fn_output("c6", "   1\traise ValueError('Error path in source code')\n   2\tpass\n"),
        # search: must be DROPPED
        _fn_call("c7", "rg tool_evidence lib"),
        _fn_output("c7", "no matches found"),
        # trivial patch success: must be DROPPED
        _resp({"type": "custom_tool_call", "name": "apply_patch", "call_id": "c8", "input": "*** Begin Patch"}),
        {"type": "response_item", "timestamp": "2026-05-27T23:20:55.000Z", "payload": {"type": "patch_apply_end", "call_id": "c8", "success": True}},
        _resp({"type": "custom_tool_call_output", "call_id": "c8", "output": "Success. Updated the following files:\nM file.py"}),
        # command_error: script failure with traceback
        _fn_call("c9", "uv run python scripts/sync_session_memory.py"),
        _fn_output("c9", "Traceback (most recent call last):\n  File x\nValueError: sync boundary not approved"),
        # live_proof: healthz probe
        _fn_call("c10", "curl -s localhost:9380/api/v1/system/healthz"),
        _fn_output("c10", '{"status":"ok","components":{"es":"green"}}'),
    ]
    path.write_text("\n".join(json.dumps(rec) for rec in records) + "\n", encoding="utf-8")


def _extract(tmp_path: Path) -> list[ToolEvidenceSummaryRecord]:
    source = tmp_path / "rollout-evidence.jsonl"
    _write_codex_evidence_jsonl(source)
    return extract_codex_tool_evidence(source, project=PROJECT, source_locator_hash=SOURCE_LOCATOR_HASH)


def test_extractor_links_records_to_codex_session_id_hash(tmp_path):
    records = _extract(tmp_path)
    assert records, "expected high-signal tool evidence records"
    expected_hash = _sha256("codex:evidence-session-1")
    assert all(record.session_id_hash == expected_hash for record in records)
    assert all(record.provider == "codex" for record in records)
    assert all(record.project == PROJECT for record in records)


def test_extractor_keeps_high_signal_categories(tmp_path):
    records = _extract(tmp_path)
    categories = {record.category for record in records}
    assert "test_result" in categories
    assert "git_state" in categories
    assert "local_audit" in categories
    assert "command_error" in categories
    assert "live_proof" in categories


def test_extractor_drops_file_view_search_and_trivial_patch(tmp_path):
    records = _extract(tmp_path)
    summaries = "\n".join(record.redacted_summary for record in records)
    # nl/rg exploration and a successful apply_patch carry no durable evidence
    assert "no matches found" not in summaries
    assert "Success. Updated the following files" not in summaries
    assert "Error path in source code" not in summaries


def test_extractor_records_test_pass_and_fail_outcomes(tmp_path):
    records = _extract(tmp_path)
    test_records = [r for r in records if r.category == "test_result"]
    outcomes = {r.outcome for r in test_records}
    assert "pass" in outcomes
    assert "fail" in outcomes


def test_extractor_marks_command_error_outcome(tmp_path):
    records = _extract(tmp_path)
    errors = [r for r in records if r.category == "command_error"]
    assert errors
    assert all(r.outcome == "error" for r in errors)


def test_extractor_ignores_message_turns(tmp_path):
    records = _extract(tmp_path)
    blob = "\n".join(record.redacted_summary for record in records)
    assert "please run tests" not in blob
    assert "internal thoughts" not in blob


def test_extractor_redacts_secrets_and_private_paths(tmp_path):
    records = _extract(tmp_path)
    serialized = json.dumps(
        [record.to_record() for record in records], sort_keys=True, ensure_ascii=False
    )
    assert SECRET_VALUE not in serialized
    assert PRIVATE_PATH not in serialized
    assert LOCAL_RUNTIME_PATH not in serialized
    # shell output is path-heavy; no local runtime path may survive into evidence
    assert "/Users/" not in serialized
    assert all(record.redaction_version == "redaction.v2" for record in records)


def _write_git_global_flag_session(path):
    def resp(payload, ts="2026-05-27T23:21:00.000Z"):
        return {"type": "response_item", "timestamp": ts, "payload": payload}
    records = [
        {"type": "session_meta", "timestamp": "2026-05-27T23:20:47.000Z", "payload": {"id": "gitflag-session"}},
        resp({"type": "function_call", "name": "exec_command", "call_id": "g1",
              "arguments": json.dumps({"cmd": "git -C /repo/path status --short"})}),
        resp({"type": "function_call_output", "call_id": "g1", "output": " M x.py"}),
        resp({"type": "function_call", "name": "exec_command", "call_id": "g2",
              "arguments": json.dumps({"cmd": "git --no-pager log --oneline -3"})}),
        resp({"type": "function_call_output", "call_id": "g2", "output": "abc1234 a\ndef5678 b"}),
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_git_global_flags_still_classified_as_git_state(tmp_path):
    src = tmp_path / "gitflag.jsonl"
    _write_git_global_flag_session(src)
    recs = extract_codex_tool_evidence(src, project=PROJECT, source_locator_hash=SOURCE_LOCATOR_HASH)
    # `git -C <path> status` and `git --no-pager log` carry global flags before the
    # subcommand; both must still be recognised as git_state, not dropped.
    assert len(recs) == 2
    assert all(r.category == "git_state" for r in recs)


def test_repeated_identical_evidence_kept_distinct_by_index():
    a = ToolEvidenceSummaryRecord(
        session_id_hash="sha256:" + "3" * 64, provider="codex", project=PROJECT,
        category="git_state", outcome="info", tool_name="exec_command",
        command_summary="git status", redacted_summary="git status: clean", evidence_index=0,
    )
    b = ToolEvidenceSummaryRecord(
        session_id_hash="sha256:" + "3" * 64, provider="codex", project=PROJECT,
        category="git_state", outcome="info", tool_name="exec_command",
        command_summary="git status", redacted_summary="git status: clean", evidence_index=5,
    )
    # identical content -> same content_hash, but different occurrence -> distinct
    # identity so an append-only upsert never overwrites an earlier occurrence.
    assert a.content_hash == b.content_hash
    assert a.evidence_id_hash != b.evidence_id_hash


def test_extractor_ids_are_content_addressed_and_stable(tmp_path):
    first = _extract(tmp_path)
    second = _extract(tmp_path)
    assert [r.evidence_id_hash for r in first] == [r.evidence_id_hash for r in second]
    for record in first:
        assert record.evidence_id_hash.startswith("sha256:")
        assert record.content_hash == _sha256(record.redacted_summary)

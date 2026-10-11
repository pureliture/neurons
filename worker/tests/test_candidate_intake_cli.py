from __future__ import annotations

import json

import pytest

from agent_knowledge.session_memory.candidate_intake_cli import (
    CandidateIntakeConfigError,
    CandidateIntakeReport,
    CandidateIntakeSettings,
    already_processed,

    checkpoint_source_hash,
    load_checkpoint,
    main,
    record_processed,
    run_candidate_intake,
    save_checkpoint,
)


PROJECT = "neurons"


def _chunk(content_hash: str = "sha256:" + "a" * 64) -> dict:
    return {
        "content": "The team decided to keep PostgreSQL as the authority store.",
        "content_hash": content_hash,
        "metadata": {
            "knowledge_id": "session-1",
            "project": PROJECT,
            "provider": "codex",
            "redaction_version": "v2",
        },
    }


def _completion(messages):
    return json.dumps(
        [
            {
                "card_type": "decision",
                "title": "Authority store",
                "statement": "PostgreSQL is the authority store.",
                "typed_payload": {
                    "decision": "Use PostgreSQL",
                    "rationale": "One authority store",
                    "alternatives": [],
                    "consequence": "Graph stays derived",
                    "authority_ref": "design-note",
                },
            }
        ]
    )


class RecordingSteward:
    def __init__(self):
        self.calls = []

    def candidate_create(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "schema_version": "brain_steward_proposal.v1",
            "accepted": False,
            "authoritative_memory_changed": False,
            "proposal": {"memory_id": "mem_steward_test"},
        }


def _settings(**overrides):
    base = {"project": PROJECT, "batch_candidates": 5}
    base.update(overrides)
    return CandidateIntakeSettings(**base)


# --------------------------------------------------------------- settings gate


def test_empty_project_is_rejected_fail_closed():
    with pytest.raises(CandidateIntakeConfigError, match="project"):
        CandidateIntakeSettings(project="")


def test_unbounded_batch_is_rejected():
    with pytest.raises(CandidateIntakeConfigError, match="batch_candidates"):
        _settings(batch_candidates=0)
    with pytest.raises(CandidateIntakeConfigError, match="batch_candidates"):
        _settings(batch_candidates=101)


# ------------------------------------------------- no model call, no write by default


def test_disabled_run_makes_no_model_call_and_no_write():
    calls = {"model": 0, "write": 0}

    def completion(messages):
        calls["model"] += 1
        return _completion(messages)

    steward = RecordingSteward()
    original = steward.candidate_create

    def counted(**kwargs):
        calls["write"] += 1
        return original(**kwargs)

    steward.candidate_create = counted
    report, _state = run_candidate_intake(
        settings=_settings(enable_proposals=False),
        chunks=[_chunk()],
        completion_fn=completion,
        steward=steward,
    )

    assert report.status == "disabled"
    assert calls["model"] == 0
    assert calls["write"] == 0
    assert report.enabled is False


def test_dry_run_makes_no_model_call_and_no_write():
    calls = {"model": 0}

    def completion(messages):
        calls["model"] += 1
        return _completion(messages)

    steward = RecordingSteward()
    report, _state = run_candidate_intake(
        settings=_settings(enable_proposals=True),
        chunks=[_chunk()],
        completion_fn=completion,
        steward=steward,
        dry_run=True,
    )

    assert report.status == "dry_run"
    assert calls["model"] == 0
    assert steward.calls == []


# ---------------------------------------------------------- enabled proposal write


def test_enabled_run_writes_review_queue_proposals_only():
    steward = RecordingSteward()
    report, _state = run_candidate_intake(
        settings=_settings(enable_proposals=True),
        chunks=[_chunk()],
        completion_fn=_completion,
        steward=steward,
    )

    assert report.status == "completed"
    assert report.proposed_count == 1
    assert len(steward.calls) == 1
    call = steward.calls[0]
    assert call["mark_needs_review"] is True
    assert call["proposer"] == "candidate-intake"
    assert call["source_span"]["project"] == PROJECT
    assert call["source_span"]["brain_id"] == f"/project/{PROJECT}"
    assert call["source_span"]["content_hash"] == "sha256:" + "a" * 64


def test_miner_failure_is_skipped_and_never_counted_as_projection_complete():
    def broken(_messages):
        return "not json at all"

    steward = RecordingSteward()
    report, state = run_candidate_intake(
        settings=_settings(enable_proposals=True),
        chunks=[_chunk()],
        completion_fn=broken,
        steward=steward,
    )

    assert report.status == "completed"
    assert report.proposed_count == 0
    assert report.skipped_count == 1
    assert steward.calls == []
    # A failed chunk must not advance repeat state.
    assert already_processed(state, project=PROJECT, source_hash=checkpoint_source_hash(PROJECT)) is False


# ------------------------------------------------------------- checkpoint repeat state


def test_checkpoint_roundtrip_is_atomic_and_repeat_safe(tmp_path):
    path = tmp_path / "nested" / "checkpoint.json"
    checkpoint = {"version": 1, "projects": {}}
    source_hash = checkpoint_source_hash(PROJECT)
    record_processed(
        checkpoint, project=PROJECT, source_hash=source_hash, watermark="wm"
    )
    save_checkpoint(str(path), checkpoint)

    reloaded = load_checkpoint(str(path))
    assert already_processed(reloaded, project=PROJECT, source_hash=source_hash) is True
    assert already_processed(reloaded, project="other", source_hash=source_hash) is False
    assert not (tmp_path / "nested" / "checkpoint.json.tmp").exists()


def test_missing_checkpoint_file_is_an_empty_checkpoint(tmp_path):
    assert load_checkpoint(str(tmp_path / "absent.json")) == {"version": 1, "projects": {}}


def test_corrupt_checkpoint_file_does_not_crash_the_run(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_checkpoint(str(path)) == {"version": 1, "projects": {}}


def test_second_run_after_a_completed_revision_skips_duplicates():
    steward = RecordingSteward()
    settings = _settings(enable_proposals=True)
    report, state = run_candidate_intake(
        settings=settings,
        chunks=[_chunk()],
        completion_fn=_completion,
        steward=steward,
        checkpoint={},
    )
    assert report.proposed_count == 1

    steward_again = RecordingSteward()
    repeat, _state = run_candidate_intake(
        settings=settings,
        chunks=[_chunk()],
        completion_fn=_completion,
        steward=steward_again,
        checkpoint=state,
    )
    assert repeat.duplicate_count == 1
    assert steward_again.calls == []


def test_empty_source_is_reported_not_completed():
    steward = RecordingSteward()
    report, _state = run_candidate_intake(
        settings=_settings(enable_proposals=True),
        chunks=[],
        completion_fn=_completion,
        steward=steward,
    )
    assert report.status == "no_source"
    assert steward.calls == []


# ------------------------------------------------------------------- CLI boundary


def test_cli_dry_run_writes_nothing_and_reports_dry_run(capsys):
    code = main(["--project", PROJECT, "--mode", "dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "dry_run"
    assert payload["proposal_only"] is True
    assert payload["authoritative_memory_changed"] is False


def test_cli_once_without_source_fails_closed(capsys):
    code = main(["--project", PROJECT, "--mode", "once"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["error_code"] == "invalid_source"
    assert "--source-json" in payload["message"]


def test_cli_continuous_is_not_enabled_without_an_explicit_gate(capsys):
    code = main(["--project", PROJECT, "--mode", "continuous"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["error_code"] == "continuous_mode_not_enabled"


def test_cli_once_without_enable_writes_no_proposals(tmp_path, capsys):
    source = tmp_path / "chunks.json"
    source.write_text(json.dumps([_chunk()]), encoding="utf-8")
    code = main(["--project", PROJECT, "--mode", "once", "--source-json", str(source)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "disabled"
    assert payload["proposed_count"] == 0

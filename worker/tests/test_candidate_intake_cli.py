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


def _hash(tag: int) -> str:
    """A valid 71-char sha256 hash for fixture chunk identities."""
    return "sha256:" + str(tag) * 64


def default_chunk_hash() -> str:
    """The hash of the default fixture chunk."""
    return _hash(0)


def _chunk(content_hash: str = _hash(0)) -> dict:
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


class CountingCompletion:
    def __init__(self, items_per_call: int = 1):
        self.calls = 0
        self.items_per_call = items_per_call

    def __call__(self, messages):
        self.calls += 1
        return json.dumps([_decision() for _ in range(self.items_per_call)])


def _decision() -> dict:
    return {
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
    assert call["source_span"]["content_hash"] == default_chunk_hash()


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


def test_checkpoint_keeps_many_revisions_of_one_project(tmp_path):
    """One project has many revisions; none of them may silently vanish."""
    path = tmp_path / "checkpoint.json"
    checkpoint = {"version": 1, "projects": {}}
    revisions = [checkpoint_source_hash(f"{PROJECT}-{index}") for index in range(5)]
    for revision in revisions:
        record_processed(checkpoint, project=PROJECT, source_hash=revision, watermark="wm")
    save_checkpoint(str(path), checkpoint)

    reloaded = load_checkpoint(str(path))
    for revision in revisions:
        assert already_processed(reloaded, project=PROJECT, source_hash=revision) is True


def test_legacy_single_hash_checkpoint_is_still_readable(tmp_path):
    path = tmp_path / "legacy.json"
    source_hash = checkpoint_source_hash(PROJECT)
    path.write_text(
        json.dumps({"version": 1, "projects": {PROJECT: {"source_hash": source_hash}}}),
        encoding="utf-8",
    )

    reloaded = load_checkpoint(str(path))
    assert already_processed(reloaded, project=PROJECT, source_hash=source_hash) is True


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


# ----------------------------------------------------------- batching and cost shape


def test_enabled_run_batches_chunks_into_fewer_model_calls():
    completion = CountingCompletion(items_per_call=1)
    steward = RecordingSteward()
    report, _state = run_candidate_intake(
        settings=_settings(enable_proposals=True, batch_candidates=5),
        chunks=[_chunk(_hash(i)) for i in range(3)],
        completion_fn=completion,
        steward=steward,
    )

    # Three chunks in one batch cost one model call, not three.
    assert completion.calls == 1
    assert report.proposed_count >= 1


def test_twenty_chunks_cost_four_model_calls():
    completion = CountingCompletion(items_per_call=1)
    steward = RecordingSteward()
    report, _state = run_candidate_intake(
        settings=_settings(enable_proposals=True, batch_candidates=5),
        chunks=[_chunk(_hash(i)) for i in range(20)],
        completion_fn=completion,
        steward=steward,
    )

    assert completion.calls == 4
    assert report.examined_chunks == 20


def test_every_distinct_chunk_is_processed_not_dropped_by_a_project_key():
    """Three different chunks in one project must yield three proposals."""
    steward = RecordingSteward()
    report, _state = run_candidate_intake(
        settings=_settings(enable_proposals=True, batch_candidates=5),
        chunks=[_chunk(_hash(i)) for i in range(3)],
        completion_fn=CountingCompletion(items_per_call=3),
        steward=steward,
    )

    assert len(steward.calls) == 3
    assert report.duplicate_count == 0
    hashes = {call["source_span"]["content_hash"] for call in steward.calls}
    assert len(hashes) == 3


def test_revision_within_the_same_project_is_processed_once():
    """A repeated revision is skipped; a new revision is not."""
    steward = RecordingSteward()
    settings = _settings(enable_proposals=True, batch_candidates=5)
    first, state = run_candidate_intake(
        settings=settings,
        chunks=[_chunk(_hash(1))],
        completion_fn=CountingCompletion(items_per_call=1),
        steward=steward,
        checkpoint={"version": 1, "projects": {}},
    )
    assert len(steward.calls) == 1

    again = RecordingSteward()
    second, _state = run_candidate_intake(
        settings=settings,
        chunks=[_chunk(_hash(1)), _chunk(_hash(2))],
        completion_fn=CountingCompletion(items_per_call=2),
        steward=again,
        checkpoint=state,
    )

    # The seen revision stays a duplicate; the new one is processed.
    assert second.duplicate_count == 1
    assert len(again.calls) == 1
    assert again.calls[0]["source_span"]["content_hash"] == _hash(2)


def test_model_failure_is_reported_not_counted_as_completed():
    def broken(_messages):
        raise RuntimeError("model unavailable")

    steward = RecordingSteward()
    report, state = run_candidate_intake(
        settings=_settings(enable_proposals=True),
        chunks=[_chunk(_hash(1))],
        completion_fn=broken,
        steward=steward,
    )

    assert report.status == "mining_failed"
    assert report.error_code == "model_call_failed"
    assert steward.calls == []
    assert already_processed(
        state, project=PROJECT, source_hash=_hash(1)
    ) is False

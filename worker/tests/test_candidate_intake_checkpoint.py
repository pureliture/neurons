"""Durable intake history regression: actual JSON files and processes, no DB."""
from __future__ import annotations

import copy
import json
import multiprocessing as mp
from pathlib import Path

import pytest

from agent_knowledge.session_memory import candidate_intake_cli as intake


def revision(i):
    return intake.checkpoint_source_hash(f"synthetic-{i}")


def test_history_retains_every_revision_beyond_old_limit(tmp_path):
    state = {"version": 1, "projects": {}}
    for i in range(2000):
        intake.record_processed(state, project="test", source_hash=revision(i), watermark="wm")
    path = str(tmp_path / "checkpoint.json")
    intake.save_checkpoint(path, state)
    loaded = intake.load_checkpoint(path)
    assert all(intake.already_processed(loaded, project="test", source_hash=revision(i)) for i in range(2000))
    assert len(loaded["projects"]["test"]["revisions"]) == 2000


def test_legacy_history_is_not_forgotten_when_new_revision_added():
    state = {"version": 1, "projects": {"test": {"source_hash": revision(0)}}}
    intake.record_processed(state, project="test", source_hash=revision(1), watermark="wm")
    assert intake.already_processed(state, project="test", source_hash=revision(0))
    assert intake.already_processed(state, project="test", source_hash=revision(1))


def test_old_limit_is_ignored_without_forgetting_existing_revisions():
    state = {"version": 1, "projects": {"test": {"revisions": [revision(0)], "revision_limit": 1}}}
    intake.record_processed(state, project="test", source_hash=revision(1), watermark="wm")
    assert intake.already_processed(state, project="test", source_hash=revision(0))


def test_corrupt_checkpoint_is_rejected_not_reset_for_remining(tmp_path):
    target = tmp_path / "checkpoint.json"
    target.write_text("{broken", encoding="utf-8")
    with pytest.raises(intake.CandidateIntakeConfigError, match="checkpoint"):
        intake.load_checkpoint(str(target))
    assert target.read_text() == "{broken"


def test_save_merges_stale_writer_without_erasing_history(tmp_path):
    target = str(tmp_path / "checkpoint.json")
    first = {"version": 1, "projects": {}}
    second = copy.deepcopy(first)
    intake.record_processed(first, project="test", source_hash=revision(0), watermark="a")
    intake.record_processed(second, project="test", source_hash=revision(1), watermark="b")
    intake.save_checkpoint(target, first)
    intake.save_checkpoint(target, second)
    loaded = intake.load_checkpoint(target)
    assert all(intake.already_processed(loaded, project="test", source_hash=revision(i)) for i in (0, 1))
    assert target and (Path(target).stat().st_mode & 0o777) == 0o600


def _save_worker(path, project, number, ready, output):
    try:
        state = intake.load_checkpoint(path)
        intake.record_processed(state, project=project, source_hash=revision(number), watermark="wm")
        ready.wait(timeout=15)
        intake.save_checkpoint(path, state)
        output.put("")
    except Exception as exc:
        output.put(type(exc).__name__)


@pytest.mark.parametrize("projects", [("test", "test"), ("test", "other")])
def test_actual_two_writer_processes_merge_all_revisions(tmp_path, projects):
    ctx = mp.get_context("spawn")
    ready, output = ctx.Barrier(2), ctx.Queue()
    path = str(tmp_path / "checkpoint.json")
    workers = [ctx.Process(target=_save_worker, args=(path, project, i, ready, output)) for i, project in enumerate(projects)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        assert all(not worker.is_alive() and worker.exitcode == 0 for worker in workers)
        assert [output.get(timeout=5) for _ in workers] == ["", ""]
        loaded = intake.load_checkpoint(path)
        assert all(intake.already_processed(loaded, project=project, source_hash=revision(i)) for i, project in enumerate(projects))
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            worker.join(timeout=5)
        output.close()
        output.join_thread()


def test_cli_accepts_positive_call_chars_before_dry_run(capsys):
    assert intake.main(["--project", "test", "--max-call-chars", "256"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"


def test_cli_rejects_zero_call_chars_before_any_call(capsys):
    assert intake.main(["--project", "test", "--max-call-chars", "0"]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "invalid_settings"

"""Exercise complete intake cycles in actual OS processes, without networking."""
from __future__ import annotations

import copy
import json
import multiprocessing as mp
import re
import time
from pathlib import Path

import pytest

from agent_knowledge.session_memory import candidate_intake_cli as intake


def _chunk(i=0, project="test"):
    return {"content": "Keep private management", "content_hash": intake.checkpoint_source_hash(str(i)),
            "project": project, "knowledge_id": f"synthetic-{i}", "provider": "test"}


def _decision():
    return {"card_type": "decision", "title": "Private management", "statement": "Keep management private",
            "typed_payload": {"decision": "Use private access", "rationale": "Limit exposure", "alternatives": [],
                              "consequence": "Private only", "authority_ref": "synthetic"}}


def _complete(messages):
    keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])
    return json.dumps([{"source_key": key, "candidate": _decision()} for key in keys]) if keys else json.dumps([_decision()])


class Sink:
    def candidate_create(self, **kwargs):
        return {"accepted": False, "authoritative_memory_changed": False, "proposal": kwargs["source_span"]}


def _intake_worker(path, index, project, ready, output):
    calls = 0
    def completion(messages):
        nonlocal calls
        calls += 1
        time.sleep(.04)
        return _complete(messages)
    try:
        # All children read before the first run; the runner must ignore stale snapshots.
        stale = intake.load_checkpoint(path)
        ready.wait(timeout=15)
        report, _ = intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(
            project=project, enable_proposals=True, checkpoint_path=path),
            chunks=[_chunk(index, project)], completion_fn=completion, steward=Sink(), checkpoint=stale)
        output.put({"status": report.status, "calls": calls, "error": ""})
    except Exception as exc:
        output.put({"status": "failed", "calls": calls, "error": type(exc).__name__})


@pytest.mark.parametrize("sources,projects,expected_calls", [((0, 0), ("test", "test"), 1),
                                                           ((0, 1), ("test", "test"), 2),
                                                           ((0, 1), ("test", "other"), 2)])
def test_two_process_complete_cycles_preserve_history(tmp_path, sources, projects, expected_calls):
    ctx = mp.get_context("spawn")
    ready, output = ctx.Barrier(2), ctx.Queue()
    path = str(tmp_path / "checkpoint.json")
    workers = [ctx.Process(target=_intake_worker, args=(path, index, project, ready, output)) for index, project in zip(sources, projects)]
    try:
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=20)
        assert all(not worker.is_alive() and worker.exitcode == 0 for worker in workers)
        results = [output.get(timeout=5) for _ in workers]
        assert all(result["status"] == "completed" and result["error"] == "" for result in results), results
        assert sum(result["calls"] for result in results) == expected_calls
        state = intake.load_checkpoint(path)
        assert all(intake.already_processed(state, project=project, source_hash=_chunk(index)["content_hash"])
                   for index, project in zip(sources, projects))
    finally:
        for worker in workers:
            if worker.is_alive(): worker.terminate()
            worker.join(timeout=5)
        output.close()
        output.join_thread()


def test_failed_storage_does_not_complete_or_advance_caller_state():
    class BrokenSink:
        def candidate_create(self, **kwargs): raise OSError("synthetic write failed")
    original = {"version": 1, "projects": {}}
    report, updated = intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test", enable_proposals=True),
                chunks=[_chunk()], completion_fn=_complete, steward=BrokenSink(), checkpoint=original)
    assert report.status == "proposal_failed"
    assert updated == original == {"version": 1, "projects": {}}


def test_enabled_cli_requires_durable_checkpoint_before_model_build(tmp_path, monkeypatch, capsys):
    from agent_knowledge.session_memory import extraction_llm
    source = tmp_path / "source.json"
    source.write_text(json.dumps([_chunk()]))
    def denied(): raise AssertionError("model builder invoked without durable history")
    monkeypatch.setattr(extraction_llm, "build_vertex_wrapper_completion_fn", denied)
    assert intake.main(["--project", "test", "--mode", "once", "--enable", "--source-json", str(source)]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "checkpoint_required"


def test_wrong_project_is_rejected_before_model_or_write():
    calls = []
    def denied(messages): calls.append(messages); raise AssertionError("wrong project sent")
    report, state = intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test", enable_proposals=True),
                chunks=[_chunk(project="other")], completion_fn=denied, steward=Sink(), checkpoint={})
    assert report.status == "invalid_source" and calls == [] and state == {}

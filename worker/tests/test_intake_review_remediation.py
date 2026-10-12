"""Independent-review counterexamples, synthetic inputs and local files only."""
from __future__ import annotations
import copy
import json
import re

import pytest

from agent_knowledge.session_memory import candidate_intake_cli as intake
from test_candidate_intake_processes import _chunk, _complete, Sink
from test_steward_intake_provenance import _candidate, _steward


@pytest.mark.parametrize("field", ["project", "content_hash"])
def test_blank_explicit_field_is_not_silently_replaced_for_completed_work(field):
    source = _chunk()
    source["metadata"] = {"project": source["project"], "content_hash": source["content_hash"]}
    source[field] = ""
    calls = []
    report, state = intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test", enable_proposals=True),
            chunks=[source], completion_fn=lambda m: calls.append(1) or _complete(m), steward=Sink(), checkpoint={})
    assert report.status == "invalid_source" and calls == [] and state == {}


@pytest.mark.parametrize("damage", ["body", "hash", "observed_at", "observed_naive"])
def test_invalid_source_is_rejected_before_model_even_with_null_response(damage):
    source = _chunk()
    if damage == "body": source["content"] = ""
    if damage == "hash": source["content_hash"] = "not-a-sha256"
    if damage == "observed_at": source["observed_at"] = "private transcript body"
    if damage == "observed_naive": source["observed_at"] = "2026-10-12T12:00:00"
    calls = []
    def null(m):
        calls.append(1)
        return json.dumps([{"source_key":k,"candidate":None} for k in re.findall(r"SOURCE_KEY: ([^\s]+)",m[-1]["content"])])
    report, state = intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test", enable_proposals=True),
            chunks=[source], completion_fn=null, steward=Sink(), checkpoint={})
    assert report.status == "invalid_source" and state == {} and calls == []


def test_tilde_checkpoint_uses_one_path_for_read_lock_save(tmp_path,monkeypatch):
    monkeypatch.setenv("HOME",str(tmp_path))
    settings = intake.CandidateIntakeSettings(project="test",enable_proposals=True,checkpoint_path="~/history.json")
    calls=[]
    for _ in range(2):
        report,_=intake.run_candidate_intake(settings=settings,chunks=[_chunk()],
                completion_fn=lambda m: calls.append(1) or _complete(m),steward=Sink())
        assert report.status=="completed"
    assert len(calls)==1
    assert intake.load_checkpoint("~/history.json")==intake.load_checkpoint(str(tmp_path/"history.json"))


def test_same_content_in_distinct_sources_is_not_lost_on_sequential_runs():
    first=_chunk()
    second=copy.deepcopy(first)
    second["knowledge_id"]="different-source"
    calls=[]
    state={}
    for source in (first,second):
        report,state=intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test",enable_proposals=True),
                chunks=[source],completion_fn=lambda m:calls.append(1) or _complete(m),steward=Sink(),checkpoint=state)
        assert report.status=="completed" and report.proposed_count==1
    assert len(calls)==2
    again,after=intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test",enable_proposals=True),
                chunks=[first,second],completion_fn=lambda m:calls.append(1) or _complete(m),steward=Sink(),checkpoint=state)
    assert len(calls)==2 and again.proposed_count==0 and after==state


@pytest.mark.parametrize("width",[1,5])
def test_exact_duplicate_inputs_are_deduplicated_before_batching(width):
    report,_=intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test",enable_proposals=True,batch_candidates=width),
            chunks=[_chunk(0),_chunk(0),_chunk(1)],completion_fn=_complete,steward=Sink(),checkpoint={})
    assert report.status=="completed" and report.proposed_count==2


@pytest.mark.parametrize("raw",['{"version":1,"projects":{"test":{"revisions":["sha256:'+'0'*64+'"]}},"projects":{}}',
                                  '{"version":0,"projects":{}}','{"version":1.5,"projects":{}}',
                                  '{"version":false,"projects":{}}'])
def test_checkpoint_duplicate_keys_and_coerced_version_are_refused(tmp_path,raw):
    target=tmp_path/"history.json"
    target.write_text(raw)
    with pytest.raises(intake.CandidateIntakeConfigError):intake.load_checkpoint(str(target))
    assert target.read_text()==raw


def test_cli_source_error_does_not_print_private_path(tmp_path,capsys):
    missing=tmp_path/"synthetic-private-marker"/"missing.json"
    assert intake.main(["--project","test","--mode","once","--source-json",str(missing)])==2
    text=capsys.readouterr().out
    assert "synthetic-private-marker" not in text and str(tmp_path) not in text
    assert json.loads(text)["error_code"]=="invalid_source"


@pytest.mark.parametrize("extra",[{"observed_at":"private transcript body"},
                                  {"observed_at":"2026-10-12T12:00:00"},
                                  {"metadata":{"content":"synthetic private source"}},
                                  {"unrecognized_note":"Bearer syntheticabcdefgh"}])
def test_prebuilt_extra_fields_cannot_bypass_storage_safety(extra):
    service,_,db=_steward()
    card=_candidate()
    card.update(extra)
    with pytest.raises(ValueError):service.candidate_create(source_span=card)
    assert db.executed==[]


def test_cap_applies_to_unprocessed_work_so_tail_is_not_starved():
    sources=[_chunk(i) for i in range(3)]
    settings=intake.CandidateIntakeSettings(project="test",enable_proposals=True,max_chunks_per_run=2)
    first,state=intake.run_candidate_intake(settings=settings,chunks=sources,completion_fn=_complete,steward=Sink(),checkpoint={})
    second,state=intake.run_candidate_intake(settings=settings,chunks=sources,completion_fn=_complete,steward=Sink(),checkpoint=state)
    assert first.proposed_count==2 and second.proposed_count==1
    assert len(state["projects"]["test"]["revisions"])==3

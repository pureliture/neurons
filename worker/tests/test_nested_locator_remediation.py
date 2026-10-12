"""Nested locator privacy keys must be rejected during source admission."""
from __future__ import annotations
import json
import re

import pytest

from agent_knowledge.session_memory import candidate_intake_cli as intake
from test_candidate_intake_processes import _chunk, Sink


def null_completion(messages):
    keys = re.findall(r"SOURCE_KEY: ([^\s]+)", messages[-1]["content"])
    return json.dumps([{"source_key": key, "candidate": None} for key in keys])


@pytest.mark.parametrize("locator", ["source_ref", "span_ref"])
@pytest.mark.parametrize("payload", [{"detail": {"content": "synthetic source"}},
                                     {"detail": {"body": "synthetic source"}},
                                     {"detail": {"raw_text": "synthetic source"}},
                                     {"detail": {"url": "https://example.invalid/private"}},
                                     {"detail": {"secret": "synthetic-secret"}}])
def test_nested_forbidden_locator_keys_reject_before_model(locator, payload):
    source = _chunk()
    source[locator] = {**source.get(locator, {"ref_id": "synthetic-ref"}), **payload}
    calls = []
    report, state = intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test", enable_proposals=True),
            chunks=[source], completion_fn=lambda m: calls.append(1) or null_completion(m), steward=Sink(), checkpoint={})
    assert report.status == "invalid_source" and calls == [] and state == {}


def test_nested_locator_keys_deeply_nested_also_rejected():
    source = _chunk()
    source["source_ref"] = {"ref_id": "x", "detail": {"deeper": [{"content": "synthetic source"}]}}
    calls = []
    report, state = intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test", enable_proposals=True),
            chunks=[source], completion_fn=lambda m: calls.append(1) or null_completion(m), steward=Sink(), checkpoint={})
    assert report.status == "invalid_source" and calls == [] and state == {}


def test_legitimate_nested_locator_metadata_still_accepted():
    source = _chunk()
    source["source_ref"] = {"ref_id": "synthetic-ref", "detail": {"machine": "synthetic-host"}}
    report, _state = intake.run_candidate_intake(settings=intake.CandidateIntakeSettings(project="test", enable_proposals=True),
            chunks=[source], completion_fn=null_completion, steward=Sink(), checkpoint={})
    assert report.status == "completed"

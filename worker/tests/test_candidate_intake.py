"""Proposal-only producer tests. No raw transcripts, no DB, no live model calls."""
import json
from types import SimpleNamespace

import pytest

from agent_knowledge.session_memory import candidate_intake_cli as cli

ARTIFACT = {
    "project": "fixture-project",
    "provider": "codex",
    "content_hash": "sha256:" + "a" * 64,
    "knowledge_id": "fixture-session",
    "content": "Decided to keep PostgreSQL as the single authority store.",
}


def _miner_response() -> str:
    return json.dumps([
        {
            "card_type": "decision",
            "title": "Authority store",
            "statement": "PostgreSQL remains the authority store.",
            "typed_payload": {
                "decision": "Use PostgreSQL", "rationale": "One authority",
                "alternatives": [], "consequence": "Graph stays derived",
                "authority_ref": "design-note",
            },
        },
    ])


def _stub_miner(**kwargs):
    """Real candidate assembly, deterministic model output, no network."""

    class _Miner:
        def __init__(self, completion_fn, **_kwargs):
            self._completion_fn = completion_fn

        def mine_chunk(self, chunk, **kwargs):
            from agent_knowledge.session_memory.memory_miner import (
                _parse_candidate_items,
                build_memory_card_candidate_from_source_span,
            )

            items = _parse_candidate_items(self._completion_fn([{"role": "user", "content": "fixture"}]))
            refresh_watermark = kwargs["refresh_watermark"]
            out = []
            for index, item in enumerate(items):
                out.append(build_memory_card_candidate_from_source_span(
                    {
                        **chunk,
                        "card_type": item["card_type"],
                        "title": item["title"],
                        "redacted_summary": item["statement"],
                        "typed_payload": item["typed_payload"],
                        "brain_id": "/project/fixture-project",
                        "scope": "project",
                        "source_ref": {"source_id": "fixture-session"},
                        "span_ref": {"span_id": f"fixture-session#{index}"},
                    },
                    refresh_watermark=refresh_watermark,
                ))
            return out

    return _Miner


def _steward(captured):
    def candidate_create(*, source_span, mark_needs_review, **kwargs):
        captured.append({"span": dict(source_span), "needs_review": mark_needs_review})
        return {
            "memory_id": "mem_steward_fixture",
            "card_type": "decision",
            "approval_state": "suggested",
            "lifecycle_state": "needs_review",
        }

    return SimpleNamespace(candidate_create=candidate_create)


def test_propose_stores_only_pending_review_candidates():
    captured = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "LlmBrainEnvelopeMiner", _stub_miner())
        result = cli.propose_from_artifacts(
            steward=_steward(captured),
            artifacts=[ARTIFACT],
            project="fixture-project",
            refresh_watermark="fixture",
            completion_fn=lambda messages: _miner_response(),
        )
    assert result["candidate_count"] == 1
    assert result["mutation_performed"] is True
    assert result["candidates"][0]["approval_state"] == "suggested"
    assert result["candidates"][0]["lifecycle_state"] == "needs_review"
    assert captured[0]["needs_review"] is True


def test_model_failure_never_counts_as_candidate():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "LlmBrainEnvelopeMiner", _stub_miner())
        result = cli.propose_from_artifacts(
            steward=_steward([]),
            artifacts=[ARTIFACT],
            project="fixture-project",
            refresh_watermark="fixture",
            completion_fn=lambda messages: (_ for _ in ()).throw(RuntimeError("fixture")),
        )
    assert result["candidate_count"] == 0
    assert result["mutation_performed"] is False
    assert result["model_calls"] == 1


def test_default_and_half_activation_never_build_runtime_or_read_sources(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "build_intake_runtime", lambda **kwargs: pytest.fail("runtime built"))
    monkeypatch.setattr(cli, "load_artifact_source", lambda path: pytest.fail("sources read"))
    source = tmp_path / "source.json"
    source.write_text(json.dumps([ARTIFACT]))
    for flags, status, code in [
        ([], "activation_required", 2),
        (["--execute"], "activation_required", 2),
        (["--allow-model-calls"], "activation_required", 2),
        (["--execute", "--allow-model-calls"], "configuration_rejected", 2),
    ]:
        argv = ["--project", "fixture-project", "--source-json", str(source), *flags]
        assert cli.main(argv, environ={"NEURON_LBRAIN_PGVECTOR_DSN": ""}) == code
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == status
        assert report["network_used"] is False
        assert report["mutation_performed"] is False


def test_dry_run_prints_plan_without_reading_anything(monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_intake_runtime", lambda **kwargs: pytest.fail("runtime built"))
    monkeypatch.setattr(cli, "load_artifact_source", lambda path: pytest.fail("sources read"))
    assert cli.main(
        ["--project", "fixture-project", "--source-json", "unused.json", "--dry-run"], environ={}
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "planned"
    assert report["mutation_performed"] is False
    assert report["network_used"] is False


def test_source_loader_rejects_non_array_payloads(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError, match="JSON array"):
        cli.load_artifact_source(str(bad))

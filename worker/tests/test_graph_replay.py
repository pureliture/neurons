"""M5b graph replay sidecar tests (4-6 core cases, fake seam only)."""

from __future__ import annotations

import json
from typing import Any
from collections.abc import Mapping

from agent_knowledge.postgres_store.graph_replay import (
    build_redacted_episode_payload,
    call_adapter_seam,
    load_checkpoint,
    run_graph_replay,
)


def _card(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_type": "memory_card",
        "source_id": "mem-001",
        "source_revision": "sha256:aaa",
        "content_hash": "sha256:" + "b" * 64,
        "memory_id": "mem-001",
        "project": "neurons",
        "card_type": "decision",
        "title": "Replay decision",
        "summary": "Replay summary",
        "typed_payload": {"note": "plain"},
    }
    base.update(over)
    return base


class _FakeSeam:
    def __init__(self, result: Any = "inserted") -> None:
        self.result = result
        self.calls: list[Mapping[str, Any]] = []

    def upsert_episode(self, payload: Mapping[str, Any]) -> Any:
        self.calls.append(payload)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_redacted_payload_keeps_canonical_key_and_drops_raw_transcript():
    item = _card(
        summary="see /Users/alice/.codex/private/sess.json and Bearer sk-secret-123",
        transcript="FULL RAW TRANSCRIPT with raw_transcript body",
        typed_payload={"path": "/Users/alice/docs/private/x.md"},
    )
    payload = build_redacted_episode_payload(item)
    assert payload["source_type"] == "memory_card"
    assert payload["source_id"] == "mem-001"
    assert payload["source_revision"] == "sha256:aaa"
    assert payload["content_hash"].startswith("sha256:")
    assert "transcript" not in payload
    blob = json.dumps(payload)
    assert "/Users/alice" not in blob
    assert "sk-secret-123" not in blob
    assert "FULL RAW TRANSCRIPT" not in blob


def test_duplicate_replay_is_idempotent_without_adapter_recall():
    seam = _FakeSeam("inserted")
    items = [_card(), dict(_card())]  # same canonical key twice
    report = run_graph_replay(items, seam, checkpoint_path=None).to_dict()
    assert report["projected"] == 1
    assert report["skipped_duplicate"] == 1
    assert len(seam.calls) == 1
    assert report["status"] == "succeeded"


def test_file_checkpoint_makes_rerun_safe(tmp_path):
    ckpt = tmp_path / "replay_checkpoint.json"
    seam = _FakeSeam("inserted")
    first = run_graph_replay([_card()], seam, checkpoint_path=ckpt).to_dict()
    assert first["projected"] == 1
    assert ckpt.exists()

    seam2 = _FakeSeam("inserted")
    second = run_graph_replay([_card()], seam2, checkpoint_path=ckpt).to_dict()
    assert second["projected"] == 0
    assert second["skipped_duplicate"] == 1
    assert seam2.calls == []
    assert load_checkpoint(ckpt).completed != set()


def test_failures_classified_without_leaking_raw_text():
    class _Flaky:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def upsert_episode(self, payload: Any) -> Any:
            self.calls.append(payload)
            sid = payload["source_id"]
            if sid == "flaky":
                raise TimeoutError("conn reset at /Users/alice/.codex/private/x")
            raise ValueError("bad row /Users/alice/secret with raw_transcript dump")

    items = [
        _card(source_id="flaky", source_revision="r1"),
        _card(source_id="poison", source_revision="r2"),
    ]
    report = run_graph_replay(items, _Flaky(), checkpoint_path=None, max_retries=3).to_dict()
    by_disp = {f["disposition"] for f in report["failures"]}
    assert report["retry"] == 1
    assert report["dead_letter"] == 1
    assert by_disp == {"retry", "dead_letter"}
    blob = json.dumps(report)
    assert "/Users/alice" not in blob
    assert "raw_transcript" not in blob
    assert "conn reset" not in blob
    assert all(set(f) == {"key_digest", "source_type", "reason_code", "disposition"} for f in report["failures"])


def test_missing_canonical_key_dead_letters_without_adapter_call():
    seam = _FakeSeam("inserted")
    bad = _card()
    del bad["source_revision"]
    report = run_graph_replay([bad], seam, checkpoint_path=None).to_dict()
    assert report["dead_letter"] == 1
    assert report["projected"] == 0
    assert seam.calls == []
    assert report["failures"][0]["reason_code"] == "missing_source_revision"


def test_add_episode_seam_variant_is_supported():
    class _AddOnly:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def add_episode(self, payload: Any) -> str:
            self.calls.append(payload)
            return "inserted"

    assert call_adapter_seam(_AddOnly(), {"a": 1}) == "inserted"

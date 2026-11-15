"""Synthetic cross-component diagnostics/progress/privacy integration."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path

from agent_knowledge.llm_brain_core.graph_trigger_cli import (
    parse_sanitized_child_error_report,
)
from agent_knowledge.public_safe_util import ensure_public_safe


def _stream_report():
    path = Path(__file__).resolve().parents[1] / "deploy/session-memory/stream_report.py"
    spec = importlib.util.spec_from_file_location("stream_report", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_safe_error_param_step_survive_child_report_but_not_progress_filter():
    child = {
        "schema_version": "llm_brain_couchdb_graph_projection.v1",
        "status": "failed",
        "error_class": "BadRequestError",
        "http_status": 400,
        "error_code": None,
        "error_type": "invalid_request_error",
        "error_param": "response_format.json_schema.schema",
        "error_step": "extract_edges.edge",
        "message": "SECRET /Users/alice sk-topsecret999",
    }
    report = parse_sanitized_child_error_report(json.dumps(child))
    assert report["error_param"] == "response_format.json_schema.schema"
    assert report["error_step"] == "extract_edges.edge"
    assert "SECRET" not in json.dumps(report)
    assert "/Users/alice" not in json.dumps(report)
    ensure_public_safe(report, field="child_report")

    progress = {
        "schema_version": "pipeline_progress.v1",
        "stage": "embedding_batch",
        "done": 3,
        "total": 9,
        "error_param": "response_format.json_schema.schema",
        "error_step": "extract_edges.edge",
        "secret": "NOPE",
    }
    module = _stream_report()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        module.forward_progress(json.dumps(progress).encode())
    forwarded = json.loads(out.getvalue())
    assert forwarded == {
        "schema_version": "pipeline_progress.v1",
        "stage": "embedding_batch",
        "done": 3,
        "total": 9,
    }
    assert "NOPE" not in out.getvalue()

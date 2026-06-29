from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

from ..index_client import RetiredIndexBridgeHttpClient


CLEANUP_READINESS_SCHEMA_VERSION = "agent_knowledge_cleanup_readiness.v1"
DEFAULT_PROJECTS = ("neurons", "dendrite")
LEGACY_PROJECT = "workspace-index-advisor"
LEGACY_AGENT_ID = "index-advisor"


@dataclass(frozen=True)
class CleanupReadinessConfig:
    index_url: str
    token_env: str = "RETIRED_INDEX_BRIDGE_API_KEY"
    transcript_dataset_name: str = "transcript-memory"
    session_dataset_name: str = "session-memory"
    projects: tuple[str, ...] = DEFAULT_PROJECTS
    page_size: int = 20


class CleanupReadinessRunner:
    def __init__(self, *, config: CleanupReadinessConfig, retired_index_bridge: RetiredIndexBridgeHttpClient):
        self.config = config
        self.retired_index_bridge = retired_index_bridge

    def run(self) -> dict:
        transcript = self._dataset_report(
            self.config.transcript_dataset_name,
            [
                *self.config.projects,
                "codex-transcript-capture",
                "antigravity-transcript-capture",
                LEGACY_PROJECT,
            ],
        )
        session = self._dataset_report(
            self.config.session_dataset_name,
            [
                *self.config.projects,
                "codex-memory-regeneration",
                "antigravity-memory-regeneration",
                LEGACY_PROJECT,
            ],
        )
        gates = _evaluate_gates(
            transcript=transcript,
            session=session,
            projects=self.config.projects,
        )
        return {
            "schema_version": CLEANUP_READINESS_SCHEMA_VERSION,
            "status": "ready_for_disable_candidate_refresh" if gates["ready"] else "blocked",
            "mode": "read_only",
            "projects": list(self.config.projects),
            "datasets": {
                "transcript_memory": transcript,
                "session_memory": session,
            },
            "gates": gates,
            "next_actions": _next_actions(gates),
            "preflight_requirements": _preflight_requirements(),
            "mutation_performed": False,
            "network_used": True,
            "raw_ids_printed": False,
            "raw_content_printed": False,
            "hard_delete_performed": False,
        }

    def _dataset_report(self, dataset_name: str, keywords: list[str]) -> dict:
        dataset_id = _resolve_dataset_id(self.retired_index_bridge, dataset_name)
        probes = {}
        for keyword in keywords:
            docs = self.retired_index_bridge.list_documents(
                dataset_id,
                page=1,
                page_size=max(int(self.config.page_size), 1),
                keywords=keyword,
            )
            probes[keyword] = _summarize_docs(docs)
        return {
            "dataset_name": dataset_name,
            "probes": probes,
            "raw_ids_printed": False,
            "raw_content_printed": False,
        }


def _resolve_dataset_id(retired_index_bridge: RetiredIndexBridgeHttpClient, dataset_name: str) -> str:
    for dataset in retired_index_bridge.list_datasets(name=dataset_name):
        if dataset.get("name") == dataset_name and dataset.get("id"):
            return str(dataset["id"])
    raise RuntimeError(f"dataset not found: {dataset_name}")


def _summarize_docs(docs: list[dict]) -> dict:
    runs: dict[str, int] = {}
    projects: dict[str, int] = {}
    agents: dict[str, int] = {}
    providers: dict[str, int] = {}
    for doc in docs:
        run = str(doc.get("run") or doc.get("status") or "")
        if run:
            runs[run] = runs.get(run, 0) + 1
        meta = doc.get("meta_fields") or doc.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        for key, bucket in (
            ("project", projects),
            ("agent_id", agents),
            ("provider", providers),
        ):
            value = str(meta.get(key) or "")
            if value:
                bucket[value] = bucket.get(value, 0) + 1
    return {
        "sample_count": len(docs),
        "runs": runs,
        "projects": projects,
        "agent_ids": agents,
        "providers": providers,
    }


def _evaluate_gates(*, transcript: dict, session: dict, projects: tuple[str, ...]) -> dict:
    blockers: list[str] = []
    transcript_projects = {
        project: _project_probe_ready(transcript, project, agent_suffix="-transcript-capture")
        for project in projects
    }
    session_projects = {
        project: _project_probe_ready(session, project, agent_suffix="-memory-regeneration")
        for project in projects
    }
    if not all(transcript_projects.values()):
        blockers.append("corrected_transcript_memory_done_coverage_missing")
    if not all(session_projects.values()):
        blockers.append("corrected_session_memory_done_coverage_missing")
    if _has_non_done_corrected_runs(transcript, projects):
        blockers.append("corrected_transcript_memory_has_non_done_runs")
    if not _legacy_pollution_present(transcript, session):
        blockers.append("legacy_pollution_inventory_missing")
    return {
        "ready": not blockers,
        "blockers": blockers,
        "corrected_transcript_memory_done_by_project": transcript_projects,
        "corrected_session_memory_done_by_project": session_projects,
        "legacy_pollution_present": _legacy_pollution_present(transcript, session),
        "disable_delete_allowed": False,
        "requires_operator_approval": True,
    }


def _project_probe_ready(dataset_report: dict, project: str, *, agent_suffix: str) -> bool:
    summary = (dataset_report.get("probes") or {}).get(project) or {}
    runs = summary.get("runs") or {}
    agents = summary.get("agent_ids") or {}
    projects = summary.get("projects") or {}
    return (
        int(runs.get("DONE", 0)) > 0
        and int(projects.get(project, 0)) > 0
        and any(agent != LEGACY_AGENT_ID and agent.endswith(agent_suffix) for agent in agents)
    )


def _has_non_done_corrected_runs(dataset_report: dict, projects: tuple[str, ...]) -> bool:
    for project in projects:
        summary = (dataset_report.get("probes") or {}).get(project) or {}
        runs = summary.get("runs") or {}
        if any(run != "DONE" and count for run, count in runs.items()):
            return True
    return False


def _legacy_pollution_present(transcript: dict, session: dict) -> bool:
    return _report_has_legacy(transcript) or _report_has_legacy(session)


def _report_has_legacy(dataset_report: dict) -> bool:
    for summary in (dataset_report.get("probes") or {}).values():
        if _summary_has_legacy(summary):
            return True
    return False


def _summary_has_legacy(summary: dict) -> bool:
    agents = summary.get("agent_ids") or {}
    projects = summary.get("projects") or {}
    return (
        int(projects.get(LEGACY_PROJECT, 0)) > 0
        or int(agents.get(LEGACY_AGENT_ID, 0)) > 0
    )


def _next_actions(gates: dict) -> list[str]:
    if gates.get("ready"):
        return [
            "refresh exact disable/supersede candidate list with dry-run GC commands",
            "prepare private backup path and operator-bound approval argv",
            "run recall regression before any live disable/delete",
        ]
    actions = ["wait for queue/RetiredIndexBridge indexing to settle, then rerun cleanup-readiness"]
    blockers = set(gates.get("blockers") or [])
    if "corrected_session_memory_done_coverage_missing" in blockers:
        actions.append(
            "run one-session session-memory regeneration canary before candidate refresh"
        )
    if "corrected_transcript_memory_has_non_done_runs" in blockers:
        actions.append("inspect corrected transcript-memory FAIL/RUNNING samples before cleanup")
    return actions


def _preflight_requirements() -> dict:
    return {
        "destructive_mutation": "blocked_until_operator_approval",
        "required_before_disable": [
            "current cleanup-readiness status ready_for_disable_candidate_refresh",
            "exact dry-run candidate report with raw ids kept out of chat output",
            "private backup/rollback evidence for every hard-delete path",
            "bounded timeout and abort criteria",
            "postcheck query proving corrected recall still passes",
        ],
        "forbidden_in_this_command": [
            "disable_document",
            "delete_documents",
            "raw transcript/body read",
        ],
    }


def _parse_projects(value: str) -> tuple[str, ...]:
    projects = tuple(part.strip() for part in value.split(",") if part.strip())
    return projects or DEFAULT_PROJECTS


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="cleanup-readiness")
    parser.add_argument("--retired-index-bridge-url", required=True)
    parser.add_argument("--retired-index-bridge-token-env", default="RETIRED_INDEX_BRIDGE_API_KEY")
    parser.add_argument("--transcript-dataset-name", default="transcript-memory")
    parser.add_argument("--session-dataset-name", default="session-memory")
    parser.add_argument("--projects", default=",".join(DEFAULT_PROJECTS))
    parser.add_argument("--page-size", type=int, default=20)
    args = parser.parse_args(raw_argv)

    token = os.environ.get(args.retired_index_bridge_token_env, "")
    if not token:
        print("token env is not set", file=sys.stderr)
        return 2
    client = RetiredIndexBridgeHttpClient(
        base_url=args.retired_index_bridge_url,
        bearer_token=token,
        request_timeout_seconds=25,
    )
    report = CleanupReadinessRunner(
        config=CleanupReadinessConfig(
            index_url=args.retired_index_bridge_url,
            token_env=args.retired_index_bridge_token_env,
            transcript_dataset_name=args.transcript_dataset_name,
            session_dataset_name=args.session_dataset_name,
            projects=_parse_projects(args.projects),
            page_size=args.page_size,
        ),
        retired_index_bridge=client,
    ).run()
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if report.get("status") == "ready_for_disable_candidate_refresh" else 1


if __name__ == "__main__":
    raise SystemExit(main())

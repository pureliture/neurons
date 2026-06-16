"""Shadow live-cutover coordinator for the CouchDB transcript migration.

Models the staged cutover (design "Live Cutover") as a small state machine:

    SHADOW       -> CouchDB write + RAGFlow transcript-memory write kept as a
                    short-lived comparison-only path.
    COUCHDB_ONLY -> CouchDB write only; no new RAGFlow transcript-memory write.

The transition SHADOW -> COUCHDB_ONLY is gated on a stability verdict: every live
provider must have proven CouchDB coverage across a mixed provider/project
window. ``gemini`` is historical-only and is rejected on the live path. ``agy`` is
Antigravity's CLI and is captured as provider ``antigravity``, so it is covered by
the antigravity lane (not a separate live provider).

This module contains orchestration logic only. The actual live event stream and
the real RAGFlow transcript-memory comparison write are injected seams; running
them against live providers / live RAGFlow is a human-gated operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .historical_import import ImportStatus, SourceLocator, import_historical_source
from .source_store import CouchDBSourceStore

# gemini excluded (historical-only); these are the live-pipeline lanes. agy is
# Antigravity's CLI and is captured as antigravity, so it is covered by that lane.
LIVE_CUTOVER_PROVIDERS = ("codex", "claude", "antigravity")


class CutoverPhase:
    SHADOW = "shadow"
    COUCHDB_ONLY = "couchdb_only"


class CutoverNotReady(RuntimeError):
    """Raised when a cutover transition is attempted before coverage is proven."""


@runtime_checkable
class ComparisonSink(Protocol):
    """Represents the short-lived RAGFlow transcript-memory comparison write.

    A real implementation would perform the comparison-only RAGFlow
    transcript-memory write during the shadow window (human-gated). The default
    recording impl below performs no external write.
    """

    def record_transcript_memory_comparison(self, *, provider: str, session_id_hash: str) -> None: ...


class RecordingComparisonSink:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_transcript_memory_comparison(self, *, provider: str, session_id_hash: str) -> None:
        self.calls.append({"provider": provider, "session_id_hash": session_id_hash})


@dataclass(frozen=True)
class ShadowObservation:
    provider: str
    project: str
    session_id_hash: str
    status: str
    couch_written: bool
    comparison_recorded: bool


@dataclass
class ShadowCoordinator:
    store: CouchDBSourceStore
    comparison_sink: ComparisonSink | None = None
    phase: str = CutoverPhase.SHADOW
    observations: list[ShadowObservation] = field(default_factory=list)

    def ingest_live_event(self, locator: SourceLocator) -> ShadowObservation:
        # Force the live scope so gemini live events are rejected as scope violations.
        live_locator = SourceLocator(
            provider=locator.provider,
            source_path=locator.source_path,
            capture_metadata_project=locator.capture_metadata_project,
            cwd=locator.cwd,
            workspace_marker=locator.workspace_marker,
            ragflow_project_hint=locator.ragflow_project_hint,
            scope="live",
        )
        result = import_historical_source(locator=live_locator, store=self.store)
        couch_written = result.status == ImportStatus.IMPORTED

        comparison_recorded = False
        # The comparison RAGFlow transcript-memory write exists ONLY during the
        # shadow window. In COUCHDB_ONLY there is no new transcript-memory write.
        if self.phase == CutoverPhase.SHADOW and couch_written and self.comparison_sink is not None:
            self.comparison_sink.record_transcript_memory_comparison(
                provider=result.provider, session_id_hash=result.session_id_hash
            )
            comparison_recorded = True

        obs = ShadowObservation(
            provider=result.provider,
            project=result.project,
            session_id_hash=result.session_id_hash,
            status=result.status,
            couch_written=couch_written,
            comparison_recorded=comparison_recorded,
        )
        self.observations.append(obs)
        return obs

    def stability_verdict(
        self,
        *,
        min_events_per_provider: int = 1,
        min_projects: int = 1,
        required_providers: tuple[str, ...] = LIVE_CUTOVER_PROVIDERS,
    ) -> dict:
        """Per-live-provider CouchDB coverage over the observed window.

        ``required_providers`` defaults to the live lanes (codex, claude,
        antigravity). An operator may narrow it to cut over a subset.
        """

        per_provider: dict[str, dict] = {}
        for provider in required_providers:
            written = [o for o in self.observations if o.provider == provider and o.couch_written]
            projects = {o.project for o in written if o.project}
            per_provider[provider] = {
                "events": len(written),
                "projects": sorted(projects),
                "covered": len(written) >= min_events_per_provider and len(projects) >= min_projects,
            }
        all_projects = {o.project for o in self.observations if o.couch_written and o.project}
        ready = all(p["covered"] for p in per_provider.values()) and len(all_projects) >= min_projects
        return {
            "ready": ready,
            "phase": self.phase,
            "per_provider": per_provider,
            "mixed_projects": sorted(all_projects),
            "uncovered_providers": [name for name, p in per_provider.items() if not p["covered"]],
        }

    def switch_to_couchdb_only(
        self,
        *,
        min_events_per_provider: int = 1,
        min_projects: int = 1,
        required_providers: tuple[str, ...] = LIVE_CUTOVER_PROVIDERS,
    ) -> dict:
        verdict = self.stability_verdict(
            min_events_per_provider=min_events_per_provider,
            min_projects=min_projects,
            required_providers=required_providers,
        )
        if not verdict["ready"]:
            raise CutoverNotReady(
                "shadow coverage not proven; uncovered=" + ",".join(verdict["uncovered_providers"])
            )
        self.phase = CutoverPhase.COUCHDB_ONLY
        return self.stability_verdict(
            min_events_per_provider=min_events_per_provider,
            min_projects=min_projects,
            required_providers=required_providers,
        )


__all__ = [
    "LIVE_CUTOVER_PROVIDERS",
    "CutoverPhase",
    "CutoverNotReady",
    "ComparisonSink",
    "RecordingComparisonSink",
    "ShadowObservation",
    "ShadowCoordinator",
]

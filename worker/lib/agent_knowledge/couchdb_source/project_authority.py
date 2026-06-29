"""Hierarchy project-authority resolver for the CouchDB transcript migration.

RetiredIndexBridge's existing ``project`` metadata is known to be polluted, so it is never a
single authority. The resolver derives a canonical project label from a priority
hierarchy (design "Project Authority Resolver"):

1. capture metadata (authoritative when present),
2. provider source path / cwd / workspace marker,
3. server inference.

Conflicting non-authoritative signals, or no signal at all, produce an *ambiguous*
resolution. Ambiguous/unresolved sessions are excluded from irreversible
retirement proof (``eligible_for_retirement`` is False).

Privacy: raw paths enter only to derive a canonical label via
``canonicalize_project``; only canonical labels (never raw paths) leave this
module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..session_memory.transcript_model import canonicalize_project


class ProjectAuthoritySource:
    CAPTURE_METADATA = "capture_metadata"
    PROVIDER_PATH = "provider_source_path"
    CWD = "cwd"
    WORKSPACE_MARKER = "workspace_marker"
    SERVER_INFERENCE = "server_inference"
    UNRESOLVED = "unresolved"


# Priority order of the directly-provided (non-inference) tiers.
_TIER_ORDER = (
    ProjectAuthoritySource.CAPTURE_METADATA,
    ProjectAuthoritySource.PROVIDER_PATH,
    ProjectAuthoritySource.CWD,
    ProjectAuthoritySource.WORKSPACE_MARKER,
)


@dataclass(frozen=True)
class ProjectAuthorityInput:
    capture_metadata_project: str = ""
    provider_source_path: str = ""
    cwd: str = ""
    workspace_marker: str = ""
    # RetiredIndexBridge's recorded project, kept for mismatch reporting only -- never an
    # authority.
    index_project_hint: str = ""


@dataclass(frozen=True)
class ProjectResolution:
    project: str
    source: str
    ambiguous: bool
    candidates: tuple[str, ...] = ()
    index_mismatch: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def eligible_for_retirement(self) -> bool:
        return bool(self.project) and not self.ambiguous

    def to_authority_block(self) -> dict:
        """Public-safe dict for embedding in a coverage_manifest document."""

        return {
            "project": self.project,
            "source": self.source,
            "ambiguous": self.ambiguous,
            "candidates": list(self.candidates),
            "index_mismatch": self.index_mismatch,
            "eligible_for_retirement": self.eligible_for_retirement,
            "notes": list(self.notes),
        }


def resolve_project(
    payload: ProjectAuthorityInput,
    *,
    server_inference=None,
) -> ProjectResolution:
    """Resolve a canonical project label from the authority hierarchy.

    ``server_inference`` is an optional ``Callable[[], str]`` consulted only when
    no direct signal (capture metadata, path, cwd, marker) is present.
    """

    raw_by_tier = {
        ProjectAuthoritySource.CAPTURE_METADATA: payload.capture_metadata_project,
        ProjectAuthoritySource.PROVIDER_PATH: payload.provider_source_path,
        ProjectAuthoritySource.CWD: payload.cwd,
        ProjectAuthoritySource.WORKSPACE_MARKER: payload.workspace_marker,
    }
    tiers: list[tuple[str, str]] = []
    for source in _TIER_ORDER:
        canonical = canonicalize_project(raw_by_tier[source])
        if canonical:
            tiers.append((source, canonical))

    index_project = canonicalize_project(payload.index_project_hint)

    if not tiers:
        inferred = canonicalize_project(server_inference() if server_inference else "")
        if inferred:
            return _finalize_resolution(
                project=inferred,
                source=ProjectAuthoritySource.SERVER_INFERENCE,
                ambiguous=False,
                candidates=(inferred,),
                index_project=index_project,
                notes=("server_inference_only",),
            )
        return _finalize_resolution(
            project="",
            source=ProjectAuthoritySource.UNRESOLVED,
            ambiguous=True,
            candidates=(),
            index_project=index_project,
            notes=("no_project_signal",),
        )

    winner_source, winner_project = tiers[0]
    distinct = tuple(dict.fromkeys(project for _, project in tiers))
    notes: list[str] = []
    ambiguous = False
    if len(distinct) > 1:
        notes.append("tier_conflict")
        # Capture metadata is authoritative: a lower-tier disagreement is recorded
        # but does not make the resolution ambiguous. A conflict among only the
        # non-authoritative tiers does (needs server-inference verification).
        if winner_source != ProjectAuthoritySource.CAPTURE_METADATA:
            ambiguous = True
            notes.append("server_inference_recommended")

    return _finalize_resolution(
        project=winner_project,
        source=winner_source,
        ambiguous=ambiguous,
        candidates=distinct,
        index_project=index_project,
        notes=tuple(notes),
    )


def _finalize_resolution(
    *,
    project: str,
    source: str,
    ambiguous: bool,
    candidates: tuple[str, ...],
    index_project: str,
    notes: tuple[str, ...],
) -> ProjectResolution:
    mismatch = bool(index_project) and bool(project) and index_project != project
    note_list = list(notes)
    if mismatch:
        note_list.append("index_project_mismatch")
    return ProjectResolution(
        project=project,
        source=source,
        ambiguous=ambiguous,
        candidates=candidates,
        index_mismatch=mismatch,
        notes=tuple(note_list),
    )


__all__ = [
    "ProjectAuthoritySource",
    "ProjectAuthorityInput",
    "ProjectResolution",
    "resolve_project",
]

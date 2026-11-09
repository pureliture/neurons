"""Hermes project-scope policy shared by every session build lane.

Hermes can switch *profile* inside one session, so its CouchDB documents may
disagree on ``project``: the session document carries one value while some
conversation chunks carry another. The generic scope check requires every
document in a session to agree, so these sessions would otherwise fail with
``session source docs have inconsistent project``.

This module is the single policy seam. Both the graph projection lane
(``llm_brain_core.runtime``) and the PG/Qdrant session-memory lane
(``couchdb_source.session_memory_materializer``) call
:func:`decide_hermes_project_scope` so one session is never admitted by one
lane and refused by the other. CouchDB documents are immutable revisions and
are never rewritten; only the in-memory view handed to the scope check is
normalized.

Policy (Hermes provider, more than one distinct ``project`` value):

1. workspace-vs-profile: the session document carries a workspace slug and
   every other value is a configured profile name. The workspace wins, the
   profile-carrying documents are treated as belonging to it, and the note
   ``hermes_profile_normalized_to_workspace`` is recorded.
2. anything else (profile-vs-profile, a profile session mixed with a
   workspace, or an unknown label): there is no faithful single project, so
   the session is reported inconsistent with reason
   ``inconsistent_hermes_project_scope``. Callers skip it as a terminal,
   deterministic outcome instead of retrying or dead-lettering.

Profile names are configuration, not code: ``HERMES_PROFILE_NAMES`` is a
comma-separated list. It defaults to the empty set, which is fail-closed --
without it every mixed Hermes session is inconsistent. Operational values
belong in the private deployment configuration (``neurons-ops``).

Non-Hermes providers and sessions whose documents already agree are returned
untouched; the generic validation still applies to them.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..session_memory.transcript_model import canonicalize_project

HERMES_PROVIDER = "hermes"
HERMES_PROFILE_NAMES_ENV = "HERMES_PROFILE_NAMES"

INCONSISTENT_HERMES_PROJECT_SCOPE = "inconsistent_hermes_project_scope"
NOTE_PROFILE_NORMALIZED_TO_WORKSPACE = "hermes_profile_normalized_to_workspace"
NOTE_PROFILE_VS_PROFILE = "profile_vs_profile"
NOTE_SCOPE_UNRESOLVED = "hermes_project_scope_unresolved"


class InconsistentHermesProjectScope(ValueError):
    """A Hermes session mixes project labels that no single project can own."""


def parse_profile_names(raw: str) -> frozenset[str]:
    """Parse a comma-separated profile list into canonical project labels."""
    names = set()
    for part in str(raw or "").split(","):
        value = canonicalize_project(part.strip())
        if value:
            names.add(value)
    return frozenset(names)


def configured_profile_names(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Hermes profile names from ``HERMES_PROFILE_NAMES`` (empty when unset)."""
    source = os.environ if environ is None else environ
    return parse_profile_names(source.get(HERMES_PROFILE_NAMES_ENV, ""))


def is_profile_value(value: str, profile_names: Iterable[str] | None = None) -> bool:
    names = frozenset(profile_names) if profile_names is not None else configured_profile_names()
    return value in names


def is_workspace_value(value: str, profile_names: Iterable[str] | None = None) -> bool:
    """A plain slug that is not a configured profile name -> workspace name."""
    if not value or "/" in value or "\\" in value:
        return False
    return not is_profile_value(value, profile_names)


@dataclass(frozen=True)
class HermesProjectScopeDecision:
    """Outcome of the shared Hermes scope policy for one session's documents."""

    documents: tuple[Mapping[str, Any], ...]
    original_project: str
    canonical_project: str
    normalized_projects: tuple[str, ...] = ()
    inconsistent: bool = False
    notes: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        return INCONSISTENT_HERMES_PROJECT_SCOPE if self.inconsistent else ""

    def raise_if_inconsistent(self) -> None:
        if self.inconsistent:
            raise InconsistentHermesProjectScope(
                "session source docs have inconsistent hermes project scope"
            )


# Historical name kept for callers that imported the previous result type.
ScopedSession = HermesProjectScopeDecision


def decide_hermes_project_scope(
    documents: Iterable[Mapping[str, Any]],
    *,
    provider: str,
    session_project: str,
    session_doc_ids: Iterable[str] | None = None,
    profile_names: Iterable[str] | None = None,
) -> HermesProjectScopeDecision:
    """Judge the project scope of one session's documents under the Hermes policy.

    *session_project* is the project of the session document (or of the first
    chunk when no session document exists). *profile_names* defaults to the
    ``HERMES_PROFILE_NAMES`` configuration.
    """
    docs = list(documents)
    original = session_project

    if provider != HERMES_PROVIDER or not session_project:
        return HermesProjectScopeDecision(
            documents=tuple(docs),
            original_project=original,
            canonical_project=original,
        )

    projects = {
        str(doc.get("project") or "") for doc in docs if doc.get("project") not in (None, "")
    }
    projects.add(session_project)
    if len(projects) <= 1:
        return HermesProjectScopeDecision(
            documents=tuple(docs),
            original_project=original,
            canonical_project=original,
        )

    names = (
        frozenset(profile_names) if profile_names is not None else configured_profile_names()
    )
    canonical = session_project
    others = projects - {canonical}
    canonical_is_workspace = is_workspace_value(canonical, names)
    others_are_profiles = all(value in names for value in others)

    if not (canonical_is_workspace and others_are_profiles):
        note = (
            NOTE_PROFILE_VS_PROFILE
            if canonical in names and any(value in names for value in others)
            else NOTE_SCOPE_UNRESOLVED
        )
        return HermesProjectScopeDecision(
            documents=tuple(docs),
            original_project=original,
            canonical_project=original,
            inconsistent=True,
            notes=(note,),
        )

    protected = frozenset(str(item) for item in (session_doc_ids or ()))
    normalized: set[str] = set()
    rewritten: list[Mapping[str, Any]] = []
    for doc in docs:
        value = str(doc.get("project") or "")
        if value in others and str(doc.get("_id") or "") not in protected:
            copied = dict(doc)
            copied["project"] = canonical
            rewritten.append(copied)
            normalized.add(value)
        else:
            rewritten.append(doc)

    return HermesProjectScopeDecision(
        documents=tuple(rewritten),
        original_project=original,
        canonical_project=canonical,
        normalized_projects=tuple(sorted(normalized)),
        notes=(NOTE_PROFILE_NORMALIZED_TO_WORKSPACE,),
    )


def normalize_hermes_project_scope(
    documents: Iterable[Mapping[str, Any]],
    *,
    provider: str,
    session_project: str,
    session_doc_ids: Iterable[str] | None = None,
) -> HermesProjectScopeDecision:
    """Backwards-compatible alias of :func:`decide_hermes_project_scope`."""
    return decide_hermes_project_scope(
        documents,
        provider=provider,
        session_project=session_project,
        session_doc_ids=session_doc_ids,
    )


def build_hermes_project_scope(
    *,
    provider: str,
    session_project: str,
    session_doc_ids: frozenset[str] | None = None,
) -> HermesProjectScopeDecision:
    """Backwards-compatible thin wrapper used by older callers."""
    return HermesProjectScopeDecision(
        documents=(),
        original_project=session_project,
        canonical_project=session_project,
        notes=(
            "hermes_scope_noop",
            f"session_doc_ids={len(session_doc_ids or ())}",
        ),
    )


__all__ = [
    "HERMES_PROFILE_NAMES_ENV",
    "INCONSISTENT_HERMES_PROJECT_SCOPE",
    "NOTE_PROFILE_NORMALIZED_TO_WORKSPACE",
    "HermesProjectScopeDecision",
    "InconsistentHermesProjectScope",
    "ScopedSession",
    "configured_profile_names",
    "decide_hermes_project_scope",
    "is_profile_value",
    "is_workspace_value",
    "normalize_hermes_project_scope",
    "parse_profile_names",
]

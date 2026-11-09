"""Hermes-only project-scope normalization for materialize.

Hermes records the *profile* name (``tyche``, ``ddalkak``, ``hermes``,
``daedalus`` ...) in some documents and the *workspace* name
(``hermes-stocks-agent``, ``workspace`` ...) in others, inside the SAME
session. The generic scope check requires every document in a session to
agree on one ``project`` value, so these sessions fail materialize with
``ValueError: session source docs have inconsistent project`` even though
the data is fine.

This module is the Hermes-only seam that the generic materialize calls into.
It never mutates CouchDB documents (they are immutable revisions); it only
rewrites the *in-memory* view handed to the seed check.

Two cases are handled, everything else is left alone:

1. workspace-vs-profile: the session document carries the workspace value
   and some chunks carry a profile value. The session document wins, so
   profile-carrying chunks are treated as belonging to the session's
   workspace. No data is lost; the profile label is not propagated.
2. profile-vs-profile: two different profile values appear in one session.
   There is no faithful single project to pick, so the session is reported
   as inconsistent and the caller skips it (``skipped_inconsistent``)
   instead of dead-lettering it forever.

Documents whose ``project`` is not a slug/profile value (paths, etc.) are
never touched, so the existing validation still fails on genuinely bad data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Hermes profile names observed in CouchDB ``transcript_source``. Anything not
# in this set that is a plain slug is treated as a workspace name.
PROFILE_VALUES = frozenset(
    {
        "ariadne",
        "atlas",
        "daedalus",
        "ddalkak",
        "default",
        "hermes",
        "metis",
        "plutus",
        "tutor",
        "tyche",
    }
)


def is_profile_value(value: str) -> bool:
    return value in PROFILE_VALUES


def is_workspace_value(value: str) -> bool:
    """A plain slug that is not a profile name -> workspace name."""
    if not value or "/" in value or "\\" in value:
        return False
    return value not in PROFILE_VALUES


@dataclass(frozen=True)
class ScopedSession:
    """Result of hermes scope normalization for one session's documents."""

    documents: tuple[Mapping[str, Any], ...]
    original_project: str
    canonical_project: str
    normalized_projects: tuple[str, ...] = ()
    inconsistent: bool = False
    notes: tuple[str, ...] = ()


def normalize_hermes_project_scope(
    documents: "Any",
    *,
    provider: str,
    session_project: str,
    session_doc_ids: "Any" = None,
) -> ScopedSession:
    """Return a hermes-normalized view of *documents*, or flag inconsistency.

    *session_project* is the project of the session document (workspace for
    workspace-vs-profile sessions). Non-Hermes providers and sessions where
    every document already agrees are returned untouched.
    """
    docs = list(documents)
    original = session_project

    if provider != "hermes" or not session_project:
        return ScopedSession(
            documents=tuple(docs),
            original_project=original,
            canonical_project=original,
        )

    if session_doc_ids is None:
        session_doc_ids = frozenset()

    projects = {
        str(doc.get("project") or "") for doc in docs if doc.get("project") is not None
    }
    if len(projects) <= 1:
        return ScopedSession(
            documents=tuple(docs),
            original_project=original,
            canonical_project=original,
        )

    profiles_present = {p for p in projects if is_profile_value(p)}
    workspaces_present = {p for p in projects if is_workspace_value(p)}

    # profile-vs-profile: two different profiles in one session cannot be
    # faithfully mapped onto one project. Report and let the caller skip.
    if len(profiles_present) >= 2:
        return ScopedSession(
            documents=tuple(docs),
            original_project=original,
            canonical_project=original,
            inconsistent=True,
            notes=("profile_vs_profile",),
        )

    # If the session document itself carries a profile value and chunks carry
    # another (i.e. the session is really a profile session), treat the session
    # value as authoritative but do not rewrite anything.
    canonical = session_project
    normalized: list[str] = []
    rewritten: list[Mapping[str, Any]] = []
    for doc in docs:
        value = str(doc.get("project") or "")
        if (
            value
            and value != canonical
            and doc.get("_id") not in session_doc_ids
            and is_profile_value(value)
        ):
            copied = dict(doc)
            copied["project"] = canonical
            rewritten.append(copied)
            normalized.append(value)
        else:
            rewritten.append(doc)

    if not normalized:
        return ScopedSession(
            documents=tuple(rewritten),
            original_project=original,
            canonical_project=canonical,
            notes=("hermes_scope_unchanged",),
        )

    return ScopedSession(
        documents=tuple(rewritten),
        original_project=original,
        canonical_project=canonical,
        normalized_projects=tuple(sorted(set(normalized))),
        notes=("hermes_profile_normalized_to_workspace",),
    )


def build_hermes_project_scope(
    *,
    provider: str,
    session_project: str,
    session_doc_ids: frozenset[str] | None = None,
) -> ScopedSession:
    """Backwards-compatible thin wrapper used by older callers."""
    return ScopedSession(
        documents=(),
        original_project=session_project,
        canonical_project=session_project,
        notes=(
            "hermes_scope_noop",
            f"session_doc_ids={len(session_doc_ids or ())}",
        ),
    )

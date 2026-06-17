from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from ..redaction import redact_public_ingress_text, redact_text_v2

REDACTION_VERSION = "redaction.v2"
MAX_TRANSCRIPT_TURN_TEXT_CHARS = 2048
MAX_TRANSCRIPT_TOOL_EVENT_TEXT_CHARS = 1024
MAX_TRANSCRIPT_CHUNK_TEXT_CHARS = 4096
MAX_TRANSCRIPT_SNIPPET_CHARS = 1024
MAX_PACKED_TRANSCRIPT_BODY_CHARS = 8192
MAX_TOOL_EVIDENCE_SUMMARY_CHARS = 1024
MAX_TOOL_EVIDENCE_COMMAND_CHARS = 200
TRUNCATED_TEXT_MARKER = "\n[truncated]"

TOOL_EVIDENCE_SUMMARY_RECORD_TYPE = "tool_evidence_summary"
TOOL_EVIDENCE_CATEGORIES = (
    "test_result",
    "git_state",
    "live_proof",
    "local_audit",
    "command_error",
    "approval_evidence",
)
TOOL_EVIDENCE_OUTCOMES = ("pass", "fail", "error", "info")


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonicalize_project(project: str) -> str:
    """Collapse observed transcript source path variants into a stable project label."""
    value = str(project or "")
    if not value:
        return ""

    path_canonical = _canonicalize_project_path(value)
    if path_canonical is not None:
        return path_canonical

    slug_canonical = _canonicalize_project_slug(value)
    if slug_canonical is not None:
        return slug_canonical

    return value


def _canonicalize_project_path(value: str) -> str | None:
    if "/" not in value and "\\" not in value:
        return None
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    if not parts:
        return ""
    lower_parts = [part.lower() for part in parts]

    for marker in (".claude-worktrees", "claude-worktrees"):
        if marker in lower_parts:
            marker_index = lower_parts.index(marker)
            parts = parts[:marker_index]
            lower_parts = lower_parts[:marker_index]
            break

    if ".openclaw" in lower_parts:
        marker_index = lower_parts.index(".openclaw")
        if marker_index + 1 < len(parts):
            return parts[marker_index + 1]

    if "projects" in lower_parts:
        marker_index = lower_parts.index("projects")
        if marker_index + 1 < len(parts):
            return parts[marker_index + 1]

    return parts[-1]


def _canonicalize_project_slug(value: str) -> str | None:
    normalized = re.sub(r"-+", "-", value.strip("-"))
    lower = normalized.lower()
    if not normalized:
        return ""

    if "claude-worktrees" in lower:
        normalized = normalized[: lower.index("claude-worktrees")].rstrip("-")
        lower = normalized.lower()

    openclaw_marker = "openclaw-"
    if openclaw_marker in lower and ("users-" in lower or "home-" in lower):
        marker_index = lower.index(openclaw_marker)
        tail = lower[marker_index + len(openclaw_marker) :]
        if tail:
            return tail

    projects_marker = "projects-"
    if projects_marker in lower and ("users-" in lower or "home-" in lower):
        marker_index = lower.index(projects_marker)
        tail = lower[marker_index + len(projects_marker) :]
        if tail:
            return tail

    return None


def bound_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= len(TRUNCATED_TEXT_MARKER):
        # Truncation is required but the limit cannot hold the marker. Refuse a
        # marker-less clamp: a silent (unmarked) truncation would defeat the
        # session-memory no-truncation guard, which detects loss by the marker.
        raise ValueError("bound_text: max_chars too small to mark truncation; refusing silent clamp")
    return text[: max_chars - len(TRUNCATED_TEXT_MARKER)] + TRUNCATED_TEXT_MARKER


def redact_and_bound_text(text: str, max_chars: int) -> str:
    return bound_text(redact_text_v2(text), max_chars)


def redact_and_bound_evidence_text(text: str, max_chars: int) -> str:
    """Stricter redaction for shell/tool evidence, which is path- and id-heavy.

    Applies the public-ingress denylist (all local user/home/volume paths,
    credential terms, dataset/document id terms) on top of redaction.v2, then
    bounds the result. Tool stdout leaks local runtime paths that the base v2
    redactor intentionally keeps for conversation text, so evidence records use
    this stricter pass instead.
    """
    return bound_text(redact_public_ingress_text(text), max_chars)


@dataclass(frozen=True)
class TranscriptSession:
    session_id_hash: str
    provider: str
    project: str
    started_at: str
    ended_at: str = ""
    source_status: str = "source_unproven"
    source_locator_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", canonicalize_project(self.project))

    def to_record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptTurn:
    turn_id_hash: str
    session_id_hash: str
    turn_index: int
    role: str
    observed_at: str
    redacted_text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "redacted_text", redact_text_v2(self.redacted_text))

    def to_record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptToolEvent:
    tool_event_id_hash: str
    turn_id_hash: str
    event_index: int
    tool_name: str
    event_type: str
    redacted_summary: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "redacted_summary",
            redact_and_bound_text(self.redacted_summary, MAX_TRANSCRIPT_TOOL_EVENT_TEXT_CHARS),
        )

    def to_record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptChunk:
    chunk_id: str
    session_id_hash: str
    provider: str
    project: str
    turn_start_index: int
    turn_end_index: int
    redacted_text: str
    content_hash: str
    redaction_version: str = REDACTION_VERSION
    source_status: str = "source_locator_private_spool_only"
    part_index: int = 1
    part_count: int = 1
    char_start: int = 0
    char_end: int = 0

    def __post_init__(self) -> None:
        redacted_text = redact_text_v2(self.redacted_text)
        object.__setattr__(self, "project", canonicalize_project(self.project))
        object.__setattr__(self, "redacted_text", redacted_text)
        object.__setattr__(self, "content_hash", _sha256(redacted_text))

    @classmethod
    def from_text(
        cls,
        *,
        chunk_id: str,
        session_id_hash: str,
        provider: str,
        project: str,
        turn_start_index: int,
        turn_end_index: int,
        text: str,
        source_status: str = "source_locator_private_spool_only",
    ) -> "TranscriptChunk":
        redacted_text = redact_text_v2(text)
        return cls(
            chunk_id=chunk_id,
            session_id_hash=session_id_hash,
            provider=provider,
            project=project,
            turn_start_index=turn_start_index,
            turn_end_index=turn_end_index,
            redacted_text=redacted_text,
            content_hash=_sha256(redacted_text),
            source_status=source_status,
        )

    def title(self) -> str:
        return f"{self.provider} conversation chunk {self.turn_start_index}-{self.turn_end_index}"

    def summary(self) -> str:
        return self.redacted_text[:500]

    def to_record(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ToolEvidenceSummaryRecord:
    """Append-only, redacted high-signal tool/function evidence for a session.

    Linked to existing ``conversation_chunk`` records by the same
    ``session_id_hash``. Carries one piece of durable evidence (a test result,
    git state/commit/diff summary, live proof, local audit count, command error
    class, or approval/timeout/retry observation) rather than raw stdout.
    """

    session_id_hash: str
    provider: str
    project: str
    category: str
    outcome: str
    tool_name: str
    command_summary: str
    redacted_summary: str
    observed_at: str = ""
    evidence_index: int = 0
    content_hash: str = ""
    evidence_id_hash: str = ""
    redaction_version: str = REDACTION_VERSION
    source_status: str = "source_locator_private_spool_only"

    def __post_init__(self) -> None:
        command_summary = redact_and_bound_evidence_text(self.command_summary, MAX_TOOL_EVIDENCE_COMMAND_CHARS)
        redacted_summary = redact_and_bound_evidence_text(self.redacted_summary, MAX_TOOL_EVIDENCE_SUMMARY_CHARS)
        object.__setattr__(self, "project", canonicalize_project(self.project))
        object.__setattr__(self, "command_summary", command_summary)
        object.__setattr__(self, "redacted_summary", redacted_summary)
        content_hash = _sha256(redacted_summary)
        object.__setattr__(self, "content_hash", content_hash)
        evidence_id_hash = _sha256(
            "|".join(
                [
                    TOOL_EVIDENCE_SUMMARY_RECORD_TYPE,
                    self.session_id_hash,
                    str(self.evidence_index),
                    self.category,
                    self.outcome,
                    command_summary,
                    content_hash,
                ]
            )
        )
        object.__setattr__(self, "evidence_id_hash", evidence_id_hash)

    def title(self) -> str:
        return f"{self.provider} tool evidence {self.category} {self.outcome}"

    def to_record(self) -> dict:
        return asdict(self)

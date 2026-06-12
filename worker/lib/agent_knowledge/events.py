from __future__ import annotations

REQUIRED_FIELDS = {
    "schema_version",
    "event_id",
    "provider",
    "project",
    "session_id_hash",
    "event_type",
    "observed_at",
    "privacy_level",
    "summary",
    "content_hash",
    "redaction_version",
}
FORBIDDEN_FIELDS = {
    "raw_prompt",
    "prompt",
    "raw_tool_input",
    "raw_tool_output",
    "raw_transcript",
    "transcript",
}
SUPPORTED_EVENTS = {
    "session_end",
    "manual_note",
    "user_prompt_seen",
    "tool_use_summary",
    "assistant_turn_summary",
    "session_start",
}


class EventValidationError(ValueError):
    pass


def validate_event(event: dict) -> dict:
    missing = sorted(REQUIRED_FIELDS - set(event))
    if missing:
        raise EventValidationError(f"missing required fields: {', '.join(missing)}")

    forbidden = sorted(FORBIDDEN_FIELDS & set(event))
    if forbidden:
        raise EventValidationError(f"forbidden raw fields: {', '.join(forbidden)}")

    if event["schema_version"] != "agent_knowledge_event.v1":
        raise EventValidationError("unsupported schema_version")
    if event["event_type"] not in SUPPORTED_EVENTS:
        raise EventValidationError(f"unsupported event_type: {event['event_type']}")
    if not str(event["session_id_hash"]).startswith("sha256:"):
        raise EventValidationError("session_id_hash must be sha256")
    if not str(event["content_hash"]).startswith("sha256:"):
        raise EventValidationError("content_hash must be sha256")
    return event

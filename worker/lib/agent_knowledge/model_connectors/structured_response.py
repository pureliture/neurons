from __future__ import annotations

import re
from typing import Any

STRUCTURED_KEY_ALIASES = {
    "entity_name": "name",
    "entity": "name",
    "entity_value": "name",
    "entity_text": "name",
}

EXISTING_FACTS_BLOCK_RE = re.compile(r"<EXISTING FACTS>\s*(.*?)\s*</EXISTING FACTS>", re.DOTALL)
FACT_IDX_RE = re.compile(r"""["']?idx["']?\s*[:=]\s*(-?\d+)""")


def normalize_structured_response(
    value: Any,
    response_model: Any = None,
    *,
    valid_duplicate_fact_idxs: set[int] | None = None,
) -> Any:
    normalized = normalize_structured_keys(value)
    if response_model is None:
        return normalized
    fields = getattr(response_model, "model_fields", {}) or {}
    if isinstance(normalized, list):
        list_field_names = [
            name for name, field in fields.items()
            if is_list_annotation(getattr(field, "annotation", None))
        ]
        if len(list_field_names) == 1:
            normalized = {list_field_names[0]: normalized}
    if isinstance(normalized, dict):
        payload = normalize_response_model_payload(normalized, fields)
        return sanitize_duplicate_fact_idxs(payload, fields, valid_duplicate_fact_idxs)
    return normalized


def normalize_structured_keys(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize_structured_keys(item) for item in value]
    if isinstance(value, dict):
        result = {key: normalize_structured_keys(val) for key, val in value.items()}
        for alias, canonical in STRUCTURED_KEY_ALIASES.items():
            if alias in result and canonical not in result:
                result[canonical] = result.pop(alias)
        return result
    return value


def is_list_annotation(annotation: Any) -> bool:
    text = str(annotation if annotation is not None else "")
    return text.startswith("list[") or text.startswith("typing.List[")


def normalize_response_model_payload(payload: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    for field_name in fields:
        items = result.get(field_name)
        if not isinstance(items, list):
            continue
        normalized_items = []
        for item in items:
            if isinstance(item, dict):
                normalized_items.append(normalize_response_model_item(item, field_name))
            else:
                normalized_items.append(item)
        result[field_name] = normalized_items
    return result


def normalize_response_model_item(item: dict[str, Any], field_name: str) -> dict[str, Any]:
    result = dict(item)
    if field_name == "extracted_entities" and "name" in result and "entity_type_id" not in result:
        result["entity_type_id"] = 0
    if isinstance(result.get("episode_indices"), list):
        indices: list[int] = []
        for value in result["episode_indices"]:
            try:
                indices.append(int(value or 0))
            except (TypeError, ValueError):
                indices.append(0)
        result["episode_indices"] = indices
    return result


def sanitize_duplicate_fact_idxs(
    payload: dict[str, Any],
    fields: dict[str, Any],
    valid_duplicate_fact_idxs: set[int] | None,
) -> dict[str, Any]:
    if valid_duplicate_fact_idxs is None or "duplicate_facts" not in fields:
        return payload
    duplicate_facts = payload.get("duplicate_facts")
    if not isinstance(duplicate_facts, list):
        return payload

    filtered: list[int] = []
    for value in duplicate_facts:
        if value is None:
            continue
        try:
            idx = int(value)
        except (TypeError, ValueError):
            continue
        if idx in valid_duplicate_fact_idxs:
            filtered.append(idx)

    result = dict(payload)
    result["duplicate_facts"] = filtered
    return result


def existing_fact_idx_values_from_messages(messages: list[Any] | None) -> set[int] | None:
    if not messages:
        return None
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if not isinstance(content, str):
            continue
        match = EXISTING_FACTS_BLOCK_RE.search(content)
        if not match:
            continue
        return {int(idx.group(1)) for idx in FACT_IDX_RE.finditer(match.group(1))}
    return None


__all__ = [
    "STRUCTURED_KEY_ALIASES",
    "existing_fact_idx_values_from_messages",
    "is_list_annotation",
    "normalize_response_model_item",
    "normalize_response_model_payload",
    "normalize_structured_keys",
    "normalize_structured_response",
    "sanitize_duplicate_fact_idxs",
]

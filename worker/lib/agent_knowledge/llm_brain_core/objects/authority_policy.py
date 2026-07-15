from __future__ import annotations

SINGLE_OBJECT_PRODUCTION_AUTHORITY_CLASSES = ("RepoDocument", "ArtifactPreference")


def knowledge_object_class_from_id(object_id: str) -> str:
    parts = str(object_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "ko":
        return ""
    return parts[1]


def allowed_object_classes_list(classes: tuple[str, ...] = SINGLE_OBJECT_PRODUCTION_AUTHORITY_CLASSES) -> list[str]:
    return list(classes)


def allowed_object_class_gap(classes: tuple[str, ...] = SINGLE_OBJECT_PRODUCTION_AUTHORITY_CLASSES) -> str:
    return "allowed_object_class_" + "_or_".join(classes)


def is_allowed_object_target(
    object_id: str,
    *,
    object_type: str = "",
    classes: tuple[str, ...] = SINGLE_OBJECT_PRODUCTION_AUTHORITY_CLASSES,
) -> bool:
    object_class = knowledge_object_class_from_id(object_id)
    if object_class not in classes:
        return False
    return not object_type or object_type == object_class

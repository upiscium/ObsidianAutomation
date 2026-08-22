from __future__ import annotations

import re
from dataclasses import dataclass

from .canonical_mutation import CreateNoteMutation, MutationValidationError


KNOWLEDGE_ROOT = "11-Knowledge"
POLICY_NAME = "knowledge-note-v0"
MAX_CONTENT_BYTES = 256 * 1024

_ALLOWED_CATEGORIES = {
    "",
    "explanation",
    "manual",
    "troubleshooting",
    "spec",
    "reference",
    "summary",
}
_ALLOWED_SOURCE_TYPES = {
    "self",
    "official",
    "paper",
    "book",
    "web",
    "other",
}
_REQUIRED_FRONTMATTER_KEYS = {
    "type",
    "status",
    "category",
    "maturity",
    "source_type",
}
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_WINDOWS_FORBIDDEN = set('<>:"|?*')


@dataclass(frozen=True)
class KnowledgeFrontmatter:
    note_type: str
    status: str
    category: str
    maturity: str
    source_type: str


def _parse_plain_scalar(raw: str, *, key: str) -> str:
    value = raw.strip()
    if value == "":
        return ""
    if value.startswith(("'", '"', "[", "{", "&", "*", "!", "|", ">", "@", "`")):
        raise MutationValidationError(
            f"Knowledge frontmatter {key} must use a plain scalar"
        )
    if " #" in value or value.startswith("#"):
        raise MutationValidationError(
            f"Knowledge frontmatter {key} must not contain YAML comments"
        )
    if any(ord(ch) < 0x20 for ch in value):
        raise MutationValidationError(
            f"Knowledge frontmatter {key} contains a control character"
        )
    return value


def _parse_frontmatter(content: str) -> tuple[KnowledgeFrontmatter, str]:
    if "\r" in content:
        raise MutationValidationError("Knowledge note content must use LF line endings")
    lines = content.split("\n")
    if not lines or lines[0] != "---":
        raise MutationValidationError("Knowledge note must start with YAML frontmatter")

    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise MutationValidationError("Knowledge frontmatter is not terminated") from exc

    if end > 32:
        raise MutationValidationError("Knowledge frontmatter is unexpectedly large")

    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line:
            continue
        if line[:1].isspace() or ":" not in line:
            raise MutationValidationError(
                "Knowledge frontmatter must contain only top-level scalar fields"
            )
        key, raw = line.split(":", 1)
        if not _KEY_RE.fullmatch(key):
            raise MutationValidationError(f"invalid Knowledge frontmatter key: {key!r}")
        if key in values:
            raise MutationValidationError(f"duplicate Knowledge frontmatter key: {key}")
        values[key] = _parse_plain_scalar(raw, key=key)

    if set(values) != _REQUIRED_FRONTMATTER_KEYS:
        missing = sorted(_REQUIRED_FRONTMATTER_KEYS - set(values))
        unknown = sorted(set(values) - _REQUIRED_FRONTMATTER_KEYS)
        raise MutationValidationError(
            f"Knowledge frontmatter keys do not match policy; missing={missing}, unknown={unknown}"
        )

    body = "\n".join(lines[end + 1 :])
    if not body.strip():
        raise MutationValidationError("Knowledge note body must not be empty")

    return (
        KnowledgeFrontmatter(
            note_type=values["type"],
            status=values["status"],
            category=values["category"],
            maturity=values["maturity"],
            source_type=values["source_type"],
        ),
        body,
    )


def _validate_target_path(target_path: str) -> None:
    parts = target_path.split("/")
    if len(parts) < 2 or parts[0] != KNOWLEDGE_ROOT:
        raise MutationValidationError("Knowledge note target must be below 11-Knowledge")
    for component in parts[1:]:
        if component.startswith("."):
            raise MutationValidationError("Knowledge note target must not use hidden path components")
        if component != component.strip():
            raise MutationValidationError("Knowledge note path components must not have edge whitespace")
        if any(ch in _WINDOWS_FORBIDDEN or ord(ch) < 0x20 for ch in component):
            raise MutationValidationError(
                "Knowledge note path contains a cross-platform unsafe character"
            )


def validate_knowledge_note_v0(mutation: CreateNoteMutation) -> None:
    """Reject create_note mutations outside the AI-created Knowledge note v0 contract."""

    _validate_target_path(mutation.target_path)
    encoded = mutation.content.encode("utf-8")
    if len(encoded) > MAX_CONTENT_BYTES:
        raise MutationValidationError(
            f"Knowledge note exceeds {MAX_CONTENT_BYTES} UTF-8 bytes"
        )

    frontmatter, _ = _parse_frontmatter(mutation.content)
    if frontmatter.note_type != "knowledge-note":
        raise MutationValidationError("Knowledge type must be knowledge-note")
    if frontmatter.status != "active":
        raise MutationValidationError("AI-created Knowledge status must be active")
    if frontmatter.maturity != "draft":
        raise MutationValidationError("AI-created Knowledge maturity must be draft")
    if frontmatter.category not in _ALLOWED_CATEGORIES:
        raise MutationValidationError(
            f"Knowledge category is not allowed: {frontmatter.category!r}"
        )
    if frontmatter.source_type not in _ALLOWED_SOURCE_TYPES:
        raise MutationValidationError(
            f"Knowledge source_type is not allowed: {frontmatter.source_type!r}"
        )

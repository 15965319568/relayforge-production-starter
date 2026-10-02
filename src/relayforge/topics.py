from __future__ import annotations

import re

from .errors import ValidationError


SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def validate_topic(topic: str) -> tuple[str, ...]:
    value = topic.strip()
    parts = tuple(value.split("."))
    if not 1 <= len(parts) <= 12:
        raise ValidationError("topic must contain between 1 and 12 segments")
    for part in parts:
        if not SEGMENT_RE.fullmatch(part):
            raise ValidationError(f"invalid topic segment: {part!r}")
    return parts


def validate_pattern(pattern: str) -> tuple[str, ...]:
    value = pattern.strip()
    parts = tuple(value.split("."))
    if not 1 <= len(parts) <= 12:
        raise ValidationError("topic pattern must contain between 1 and 12 segments")
    for index, part in enumerate(parts):
        if part == "*":
            continue
        if part == "#":
            if index != len(parts) - 1:
                raise ValidationError("# wildcard is only allowed as the final segment")
            continue
        if not SEGMENT_RE.fullmatch(part):
            raise ValidationError(f"invalid pattern segment: {part!r}")
    return parts


def topic_matches(pattern: str, topic: str) -> bool:
    pattern_parts = validate_pattern(pattern)
    topic_parts = validate_topic(topic)
    for index, expected in enumerate(pattern_parts):
        if expected == "#":
            return index < len(topic_parts)
        if index >= len(topic_parts):
            return False
        if expected == "*" and index == len(pattern_parts) - 1:
            return True
        if expected != "*" and expected != topic_parts[index]:
            return False
    return len(topic_parts) == len(pattern_parts)

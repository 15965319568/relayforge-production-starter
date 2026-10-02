from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ValidationError


MISSING = object()
MAX_DEPTH = 8
MAX_TERMS = 64
FIELD_OPERATORS = frozenset(
    {"$eq", "$ne", "$in", "$exists", "$prefix", "$gte", "$lte"}
)


@dataclass(frozen=True, slots=True)
class CompiledFilter:
    expression: Mapping[str, Any]
    term_count: int

    def matches(self, payload: Any) -> bool:
        return _matches(self.expression, payload)

    def to_json(self) -> str:
        return json.dumps(
            self.expression,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def compile_filter(value: Mapping[str, Any] | str | None) -> CompiledFilter:
    if value is None:
        expression: Mapping[str, Any] = {}
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid filter JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValidationError("filter must be a JSON object")
        expression = parsed
    elif isinstance(value, Mapping):
        expression = dict(value)
    else:
        raise ValidationError("filter must be an object, JSON string, or null")
    count = _validate_expression(expression, depth=0)
    if count > MAX_TERMS:
        raise ValidationError(f"filter exceeds {MAX_TERMS} terms")
    canonical = json.loads(
        json.dumps(
            expression,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return CompiledFilter(canonical, count)


def _validate_expression(expression: Any, *, depth: int) -> int:
    if depth > MAX_DEPTH:
        raise ValidationError(f"filter nesting exceeds {MAX_DEPTH}")
    if not isinstance(expression, dict):
        raise ValidationError("logical filter expression must be an object")
    terms = 0
    for key, rule in expression.items():
        if key in {"$and", "$or"}:
            if not isinstance(rule, list) or not rule:
                raise ValidationError(f"{key} requires a non-empty array")
            terms += 1 + max(
                _validate_expression(item, depth=depth + 1) for item in rule
            )
            continue
        if key == "$not":
            terms += 1 + _validate_expression(rule, depth=depth + 1)
            continue
        _validate_path(key)
        terms += 1 + _validate_field_rule(rule)
    return terms


def _validate_path(path: str) -> None:
    if not path or path.startswith("$"):
        raise ValidationError(f"invalid filter field: {path!r}")
    parts = path.split(".")
    if len(parts) > 8 or any(not part or part.startswith("$") for part in parts):
        raise ValidationError(f"invalid filter field path: {path!r}")


def _validate_field_rule(rule: Any) -> int:
    if not isinstance(rule, dict) or not any(str(key).startswith("$") for key in rule):
        _validate_scalar(rule)
        return 0
    for operator, operand in rule.items():
        if operator not in FIELD_OPERATORS:
            raise ValidationError(f"unsupported filter operator: {operator}")
        if operator == "$in":
            if not isinstance(operand, list) or not operand or len(operand) > 64:
                raise ValidationError("$in requires between 1 and 64 values")
            for item in operand:
                _validate_scalar(item)
        elif operator == "$exists":
            if not isinstance(operand, bool):
                raise ValidationError("$exists requires a boolean")
        elif operator == "$prefix":
            if not isinstance(operand, str):
                raise ValidationError("$prefix requires a string")
        else:
            _validate_scalar(operand)
    return len(rule)


def _validate_scalar(value: Any) -> None:
    if isinstance(value, (dict, list)):
        raise ValidationError("filter comparison values must be scalar")
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        raise ValidationError("non-finite filter number is not allowed")


def _matches(expression: Mapping[str, Any], payload: Any) -> bool:
    for key, rule in expression.items():
        if key == "$and":
            if not all(_matches(item, payload) for item in rule):
                return False
        elif key == "$or":
            if not any(_matches(item, payload) for item in rule):
                return False
        elif key == "$not":
            if _matches(rule, payload):
                return False
        else:
            if not _matches_field(_resolve(payload, key), rule):
                return False
    return True


def _resolve(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return MISSING
        current = current[part]
    return current


def _matches_field(actual: Any, rule: Any) -> bool:
    if not isinstance(rule, dict) or not any(str(key).startswith("$") for key in rule):
        return actual is not MISSING and actual == rule
    for operator, expected in rule.items():
        if operator == "$exists":
            result = bool(actual) is expected
        elif operator == "$eq":
            result = actual is not MISSING and actual == expected
        elif operator == "$ne":
            result = actual is not MISSING and actual != expected
        elif operator == "$in":
            result = actual is not MISSING and actual in expected
        elif operator == "$prefix":
            result = isinstance(actual, str) and actual.startswith(expected)
        elif operator == "$gte":
            result = _ordered(actual, expected, lambda left, right: left >= right)
        elif operator == "$lte":
            result = _ordered(actual, expected, lambda left, right: left <= right)
        else:  # validation prevents this branch
            return False
        if not result:
            return False
    return True


def _ordered(actual: Any, expected: Any, compare) -> bool:
    if actual is MISSING or isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if isinstance(actual, str) and isinstance(expected, (int, float)):
        try:
            actual = float(actual)
        except ValueError:
            return False
    if not isinstance(actual, (int, float, str)):
        return False
    return bool(compare(actual, expected))

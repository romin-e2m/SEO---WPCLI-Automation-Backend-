"""Shared row coercion / URL checks / row validation for static and dynamic sheet mapping."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from typing import Any

_URL_START_RE = re.compile(r"^https?://", re.IGNORECASE)


def coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        try:
            if math.isnan(v) or math.isinf(v):
                return ""
        except Exception:
            pass
    return str(v).strip()


def looks_like_url(v: str) -> bool:
    return bool(_URL_START_RE.match(v))


def resolve_source_value(
    source_col: str,
    row: dict[str, Any],
    available_columns: list[str],
    normalize: Callable[[str], str],
) -> Any:
    """Resolve column value from row using ``normalize`` for header matching."""
    if not source_col:
        return None
    if source_col in row:
        return row.get(source_col)
    target = normalize(source_col)
    for c in available_columns:
        if normalize(c) == target:
            return row.get(c)
    return None


def validate_row_fields(
    row_values: dict[str, Any],
    fields: Iterable[tuple[str, bool, str]],
    any_of_groups: Iterable[list[str]],
) -> tuple[bool, list[str]]:
    """
    Validate mapped row values.

    Each field is (key, required, type) where type is e.g. 'url', 'int', 'text'.
    """
    reasons: list[str] = []
    has_any_value = any(coerce_str(v) for v in row_values.values())
    if not has_any_value:
        return False, ["empty_row"]

    for key, required, ftype in fields:
        v = coerce_str(row_values.get(key))
        if not v:
            if required:
                reasons.append(f"missing:{key}")
            continue
        if ftype == "url" and not looks_like_url(v):
            reasons.append(f"invalid_url:{key}")
        elif ftype == "int":
            try:
                int(v)
            except ValueError:
                reasons.append(f"invalid_int:{key}")

    for group in any_of_groups:
        if group and not any(coerce_str(row_values.get(k)) for k in group):
            reasons.append(f"missing_any_of:{'|'.join(group)}")

    return (len(reasons) == 0), reasons

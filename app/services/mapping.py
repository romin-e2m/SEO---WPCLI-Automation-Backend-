from __future__ import annotations

import re
from typing import Any, Iterable

from app.schemas.workbook import (
    ActionType,
    MappingNormalizeResponse,
    MappingValidateResponse,
    NormalizedRow,
    SheetMapping,
    SheetMappingError,
    SheetSummary,
)


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# canonical_field -> {required: bool, type: 'url'|'text'|'int'}
# action_type -> ordered list of canonical fields with metadata.
# `any_of_groups` lets us require "at least one of" for actions like on_page.
ACTION_FIELDS: dict[ActionType, dict[str, Any]] = {
    "on_page": {
        "label": "On-Page (Title / H1 / Content)",
        "fields": [
            {"key": "page_url", "label": "Page URL", "required": True, "type": "url"},
            {"key": "current_title", "label": "Current title", "required": False, "type": "text"},
            {"key": "recommended_title", "label": "Recommended title", "required": False, "type": "text"},
            {"key": "current_h1", "label": "Current H1", "required": False, "type": "text"},
            {"key": "recommended_h1", "label": "Recommended H1", "required": False, "type": "text"},
            {"key": "current_content", "label": "Current content", "required": False, "type": "text"},
            {"key": "recommended_content", "label": "Recommended content", "required": False, "type": "text"},
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [
            ["recommended_title", "recommended_h1", "recommended_content"],
        ],
    },
    "meta": {
        "label": "Meta description",
        "fields": [
            {"key": "page_url", "label": "Page URL", "required": True, "type": "url"},
            {"key": "current_meta_description", "label": "Current meta description", "required": False, "type": "text"},
            {"key": "recommended_meta_description", "label": "Recommended meta description", "required": True, "type": "text"},
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [],
    },
    "images": {
        "label": "Image alt text",
        "fields": [
            {"key": "page_url", "label": "Page URL", "required": True, "type": "url"},
            {"key": "image_url", "label": "Image URL", "required": True, "type": "url"},
            {"key": "current_alt_text", "label": "Current alt text", "required": False, "type": "text"},
            {"key": "recommended_alt_text", "label": "Recommended alt text", "required": True, "type": "text"},
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [],
    },
    "url_cleanup": {
        "label": "URL cleanup in content",
        "fields": [
            {"key": "page_url", "label": "Page URL", "required": True, "type": "url"},
            {"key": "old_url", "label": "URL to replace", "required": True, "type": "url"},
            {"key": "new_url", "label": "New URL", "required": True, "type": "url"},
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [],
    },
    "redirects_301": {
        "label": "301 Redirects",
        "fields": [
            {"key": "source_url", "label": "Source (from) URL", "required": True, "type": "url"},
            {"key": "target_url", "label": "Destination (to) URL", "required": True, "type": "url"},
            {"key": "notes", "label": "Notes", "required": False, "type": "text"},
        ],
        "any_of_groups": [],
    },
}


# canonical_field -> list of header aliases (lower-cased, normalized).
HEADER_ALIASES: dict[str, list[str]] = {
    "page_url": ["page url", "url", "address", "page", "page address", "landing page"],
    "current_title": ["title tag", "current title", "title", "meta title", "page title"],
    "recommended_title": [
        "recommended title",
        "new title",
        "proposed title",
        "suggested title",
        "title (new)",
    ],
    "current_h1": ["h1", "current h1"],
    "recommended_h1": ["recommended h1", "new h1", "proposed h1", "h1 (new)"],
    "current_content": ["content", "current content", "body", "current body"],
    "recommended_content": [
        "recommended content",
        "new content",
        "proposed content",
        "updated content",
        "content (new)",
    ],
    "current_meta_description": [
        "meta description",
        "current meta description",
        "description",
        "current description",
    ],
    "recommended_meta_description": [
        "recommended meta description",
        "new meta description",
        "proposed meta description",
        "new meta",
        "meta description (new)",
        "meta (new)",
    ],
    "image_url": [
        "image url",
        "image",
        "image src",
        "img url",
        "image source",
        "img",
    ],
    "current_alt_text": ["current alt", "alt", "alt text", "current alt text"],
    "recommended_alt_text": [
        "recommended alt text",
        "new alt",
        "proposed alt text",
        "alt (new)",
        "new alt text",
        "alt text (new)",
    ],
    "old_url": [
        "url to replace",
        "old url",
        "from url",
        "find",
        "old",
        "replace from",
    ],
    "new_url": [
        "new url",
        "replacement url",
        "to url",
        "replace with",
        "new",
        "replacement",
    ],
    "source_url": [
        "source (from) url",
        "source url",
        "from",
        "source",
        "from url",
        "redirect from",
    ],
    "target_url": [
        "destination (to) url",
        "destination url",
        "to",
        "target",
        "target url",
        "to url",
        "redirect to",
    ],
    "notes": ["notes", "note", "comment", "comments", "issue / priority", "issue", "priority"],
}


def _norm_header(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def guess_column_map(action_type: ActionType, columns: list[str]) -> dict[str, str]:
    """Return a best-effort canonical_field -> source_header guess."""
    spec = ACTION_FIELDS.get(action_type)
    if not spec:
        return {}
    norm_to_original: dict[str, str] = {}
    for c in columns:
        n = _norm_header(c)
        if n and n not in norm_to_original:
            norm_to_original[n] = c
    out: dict[str, str] = {}
    for f in spec["fields"]:
        key: str = f["key"]
        aliases = HEADER_ALIASES.get(key, [])
        # exact alias match first
        for a in aliases:
            if a in norm_to_original:
                out[key] = norm_to_original[a]
                break
        if key in out:
            continue
        # contains-match fallback: header that contains the alias as a whole word
        for a in aliases:
            for n, original in norm_to_original.items():
                if re.search(rf"(^|\W){re.escape(a)}($|\W)", n):
                    out[key] = original
                    break
            if key in out:
                break
    return out


def guess_action_type(sheet_name: str, columns: list[str]) -> ActionType | None:
    """Return the most likely action type based on the sheet name and headers."""
    n = _norm_header(sheet_name)
    name_hits: dict[ActionType, int] = {}
    if n:
        if "redirect" in n or "301" in n:
            name_hits["redirects_301"] = name_hits.get("redirects_301", 0) + 5
        if "image" in n or "alt" in n:
            name_hits["images"] = name_hits.get("images", 0) + 5
        if "meta" in n and "description" in n:
            name_hits["meta"] = name_hits.get("meta", 0) + 5
        elif n == "meta":
            name_hits["meta"] = name_hits.get("meta", 0) + 5
        if "url" in n and ("cleanup" in n or "clean" in n or "replace" in n):
            name_hits["url_cleanup"] = name_hits.get("url_cleanup", 0) + 5
        if "on_page" in n.replace(" ", "_") or "on page" in n or "title" in n or "h1" in n:
            name_hits["on_page"] = name_hits.get("on_page", 0) + 4

    # Score each action by how many of its required fields are matchable.
    col_hits: dict[ActionType, int] = {}
    for action in ACTION_FIELDS.keys():
        guess = guess_column_map(action, columns)
        spec = ACTION_FIELDS[action]
        required_keys = [f["key"] for f in spec["fields"] if f["required"]]
        matched_required = sum(1 for k in required_keys if k in guess)
        col_hits[action] = matched_required

    best: ActionType | None = None
    best_score = -1
    for action in ACTION_FIELDS.keys():
        score = col_hits.get(action, 0) * 2 + name_hits.get(action, 0)
        # require at least all required mapped to be a confident pick
        spec = ACTION_FIELDS[action]
        required_keys = [f["key"] for f in spec["fields"] if f["required"]]
        if col_hits.get(action, 0) < len(required_keys):
            score = score - 10
        if score > best_score:
            best_score = score
            best = action
    if best is None or best_score < 0:
        return None
    return best


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        # avoid "nan"
        try:
            import math

            if math.isnan(v) or math.isinf(v):
                return ""
        except Exception:
            pass
    return str(v).strip()


def _looks_like_url(v: str) -> bool:
    return bool(_URL_RE.match(v))


def _validate_static(mapping: SheetMapping, available_columns: list[str]) -> list[str]:
    """Static checks against the column_map without inspecting data rows."""
    issues: list[str] = []
    spec = ACTION_FIELDS.get(mapping.action_type)
    if not spec:
        issues.append(f"Unknown action_type '{mapping.action_type}'.")
        return issues

    col_set = {_norm_header(c): c for c in available_columns}
    for canonical, source_col in mapping.column_map.items():
        if not source_col:
            continue
        if _norm_header(source_col) not in col_set:
            issues.append(
                f"Column '{source_col}' (mapped to {canonical}) is not in the sheet headers."
            )

    required_keys = [f["key"] for f in spec["fields"] if f["required"]]
    for k in required_keys:
        if not mapping.column_map.get(k):
            issues.append(f"Required field '{k}' is not mapped.")

    for group in spec.get("any_of_groups", []):
        if group and not any(mapping.column_map.get(k) for k in group):
            pretty = " / ".join(group)
            issues.append(f"At least one of [{pretty}] must be mapped.")

    return issues


def _resolve_source_value(
    canonical_field: str,
    source_col: str,
    row: dict[str, Any],
    available_columns: list[str],
) -> Any:
    if not source_col:
        return None
    if source_col in row:
        return row.get(source_col)
    # case-insensitive fallback
    target = _norm_header(source_col)
    for c in available_columns:
        if _norm_header(c) == target:
            return row.get(c)
    return None


def _validate_row(
    mapping: SheetMapping,
    row_values: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Return (is_valid, skip_reasons). is_valid means it should be included in normalized output."""
    spec = ACTION_FIELDS[mapping.action_type]
    reasons: list[str] = []

    # row is "empty" if all mapped fields are blank: skip silently.
    has_any_value = any(_coerce_str(v) for v in row_values.values())
    if not has_any_value:
        return False, ["empty_row"]

    for f in spec["fields"]:
        key: str = f["key"]
        required: bool = bool(f["required"])
        ftype: str = f["type"]
        v = _coerce_str(row_values.get(key))
        if not v:
            if required:
                reasons.append(f"missing:{key}")
            continue
        if ftype == "url" and not _looks_like_url(v):
            reasons.append(f"invalid_url:{key}")

    # any_of groups
    for group in spec.get("any_of_groups", []):
        if group and not any(_coerce_str(row_values.get(k)) for k in group):
            reasons.append(f"missing_any_of:{'|'.join(group)}")

    return (len(reasons) == 0), reasons


def validate_mapping(
    mappings: list[SheetMapping],
    sheet_columns: dict[str, list[str]],
    sheet_rows: dict[str, list[dict[str, Any]]],
    *,
    filename: str,
    site_url: str | None,
) -> MappingValidateResponse:
    errors: list[SheetMappingError] = []
    summaries: list[SheetSummary] = []

    seen_names: set[str] = set()

    for m in mappings:
        if not m.enabled:
            continue
        sheet_issues: list[str] = []
        if m.sheet_name in seen_names:
            sheet_issues.append("Sheet is referenced more than once in the mapping plan.")
        seen_names.add(m.sheet_name)

        if m.sheet_name not in sheet_columns:
            sheet_issues.append(f"Sheet '{m.sheet_name}' was not found in the workbook.")
            errors.append(
                SheetMappingError(
                    sheet_name=m.sheet_name,
                    action_type=m.action_type,
                    issues=sheet_issues,
                )
            )
            continue

        cols = sheet_columns[m.sheet_name]
        sheet_issues.extend(_validate_static(m, cols))
        if sheet_issues:
            errors.append(
                SheetMappingError(
                    sheet_name=m.sheet_name,
                    action_type=m.action_type,
                    issues=sheet_issues,
                )
            )

        # row-level summary (cheap)
        rows = sheet_rows.get(m.sheet_name, [])
        skip_n = m.skip_header_rows
        # we already excluded the column-header row when reading,
        # so skip_header_rows beyond 1 means extra banner rows to drop.
        extra_skip = max(0, skip_n - 1)
        data_rows = rows[extra_skip:]

        rows_total = 0
        rows_valid = 0
        rows_skipped = 0
        skipped_reasons: dict[str, int] = {}

        for raw in data_rows:
            mapped: dict[str, Any] = {}
            for canonical, source_col in m.column_map.items():
                if not source_col:
                    continue
                mapped[canonical] = _resolve_source_value(canonical, source_col, raw, cols)
            ok, reasons = _validate_row(m, mapped)
            if reasons == ["empty_row"]:
                # silently dropped, doesn't count toward total
                continue
            rows_total += 1
            if ok:
                rows_valid += 1
            else:
                rows_skipped += 1
                for r in reasons:
                    skipped_reasons[r] = skipped_reasons.get(r, 0) + 1

        summaries.append(
            SheetSummary(
                sheet_name=m.sheet_name,
                action_type=m.action_type,
                rows_total=rows_total,
                rows_valid=rows_valid,
                rows_skipped=rows_skipped,
                skipped_reasons=skipped_reasons,
            )
        )

    return MappingValidateResponse(
        ok=len(errors) == 0,
        filename=filename,
        site_url=site_url,
        errors=errors,
        summaries=summaries,
    )


def normalize_mapping(
    mappings: list[SheetMapping],
    sheet_columns: dict[str, list[str]],
    sheet_rows: dict[str, list[dict[str, Any]]],
    *,
    filename: str,
    site_url: str | None,
) -> MappingNormalizeResponse:
    validate = validate_mapping(
        mappings,
        sheet_columns,
        sheet_rows,
        filename=filename,
        site_url=site_url,
    )
    grouped: dict[str, list[NormalizedRow]] = {a: [] for a in ACTION_FIELDS.keys()}

    if not validate.ok:
        return MappingNormalizeResponse(
            ok=False,
            filename=filename,
            site_url=site_url,
            summaries=validate.summaries,
            errors=validate.errors,
            grouped=grouped,
        )

    for m in mappings:
        if not m.enabled:
            continue
        cols = sheet_columns.get(m.sheet_name, [])
        rows = sheet_rows.get(m.sheet_name, [])
        extra_skip = max(0, m.skip_header_rows - 1)
        data_rows = rows[extra_skip:]

        for idx, raw in enumerate(data_rows, start=1):
            mapped: dict[str, Any] = {}
            for canonical, source_col in m.column_map.items():
                if not source_col:
                    continue
                v = _resolve_source_value(canonical, source_col, raw, cols)
                mapped[canonical] = _coerce_str(v) if v is not None else ""
            ok, reasons = _validate_row(m, mapped)
            if reasons == ["empty_row"]:
                continue
            if not ok:
                continue
            grouped[m.action_type].append(
                NormalizedRow(
                    sheet_name=m.sheet_name,
                    row_index=idx,
                    values=mapped,
                )
            )

    return MappingNormalizeResponse(
        ok=True,
        filename=filename,
        site_url=site_url,
        summaries=validate.summaries,
        errors=[],
        grouped=grouped,
    )


def action_fields_descriptor() -> dict[str, Any]:
    """Public descriptor for the frontend so it can render the mapping form
    without hard-coding canonical field metadata."""
    return {
        action: {
            "label": spec["label"],
            "fields": spec["fields"],
            "any_of_groups": spec.get("any_of_groups", []),
        }
        for action, spec in ACTION_FIELDS.items()
    }


def column_aliases_for(canonical: str) -> Iterable[str]:
    return HEADER_ALIASES.get(canonical, [])

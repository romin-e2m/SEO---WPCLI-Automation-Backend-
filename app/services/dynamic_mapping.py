from __future__ import annotations

from typing import Any

from app.schemas.schema import ActionSchema
from app.schemas.workbook import SheetMapping, SheetMappingError, SheetSummary
from app.services.mapping_common import (
    coerce_str as _coerce_str,
    prune_invalid_column_mappings,
    resolve_source_value as _resolve_mapped_cell,
    validate_row_fields,
)


def _norm_header(s: str) -> str:
    return " ".join(str(s or "").strip().lower().split())


def _validate_static(mapping: SheetMapping, available_columns: list[str], schema: ActionSchema) -> list[str]:
    """Static checks against column mapping without inspecting data."""
    issues: list[str] = []

    required_keys = [f.key for f in schema.fields if f.required]
    column_map = prune_invalid_column_mappings(
        mapping.column_map,
        available_columns,
        required_field_keys=required_keys,
        normalize=_norm_header,
    )

    col_set = {_norm_header(c): c for c in available_columns}

    for canonical, source_col in column_map.items():
        if not source_col:
            continue
        if _norm_header(source_col) not in col_set:
            issues.append(
                f"Column '{source_col}' (mapped to {canonical}) is not in the sheet headers."
            )

    for k in required_keys:
        if not column_map.get(k):
            issues.append(f"Required field '{k}' is not mapped.")

    for group in schema.any_of_groups:
        if group.fields and not any(column_map.get(k) for k in group.fields):
            pretty = " / ".join(group.fields)
            issues.append(f"At least one of [{pretty}] must be mapped.")
    
    return issues


def _resolve_source_value(
    canonical_field: str,
    source_col: str,
    row: dict[str, Any],
    available_columns: list[str],
) -> Any:
    _ = canonical_field
    return _resolve_mapped_cell(source_col, row, available_columns, lambda c: c.lower())


def _validate_row(
    row_values: dict[str, Any],
    schema: ActionSchema,
) -> tuple[bool, list[str]]:
    fields = [(f.key, f.required, str(f.type)) for f in schema.fields]
    groups = [list(g.fields) for g in schema.any_of_groups if g.fields]
    return validate_row_fields(row_values, fields, groups)


def validate_mapping_dynamic(
    mappings: list[SheetMapping],
    sheet_columns: dict[str, list[str]],
    sheet_rows: dict[str, list[dict[str, Any]]],
    schemas: dict[str, ActionSchema],
    *,
    filename: str,
    site_url: str | None,
) -> dict[str, Any]:
    """Validate mappings using dynamic schemas.
    
    Returns dict with ok, filename, site_url, errors, summaries.
    """
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
        
        # Get the schema
        schema = schemas.get(m.action_type)
        if not schema:
            sheet_issues.append(f"Action type '{m.action_type}' schema not found.")
            errors.append(
                SheetMappingError(
                    sheet_name=m.sheet_name,
                    action_type=m.action_type,
                    issues=sheet_issues,
                )
            )
            continue
        
        cols = sheet_columns[m.sheet_name]
        sheet_issues.extend(_validate_static(m, cols, schema))
        
        if sheet_issues:
            errors.append(
                SheetMappingError(
                    sheet_name=m.sheet_name,
                    action_type=m.action_type,
                    issues=sheet_issues,
                )
            )
        
        # header rows already excluded by read_full_sheets
        data_rows = sheet_rows.get(m.sheet_name, [])
        
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
            
            ok, reasons = _validate_row(mapped, schema)
            if reasons == ["empty_row"]:
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
    
    return {
        "ok": len(errors) == 0,
        "filename": filename,
        "site_url": site_url,
        "errors": errors,
        "summaries": summaries,
    }


def normalize_mapping_dynamic(
    mappings: list[SheetMapping],
    sheet_columns: dict[str, list[str]],
    sheet_rows: dict[str, list[dict[str, Any]]],
    schemas: dict[str, ActionSchema],
    *,
    filename: str,
    site_url: str | None,
) -> dict[str, Any]:
    """Normalize mappings using dynamic schemas.
    
    Returns dict with ok, filename, site_url, summaries, errors, grouped.
    """
    validate_resp = validate_mapping_dynamic(
        mappings,
        sheet_columns,
        sheet_rows,
        schemas,
        filename=filename,
        site_url=site_url,
    )
    
    grouped: dict[str, list[dict[str, Any]]] = {}
    
    if not validate_resp["ok"]:
        return {
            "ok": False,
            "filename": filename,
            "site_url": site_url,
            "summaries": validate_resp["summaries"],
            "errors": validate_resp["errors"],
            "grouped": grouped,
        }
    
    for m in mappings:
        if not m.enabled:
            continue
        
        cols = sheet_columns.get(m.sheet_name, [])
        # header rows already excluded by read_full_sheets
        data_rows = sheet_rows.get(m.sheet_name, [])
        
        if m.action_type not in grouped:
            grouped[m.action_type] = []
        
        for idx, raw in enumerate(data_rows, start=1):
            mapped: dict[str, Any] = {}
            for canonical, source_col in m.column_map.items():
                if not source_col:
                    continue
                v = _resolve_source_value(canonical, source_col, raw, cols)
                mapped[canonical] = _coerce_str(v) if v is not None else ""
            
            schema = schemas.get(m.action_type)
            if not schema:
                continue
            
            ok, reasons = _validate_row(mapped, schema)
            if reasons == ["empty_row"]:
                continue
            if not ok:
                continue
            
            grouped[m.action_type].append({
                "sheet_name": m.sheet_name,
                "row_index": idx,
                "values": mapped,
                "original_values": dict(raw),
            })
    
    return {
        "ok": True,
        "filename": filename,
        "site_url": site_url,
        "summaries": validate_resp["summaries"],
        "errors": [],
        "grouped": grouped,
    }

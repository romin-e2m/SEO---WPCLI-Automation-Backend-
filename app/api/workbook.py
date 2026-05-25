import json
import os
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request

from app.schemas.workbook import (
    MappingNormalizeResponse,
    MappingNormalizeUrlRequest,
    MappingValidateResponse,
    MappingValidateUrlRequest,
    SheetMapping,
    SheetPreview,
    WorkbookAnalyzeResponse,
    WorkbookAnalyzeUrlRequest,
)
from app.services.excel_ingest import (
    analyze_spreadsheet_bytes,
    download_spreadsheet_from_url,
    read_full_sheets,
)
from app.services.mapping import (
    ACTION_FIELDS,
    action_fields_descriptor,
    guess_action_type,
    guess_column_map,
    normalize_mapping,
    validate_mapping,
)
from app.services.dynamic_mapping import (
    validate_mapping_dynamic,
    normalize_mapping_dynamic,
)

router = APIRouter(prefix="/api/workbook", tags=["workbook"])

_DEFAULT_MAX_BYTES = 20 * 1024 * 1024


def _header_rows_from_mappings(mappings: list[SheetMapping]) -> dict[str, int]:
    """Build {sheet_name: skip_header_rows} for enabled mappings."""
    return {m.sheet_name: m.skip_header_rows for m in mappings if m.enabled}


async def _download_workbook_full_for_mappings(
    url: str, mappings: list[SheetMapping]
) -> tuple[str, dict[str, Any]]:
    """Download spreadsheet and read full data for the sheets referenced by mappings."""
    body, filename = await download_spreadsheet_from_url(str(url))
    wanted = sorted({m.sheet_name for m in mappings if m.enabled})
    header_rows = _header_rows_from_mappings(mappings)
    full = read_full_sheets(body, filename, sheet_names=wanted or None, header_rows=header_rows)
    return filename, full


def _max_upload_bytes() -> int:
    raw = os.getenv("MAX_WORKBOOK_UPLOAD_BYTES", str(_DEFAULT_MAX_BYTES))
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _decorate_with_suggestions(sheet: SheetPreview, manager: Any = None) -> SheetPreview:
    """Attach a best-effort action_type + column_map guess to a SheetPreview."""
    if not sheet.columns:
        return sheet
    
    # Try dynamic inference first if manager available
    if manager:
        schema, confidence, _ = manager.infer_schema(sheet.name, sheet.columns)
        if schema and confidence >= 0.3:
            sheet.suggested_action_type = schema.id
            # Build column_map based on field matching
            col_map = {}
            cols_lower = {c.lower(): c for c in sheet.columns}
            for field in schema.fields:
                for alias in [field.key.lower(), field.key.lower().replace("_", " ")]:
                    for col_lower, col_orig in cols_lower.items():
                        if alias in col_lower or col_lower == alias:
                            col_map[field.key] = col_orig
                            break
                    if field.key in col_map:
                        break
            # Key-alias matching misses short headers like "H1" for current_h1; merge static guesses.
            if schema.id in ACTION_FIELDS:
                col_set = set(sheet.columns)
                for k, v in guess_column_map(schema.id, sheet.columns).items():
                    if k not in col_map and v and v in col_set:
                        col_map[k] = v
            sheet.suggested_column_map = col_map
            return sheet
    
    # Fall back to old method for backwards compatibility
    action = guess_action_type(sheet.name, sheet.columns)
    if action is None:
        return sheet
    sheet.suggested_action_type = action
    sheet.suggested_column_map = guess_column_map(action, sheet.columns)
    return sheet


@router.post("/analyze", response_model=WorkbookAnalyzeResponse)
async def analyze_workbook(
    file: Annotated[
        UploadFile,
        File(description="Semrush / SEO data: .xlsx, .xls, or Google Sheets .csv export"),
    ],
    site_url: Annotated[str | None, Form()] = None,
    preview_rows: Annotated[int, Form()] = 10,
    header_rows: Annotated[str | None, Form()] = None,
    request: Request = None,
) -> WorkbookAnalyzeResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing file name.")
    lower = file.filename.lower()
    if not lower.endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(
            status_code=400,
            detail="Supported types: .xlsx, .xls (Excel 97-2003), .csv (e.g. Google Sheets export).",
        )

    limit = _max_upload_bytes()
    body = await file.read()
    if len(body) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum size of {limit} bytes.",
        )

    pr = preview_rows
    if pr < 1:
        pr = 1
    if pr > 100:
        pr = 100

    parsed_header_rows: dict[str, int] | None = None
    if header_rows:
        try:
            raw = json.loads(header_rows)
            if isinstance(raw, dict):
                parsed_header_rows = {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}
        except Exception:
            pass

    try:
        sheets_raw, _ = analyze_spreadsheet_bytes(body, file.filename, preview_rows=pr, header_rows=parsed_header_rows)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="Could not read file. Use a valid .xlsx, .xls, or UTF-8/Windows CSV export.",
        ) from e

    manager = request.app.schema_manager if request else None
    sheets = [_decorate_with_suggestions(SheetPreview.model_validate(s), manager) for s in sheets_raw]
    cleaned_site = (site_url or "").strip() or None

    return WorkbookAnalyzeResponse(
        filename=file.filename,
        site_url=cleaned_site,
        sheets=sheets,
    )


@router.post("/analyze-url", response_model=WorkbookAnalyzeResponse)
async def analyze_workbook_url(payload: WorkbookAnalyzeUrlRequest, request: Request) -> WorkbookAnalyzeResponse:
    cleaned_site = (payload.site_url or "").strip() or None

    try:
        body, filename = await download_spreadsheet_from_url(str(payload.url))
        sheets_raw, _ = analyze_spreadsheet_bytes(body, filename, preview_rows=payload.preview_rows)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail="Could not download or read spreadsheet from URL.") from e

    manager = request.app.schema_manager
    sheets = [_decorate_with_suggestions(SheetPreview.model_validate(s), manager) for s in sheets_raw]
    return WorkbookAnalyzeResponse(filename=filename, site_url=cleaned_site, sheets=sheets)


@router.get("/mapping/descriptor")
def mapping_descriptor() -> dict[str, Any]:
    """Return canonical field metadata so the frontend can render the mapping form."""
    return {"actions": action_fields_descriptor()}


@router.post("/mapping/validate", response_model=MappingValidateResponse)
async def validate_mapping_url(payload: MappingValidateUrlRequest) -> MappingValidateResponse:
    cleaned_site = (payload.site_url or "").strip() or None
    try:
        filename, full = await _download_workbook_full_for_mappings(
            str(payload.url), payload.mappings
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="Could not download or read spreadsheet from URL.",
        ) from e

    sheet_columns = {name: data["columns"] for name, data in full.items()}
    sheet_rows = {name: data["rows"] for name, data in full.items()}
    return validate_mapping(
        payload.mappings,
        sheet_columns,
        sheet_rows,
        filename=filename,
        site_url=cleaned_site,
    )


@router.post("/mapping/normalize", response_model=MappingNormalizeResponse)
async def normalize_mapping_url(payload: MappingNormalizeUrlRequest) -> MappingNormalizeResponse:
    cleaned_site = (payload.site_url or "").strip() or None
    try:
        filename, full = await _download_workbook_full_for_mappings(
            str(payload.url), payload.mappings
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="Could not download or read spreadsheet from URL.",
        ) from e

    sheet_columns = {name: data["columns"] for name, data in full.items()}
    sheet_rows = {name: data["rows"] for name, data in full.items()}
    return normalize_mapping(
        payload.mappings,
        sheet_columns,
        sheet_rows,
        filename=filename,
        site_url=cleaned_site,
    )


@router.post("/mapping/validate/upload", response_model=MappingValidateResponse)
async def validate_mapping_upload(
    file: Annotated[UploadFile, File()],
    mappings: Annotated[str, Form()],
    site_url: Annotated[str | None, Form()] = None,
) -> MappingValidateResponse:
    body = await file.read()
    try:
        mapping_list = [SheetMapping(**m) for m in json.loads(mappings)]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid mappings payload: {e}") from e
    wanted = sorted({m.sheet_name for m in mapping_list if m.enabled})
    header_rows = _header_rows_from_mappings(mapping_list)
    try:
        full = read_full_sheets(body, file.filename or "upload", sheet_names=wanted or None, header_rows=header_rows)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Could not read uploaded file.") from e
    sheet_columns = {name: data["columns"] for name, data in full.items()}
    sheet_rows = {name: data["rows"] for name, data in full.items()}
    cleaned_site = (site_url or "").strip() or None
    return validate_mapping(
        mapping_list, sheet_columns, sheet_rows,
        filename=file.filename or "upload", site_url=cleaned_site,
    )


@router.post("/mapping/normalize/upload", response_model=MappingNormalizeResponse)
async def normalize_mapping_upload(
    file: Annotated[UploadFile, File()],
    mappings: Annotated[str, Form()],
    site_url: Annotated[str | None, Form()] = None,
) -> MappingNormalizeResponse:
    body = await file.read()
    try:
        mapping_list = [SheetMapping(**m) for m in json.loads(mappings)]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid mappings payload: {e}") from e
    wanted = sorted({m.sheet_name for m in mapping_list if m.enabled})
    header_rows = _header_rows_from_mappings(mapping_list)
    try:
        full = read_full_sheets(body, file.filename or "upload", sheet_names=wanted or None, header_rows=header_rows)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Could not read uploaded file.") from e
    sheet_columns = {name: data["columns"] for name, data in full.items()}
    sheet_rows = {name: data["rows"] for name, data in full.items()}
    cleaned_site = (site_url or "").strip() or None
    return normalize_mapping(
        mapping_list, sheet_columns, sheet_rows,
        filename=file.filename or "upload", site_url=cleaned_site,
    )


@router.post("/mapping/validate/dynamic", response_model=MappingValidateResponse)
async def validate_mapping_dynamic_url(payload: MappingValidateUrlRequest, request: Request) -> MappingValidateResponse:
    """Validate mappings using dynamic/user-defined schemas."""
    cleaned_site = (payload.site_url or "").strip() or None
    manager = request.app.schema_manager
    
    try:
        filename, full = await _download_workbook_full_for_mappings(
            str(payload.url), payload.mappings
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="Could not download or read spreadsheet from URL.",
        ) from e

    sheet_columns = {name: data["columns"] for name, data in full.items()}
    sheet_rows = {name: data["rows"] for name, data in full.items()}
    
    # Convert schemas to dict for dynamic validation
    schemas = {s.id: s for s in manager.list_schemas()}
    
    resp = validate_mapping_dynamic(
        payload.mappings,
        sheet_columns,
        sheet_rows,
        schemas,
        filename=filename,
        site_url=cleaned_site,
    )
    return MappingValidateResponse(**resp)


@router.post("/mapping/normalize/dynamic", response_model=MappingNormalizeResponse)
async def normalize_mapping_dynamic_url(payload: MappingNormalizeUrlRequest, request: Request) -> MappingNormalizeResponse:
    """Normalize mappings using dynamic/user-defined schemas."""
    cleaned_site = (payload.site_url or "").strip() or None
    manager = request.app.schema_manager
    
    try:
        filename, full = await _download_workbook_full_for_mappings(
            str(payload.url), payload.mappings
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail="Could not download or read spreadsheet from URL.",
        ) from e

    sheet_columns = {name: data["columns"] for name, data in full.items()}
    sheet_rows = {name: data["rows"] for name, data in full.items()}
    
    # Convert schemas to dict for dynamic normalization
    schemas = {s.id: s for s in manager.list_schemas()}
    
    resp = normalize_mapping_dynamic(
        payload.mappings,
        sheet_columns,
        sheet_rows,
        schemas,
        filename=filename,
        site_url=cleaned_site,
    )
    return MappingNormalizeResponse(**resp)
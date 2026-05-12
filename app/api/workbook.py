import os
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request

from app.schemas.workbook import (
    MappingNormalizeResponse,
    MappingNormalizeUrlRequest,
    MappingValidateResponse,
    MappingValidateUrlRequest,
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

    try:
        sheets_raw, _ = analyze_spreadsheet_bytes(body, file.filename, preview_rows=pr)
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
        body, filename = await download_spreadsheet_from_url(str(payload.url))
        wanted = sorted({m.sheet_name for m in payload.mappings if m.enabled})
        full = read_full_sheets(body, filename, sheet_names=wanted or None)
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
        body, filename = await download_spreadsheet_from_url(str(payload.url))
        wanted = sorted({m.sheet_name for m in payload.mappings if m.enabled})
        full = read_full_sheets(body, filename, sheet_names=wanted or None)
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


@router.post("/mapping/validate/dynamic", response_model=MappingValidateResponse)
async def validate_mapping_dynamic_url(payload: MappingValidateUrlRequest, request: Request) -> MappingValidateResponse:
    """Validate mappings using dynamic/user-defined schemas."""
    cleaned_site = (payload.site_url or "").strip() or None
    manager = request.app.schema_manager
    
    try:
        body, filename = await download_spreadsheet_from_url(str(payload.url))
        wanted = sorted({m.sheet_name for m in payload.mappings if m.enabled})
        full = read_full_sheets(body, filename, sheet_names=wanted or None)
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
        body, filename = await download_spreadsheet_from_url(str(payload.url))
        wanted = sorted({m.sheet_name for m in payload.mappings if m.enabled})
        full = read_full_sheets(body, filename, sheet_names=wanted or None)
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
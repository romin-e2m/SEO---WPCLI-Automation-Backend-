import os
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

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

router = APIRouter(prefix="/api/workbook", tags=["workbook"])

_DEFAULT_MAX_BYTES = 20 * 1024 * 1024


def _max_upload_bytes() -> int:
    raw = os.getenv("MAX_WORKBOOK_UPLOAD_BYTES", str(_DEFAULT_MAX_BYTES))
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _decorate_with_suggestions(sheet: SheetPreview) -> SheetPreview:
    """Attach a best-effort action_type + column_map guess to a SheetPreview."""
    if not sheet.columns:
        return sheet
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

    sheets = [_decorate_with_suggestions(SheetPreview.model_validate(s)) for s in sheets_raw]
    cleaned_site = (site_url or "").strip() or None

    return WorkbookAnalyzeResponse(
        filename=file.filename,
        site_url=cleaned_site,
        sheets=sheets,
    )


@router.post("/analyze-url", response_model=WorkbookAnalyzeResponse)
async def analyze_workbook_url(payload: WorkbookAnalyzeUrlRequest) -> WorkbookAnalyzeResponse:
    cleaned_site = (payload.site_url or "").strip() or None

    try:
        body, filename = await download_spreadsheet_from_url(str(payload.url))
        sheets_raw, _ = analyze_spreadsheet_bytes(body, filename, preview_rows=payload.preview_rows)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail="Could not download or read spreadsheet from URL.") from e

    sheets = [_decorate_with_suggestions(SheetPreview.model_validate(s)) for s in sheets_raw]
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


__all__ = ["router", "guess_action_type", "guess_column_map"]

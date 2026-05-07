import os
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.schemas.workbook import SheetPreview, WorkbookAnalyzeResponse, WorkbookAnalyzeUrlRequest
from app.services.excel_ingest import analyze_spreadsheet_bytes, download_spreadsheet_from_url

router = APIRouter(prefix="/api/workbook", tags=["workbook"])

_DEFAULT_MAX_BYTES = 20 * 1024 * 1024


def _max_upload_bytes() -> int:
    raw = os.getenv("MAX_WORKBOOK_UPLOAD_BYTES", str(_DEFAULT_MAX_BYTES))
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MAX_BYTES


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

    sheets = [SheetPreview.model_validate(s) for s in sheets_raw]
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

    sheets = [SheetPreview.model_validate(s) for s in sheets_raw]
    return WorkbookAnalyzeResponse(filename=filename, site_url=cleaned_site, sheets=sheets)

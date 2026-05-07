from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class SheetPreview(BaseModel):
    name: str
    columns: list[str]
    row_count: int | None = Field(
        default=None,
        description="Total rows reported by the workbook reader when available.",
    )
    preview_row_count: int
    rows: list[dict[str, Any]]


class WorkbookAnalyzeResponse(BaseModel):
    filename: str
    site_url: str | None = None
    sheets: list[SheetPreview]


class WorkbookAnalyzeUrlRequest(BaseModel):
    url: HttpUrl = Field(description="Publicly accessible spreadsheet URL.")
    site_url: str | None = Field(default=None)
    preview_rows: int = Field(default=10, ge=1, le=100)

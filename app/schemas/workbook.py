from typing import Any

from pydantic import BaseModel, Field


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

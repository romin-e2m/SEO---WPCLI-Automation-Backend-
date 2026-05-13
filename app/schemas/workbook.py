from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


ActionType = Literal[
    "on_page",
    "meta",
    "meta_title",
    "images",
    "url_cleanup",
    "redirects_301",
]


class SheetPreview(BaseModel):
    name: str
    columns: list[str]
    row_count: int | None = Field(
        default=None,
        description="Total rows reported by the workbook reader when available.",
    )
    preview_row_count: int
    rows: list[dict[str, Any]]
    suggested_action_type: ActionType | None = Field(
        default=None,
        description="Best-effort guess of which canonical action this sheet represents.",
    )
    suggested_column_map: dict[str, str] = Field(
        default_factory=dict,
        description="Best-effort canonical_field -> source_column header guess.",
    )


class WorkbookAnalyzeResponse(BaseModel):
    filename: str
    site_url: str | None = None
    sheets: list[SheetPreview]


class WorkbookAnalyzeUrlRequest(BaseModel):
    url: HttpUrl = Field(description="Publicly accessible spreadsheet URL.")
    site_url: str | None = Field(default=None)
    preview_rows: int = Field(default=10, ge=1)


class SheetMapping(BaseModel):
    """User decision: this spreadsheet sheet plays the role of <action_type>,
    and these source columns map to the canonical fields the executor expects."""

    sheet_name: str = Field(description="Sheet name from the analyzed workbook.")
    action_type: ActionType
    column_map: dict[str, str] = Field(
        default_factory=dict,
        description="canonical_field -> source column header in the sheet.",
    )
    skip_header_rows: int = Field(
        default=1,
        ge=0,
        le=20,
        description="Header rows to skip before data starts (default 1).",
    )
    enabled: bool = True


class MappingValidateUrlRequest(BaseModel):
    url: HttpUrl
    site_url: str | None = None
    mappings: list[SheetMapping]


class MappingNormalizeUrlRequest(MappingValidateUrlRequest):
    pass


class SheetMappingError(BaseModel):
    sheet_name: str
    action_type: ActionType | None = None
    issues: list[str]


class SheetSummary(BaseModel):
    sheet_name: str
    action_type: ActionType
    rows_total: int = 0
    rows_valid: int = 0
    rows_skipped: int = 0
    skipped_reasons: dict[str, int] = Field(default_factory=dict)


class MappingValidateResponse(BaseModel):
    ok: bool
    filename: str
    site_url: str | None = None
    errors: list[SheetMappingError] = Field(default_factory=list)
    summaries: list[SheetSummary] = Field(default_factory=list)


class NormalizedRow(BaseModel):
    sheet_name: str
    row_index: int = Field(description="1-based index in the sheet (excluding skipped headers).")
    values: dict[str, Any]


class MappingNormalizeResponse(BaseModel):
    ok: bool
    filename: str
    site_url: str | None = None
    summaries: list[SheetSummary] = Field(default_factory=list)
    errors: list[SheetMappingError] = Field(default_factory=list)
    grouped: dict[str, list[NormalizedRow]] = Field(default_factory=dict)

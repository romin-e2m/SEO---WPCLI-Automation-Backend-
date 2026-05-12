from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.workbook import NormalizedRow
from app.schemas.wp import SiteAccess


class RunGroupedPayload(BaseModel):
    """Normalized workbook rows grouped by action type (same shape as /mapping/normalize)."""

    grouped: dict[str, list[NormalizedRow]] = Field(default_factory=dict)


class FieldDiff(BaseModel):
    field: str
    current: str | None = None
    proposed: str | None = None


class DryRunRowResult(BaseModel):
    action_type: str
    sheet_name: str
    row_index: int
    outcome: Literal["change", "no_change", "blocked", "error"]
    message: str | None = None
    post_id: int | None = None
    attachment_id: int | None = None
    resolution_method: str | None = None
    diffs: list[FieldDiff] = Field(default_factory=list)


class DryRunResponse(BaseModel):
    rows_processed: int
    ready_to_execute: int
    blocked: int
    errors: int
    no_change: int
    rows: list[DryRunRowResult] = Field(default_factory=list)


class DryRunRequest(RunGroupedPayload):
    site: SiteAccess
    redirect_plugin: str | None = None
    seo_plugin: str | None = None


class ExecuteRowResult(BaseModel):
    action_type: str
    sheet_name: str
    row_index: int
    outcome: Literal["updated", "skipped", "failed"]
    message: str | None = None
    post_id: int | None = None
    attachment_id: int | None = None
    detail: dict[str, Any] | None = None


class ExecuteResponse(BaseModel):
    rows_processed: int
    updated: int
    skipped: int
    failed: int
    rows: list[ExecuteRowResult] = Field(default_factory=list)
    execution_id: str | None = Field(
        default=None,
        description="Populated for /api/run/execute: use with GET /api/run/status and /api/run/stream.",
    )


class ExecuteRequest(RunGroupedPayload):
    site: SiteAccess
    confirm_execute: bool = Field(
        ...,
        description="Must be true to apply writes.",
    )
    redirect_plugin: str | None = None
    seo_plugin: str | None = None

    @field_validator("confirm_execute")
    @classmethod
    def _must_be_true(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("confirm_execute must be true to run the executor.")
        return v

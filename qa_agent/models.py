from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExecuteRowResult(BaseModel):
    """Input model representing one row result from the automation backend."""

    action_type: str  # "on_page" | "meta" | "meta_title" | "images" | "url_cleanup" | "redirects_301"
    sheet_name: str
    row_index: int
    outcome: str  # "updated" | "skipped" | "failed"
    message: str | None = None
    post_id: int | None = None          # present for on_page, meta, meta_title, url_cleanup
    attachment_id: int | None = None    # present ONLY for images
    detail: dict[str, Any] | None = None


class ExecuteResponse(BaseModel):
    """Full response from POST /api/run/execute."""

    execution_id: str | None = None
    rows: list[ExecuteRowResult] = Field(default_factory=list)
    # Allow extra fields from the real API response without validation errors
    model_config = {"extra": "allow"}


class QARowResult(BaseModel):
    """Result of QA verification for a single row."""

    action_type: str
    row_index: int
    sheet_name: str
    post_id: int | None = None
    attachment_id: int | None = None
    expected: str | None = None
    actual: str | None = None
    verified: bool
    method: str | None = None       # "rest_api", "html_scrape", "http_head", etc.
    error: str | None = None
    extra: dict[str, Any] | None = None  # bonus info (e.g. layer1 result for redirects)


class QASubagentReport(BaseModel):
    """Aggregated results for one action_type from a single subagent."""

    action_type: str
    total: int
    passed: int
    failed: int
    errors: int
    rows: list[QARowResult]


class QAReport(BaseModel):
    """Top-level QA report produced by the orchestrator."""

    execution_id: str | None = None
    timestamp: str
    site_url: str
    total_rows_checked: int
    total_passed: int
    total_failed: int
    total_errors: int
    subagent_reports: list[QASubagentReport]
    summary_lines: list[str]        # human-readable lines for printing

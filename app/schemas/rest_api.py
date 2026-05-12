"""
Pydantic schemas for REST API task operations.
"""
from pydantic import BaseModel, Field
from typing import Any, Literal
from app.services.wp_rest_api import TaskType, RedirectSource


class DryRunRequest(BaseModel):
    """Base dry-run request."""
    task_type: TaskType
    site_url: str = Field(..., description="WordPress site URL")
    username: str = Field(..., description="WordPress username")
    app_password: str = Field(..., description="Application password")


class UpdateTitleDryRunRequest(DryRunRequest):
    """Dry-run for title update."""
    task_type: Literal[TaskType.UPDATE_TITLE]
    post_id: int
    new_title: str


class UpdateContentDryRunRequest(DryRunRequest):
    """Dry-run for content update."""
    task_type: Literal[TaskType.UPDATE_CONTENT]
    post_id: int
    new_content: str


class UpdateAltTextDryRunRequest(DryRunRequest):
    """Dry-run for alt text update."""
    task_type: Literal[TaskType.UPDATE_ALT_TEXT]
    media_id: int
    alt_text: str


class CleanupUrlsDryRunRequest(DryRunRequest):
    """Dry-run for URL cleanup."""
    task_type: Literal[TaskType.CLEANUP_URLS]
    post_id: int
    replacements: dict[str, str] = Field(..., description="Old URL -> New URL mapping")


class CreateRedirectDryRunRequest(DryRunRequest):
    """Dry-run for redirect creation."""
    task_type: Literal[TaskType.CREATE_REDIRECT]
    source_url: str
    target_url: str
    plugin: RedirectSource = RedirectSource.REDIRECTION_PLUGIN


class DryRunResponse(BaseModel):
    """Response for dry-run operations."""
    task_type: str
    resource_id: int | str
    current_state: dict[str, Any]
    proposed_changes: dict[str, Any]
    validation_passed: bool
    warnings: list[str]
    errors: list[str]


class ExecuteOperationRequest(BaseModel):
    """Request to execute an operation."""
    task_type: TaskType
    site_url: str = Field(..., description="WordPress site URL")
    username: str = Field(..., description="WordPress username")
    app_password: str = Field(..., description="Application password")


class UpdateTitleExecuteRequest(ExecuteOperationRequest):
    """Execute title update."""
    task_type: Literal[TaskType.UPDATE_TITLE]
    post_id: int
    new_title: str


class UpdateContentExecuteRequest(ExecuteOperationRequest):
    """Execute content update."""
    task_type: Literal[TaskType.UPDATE_CONTENT]
    post_id: int
    new_content: str


class UpdateAltTextExecuteRequest(ExecuteOperationRequest):
    """Execute alt text update."""
    task_type: Literal[TaskType.UPDATE_ALT_TEXT]
    media_id: int
    alt_text: str


class CleanupUrlsExecuteRequest(ExecuteOperationRequest):
    """Execute URL cleanup."""
    task_type: Literal[TaskType.CLEANUP_URLS]
    post_id: int
    replacements: dict[str, str]


class CreateRedirectExecuteRequest(ExecuteOperationRequest):
    """Execute redirect creation."""
    task_type: Literal[TaskType.CREATE_REDIRECT]
    source_url: str
    target_url: str
    plugin: RedirectSource = RedirectSource.REDIRECTION_PLUGIN


class OperationResponse(BaseModel):
    """Response for executed operations."""
    task_type: str
    resource_id: int | str
    success: bool
    message: str
    data: dict[str, Any] | None = None
    duration_ms: float
    error: str | None = None


class BatchOperationRequest(BaseModel):
    """Request to execute multiple operations."""
    operations: list[
        UpdateTitleExecuteRequest
        | UpdateContentExecuteRequest
        | UpdateAltTextExecuteRequest
        | CleanupUrlsExecuteRequest
        | CreateRedirectExecuteRequest
    ]
    stop_on_error: bool = False


class BatchOperationResponse(BaseModel):
    """Response for batch operations."""
    total_operations: int
    successful: int
    failed: int
    operations: list[OperationResponse]
    total_duration_ms: float

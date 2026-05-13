"""
REST API endpoints for WordPress SEO automation tasks.
"""
from __future__ import annotations

import logging
import time
from fastapi import APIRouter, HTTPException

from app.services.wp_rest import WordPressRESTClient, RedirectSource, TaskType
from app.schemas.rest_api import (
    DryRunResponse,
    OperationResponse,
    BatchOperationResponse,
    UpdateTitleDryRunRequest,
    UpdateTitleExecuteRequest,
    UpdateContentDryRunRequest,
    UpdateContentExecuteRequest,
    UpdateAltTextDryRunRequest,
    UpdateAltTextExecuteRequest,
    CleanupUrlsDryRunRequest,
    CleanupUrlsExecuteRequest,
    CreateRedirectDryRunRequest,
    CreateRedirectExecuteRequest,
    BatchOperationRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/rest-api", tags=["rest-api"])


def _get_client(site_url: str, username: str, app_password: str) -> WordPressRESTClient:
    """Create a WordPress REST client."""
    try:
        client = WordPressRESTClient(site_url, username, app_password)
        client.health_check()
        return client
    except Exception as e:
        logger.error("Failed to create REST client: %s", e)
        raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")


# ========================
# TITLE UPDATE ENDPOINTS
# ========================


@router.post("/dry-run/update-title", response_model=DryRunResponse)
def dry_run_update_title(body: UpdateTitleDryRunRequest) -> DryRunResponse:
    """Dry-run for updating post title."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())
        result = client.update_post_title(body.post_id, body.new_title)
        return DryRunResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Dry-run failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Dry-run failed: {str(e)}")


@router.post("/execute/update-title", response_model=OperationResponse)
def execute_update_title(body: UpdateTitleExecuteRequest) -> OperationResponse:
    """Execute post title update."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())
        result = client.apply_update_post_title(body.post_id, body.new_title)
        return OperationResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Execution failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Execution failed: {str(e)}")


# ========================
# CONTENT UPDATE ENDPOINTS
# ========================


@router.post("/dry-run/update-content", response_model=DryRunResponse)
def dry_run_update_content(body: UpdateContentDryRunRequest) -> DryRunResponse:
    """Dry-run for updating post content."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())
        result = client.update_post_content(body.post_id, body.new_content)
        return DryRunResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Dry-run failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Dry-run failed: {str(e)}")


@router.post("/execute/update-content", response_model=OperationResponse)
def execute_update_content(body: UpdateContentExecuteRequest) -> OperationResponse:
    """Execute post content update."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())
        result = client.apply_update_post_content(body.post_id, body.new_content)
        return OperationResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Execution failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Execution failed: {str(e)}")


# ========================
# ALT TEXT UPDATE ENDPOINTS
# ========================


@router.post("/dry-run/update-alt-text", response_model=DryRunResponse)
def dry_run_update_alt_text(body: UpdateAltTextDryRunRequest) -> DryRunResponse:
    """Dry-run for updating image alt text."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())
        result = client.update_image_alt_text(body.media_id, body.alt_text)
        return DryRunResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Dry-run failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Dry-run failed: {str(e)}")


@router.post("/execute/update-alt-text", response_model=OperationResponse)
def execute_update_alt_text(body: UpdateAltTextExecuteRequest) -> OperationResponse:
    """Execute image alt text update."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())
        result = client.apply_update_image_alt_text(body.media_id, body.alt_text)
        return OperationResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Execution failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Execution failed: {str(e)}")


# ========================
# URL CLEANUP ENDPOINTS
# ========================


@router.post("/dry-run/cleanup-urls", response_model=DryRunResponse)
def dry_run_cleanup_urls(body: CleanupUrlsDryRunRequest) -> DryRunResponse:
    """Dry-run for cleaning up URLs in post content."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())
        result = client.cleanup_urls_in_post(body.post_id, body.replacements)
        return DryRunResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Dry-run failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Dry-run failed: {str(e)}")


@router.post("/execute/cleanup-urls", response_model=OperationResponse)
def execute_cleanup_urls(body: CleanupUrlsExecuteRequest) -> OperationResponse:
    """Execute URL cleanup in post content."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())
        result = client.apply_cleanup_urls_in_post(body.post_id, body.replacements)
        return OperationResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Execution failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Execution failed: {str(e)}")


# ========================
# REDIRECT ENDPOINTS
# ========================


@router.post("/dry-run/create-redirect", response_model=DryRunResponse)
def dry_run_create_redirect(body: CreateRedirectDryRunRequest) -> DryRunResponse:
    """Dry-run for creating a 301 redirect."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())
        result = client.create_redirect_dry_run(
            body.source_url, body.target_url, body.plugin
        )
        return DryRunResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Dry-run failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Dry-run failed: {str(e)}")


@router.post("/execute/create-redirect", response_model=OperationResponse)
def execute_create_redirect(body: CreateRedirectExecuteRequest) -> OperationResponse:
    """Execute redirect creation."""
    try:
        client = _get_client(body.site_url, body.username, body.app_password.get_secret_value())

        if body.plugin == RedirectSource.REDIRECTION_PLUGIN:
            result = client.apply_create_redirect_redirection_plugin(
                body.source_url, body.target_url
            )
        elif body.plugin == RedirectSource.RANK_MATH:
            result = client.apply_create_redirect_rank_math(
                body.source_url, body.target_url
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported redirect plugin: {body.plugin}",
            )

        return OperationResponse(**result.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Execution failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Execution failed: {str(e)}")


# ========================
# BATCH OPERATIONS
# ========================


@router.post("/batch/execute", response_model=BatchOperationResponse)
def execute_batch_operations(body: BatchOperationRequest) -> BatchOperationResponse:
    """Execute multiple operations in sequence."""
    operations = []
    successful = 0
    failed = 0
    start_time = time.time()

    for i, op in enumerate(body.operations):
        try:
            logger.info(
                "Processing operation %s/%s: %s",
                i + 1,
                len(body.operations),
                op.task_type,
            )

            client = _get_client(op.site_url, op.username, op.app_password.get_secret_value())

            if isinstance(op, UpdateTitleExecuteRequest):
                result = client.apply_update_post_title(op.post_id, op.new_title)
            elif isinstance(op, UpdateContentExecuteRequest):
                result = client.apply_update_post_content(op.post_id, op.new_content)
            elif isinstance(op, UpdateAltTextExecuteRequest):
                result = client.apply_update_image_alt_text(op.media_id, op.alt_text)
            elif isinstance(op, CleanupUrlsExecuteRequest):
                result = client.apply_cleanup_urls_in_post(op.post_id, op.replacements)
            elif isinstance(op, CreateRedirectExecuteRequest):
                if op.plugin == RedirectSource.REDIRECTION_PLUGIN:
                    result = client.apply_create_redirect_redirection_plugin(
                        op.source_url, op.target_url
                    )
                elif op.plugin == RedirectSource.RANK_MATH:
                    result = client.apply_create_redirect_rank_math(
                        op.source_url, op.target_url
                    )
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unsupported redirect plugin: {op.plugin}",
                    )
            else:
                raise ValueError(f"Unknown operation type: {type(op)}")

            op_response = OperationResponse(**result.to_dict())
            operations.append(op_response)

            if result.success:
                successful += 1
            else:
                failed += 1
                if body.stop_on_error:
                    logger.warning("Stopping batch at operation %s due to error", i + 1)
                    break

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Operation %s failed: %s", i + 1, e)
            operations.append(
                OperationResponse(
                    task_type=op.task_type.value if hasattr(op, 'task_type') else "unknown",
                    resource_id="",
                    success=False,
                    message="Batch operation failed",
                    error=str(e),
                    duration_ms=0,
                )
            )
            failed += 1
            if body.stop_on_error:
                break

    total_duration = (time.time() - start_time) * 1000

    return BatchOperationResponse(
        total_operations=len(body.operations),
        successful=successful,
        failed=failed,
        operations=operations,
        total_duration_ms=total_duration,
    )

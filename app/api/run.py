from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.run import DryRunRequest, DryRunResponse, ExecuteRequest, ExecuteResponse
from app.services.run_pipeline import run_dry_run, run_execute
from app.services.wp_site import rest_client

router = APIRouter(prefix="/api/run", tags=["run"])


@router.post("/dry-run", response_model=DryRunResponse)
def dry_run(body: DryRunRequest) -> DryRunResponse:
    try:
        rest_client(body.site).health_check()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"REST auth failed: {str(e)}") from e
    try:
        return run_dry_run(body.site, body.grouped)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/execute", response_model=ExecuteResponse)
def execute(body: ExecuteRequest) -> ExecuteResponse:
    try:
        rest_client(body.site).health_check()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"REST auth failed: {str(e)}") from e
    try:
        return run_execute(body.site, body.grouped)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

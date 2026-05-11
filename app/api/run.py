from __future__ import annotations

import asyncio
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas.run import DryRunRequest, DryRunResponse, ExecuteRequest, ExecuteResponse
from app.services.run_pipeline import run_dry_run, run_execute
from app.services.wp_site import rest_client
from app.services.execution_logger import ExecutionLogger

router = APIRouter(prefix="/api/run", tags=["run"])

# Store for tracking execution logs
_active_executions: dict[str, ExecutionLogger] = {}


@router.post("/dry-run", response_model=DryRunResponse)
def dry_run(body: DryRunRequest) -> DryRunResponse:
    try:
        rest_client(body.site).health_check()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"REST auth failed: {str(e)}") from e
    try:
        return run_dry_run(body.site, body.grouped, body.redirect_plugin, body.seo_plugin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/execute", response_model=ExecuteResponse)
def execute(body: ExecuteRequest) -> ExecuteResponse:
    try:
        rest_client(body.site).health_check()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"REST auth failed: {str(e)}") from e
    try:
        return run_execute(body.site, body.grouped, body.redirect_plugin, body.seo_plugin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/status/{execution_id}")
def get_execution_status(execution_id: str) -> dict:
    """Get current status of an execution."""
    execution = _active_executions.get(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
    
    return {
        "execution_id": execution_id,
        "logs": execution.get_logs(),
        "summary": execution.get_summary(),
    }


async def _log_stream_generator(execution_id: str):
    """Generate server-sent events for execution logs."""
    queue: asyncio.Queue = asyncio.Queue()
    execution = _active_executions.get(execution_id)
    
    if not execution:
        yield f"data: {json.dumps({'error': 'Execution not found'})}\n\n"
        return
    
    execution.add_subscriber(execution_id, queue)
    
    try:
        while True:
            try:
                log_entry = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"data: {json.dumps(log_entry)}\n\n"
            except asyncio.TimeoutError:
                yield f": heartbeat\n\n"
    except GeneratorExit:
        pass
    finally:
        execution.remove_subscriber(execution_id)


@router.get("/stream/{execution_id}")
async def stream_execution_logs(execution_id: str):
    """Stream execution logs in real-time via Server-Sent Events."""
    if execution_id not in _active_executions:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
    
    return StreamingResponse(
        _log_stream_generator(execution_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )

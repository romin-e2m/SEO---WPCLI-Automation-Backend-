from __future__ import annotations

import asyncio
import json
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas.run import DryRunRequest, DryRunResponse, ExecuteRequest, ExecuteResponse
from app.services.run_pipeline import run_dry_run, run_execute
from app.services.wp_site import rest_client
from app.services.execution_logger import ExecutionLogger

router = APIRouter(prefix="/api/run", tags=["run"])

_MAX_TRACKED_EXECUTIONS = 50

# execution_id -> logger (completed entries pruned when over capacity)
_active_executions: dict[str, ExecutionLogger] = {}


def _register_execution(execution_id: str, logger: ExecutionLogger) -> None:
    if len(_active_executions) >= _MAX_TRACKED_EXECUTIONS:
        for eid, ex in list(_active_executions.items()):
            if ex.is_complete():
                _active_executions.pop(eid, None)
            if len(_active_executions) < _MAX_TRACKED_EXECUTIONS:
                break
    _active_executions[execution_id] = logger


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
    execution_id = str(uuid.uuid4())
    execution = ExecutionLogger(execution_id)
    _register_execution(execution_id, execution)
    try:
        result = run_execute(
            body.site,
            body.grouped,
            body.redirect_plugin,
            body.seo_plugin,
            execution_logger=execution,
        )
        return result.model_copy(update={"execution_id": execution_id})
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
    """Generate server-sent events for execution logs (polls; safe with sync executor)."""
    execution = _active_executions.get(execution_id)
    if not execution:
        yield f"data: {json.dumps({'error': 'Execution not found'})}\n\n"
        return

    last_len = 0
    ticks_since_new = 0
    while True:
        await asyncio.sleep(0.4)
        logs = execution.get_logs()
        n = len(logs)
        if n > last_len:
            for entry in logs[last_len:n]:
                yield f"data: {json.dumps(entry)}\n\n"
            last_len = n
            ticks_since_new = 0
        else:
            ticks_since_new += 1

        if execution.is_complete():
            if n == last_len and ticks_since_new >= 2:
                yield f"data: {json.dumps({'event': 'completed', 'summary': execution.get_summary()})}\n\n"
                return
        elif ticks_since_new >= 75:
            yield ": heartbeat\n\n"
            ticks_since_new = 0


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
        },
    )

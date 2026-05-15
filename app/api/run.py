from __future__ import annotations

import asyncio
import json
import threading
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.schemas.run import DryRunRequest, DryRunResponse, ExecuteRequest, ExecuteResponse
from app.services.run_pipeline import run_dry_run, run_execute
from app.services.wp_site import rest_client
from app.services.execution_logger import ExecutionLogger
from app.services.execution_log_registry import get as get_execution_logger, register as register_execution
from app.services.excel_export import build_export_excel

router = APIRouter(prefix="/api/run", tags=["run"])

# ── Dry-run ──────────────────────────────────────────────────────────────────

def _run_dry_run_background(
    dry_run_id: str,
    execution: ExecutionLogger,
    body: DryRunRequest,
    start_from_row: int = 0,
) -> None:
    try:
        run_dry_run(
            body.site,
            body.grouped,
            body.redirect_plugin,
            body.seo_plugin,
            execution_logger=execution,
            start_from_row=start_from_row,
        )
    except Exception as e:
        execution.log_sync("dry_run_error", "error", str(e))
        execution.mark_complete()


@router.post("/dry-run", response_model=DryRunResponse)
def dry_run(body: DryRunRequest) -> DryRunResponse:
    try:
        rest_client(body.site).health_check()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"REST auth failed: {str(e)}") from e

    dry_run_id = body.dry_run_id or str(uuid.uuid4())
    execution = ExecutionLogger(dry_run_id)
    execution.store_payload(body.model_dump())
    register_execution(dry_run_id, execution)

    t = threading.Thread(
        target=_run_dry_run_background,
        args=(dry_run_id, execution, body, 0),
        daemon=True,
    )
    t.start()

    return DryRunResponse(
        rows_processed=0,
        ready_to_execute=0,
        blocked=0,
        errors=0,
        no_change=0,
        rows=[],
        dry_run_id=dry_run_id,
        paused=False,
        rows_completed=0,
    )


@router.post("/pause-dry-run/{dry_run_id}", response_model=dict)
def pause_dry_run(dry_run_id: str) -> dict:
    execution = get_execution_logger(dry_run_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Dry-run {dry_run_id} not found")
    if execution.is_complete() and not execution.is_paused():
        # Run finished before the pause request arrived — tell the frontend it's
        # complete rather than raising a jarring 409 error.
        return {"dry_run_id": dry_run_id, "paused": False, "already_complete": True}
    execution.pause()
    return {"dry_run_id": dry_run_id, "paused": True}


@router.post("/resume-dry-run/{dry_run_id}", response_model=dict)
def resume_dry_run(dry_run_id: str) -> dict:
    execution = get_execution_logger(dry_run_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Dry-run {dry_run_id} not found")
    if not execution.is_paused():
        raise HTTPException(status_code=409, detail="Dry-run is not paused")

    payload = execution.get_payload()
    if not payload:
        raise HTTPException(status_code=409, detail="No stored payload; cannot resume")

    start_from = execution.get_rows_completed()

    try:
        body = DryRunRequest.model_validate(payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Stored payload invalid: {str(e)}") from e

    execution.resume_and_reset_complete()

    t = threading.Thread(
        target=_run_dry_run_background,
        args=(dry_run_id, execution, body, start_from),
        daemon=True,
    )
    t.start()
    return {"dry_run_id": dry_run_id, "resumed": True, "start_from_row": start_from}


@router.get("/checkpoint-dry-run/{dry_run_id}", response_model=dict)
def get_dry_run_checkpoint(dry_run_id: str) -> dict:
    execution = get_execution_logger(dry_run_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Dry-run {dry_run_id} not found")
    return {
        "dry_run_id": dry_run_id,
        "paused": execution.is_paused(),
        "complete": execution.is_complete(),
        "rows_completed": execution.get_rows_completed(),
        "summary": execution.get_summary(),
        "has_payload": execution.get_payload() is not None,
    }


# ── Execute ───────────────────────────────────────────────────────────────────

def _run_execute_background(
    execution_id: str,
    execution: ExecutionLogger,
    body: ExecuteRequest,
    start_from_row: int = 0,
) -> None:
    try:
        run_execute(
            body.site,
            body.grouped,
            body.redirect_plugin,
            body.seo_plugin,
            execution_logger=execution,
            start_from_row=start_from_row,
        )
    except Exception as e:
        execution.log_sync("execute_error", "error", str(e))
        execution.mark_complete()


@router.post("/execute", response_model=ExecuteResponse)
def execute(body: ExecuteRequest) -> ExecuteResponse:
    try:
        rest_client(body.site).health_check()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"REST auth failed: {str(e)}") from e
    execution_id = body.execution_id or str(uuid.uuid4())
    execution = ExecutionLogger(execution_id)
    execution.store_payload(body.model_dump())
    register_execution(execution_id, execution)
    t = threading.Thread(
        target=_run_execute_background,
        args=(execution_id, execution, body, 0),
        daemon=True,
    )
    t.start()
    return ExecuteResponse(
        rows_processed=0,
        updated=0,
        skipped=0,
        failed=0,
        rows=[],
        execution_id=execution_id,
        paused=False,
        rows_completed=0,
    )


@router.post("/pause/{execution_id}", response_model=dict)
def pause_execution(execution_id: str) -> dict:
    execution = get_execution_logger(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
    if execution.is_complete() and not execution.is_paused():
        # Run finished before the pause request arrived — tell the frontend it's
        # complete rather than raising a jarring 409 error.
        return {"execution_id": execution_id, "paused": False, "already_complete": True}
    execution.pause()
    return {"execution_id": execution_id, "paused": True}


@router.post("/resume/{execution_id}", response_model=dict)
def resume_execution(execution_id: str) -> dict:
    execution = get_execution_logger(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
    if not execution.is_paused():
        raise HTTPException(status_code=409, detail="Execution is not paused")

    payload = execution.get_payload()
    if not payload:
        raise HTTPException(status_code=409, detail="No stored payload; cannot resume (re-submit the execute request)")

    start_from = execution.get_rows_completed()

    try:
        body = ExecuteRequest.model_validate(payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Stored payload invalid: {str(e)}") from e

    execution.resume_and_reset_complete()

    t = threading.Thread(
        target=_run_execute_background,
        args=(execution_id, execution, body, start_from),
        daemon=True,
    )
    t.start()
    return {"execution_id": execution_id, "resumed": True, "start_from_row": start_from}


@router.get("/checkpoint/{execution_id}", response_model=dict)
def get_checkpoint(execution_id: str) -> dict:
    execution = get_execution_logger(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
    summary = execution.get_summary()
    return {
        "execution_id": execution_id,
        "paused": execution.is_paused(),
        "complete": execution.is_complete(),
        "rows_completed": execution.get_rows_completed(),
        "summary": summary,
        "has_payload": execution.get_payload() is not None,
    }


@router.get("/status/{execution_id}")
def get_execution_status(execution_id: str) -> dict:
    execution = get_execution_logger(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
    return {
        "execution_id": execution_id,
        "logs": execution.get_logs(),
        "summary": execution.get_summary(),
    }


async def _log_stream_generator(execution_id: str):
    execution = get_execution_logger(execution_id)
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
                summary = execution.get_summary()
                yield f"data: {json.dumps({'event': 'completed', 'paused': execution.is_paused(), 'summary': summary})}\n\n"
                return
        elif ticks_since_new >= 75:
            yield ": heartbeat\n\n"
            ticks_since_new = 0


@router.get("/stream/{execution_id}")
async def stream_execution_logs(execution_id: str):
    if get_execution_logger(execution_id) is None:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
    return StreamingResponse(
        _log_stream_generator(execution_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/export/{execution_id}")
def export_results(execution_id: str):
    """Download an Excel file with all original row data and a trailing Status column."""
    execution = get_execution_logger(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")

    payload = execution.get_payload()
    if not payload:
        raise HTTPException(status_code=409, detail="No stored payload for this execution; cannot export")

    try:
        body = ExecuteRequest.model_validate(payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Stored payload invalid: {str(e)}") from e

    row_results = execution.get_row_results()
    excel_bytes = build_export_excel(body.grouped, row_results)

    short_id = execution_id[:8]
    filename = f"seo_results_{short_id}.xlsx"

    return StreamingResponse(
        iter([excel_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

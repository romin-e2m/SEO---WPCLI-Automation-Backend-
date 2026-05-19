import os
import json
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.services.execution_log_registry import ensure

router = APIRouter(prefix="/system", tags=["system"])

_SSE_POLL_INTERVAL_SEC = float(os.getenv("SSE_EXECUTION_STREAM_POLL_INTERVAL_SEC", "0.5"))
# Default raised to 4 hours; most runs finish well before this.
_SSE_MAX_DURATION_SEC = float(os.getenv("SSE_EXECUTION_STREAM_MAX_DURATION_SEC", "14400"))
_SSE_MAX_ITERATIONS = max(
    1,
    int(_SSE_MAX_DURATION_SEC / max(_SSE_POLL_INTERVAL_SEC, 0.01)),
)


def _sse_log_payload(log: dict) -> str:
    """One SSE event named 'log' so EventSource.addEventListener('log') receives it."""
    return f"event: log\ndata: {json.dumps(log)}\n\n"


def _sse_row_result_payload(result: dict) -> str:
    """Named 'row_result' event carrying structured per-row data for live table rendering."""
    return f"event: row_result\ndata: {json.dumps(result)}\n\n"


def _sse_status_payload(logger) -> str:
    """Named 'status' event carrying completion/pause info."""
    summary = logger.get_summary()
    return (
        f"event: status\n"
        f"data: {json.dumps({'complete': True, 'paused': logger.is_paused(), 'rows_completed': logger.get_rows_completed(), 'summary': summary})}\n\n"
    )


async def _stream_generator(execution_id: str):
    """Stream execution logs via SSE. Emits a 'status' event when done or paused then closes."""
    yield _sse_log_payload(
        {
            "id": "stream_ready",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": "Monitor connected",
            "status": "success",
            "details": f"execution_id={execution_id}",
        }
    )

    last_index = 0
    last_row_index = 0
    logger = ensure(execution_id)
    # Issue 4: for dry-run (execution_id == 'default'), clear stale logs from the previous run
    # only if that run is already complete, so we never wipe logs mid-run.
    if execution_id == "default" and logger.is_complete():
        logger.clear()

    # Flush immediately (no initial sleep) so fast runs deliver all logs at once.
    logs = logger.get_logs()
    if len(logs) > last_index:
        for log in logs[last_index:]:
            yield _sse_log_payload(log)
        last_index = len(logs)

    row_results = logger.get_row_results()
    if len(row_results) > last_row_index:
        for rr in row_results[last_row_index:]:
            yield _sse_row_result_payload(rr)
        last_row_index = len(row_results)

    if logger.is_complete():
        # Run already finished by the time SSE connected — emit status and close.
        logs = logger.get_logs()
        if len(logs) > last_index:
            for log in logs[last_index:]:
                yield _sse_log_payload(log)
        row_results = logger.get_row_results()
        if len(row_results) > last_row_index:
            for rr in row_results[last_row_index:]:
                yield _sse_row_result_payload(rr)
        yield _sse_status_payload(logger)
        return

    for _ in range(_SSE_MAX_ITERATIONS):
        await asyncio.sleep(_SSE_POLL_INTERVAL_SEC)

        # Flush any new log entries
        logs = logger.get_logs()
        if last_index > len(logs):
            last_index = 0
        if len(logs) > last_index:
            for log in logs[last_index:]:
                yield _sse_log_payload(log)
            last_index = len(logs)

        # Flush any new row result entries
        row_results = logger.get_row_results()
        if last_row_index > len(row_results):
            last_row_index = 0
        if len(row_results) > last_row_index:
            for rr in row_results[last_row_index:]:
                yield _sse_row_result_payload(rr)
            last_row_index = len(row_results)

        # After flushing logs, check if done
        if logger.is_complete():
            # One final flush in case logs were added between the last check and mark_complete
            logs = logger.get_logs()
            if len(logs) > last_index:
                for log in logs[last_index:]:
                    yield _sse_log_payload(log)
            row_results = logger.get_row_results()
            if len(row_results) > last_row_index:
                for rr in row_results[last_row_index:]:
                    yield _sse_row_result_payload(rr)
            yield _sse_status_payload(logger)
            return


@router.get("/execution/stream")
async def get_execution_stream(execution_id: str = Query("default")):
    """SSE endpoint for live execution logs."""
    ensure(execution_id)

    return StreamingResponse(
        _stream_generator(execution_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

import os
import json
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.services.execution_log_registry import ensure

router = APIRouter(prefix="/system", tags=["system"])

# SSE execution log stream: bounded duration / poll cadence (override via env).
_SSE_POLL_INTERVAL_SEC = float(os.getenv("SSE_EXECUTION_STREAM_POLL_INTERVAL_SEC", "0.5"))
_SSE_MAX_DURATION_SEC = float(os.getenv("SSE_EXECUTION_STREAM_MAX_DURATION_SEC", "300"))
_SSE_MAX_ITERATIONS = max(
    1,
    int(_SSE_MAX_DURATION_SEC / max(_SSE_POLL_INTERVAL_SEC, 0.01)),
)


def _sse_log_payload(log: dict) -> str:
    """One SSE event named 'log' so EventSource.addEventListener('log') receives it."""
    return f"event: log\ndata: {json.dumps(log)}\n\n"


async def _stream_generator(execution_id: str):
    """Generate SSE stream for execution logs."""
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

    for _ in range(_SSE_MAX_ITERATIONS):
        logs = ensure(execution_id).get_logs()
        if last_index > len(logs):
            last_index = 0
        if len(logs) > last_index:
            for log in logs[last_index:]:
                yield _sse_log_payload(log)
            last_index = len(logs)

        await asyncio.sleep(_SSE_POLL_INTERVAL_SEC)


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

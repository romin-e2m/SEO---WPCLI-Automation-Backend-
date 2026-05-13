import os
import json
import asyncio
import platform
import socket
import sys
from datetime import datetime, timezone
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.services.execution_log_registry import append_log_entry, ensure

router = APIRouter(prefix="/system", tags=["system"])

# SSE execution log stream: bounded duration / poll cadence (override via env).
_SSE_POLL_INTERVAL_SEC = float(os.getenv("SSE_EXECUTION_STREAM_POLL_INTERVAL_SEC", "0.5"))
_SSE_MAX_DURATION_SEC = float(os.getenv("SSE_EXECUTION_STREAM_MAX_DURATION_SEC", "300"))
_SSE_MAX_ITERATIONS = max(
    1,
    int(_SSE_MAX_DURATION_SEC / max(_SSE_POLL_INTERVAL_SEC, 0.01)),
)


def _detect_environment():
    """Detect if running in Docker or locally."""
    # Check for Docker environment indicators
    if os.path.exists("/.dockerenv"):
        return "docker"

    if os.path.exists("/run/.containerenv"):
        return "docker"

    if "DOCKER_HOST" in os.environ:
        return "docker"

    if os.path.exists("/.dockerinit"):
        return "docker"

    # Check cgroup for Docker
    try:
        with open("/proc/self/cgroup", "r") as f:
            if "docker" in f.read().lower() or "container" in f.read().lower():
                return "docker"
    except Exception:
        pass

    return "local"


def _get_environment_details():
    """Get detailed information about the environment."""
    env = _detect_environment()

    if env == "docker":
        details = []

        # Get container ID if available
        try:
            with open("/.dockerenv", "r") as f:
                details.append("Docker container: confirmed")
        except Exception:
            pass

        # Get hostname
        try:
            hostname = socket.gethostname()
            details.append(f"Container ID: {hostname[:12]}...")
        except Exception:
            pass

        # Check if services are up
        services_info = []

        # Check PostgreSQL
        try:
            db_host = os.getenv("DB_HOST", "localhost")
            db_port = int(os.getenv("DB_PORT", "5432"))

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex((db_host, db_port))
            sock.close()

            if result == 0:
                services_info.append(f"DB ({db_host}:{db_port}): ✓")
            else:
                services_info.append(f"DB ({db_host}:{db_port}): ✗")
        except Exception as e:
            services_info.append(f"DB check failed: {str(e)[:30]}")

        if services_info:
            details.append("Services: " + ", ".join(services_info))

        return env, " | ".join(details) if details else "Docker environment"

    else:
        details = []

        # Get OS info
        try:
            details.append(f"OS: {platform.system()} {platform.release()[:10]}")
        except Exception:
            pass

        # Get Python version
        try:
            details.append(f"Python: {sys.version.split()[0]}")
        except Exception:
            pass

        # Get current working directory
        try:
            cwd = os.getcwd()
            cwd_short = cwd.split("/")[-1] or cwd.split("/")[-2]
            details.append(f"Working dir: .../{cwd_short}")
        except Exception:
            pass

        return env, " | ".join(details) if details else "Local environment"


@router.get("/status")
def get_system_status():
    """Get current system status including environment detection."""
    env, details = _get_environment_details()

    return {
        "environment": env,
        "details": details,
        "docker": env == "docker",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def clear_execution_logs(execution_id: str) -> None:
    """Reset buffered logs for an execution (e.g. before a new dry-run / execute)."""
    from app.services.execution_log_registry import clear_logs

    clear_logs(execution_id)


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


def add_execution_log(execution_id: str, log_entry: dict):
    """Add a log entry to the execution stream (same backing store as /api/run)."""
    append_log_entry(execution_id, log_entry)
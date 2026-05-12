from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Any

router = APIRouter(prefix="/api/qa", tags=["qa"])

# qa_agent/ is copied to /qa_agent inside the container (see Dockerfile).
# On the host (local dev without Docker) it lives one level above the backend/ dir.
_QA_AGENT_CANDIDATES = [
    Path("/qa_agent"),                                      # inside Docker container
    Path(__file__).parent.parent.parent.parent / "qa_agent",  # host: project_root/qa_agent
]
for _p in _QA_AGENT_CANDIDATES:
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
        break


class QARunRequest(BaseModel):
    execute_response: dict[str, Any] = Field(..., description="Full ExecuteResponse JSON from POST /api/run/execute")
    base_url: str = Field(..., description="WordPress site root URL")
    username: str = Field(..., description="WordPress username")
    app_password: str = Field(..., description="WordPress Application Password")


@router.post("/run")
async def run_qa_check(body: QARunRequest) -> dict:
    """
    Run QA verification against a completed execute response.
    Returns a QAReport as a dict.
    """
    from orchestrator import run_qa  # imported here so sys.path insert above takes effect
    report = await run_qa(
        execute_response=body.execute_response,
        base_url=body.base_url,
        username=body.username,
        app_password=body.app_password,
    )
    return report.model_dump()

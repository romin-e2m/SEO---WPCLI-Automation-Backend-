import os
import logging.config

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.run import router as run_router
from app.api.workbook import router as workbook_router
from app.api.wp import router as wp_router
from app.api.schema import router as schema_router
from app.api.rest_api import router as rest_api_router
from app.api.system import router as system_router
from app.services.schema_manager import SchemaManager
from app.logging_config import LOGGING_CONFIG

load_dotenv()

# Configure logging to suppress health check logs
logging.config.dictConfig(LOGGING_CONFIG)


def _allowed_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOWED_ORIGINS")
    if not raw:
        raise RuntimeError(
            "CORS_ALLOWED_ORIGINS environment variable is required. "
            "Provide a comma-separated list of allowed origins, e.g.: "
            "http://localhost:3013,http://localhost:5173"
        )
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "*" in origins:
        raise RuntimeError(
            "Invalid CORS_ALLOWED_ORIGINS: wildcard '*' is not allowed. "
            "Provide an explicit comma-separated allowlist of origins."
        )
    return origins


app = FastAPI(title="WP SEO Automation API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

schema_storage = os.getenv("SCHEMA_STORAGE_PATH", "/tmp/schemas.json")
app.schema_manager = SchemaManager(storage_path=schema_storage)

app.include_router(workbook_router)
app.include_router(wp_router)
app.include_router(run_router)
app.include_router(schema_router)
app.include_router(rest_api_router)
app.include_router(system_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


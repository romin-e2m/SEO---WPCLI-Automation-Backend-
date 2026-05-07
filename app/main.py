import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.run import router as run_router
from app.api.workbook import router as workbook_router
from app.api.wp import router as wp_router
from app.api.schema import router as schema_router
from app.services.schema_manager import SchemaManager

load_dotenv()


def _allowed_origins() -> list[str]:
    raw = os.getenv(
        "CORS_ALLOWED_ORIGINS",
        # Dev defaults (React app commonly runs on 3013 here, but also cover
        # typical Vite defaults and localhost/127 variations).
        "http://localhost:3013,http://127.0.0.1:3013,http://0.0.0.0:3013,"
        "http://localhost:5173,http://127.0.0.1:5173,http://0.0.0.0:5173",
    )
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if "*" in origins:
        raise RuntimeError(
            "Invalid CORS_ALLOWED_ORIGINS: wildcard '*' is not allowed when credentials are enabled. "
            "Provide an explicit comma-separated allowlist of origins."
        )
    return origins


app = FastAPI(title="WP SEO Automation API", version="0.1.0")

_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
_cors_allow_all = "*" in [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
if _cors_allow_all:
    raise RuntimeError(
        "Invalid CORS_ALLOWED_ORIGINS: wildcard '*' is not allowed when credentials are enabled. "
        "Provide an explicit comma-separated allowlist of origins."
    )
_cors_origin_regex = os.getenv("CORS_ALLOWED_ORIGIN_REGEX") or r"^http://(localhost|127\.0\.0\.1|0\.0\.0\.0):(3013|5173)$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize schema manager
schema_storage = os.getenv("SCHEMA_STORAGE_PATH", "/tmp/schemas.json")
app.schema_manager = SchemaManager(storage_path=schema_storage)

app.include_router(workbook_router)
app.include_router(wp_router)
app.include_router(run_router)
app.include_router(schema_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


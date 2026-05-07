import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.run import router as run_router
from app.api.workbook import router as workbook_router
from app.api.wp import router as wp_router

load_dotenv()


def _allowed_origins() -> list[str]:
    raw = os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:3013,http://127.0.0.1:3013",
    )
    return [o.strip() for o in raw.split(",") if o.strip()]


app = FastAPI(title="WP SEO Automation API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(workbook_router)
app.include_router(wp_router)
app.include_router(run_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

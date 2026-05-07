# Backend (Python)

FastAPI service for the WordPress SEO automation pipeline. The React frontend talks to this API **only** over HTTP with **CORS** configured via environment variables.

## Setup

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set `CORS_ALLOWED_ORIGINS` to match your frontend origin(s).

3. Run the API:

   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8013
   ```

## Docker

Build and run (set `CORS_ALLOWED_ORIGINS` to your deployed frontend URL):

```bash
docker build -t wp-seo-api .
docker run --rm -p 8013:8013 -e CORS_ALLOWED_ORIGINS=http://localhost:3013 wp-seo-api
```

## Endpoints

- `GET /health` — connectivity check used by the frontend to verify CORS and reachability.
- `POST /api/workbook/analyze` — multipart form: `file` (`.xlsx`, `.xls`, or `.csv` such as a Google Sheets export), optional `site_url`, optional `preview_rows` (1–100). Returns sheet names (or one logical “sheet” for CSV named from the file stem), column headers, row counts, and JSON-safe preview rows for UI mapping.

Upload size cap: `MAX_WORKBOOK_UPLOAD_BYTES` (default 20 MiB).

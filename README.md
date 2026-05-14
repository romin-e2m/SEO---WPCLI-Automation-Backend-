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
- `POST /api/workbook/analyze` — multipart form: `file` (`.xlsx`, `.xls`, or `.csv` such as a Google Sheets export), optional `site_url`, optional `preview_rows` (1–100). Returns sheet names, column headers, row counts, JSON-safe preview rows, and per-sheet `suggested_action_type` + `suggested_column_map` defaults for the mapping UI.
- `POST /api/workbook/analyze-url` — JSON body: `url`, optional `site_url`, optional `preview_rows`. Same response shape as `/analyze`. Downloads from the given URL (Google Sheets share links auto-converted to `xlsx` export).
- `GET /api/workbook/mapping/descriptor` — Returns `{ actions: { <action_type>: { label, fields[], any_of_groups[] } } }`. Drives the dynamic column-mapping form on the frontend so canonical fields are not hard-coded in the UI. Action types: `on_page`, `meta`, `images`, `url_cleanup`, `redirects_301`.
- `POST /api/workbook/mapping/validate` — JSON body: `url`, optional `site_url`, `mappings: [{ sheet_name, action_type, column_map, skip_header_rows, enabled }]`. Re-downloads the workbook, then statically checks each mapping (sheet exists, columns exist, required fields mapped, any-of groups satisfied) and returns per-sheet row-level summaries (`rows_total`, `rows_valid`, `rows_skipped`, `skipped_reasons`).
- `POST /api/workbook/mapping/normalize` — Same input as `/mapping/validate`. Runs validation; if clean, returns `grouped: { <action_type>: NormalizedRow[] }` where each row has canonical field names — ready for the resolution / dry-run / executor stages.

### Size & row caps

- Upload cap: `MAX_WORKBOOK_UPLOAD_BYTES` (default 20 MiB).
- Remote download cap: `MAX_WORKBOOK_DOWNLOAD_BYTES` (default 20 MiB).
- Full-sheet read cap (validate / normalize): `MAX_WORKBOOK_FULL_ROWS` (default 100 000 rows per sheet).

### Run pipeline (resolution + dry-run + execute)

- **`/api/run/*`** — Workbook batch flow (`SiteAccess` + normalized `grouped` rows). Uses the shared pipeline (`WpRestClient`): REST for posts/media/redirect attempts plus WP-CLI where configured. This is what the mapping UI drives after normalize.

- `POST /api/run/dry-run` — JSON body: `site` (`SiteAccess`: REST credentials + optional `wp_cli`), `grouped` (same `grouped` object returned by `/api/workbook/mapping/normalize`). Performs URL→post/attachment resolution against the live site, reads current title/H1/body, meta description, alt text, or content for URL replacement, and returns per-row `diffs` without writing. Meta + alt + redirects require WP-CLI as in the granular `/api/wp/*` endpoints.
- `POST /api/run/execute` — Same body plus `confirm_execute: true` (required). Re-runs the same checks and applies updates: REST for title/body/H1 patch and URL-in-content replacements; WP-CLI for meta description and attachment alt; Redirection plugin CLI for 301 rows when detected.

Row cap: `MAX_RUN_ROWS` (default 500) per dry-run/execute request.

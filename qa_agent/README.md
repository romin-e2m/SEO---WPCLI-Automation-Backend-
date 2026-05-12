# QA Agent — WordPress SEO Automation Verifier

A standalone QA agent that verifies whether WordPress SEO automation changes were
successfully applied. It reads the output of the automation backend
(`POST /api/run/execute`) and independently re-checks every updated row using
WordPress REST API calls and direct HTTP checks.

No LLM is required. All verification is deterministic Python code.

---

## What it checks

| Action Type     | Subagent                  | Verification method                                                     |
|-----------------|---------------------------|-------------------------------------------------------------------------|
| `on_page`       | `content_verifier`        | Fetches post/page via WP REST, extracts first `<h1>` from rendered HTML |
| `meta`          | `meta_desc_verifier`      | REST: Rank Math field → Yoast field → HTML `<meta name="description">`  |
| `meta_title`    | `meta_title_verifier`     | REST: Rank Math field → Yoast field → HTML `<title>` tag                |
| `images`        | `image_alt_verifier`      | Fetches media item via WP REST, reads `alt_text` field                  |
| `url_cleanup`   | `url_cleanup_verifier`    | Fetches post/page, checks old URL absent + new URL present in content   |
| `redirects_301` | `redirect_verifier`       | Plugin REST (bonus) + HTTP HEAD to verify 301 status and Location header|

Only rows with `outcome == "updated"` are checked. `skipped` and `failed` rows are ignored.

---

## Setup

### 1. Create a virtual environment

```bash
cd qa_agent/
python -m venv .venv
source .venv/bin/activate       # macOS / Linux
.venv\Scripts\activate          # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure credentials

Copy `.env.example` to `.env` and fill in your WordPress credentials:

```bash
cp .env.example .env
```

```env
WP_REST_BASE_URL=https://your-site.com
WP_USERNAME=admin
WP_APP_PASSWORD=xxxx xxxx xxxx xxxx xxxx xxxx
```

The `WP_APP_PASSWORD` must be a WordPress **Application Password** (not your login
password). Generate one from: WP Admin > Users > Your Profile > Application Passwords.

---

## Running the QA agent

### Basic usage (credentials from `.env`)

```bash
python main.py --execute-response execute_result.json
```

### Override credentials at the command line

```bash
python main.py \
  --execute-response execute_result.json \
  --site https://example.com \
  --user admin \
  --password "xxxx yyyy zzzz"
```

### Save report to a specific path

```bash
python main.py --execute-response execute_result.json --output my_report.json
```

### Skip saving the JSON report

```bash
python main.py --execute-response execute_result.json --no-save
```

---

## Input format — ExecuteResponse JSON

The QA agent expects the JSON body returned by `POST /api/run/execute`. Example:

```json
{
  "execution_id": "exec_abc123",
  "rows": [
    {
      "action_type": "on_page",
      "sheet_name": "Sheet1",
      "row_index": 2,
      "outcome": "updated",
      "message": null,
      "post_id": 42,
      "attachment_id": null,
      "detail": {
        "url": "https://example.com/my-page/",
        "old_title": "Old Heading",
        "new_title": "New SEO Heading",
        "fields_updated": ["content"],
        "raw_id": "A2"
      }
    },
    {
      "action_type": "redirects_301",
      "sheet_name": "Redirects",
      "row_index": 5,
      "outcome": "updated",
      "message": null,
      "post_id": null,
      "attachment_id": null,
      "detail": {
        "source_url": "https://example.com/old-path/",
        "destination_url": "https://example.com/new-path/",
        "plugin": "Redirection",
        "redirect_id": 17
      }
    }
  ]
}
```

---

## Output

### Terminal (rich colourised)

```
QA Report  |  https://example.com
Timestamp: 2024-01-15T10:30:00+00:00

Overall: 12 passed  1 failed  0 errors  13 total checked

ON_PAGE  12/13 passed  1 failed
 Row  Status    Method          Expected                  Actual                    Error / Info
   2  PASS      rest_api        New SEO Heading           New SEO Heading
  15  FAIL      rest_api        Updated H1 Text           Old H1 Text

Summary:
  Total rows checked : 13
  Passed             : 12
  Failed             : 1
  Errors             : 0

  [ISSUES] on_page               12/13 passed, 1 failed
    FAIL   row 15: expected='Updated H1 Text'  actual='Old H1 Text'
```

### JSON report file

Saved to `qa_report_<timestamp>.json` by default. Contains the full `QAReport`
structure including all row-level details, expected/actual values, and error messages.

---

## Exit codes

| Code | Meaning                                    |
|------|--------------------------------------------|
| `0`  | All checks passed                          |
| `1`  | One or more checks failed or errored       |

This makes the QA agent suitable for use in CI pipelines.

---

## Using OpenRouter (optional)

The `config.py` includes OpenRouter settings for future LLM-based summary generation.
For the current MVP, all verification is deterministic and no LLM is used.

If you want to extend the orchestrator with LLM narrative summaries:

```env
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-haiku-4-5
```

---

## Architecture

```
qa_agent/
├── main.py                    # CLI entry point
├── orchestrator.py            # Runs all subagents concurrently via asyncio.gather()
├── config.py                  # Env var loader
├── models.py                  # Pydantic models (QARowResult, QAReport, etc.)
├── skills/
│   ├── wp_rest_client.py      # Async WP REST API GET client
│   ├── html_scraper.py        # Page fetcher + HTML parser
│   ├── http_checker.py        # HTTP HEAD for redirect checks
│   └── report_builder.py      # Rich terminal output + QAReport assembly
└── subagents/
    ├── content_verifier.py    # on_page: H1 verification
    ├── meta_desc_verifier.py  # meta: meta description verification
    ├── meta_title_verifier.py # meta_title: meta title verification
    ├── image_alt_verifier.py  # images: alt text verification
    ├── url_cleanup_verifier.py# url_cleanup: URL replacement verification
    └── redirect_verifier.py   # redirects_301: 301 redirect verification
```

All subagents run in parallel. Each subagent catches all exceptions per row and
returns a `QARowResult(verified=False, error=...)` rather than crashing the
orchestrator.

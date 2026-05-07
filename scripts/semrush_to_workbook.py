from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from openpyxl import load_workbook


URL_RE = re.compile(r"https?://[^\s\]]+", re.IGNORECASE)


def _norm_cell(v: object) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def _iter_rows_values(ws, *, max_row: Optional[int] = None, max_col: Optional[int] = None) -> Iterator[list[str]]:
    mr = ws.max_row if max_row is None else min(ws.max_row or 0, max_row)
    mc = ws.max_column if max_col is None else min(ws.max_column or 0, max_col)
    for row in ws.iter_rows(min_row=1, max_row=mr, max_col=mc, values_only=True):
        yield [_norm_cell(v) for v in row]


def _find_header_row(
    ws,
    required: set[str],
    *,
    max_row: int = 250,
    max_col: int = 40,
) -> Optional[tuple[int, dict[str, int]]]:
    required_l = {r.lower() for r in required}
    for idx, row in enumerate(_iter_rows_values(ws, max_row=max_row, max_col=max_col), start=1):
        mapping: dict[str, int] = {}
        for col_i, val in enumerate(row):
            v = val.lower()
            if not v:
                continue
            for req in required_l:
                if v == req:
                    mapping[req] = col_i
        if required_l.issubset(mapping.keys()):
            return idx, mapping
    return None


def _extract_urls_from_sheet(ws, *, max_row: int = 500) -> set[str]:
    urls: set[str] = set()
    for row in _iter_rows_values(ws, max_row=max_row, max_col=min(ws.max_column or 0, 30)):
        for cell in row:
            if not cell:
                continue
            for m in URL_RE.finditer(cell):
                urls.add(m.group(0).rstrip(".,;)"))
    return urls


@dataclass(frozen=True)
class ImageIssue:
    page_url: str
    image_url: str
    notes: str


def _extract_missing_alt_images(semrush_wb) -> list[ImageIssue]:
    """
    Prefer parsing tables that have a clear header row with *exact* 'Page URL' + 'Image URL'.

    In this specific Semrush export, the cleanest tables look like:
      Row 1: ['Page URL', 'Image URL', 'Discovered']
      Row 2: [<page title>, <image base>, <date>]
      Row 3: [<page url>,  <image path>, '[new]']
      Row 4: [<page url continuation>, <image url continuation>, None]

    We'll reconstruct each issue by concatenating consecutive fragments until a new non-URL
    row begins.
    """
    issues: list[ImageIssue] = []

    for sheet in semrush_wb.sheetnames:
        ws = semrush_wb[sheet]
        header = _find_header_row(ws, {"Page URL", "Image URL"}, max_row=600, max_col=10)
        if not header:
            continue
        header_row, mapping = header

        # Only trust truly tabular sections (header very early, and likely exactly these columns).
        if header_row > 5:
            continue

        page_col = mapping["page url"]
        img_col = mapping["image url"]

        cur_page = ""
        cur_img = ""

        def flush():
            nonlocal cur_page, cur_img
            page = cur_page.strip()
            img = cur_img.strip()
            if page.lower().startswith("http") and img.lower().startswith("http"):
                issues.append(ImageIssue(page_url=page, image_url=img, notes="Missing ALT attribute (Semrush)"))
            cur_page = ""
            cur_img = ""

        for r_i, row in enumerate(
            _iter_rows_values(
                ws,
                max_row=min(ws.max_row or 0, 5000),
                max_col=max(page_col, img_col) + 3,
            ),
            start=1,
        ):
            if r_i <= header_row:
                continue

            page = row[page_col] if page_col < len(row) else ""
            img = row[img_col] if img_col < len(row) else ""

            if not page and not img:
                continue

            # New record marker: a non-URL label/title row while we already have a record in progress.
            if cur_page and (page and not page.lower().startswith("http")):
                flush()

            # Append fragments.
            if page:
                if not cur_page:
                    cur_page = page
                else:
                    # continuation lines often start with '/' or 'recent...'
                    if not page.lower().startswith("http"):
                        cur_page += page
                    elif cur_page.lower().startswith("http"):
                        # If we see a new URL while collecting, flush and start anew.
                        flush()
                        cur_page = page
                    else:
                        cur_page = page

            if img:
                if not cur_img:
                    cur_img = img
                else:
                    if not img.lower().startswith("http"):
                        cur_img += img
                    else:
                        # new image url while collecting: flush current and start.
                        flush()
                        cur_img = img

            # If we have both pieces, keep collecting one more continuation row if it exists;
            # otherwise this will flush when a new title row starts or at end.

        flush()
    # de-dupe
    seen = set()
    out: list[ImageIssue] = []
    for it in issues:
        key = (it.page_url, it.image_url)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _extract_status_codes(semrush_wb) -> dict[str, str]:
    """
    In this export, status codes appear in 'Table 63' with a header like:
      '... Page URL' and 'Status Code'
    We'll scan for a clean header row containing 'Status Code' and collect URL->code.
    """
    url_to_code: dict[str, str] = {}
    for sheet in semrush_wb.sheetnames:
        ws = semrush_wb[sheet]
        header = _find_header_row(ws, {"Status Code"}, max_row=400, max_col=20)
        if not header:
            continue
        header_row, mapping = header
        code_col = mapping["status code"]

        # try to find a column that contains 'Page URL' on the same header row
        row_vals = next(_iter_rows_values(ws, max_row=header_row, max_col=30))
        # but _iter_rows_values starts at row 1; easiest to just read the header row directly
        header_cells = [_norm_cell(ws.cell(header_row, c).value) for c in range(1, min(ws.max_column or 0, 30) + 1)]
        page_col = None
        for i, v in enumerate(header_cells):
            if v.lower() == "page url" or v.lower().endswith("page url"):
                page_col = i
                break
        if page_col is None:
            continue

        for r_i, row in enumerate(_iter_rows_values(ws, max_row=min(ws.max_row or 0, 2000), max_col=max(code_col, page_col) + 2), start=1):
            if r_i <= header_row:
                continue
            page = row[page_col] if page_col < len(row) else ""
            code = row[code_col] if code_col < len(row) else ""
            if not page.lower().startswith("http"):
                continue
            if code:
                url_to_code[page] = code
    return url_to_code


def _extract_missing_meta_urls(semrush_wb) -> set[str]:
    # In this export, missing meta description URLs appear in 'Table 53' but are messy; safest is URL regex.
    urls: set[str] = set()
    for sheet in semrush_wb.sheetnames:
        if "missing meta description" not in sheet.lower():
            continue
        urls |= _extract_urls_from_sheet(semrush_wb[sheet], max_row=500)
    # fallback: some exports keep it in Table 53 without sheet name match
    if not urls and "Table 53" in semrush_wb.sheetnames:
        urls |= _extract_urls_from_sheet(semrush_wb["Table 53"], max_row=200)
    return urls


def _extract_too_many_params_urls(semrush_wb) -> set[str]:
    urls: set[str] = set()
    for sheet in semrush_wb.sheetnames:
        ws = semrush_wb[sheet]
        # find the "Too many URL parameters" section by a header row "Page URL" + "Count"
        header = _find_header_row(ws, {"Page URL", "Count"}, max_row=500, max_col=20)
        if not header:
            continue
        header_row, mapping = header
        page_col = mapping["page url"]
        count_col = mapping["count"]
        # collect URL-like rows following the header row; semrush often puts URL itself in next line
        for r_i, row in enumerate(_iter_rows_values(ws, max_row=min(ws.max_row or 0, 2000), max_col=max(page_col, count_col) + 2), start=1):
            if r_i <= header_row:
                continue
            page = row[page_col] if page_col < len(row) else ""
            cnt = row[count_col] if count_col < len(row) else ""
            if page.lower().startswith("http") and "?" in page:
                urls.add(page)
    return urls


def _append_rows(ws, rows: Iterable[list[object]]) -> int:
    start = ws.max_row or 0
    appended = 0
    for r in rows:
        ws.append(r)
        appended += 1
    return (ws.max_row or 0) - start if appended else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract Semrush Site Audit items into SEO workbook template.")
    ap.add_argument("--semrush", required=True, type=Path)
    ap.add_argument("--template", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    semrush_wb = load_workbook(args.semrush, read_only=True, data_only=True)
    template_wb = load_workbook(args.template)

    missing_meta_urls = sorted(_extract_missing_meta_urls(semrush_wb))
    param_urls = sorted(_extract_too_many_params_urls(semrush_wb))
    image_issues = _extract_missing_alt_images(semrush_wb)
    status_codes = _extract_status_codes(semrush_wb)

    # Meta sheet
    meta_ws = template_wb["Meta"]
    meta_rows = [[u, "", "Missing meta description (Semrush)"] for u in missing_meta_urls]
    _append_rows(meta_ws, meta_rows)

    # URL_Cleanup sheet
    cleanup_ws = template_wb["URL_Cleanup"]
    cleanup_rows = [[u, u, "", "Too many URL parameters (Semrush)"] for u in param_urls]
    _append_rows(cleanup_ws, cleanup_rows)

    # Images sheet
    images_ws = template_wb["Images"]
    img_rows = [[it.page_url, it.image_url, "", ""] for it in image_issues]
    _append_rows(images_ws, img_rows)

    # On_Page: use status codes as issues (since this export lacks actual title/h1 values)
    on_page_ws = template_wb["On_Page"]
    on_page_rows = []
    for url, code in sorted(status_codes.items()):
        # only include obvious non-200s
        try:
            code_i = int(float(str(code)))
        except Exception:
            code_i = None
        if code_i is not None and code_i != 200:
            on_page_rows.append([url, "", "", "", "", f"HTTP status {code_i} (Semrush)"])
    _append_rows(on_page_ws, on_page_rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    template_wb.save(args.out)

    print("Wrote:", args.out)
    print("Meta rows:", len(meta_rows))
    print("URL_Cleanup rows:", len(cleanup_rows))
    print("Images rows:", len(img_rows))
    print("On_Page rows:", len(on_page_rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


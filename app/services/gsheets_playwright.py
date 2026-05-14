"""
Google Sheets status write-back via Playwright browser automation.

No API key required — the user logs into Google once via the browser,
then this class navigates to the correct sheet tab and row, finds or
creates a "Status" column at the end, writes status text, and applies
a background colour via the toolbar.

Colour mapping
--------------
  updated  -> "Done"                  green  #B7E1CD
  skipped  -> "Skipped - {message}"   grey   #E8EAED
  failed   -> "Blocked - {message}"   red    #F4CCCC
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# Maximum length for the message appended to Skipped / Blocked text.
_MAX_MSG_LEN = 120

# Outcome type alias
Outcome = Literal["updated", "skipped", "failed"]

# --------------------------------------------------------------------- #
# Colour map: outcome -> (cell_text_prefix, hex_colour)                  #
# --------------------------------------------------------------------- #
_OUTCOME_COLOUR: dict[str, tuple[str, str]] = {
    "updated": ("Done", "#B7E1CD"),
    "skipped": ("Skipped", "#E8EAED"),
    "failed":  ("Blocked", "#F4CCCC"),
}


def _col_letter(n: int) -> str:
    """Convert 1-based column index to spreadsheet letter(s) (1→A, 26→Z, 27→AA, …)."""
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _cell_ref(col_letter: str, row: int) -> str:
    return f"{col_letter}{row}"


class GoogleSheetsPlaywright:
    """
    Manages a Playwright session against a Google Sheets document for status
    write-back.  One instance is created per run; the browser page is shared
    between calls (no re-launch per row).

    State kept between calls
    ------------------------
    * Current tab (sheet name) — avoids redundant tab navigation.
    * Status column letter per sheet name — avoids re-scanning headers each row.
    """

    def __init__(self, sheets_url: str) -> None:
        """
        Parameters
        ----------
        sheets_url:
            Full URL of the Google Sheets document, e.g.
            ``https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0``.
            The sheet ID is extracted from this URL.
        """
        self.sheets_url = sheets_url.rstrip("/")
        self._sheet_id: str = self._extract_sheet_id(sheets_url)

        # Per-sheet-name cache: sheet_name -> column letter for "Status"
        self._status_col: dict[str, str] = {}

        # Track which tab is currently active to skip unnecessary navigation.
        self._current_tab: str | None = None

        # Maps sheet_name -> gid (populated lazily from tab DOM)
        self._gid_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def login(self, page: Page) -> bool:
        """
        Navigate to the spreadsheet URL.  If the user is already logged in, the
        sheet loads immediately.  If not, Google's sign-in page appears — the
        caller is expected to have opened a headed browser so the user can log in
        manually.  This method waits up to 120 s for the sheet to become visible.

        Returns True when the sheet editor is detected, False on timeout/error.
        """
        try:
            logger.info("GSheets: navigating to %s", self.sheets_url)
            await page.goto(self.sheets_url, wait_until="domcontentloaded", timeout=30_000)

            # Wait for the sheet grid to become present (up to 2 min for manual login)
            await page.wait_for_selector(
                ".docs-sheet-tab-name, .goog-menu-button, #docs-toolbar",
                timeout=120_000,
            )
            logger.info("GSheets: sheet editor detected — login successful")
            return True
        except Exception as exc:
            logger.error("GSheets: login/load failed: %s", exc)
            return False

    async def write_status(
        self,
        page: Page,
        sheet_name: str,
        row_index: int,
        outcome: str,
        message: str | None,
    ) -> bool:
        """
        Write a status cell for a single processed row.

        Parameters
        ----------
        page:
            Active Playwright Page (same instance across calls).
        sheet_name:
            Name of the sheet tab (matches ``NormalizedRow.sheet_name``).
        row_index:
            0-based data row index.  Spreadsheet row = row_index + 2 (row 1 is header).
        outcome:
            One of ``"updated"``, ``"skipped"``, ``"failed"``.
        message:
            Optional human-readable detail appended to Skipped/Blocked text.

        Returns True on success, False if anything went wrong (errors are logged but
        not raised — the caller should continue with the next row).
        """
        try:
            # 1. Make sure the right tab is active.
            await self._ensure_tab(page, sheet_name)

            # 2. Make sure the Status column exists and get its letter.
            col = await self._ensure_status_column(page, sheet_name)
            if not col:
                logger.warning("GSheets: could not ensure Status column for sheet %r", sheet_name)
                return False

            # 3. Build spreadsheet row number (header is row 1).
            sp_row = row_index + 2

            # 4. Navigate to the target cell via the Name Box.
            cell = _cell_ref(col, sp_row)
            await self._navigate_to_cell(page, cell)

            # 5. Build status text.
            prefix, colour = _OUTCOME_COLOUR.get(outcome, ("Unknown", "#FFFFFF"))
            if outcome in ("skipped", "failed") and message:
                msg_trimmed = message[:_MAX_MSG_LEN]
                status_text = f"{prefix} - {msg_trimmed}"
            else:
                status_text = prefix

            # 6. Type the status text into the active cell.
            await self._type_in_cell(page, status_text)

            # 7. Apply background colour.
            await self._apply_background_colour(page, colour)

            logger.info(
                "GSheets: wrote %r to %s!%s (outcome=%s)",
                status_text,
                sheet_name,
                cell,
                outcome,
            )
            return True

        except Exception as exc:
            logger.error(
                "GSheets: write_status failed for sheet=%r row_index=%d outcome=%s: %s",
                sheet_name,
                row_index,
                outcome,
                exc,
            )
            return False

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_sheet_id(url: str) -> str:
        """Pull the spreadsheet ID from a Google Sheets URL."""
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
        return m.group(1) if m else ""

    async def _get_gid_for_sheet(self, page: Page, sheet_name: str) -> str | None:
        """
        Find the ``#gid=...`` value for a named tab by reading the tab bar DOM.
        Result is cached per sheet_name.
        """
        if sheet_name in self._gid_cache:
            return self._gid_cache[sheet_name]

        try:
            gids: list[dict] = await page.evaluate(
                """
                () => {
                    const tabs = [];
                    for (const el of document.querySelectorAll('.docs-sheet-tab')) {
                        const nameEl = el.querySelector('.docs-sheet-tab-name');
                        const name = nameEl ? nameEl.textContent.trim() : '';
                        // The tab anchor or its parent carries the gid in the URL
                        const anchor = el.querySelector('a');
                        if (anchor && anchor.href) {
                            const m = anchor.href.match(/#gid=(\\d+)/);
                            if (m) {
                                tabs.push({ name, gid: m[1] });
                            }
                        }
                    }
                    return tabs;
                }
                """
            )
            for entry in gids:
                self._gid_cache[entry["name"]] = entry["gid"]
            return self._gid_cache.get(sheet_name)
        except Exception as exc:
            logger.warning("GSheets: could not read tab GIDs: %s", exc)
            return None

    async def _ensure_tab(self, page: Page, sheet_name: str) -> None:
        """Navigate to the correct sheet tab if it is not already active."""
        if self._current_tab == sheet_name:
            return

        # Try navigating via URL fragment first (most reliable).
        gid = await self._get_gid_for_sheet(page, sheet_name)
        if gid:
            target_url = f"https://docs.google.com/spreadsheets/d/{self._sheet_id}/edit#gid={gid}"
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(1_500)
                self._current_tab = sheet_name
                logger.debug("GSheets: navigated to tab %r via gid=%s", sheet_name, gid)
                return
            except Exception as exc:
                logger.warning("GSheets: URL navigation to tab failed: %s — trying click", exc)

        # Fallback: click the tab in the tab bar.
        try:
            tab_selector = f'.docs-sheet-tab-name:text-is("{sheet_name}")'
            tab = page.locator(tab_selector).first
            if await tab.count() > 0:
                await tab.click(timeout=8_000)
                await page.wait_for_timeout(1_500)
                self._current_tab = sheet_name
                logger.debug("GSheets: clicked tab %r", sheet_name)
                return
            # Try partial text match as last resort
            tab_partial = page.locator(f'.docs-sheet-tab-name').filter(has_text=sheet_name).first
            if await tab_partial.count() > 0:
                await tab_partial.click(timeout=8_000)
                await page.wait_for_timeout(1_500)
                self._current_tab = sheet_name
                return
        except Exception as exc:
            logger.warning("GSheets: tab click fallback failed: %s", exc)

        # If we could not find the tab we still proceed; maybe the sheet is already active.
        self._current_tab = sheet_name

    async def _ensure_status_column(self, page: Page, sheet_name: str) -> str | None:
        """
        Find the "Status" header in row 1.  If it does not exist, create it in
        the first empty column after the last used one.  Returns the column letter.

        Result is cached so row-by-row writes avoid re-scanning headers.
        """
        if sheet_name in self._status_col:
            return self._status_col[sheet_name]

        # --- Step 1: find the last-used column by pressing Ctrl+End -----------
        # Navigate to A1 first, then Ctrl+End to find the extent of the data.
        await self._navigate_to_cell(page, "A1")
        await page.wait_for_timeout(300)

        # Read current cell ref from Name Box BEFORE and AFTER Ctrl+End.
        await page.keyboard.press("Control+End")
        await page.wait_for_timeout(600)

        last_cell = await self._read_name_box(page)
        # last_cell is like "E10" — extract just the column letter(s).
        col_match = re.match(r"([A-Z]+)", last_cell or "")
        last_col_letter = col_match.group(1) if col_match else "A"

        # Convert last column letter to index.
        last_col_idx = self._col_letter_to_index(last_col_letter)

        # --- Step 2: scan row 1 for an existing "Status" header ---------------
        # We read cells A1 … {last_col}1 via the Name Box + reading cell content.
        status_col_letter: str | None = None

        for idx in range(1, last_col_idx + 1):
            letter = _col_letter(idx)
            await self._navigate_to_cell(page, f"{letter}1")
            await page.wait_for_timeout(150)
            text = await self._read_active_cell_value(page)
            if text.strip().lower() == "status":
                status_col_letter = letter
                break

        # --- Step 3: if not found, create it in the next column ---------------
        if status_col_letter is None:
            next_col_letter = _col_letter(last_col_idx + 1)
            await self._navigate_to_cell(page, f"{next_col_letter}1")
            await self._type_in_cell(page, "Status")
            status_col_letter = next_col_letter
            logger.info(
                "GSheets: created Status header in column %s of sheet %r",
                next_col_letter,
                sheet_name,
            )

        self._status_col[sheet_name] = status_col_letter
        logger.debug(
            "GSheets: Status column for sheet %r is %s",
            sheet_name,
            status_col_letter,
        )
        return status_col_letter

    async def _navigate_to_cell(self, page: Page, cell_ref: str) -> None:
        """
        Use the Name Box (top-left cell reference input) to jump to a cell.
        This is more reliable than keyboard navigation for arbitrary cell positions.
        """
        # Click the Name Box — it has aria-label "Cell reference" or a similar selector.
        name_box = page.locator(
            '.cell-ref-g, input.jfk-textinput[aria-label="Cell reference"], '
            '.waffle-name-box, [data-input-type="nameBox"]'
        ).first

        # Fallback: find by position (top-left of the sheet grid).
        if await name_box.count() == 0:
            # Try the input inside the formula bar / name box area.
            name_box = page.locator('input.cell-name').first

        try:
            await name_box.click(timeout=5_000)
        except Exception:
            # Last resort: use Ctrl+G / Escape trick to focus the Name Box.
            # In Google Sheets, pressing Escape then typing in the Name Box
            # is not straightforward — use Ctrl+Home to go to A1 then navigate.
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
            # Try clicking the Name Box area (roughly top-left).
            try:
                await name_box.click(force=True, timeout=3_000)
            except Exception:
                logger.warning("GSheets: could not click Name Box for cell %s", cell_ref)
                return

        await page.wait_for_timeout(200)
        await page.keyboard.press("Control+a")  # select existing content
        await page.keyboard.type(cell_ref)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(400)

    async def _read_name_box(self, page: Page) -> str:
        """Read the current cell reference from the Name Box input."""
        name_box = page.locator(
            '.cell-ref-g, input.jfk-textinput[aria-label="Cell reference"], '
            '.waffle-name-box, [data-input-type="nameBox"], input.cell-name'
        ).first
        try:
            val = await name_box.input_value(timeout=3_000)
            return (val or "").strip()
        except Exception:
            return ""

    async def _read_active_cell_value(self, page: Page) -> str:
        """
        Read the display text of the currently selected cell.

        Google Sheets renders cell content in the formula bar as well as in
        the cell itself.  Reading the formula bar is the most reliable approach.
        """
        # The formula bar input (not the Name Box).
        formula_bar = page.locator(
            'input.cell-input, textarea.cell-input, '
            '[data-input-type="formulaBar"], .formula-bar-input'
        ).first

        try:
            val = await formula_bar.input_value(timeout=3_000)
            if val is not None:
                return val.strip()
        except Exception:
            pass

        # Fallback: read inner text from the selected cell in the grid.
        try:
            selected = page.locator(
                '.selected-cell-content, [data-cell-state="selected"] .cell-value, '
                'td.waffle-selected .cell-value'
            ).first
            if await selected.count() > 0:
                return (await selected.inner_text(timeout=2_000) or "").strip()
        except Exception:
            pass

        return ""

    async def _type_in_cell(self, page: Page, text: str) -> None:
        """
        Type text into the currently selected (active) cell and confirm with Enter.

        We press Enter first (in case the cell is already in edit mode from a
        previous operation), then re-navigate to the cell is NOT done here — the
        caller navigates before calling this method.
        """
        # Press Escape to exit any existing edit mode, then Enter to go into edit mode.
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(100)
        # Type directly — Google Sheets enters edit mode on first keypress.
        await page.keyboard.type(text, delay=10)
        await page.wait_for_timeout(200)
        # Confirm with Enter (moves selection down, but we navigate explicitly after).
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(300)

        # Navigate back to the cell we just typed into so subsequent operations
        # (e.g. apply background colour) target the correct cell.
        # The caller is responsible for re-navigating after this call if needed.

    async def _apply_background_colour(self, page: Page, hex_colour: str) -> None:
        """
        Apply a background (fill) colour to the currently selected cell.

        Strategy
        --------
        1. Click the fill-colour dropdown arrow in the toolbar.
        2. In the colour picker that opens, click the "Custom" option.
        3. Type the hex code into the custom colour input.
        4. Press Enter / click OK.

        If the toolbar button cannot be found or the picker interaction fails,
        the error is logged and we continue (colour is best-effort).
        """
        try:
            # The fill colour button in the Google Sheets toolbar carries the
            # aria-label "Fill color".  The dropdown arrow is a sibling element.
            fill_btn_dropdown = page.locator(
                '[aria-label="Fill color"] + .goog-toolbar-combo-button-dropdown, '
                'div[aria-label="Fill color"] .goog-toolbar-combo-button-dropdown, '
                '.goog-color-menu-button-dropdown[title*="Fill"], '
                # Newer Sheets UI
                '[data-tooltip="Fill color"] .dropdown-arrow, '
                '[aria-label*="Fill color"] .dropdown-arrow'
            ).first

            # Also accept clicking the main fill-colour button (which may open the picker).
            fill_btn_main = page.locator(
                '[aria-label="Fill color"], [data-tooltip="Fill color"]'
            ).first

            opened = False

            if await fill_btn_dropdown.count() > 0:
                try:
                    await fill_btn_dropdown.click(timeout=5_000)
                    opened = True
                except Exception:
                    pass

            if not opened and await fill_btn_main.count() > 0:
                try:
                    # Click the small dropdown arrow region (right side of button).
                    box = await fill_btn_main.bounding_box()
                    if box:
                        await page.mouse.click(
                            box["x"] + box["width"] - 5,
                            box["y"] + box["height"] / 2,
                        )
                        opened = True
                except Exception:
                    pass

            if not opened:
                logger.warning(
                    "GSheets: could not open Fill colour picker; cell colour not applied"
                )
                return

            await page.wait_for_timeout(600)

            # --- Try clicking a colour swatch directly (by hex) ---------------
            # Google Sheets renders a grid of named swatches; we try to find one
            # matching our target colour.
            hex_upper = hex_colour.upper().lstrip("#")
            hex_title = f"#{hex_upper}"

            swatch = page.locator(
                f'[title="{hex_title}"], [title="{hex_upper}"], '
                f'[data-color="{hex_title}"], [data-color="#{hex_upper.lower()}"]'
            ).first

            if await swatch.count() > 0:
                try:
                    await swatch.click(timeout=4_000)
                    await page.wait_for_timeout(400)
                    logger.debug("GSheets: applied colour %s via swatch", hex_colour)
                    return
                except Exception:
                    pass

            # --- Fallback: use the "Custom" colour entry ----------------------
            await self._apply_custom_colour(page, hex_colour)

        except Exception as exc:
            logger.warning("GSheets: _apply_background_colour failed: %s", exc)

    async def _apply_custom_colour(self, page: Page, hex_colour: str) -> None:
        """
        Open the custom colour dialog (if present) and enter the hex value.
        """
        try:
            # Look for "Custom..." or "Add custom color" button in the picker.
            custom_btn = page.locator(
                'text="Custom", text="Add custom color", '
                '[aria-label="Custom"], [title="Custom"]'
            ).first
            if await custom_btn.count() == 0:
                # Close the picker by pressing Escape and give up.
                await page.keyboard.press("Escape")
                logger.warning("GSheets: custom colour entry not found; skipping")
                return

            await custom_btn.click(timeout=4_000)
            await page.wait_for_timeout(500)

            # The hex input field in the custom colour dialog.
            hex_input = page.locator(
                'input[placeholder="HEX"], input[aria-label="Hex"], '
                'input.hex-color-input, input[maxlength="6"]'
            ).first

            if await hex_input.count() == 0:
                await page.keyboard.press("Escape")
                return

            await hex_input.click(timeout=3_000)
            await hex_input.triple_click(timeout=3_000)
            clean_hex = hex_colour.lstrip("#").upper()
            await hex_input.type(clean_hex, delay=30)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(300)

            # Click OK / Apply if a confirm button exists.
            ok_btn = page.locator(
                'button:has-text("OK"), button:has-text("Apply")'
            ).first
            if await ok_btn.count() > 0:
                await ok_btn.click(timeout=3_000)
                await page.wait_for_timeout(400)

            logger.debug("GSheets: applied custom colour %s", hex_colour)

        except Exception as exc:
            logger.warning("GSheets: _apply_custom_colour failed: %s", exc)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    @staticmethod
    def _col_letter_to_index(letters: str) -> int:
        """Convert column letter(s) to 1-based index (A→1, Z→26, AA→27, …)."""
        n = 0
        for ch in letters.upper():
            n = n * 26 + (ord(ch) - ord("A") + 1)
        return n

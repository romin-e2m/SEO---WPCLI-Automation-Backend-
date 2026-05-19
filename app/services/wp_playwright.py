"""
WordPress automation via Playwright for UI-based operations.
Handles 301 redirects and meta descriptions through WordPress admin panel.

Pause/Resume Integration Notes:
- PauseController is passed through _exec_seo_playwright_batch_run
- Use: await pause_ctrl.wait_if_paused() before major browser operations
- Key checkpoint locations:
  1. Before login (before page.goto)
  2. Before navigating to edit page
  3. Before filling form fields
  4. Before saving/submitting
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urljoin, urlunparse

from playwright.async_api import Locator, Playwright, async_playwright, Page

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = os.getenv("PLAYWRIGHT_SCREENSHOTS_DIR", "/tmp/playwright_screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def is_display_available() -> bool:
    """
    Check if an X server display is available for headed browser mode.
    
    Returns:
        True if display is available, False for headless environments
    """
    display = os.environ.get("DISPLAY", "").strip()
    if display:
        return True
    
    if os.name == "nt":
        return True
    
    return False


def should_run_headless() -> bool:
    """
    Determine if browser should run in headless mode.
    
    - Windows: Always headed (has native GUI)
    - macOS: Headed by default (has native GUI)
    - Linux: Check for X server; headless if not available
    
    Returns:
        True if should run headless, False for headed mode
    """
    if sys.platform == "win32" or sys.platform == "darwin":
        return False
    
    return not is_display_available()


async def launch_chromium(p: Playwright, *, headless: bool = True):
    """
    Launch Chromium with a clear error if the browser bundle is missing.
    System users on Debian often have HOME=/nonexistent, so Playwright looks
    under /nonexistent/.cache/ms-playwright unless PLAYWRIGHT_BROWSERS_PATH is set.
    """
    try:
        return await p.chromium.launch(headless=headless)
    except Exception as e:
        err = str(e)
        if "Executable doesn't exist" in err or "doesn't exist at" in err:
            raise RuntimeError(
                "Playwright Chromium is not installed or not on PATH. "
                "On the host: run `python -m playwright install chromium`. "
                "In Docker: rebuild the API image (it must bundle browsers or run `playwright install` at build). "
                "If HOME is /nonexistent, set PLAYWRIGHT_BROWSERS_PATH to the directory that contains the install."
            ) from e
        raise


def extract_slug_with_trailing_slash(url: str) -> str:
    """
    Extract the last slug from a URL with trailing slash.
    
    Examples:
        http://seo-automation.local/elementor-7/ → "elementor-7/"
        http://seo-automation.local/sample-page/ → "sample-page/"
        http://seo-automation.local/elementor-7 → "elementor-7/"
    
    Args:
        url: Full URL
    
    Returns:
        Last slug with trailing slash
    """
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    
    if not path:
        return ""
    
    # Get the last segment
    segments = path.split('/')
    last_slug = segments[-1] if segments else ""
    
    # Ensure trailing slash
    return last_slug + "/" if last_slug else ""


def _normalize_eps_from_slug(slug: str) -> str:
    s = (slug or "").strip().lower()
    if not s:
        return ""
    return s if s.endswith("/") else s + "/"


def _normalize_from_cell(from_text: str) -> str:
    """Table may show slug only or a full URL; align with extract_slug_with_trailing_slash."""
    t = (from_text or "").strip()
    if not t:
        return ""
    if "://" in t or t.startswith("//"):
        return _normalize_eps_from_slug(extract_slug_with_trailing_slash(t))
    return _normalize_eps_from_slug(t)


def _host_for_compare(netloc: str) -> str:
    h = (netloc or "").strip().lower()
    if h.startswith("www."):
        return h[4:]
    return h


def _url_identity_key(url: str) -> str | None:
    """Host (without www) + path, lowercased, no trailing slash — compare destinations."""
    try:
        raw = (url or "").strip()
        if not raw:
            return None
        p = urlparse(raw)
        if not p.scheme and raw.startswith("//"):
            p = urlparse("https:" + raw)
        netloc = _host_for_compare(p.netloc or "")
        path = (p.path or "/").rstrip("/").lower()
        if not netloc and path.startswith("//"):
            inner = urlparse("https:" + path)
            netloc = _host_for_compare(inner.netloc or "")
            path = (inner.path or "/").rstrip("/").lower()
        if path.startswith("/"):
            path = path[1:]
        return f"{netloc}/{path}" if path else netloc or None
    except Exception:
        return None


def _slugify_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _to_cell_text_matches_url(to_cell_text: str, to_url: str) -> bool:
    """Match list column text (often post title) to target URL path (no REST)."""
    label = (to_cell_text or "").strip()
    if not label:
        return False
    slug_seg = extract_slug_with_trailing_slash(to_url).rstrip("/").lower()
    if len(slug_seg) < 2:
        return False
    ls = _slugify_label(slug_seg)
    lt = _slugify_label(label)
    if not ls or not lt:
        return False
    if ls == lt:
        return True
    if len(ls) >= 4 and (ls in lt or lt in ls):
        return True
    return False


def _eps_row_matches_destination(to_hrefs: list[str], to_text: str, to_url: str) -> bool:
    want = _url_identity_key(to_url)
    if want:
        for h in to_hrefs or []:
            hk = _url_identity_key(h)
            if hk and hk == want:
                return True
    ulow = (to_url or "").strip().lower()
    for h in to_hrefs or []:
        if (h or "").strip().lower() == ulow:
            return True
    if _to_cell_text_matches_url(to_text, to_url):
        return True
    tnorm = re.sub(r"\s+", " ", (to_text or "").strip().lower())
    if tnorm and ulow and ulow in tnorm:
        return True
    return False


def _eps_redirect_is_duplicate(
    from_slug: str, to_url: str, rows: list[dict[str, Any]]
) -> bool:
    nf = _normalize_eps_from_slug(from_slug)
    if not nf:
        return False
    for row in rows:
        rf = _normalize_from_cell(str(row.get("fromText") or ""))
        if rf != nf:
            continue
        hrefs = row.get("hrefs") or []
        if not isinstance(hrefs, list):
            hrefs = []
        hrefs = [str(h) for h in hrefs if h]
        if _eps_row_matches_destination(hrefs, str(row.get("toText") or ""), to_url):
            return True
    return False


def _is_wordpress_editor_save_response(resp) -> bool:
    """Detect block editor save (REST) or classic post.php save."""
    u = resp.url or ""
    m = (resp.request.method or "").upper()
    if m not in ("PUT", "POST", "PATCH"):
        return False
    if "wp-json/wp/v2/pages" in u or "wp-json/wp/v2/posts" in u:
        return True
    if "/wp/v2/pages/" in u or "/wp/v2/posts/" in u:
        return True
    if "rest_route=%2Fwp%2Fv2%2Fpages" in u or "rest_route=%2Fwp%2Fv2%2Fposts" in u:
        return True
    if "post.php" in u and m == "POST":
        return True
    return False


def normalize_wp_admin_root(admin_url: str) -> str:
    """
    Force admin base to ``.../wp-admin/`` (never ``.../wp-admin/index.php/``).

    ``urljoin(".../index.php/", "post.php?post=1")`` incorrectly becomes
    ``.../index.php/post.php?...``, which breaks the block editor and Yoast metabox.
    """
    parsed = urlparse((admin_url or "").strip())
    path = (parsed.path or "").rstrip("/")
    marker = "/wp-admin"
    idx = path.find(marker)
    if idx >= 0:
        root_path = path[: idx + len(marker)] + "/"
    else:
        root_path = "/wp-admin/"
    return urlunparse((parsed.scheme, parsed.netloc, root_path, "", "", ""))


def slug_matches_admin_title(slug: str, title: str) -> bool:
    """
    Match a URL path slug to a WordPress list-table title (titles rarely contain the raw slug).
    e.g. slug 'elementor-7' matches 'Elementor #7 – SEO – Automation'.
    """
    if not slug or not title:
        return False
    s = slug.strip().lower().strip("/")
    t = title.lower()
    if not s:
        return False
    if s in t.replace(" ", "-").replace("_", "-"):
        return True
    parts = [p for p in re.split(r"[-_]+", s) if p]
    if not parts:
        return False
    return all(p in t for p in parts)


class PlaywrightLogger:
    """Captures Playwright action logs for UI display."""
    
    def __init__(self, stream_logs: bool = True):
        self.logs: list[dict[str, Any]] = []
        self.screenshots: list[str] = []
        self.stream_logs = stream_logs
    
    def add_log(self, action: str, level: str = "info", details: str = ""):
        """
        Add a log entry and stream it in real-time.
        
        Args:
            action: Description of the action
            level: "info", "warning", "error", or "success"
            details: Additional details
        """
        timestamp = datetime.now().isoformat()
        log_entry = {
            "action": action,
            "level": level,
            "details": details,
            "timestamp": timestamp
        }
        self.logs.append(log_entry)
        
        formatted = f"[{timestamp}] [{level.upper():7}] {action}"
        if details:
            formatted += f" | {details}"
        
        log_level = (
            logging.INFO if level in ["info", "success"] else 
            logging.WARNING if level == "warning" else 
            logging.ERROR
        )
        
        logger.log(log_level, formatted)

        try:
            from app.services.monitor_broadcast import emit_monitor_playwright

            emit_monitor_playwright(action, level, details)
        except Exception:
            pass

    def add_screenshot(self, screenshot_path: str):
        """Record a screenshot path."""
        self.screenshots.append(screenshot_path)
        logger.debug('Screenshot captured: %s', screenshot_path)
    
    def get_logs(self) -> list[dict[str, Any]]:
        """Return all logs."""
        return self.logs
    
    def get_screenshots(self) -> list[str]:
        """Return all screenshot paths."""
        return self.screenshots
    
    def clear_logs(self):
        """Clear all logs."""
        self.logs.clear()
        self.screenshots.clear()


class WordPressPlaywright:
    """
    Automates WordPress admin panel operations using Playwright.
    Supports 301 redirects and meta description updates.
    
    Optional pause_controller for fine-grained pause/resume at action level.
    Pass pause_ctrl to enable pause checks before major operations.
    """
    
    def __init__(self, admin_url: str, username: str, password: str, pause_ctrl: Any = None, execution_logger: Any = None):
        """
        Initialize WordPress Playwright automation.
        
        Args:
            admin_url: WordPress admin panel URL (e.g., http://example.com/wp-admin/)
            username: WordPress username
            password: WordPress password
            pause_ctrl: Optional PauseController for action-level pause checkpoints
            execution_logger: Optional ExecutionLogger to check pause state
        """
        self.admin_url = normalize_wp_admin_root(admin_url)
        self.username = username
        self.password = password
        self.logger = PlaywrightLogger()
        self.pause_ctrl = pause_ctrl
        self.execution_logger = execution_logger
        
        # Parse base URL from admin_url
        parsed = urlparse(self.admin_url)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
    
    async def login(self, page: Page) -> bool:
        """
        Log into WordPress admin panel with optimized performance.
        Skips screenshots and reduces waits.
        
        Returns:
            True if login successful, False otherwise
        """
        try:
            if self.pause_ctrl is not None:
                await self.pause_ctrl.wait_if_paused()
            
            self.logger.add_log("🔐 WordPress Login", "info", "")
            
            await page.goto(self.admin_url, wait_until="domcontentloaded", timeout=20000)
            
            try:
                await page.wait_for_url("**/wp-admin/", timeout=2000)
                self.logger.add_log("✅ Already logged in", "success", "")
                return True
            except Exception:
                pass
            
            username_field = await page.query_selector('input[name="log"]') or await page.query_selector('input[type="text"]')
            password_field = await page.query_selector('input[name="pwd"]') or await page.query_selector('input[type="password"]')
            
            if not username_field or not password_field:
                self.logger.add_log("❌ Login form not found", "error", "")
                return False
            
            # Pause checkpoint before filling credentials
            if self.pause_ctrl is not None:
                await self.pause_ctrl.wait_if_paused()
            
            await username_field.fill(self.username)
            await password_field.fill(self.password)
            
            login_button = await page.query_selector('button[type="submit"], input[type="submit"]')
            if login_button:
                await login_button.click()
            else:
                await page.press('input[type="password"]', 'Enter')
            
            await page.wait_for_url("**/wp-admin/", timeout=12000)
            self.logger.add_log("✅ Login successful", "success", "")
            return True
            
        except Exception as e:
            self.logger.add_log("❌ Login failed", "error", str(e)[:60])
            logger.error(f"WordPress login failed: {e}")
            return False
    
    async def _close_popup_if_exists(self, page: Page) -> None:
        """
        Safely close any popups or modals that might appear on the page.
        Clicks outside the dialog area to dismiss it.
        """
        try:
            dialog = await page.query_selector('dialog, .modal, [role="dialog"]')
            if dialog:
                self.logger.add_log("Popup detected, attempting to close", "info")
                await page.keyboard.press('Escape')
                await page.wait_for_timeout(300)
                
                textbox = await page.query_selector('dialog textbox')
                if textbox:
                    await textbox.press('Escape')
                
                await page.wait_for_timeout(300)
                self.logger.add_log("Popup closed", "success")
        except Exception as e:
            self.logger.add_log("Popup close attempt (non-critical)", "warning", str(e))

    async def _find_redirect_form_fields(self, page: Page) -> dict[str, Any]:
        """
        Find the redirect form input fields using multiple strategies with enhanced detection.
        
        Returns:
            Dict with 'from_field', 'to_field', and 'save_button' or None if not found
        """
        try:
            from_field = None
            to_field = None
            save_button = None
            
            all_inputs = await page.query_selector_all('input[type="text"]')
            
            self.logger.add_log(
                "🔍 Scanning form fields",
                "info",
                f"Found {len(all_inputs)} text input fields"
            )
            
            for idx, inp in enumerate(all_inputs):
                placeholder = await inp.get_attribute('placeholder') or ""
                name = await inp.get_attribute('name') or ""
                value = await inp.get_attribute('value') or ""
                visible = await inp.is_visible()
                
                if not from_field and any(x in placeholder.lower() + name.lower() for x in ['from', 'redirect']):
                    if 'to' not in placeholder.lower():
                        from_field = inp
                        self.logger.add_log(
                            f"📍 'From' field found (#{idx})",
                            "info",
                            f"name='{name}' | visible={visible}"
                        )
                
                if not to_field and any(x in placeholder.lower() + name.lower() for x in ['to', 'target', 'destination']):
                    to_field = inp
                    self.logger.add_log(
                        f"📍 'To' field found (#{idx})",
                        "info",
                        f"name='{name}' | visible={visible}"
                    )
            
            if not save_button:
                all_buttons = await page.query_selector_all('button, input[type="submit"], input[type="button"]')
                self.logger.add_log(
                    "🔍 Scanning buttons",
                    "info",
                    f"Found {len(all_buttons)} buttons/submit inputs"
                )
                
                for btn_idx, btn in enumerate(all_buttons):
                    text = (await btn.text_content() or "").strip()
                    btn_type = await btn.get_attribute('type') or await btn.tag_name()
                    visible = await btn.is_visible()
                    name = await btn.get_attribute('name') or ""
                    
                    keywords = ['save', 'add', 'create', 'submit', 'update', 'redirect', 'store']
                    if any(x in text.lower() + name.lower() for x in keywords):
                        save_button = btn
                        self.logger.add_log(
                            f"✅ Action button found (#{btn_idx})",
                            "success",
                            f"Text: '{text}' | Type: {btn_type} | visible={visible}"
                        )
                        break
            
            if not from_field or not to_field or not save_button:
                missing_parts = []
                if not from_field:
                    missing_parts.append("'From' field")
                if not to_field:
                    missing_parts.append("'To' field")
                if not save_button:
                    missing_parts.append("Save button")
                
                self.logger.add_log(
                    "⚠️  Some form elements missing",
                    "warning",
                    f"Missing: {', '.join(missing_parts)}"
                )
            
            return {
                "from_field": from_field,
                "to_field": to_field,
                "save_button": save_button
            }
        except Exception as e:
            self.logger.add_log("❌ Error finding form fields", "error", str(e))
            return {
                "from_field": None,
                "to_field": None,
                "save_button": None
            }

    async def _collect_existing_eps_redirect_rows(self, page: Page) -> list[dict[str, Any]]:
        """
        Read current redirect rules from the EPS 301 Redirects admin table (DOM only, no API).

        Returns rows with fromText, toText, and hrefs from the Redirect To cell.
        """
        try:
            raw = await page.evaluate(
                """
                () => {
                    const rows = [];
                    const normHeader = (t) => (t || "").replace(/\\s+/g, " ").trim().toLowerCase();
                    for (const table of document.querySelectorAll("table")) {
                        const trs = [...table.querySelectorAll("tr")];
                        let fromI = -1;
                        let toI = -1;
                        let start = -1;
                        for (let ri = 0; ri < trs.length; ri++) {
                            const cells = [...trs[ri].querySelectorAll("th, td")];
                            if (!cells.length) continue;
                            const texts = cells.map((c) => normHeader(c.innerText));
                            if (texts.some((t) => t.includes("redirect from")) && texts.some((t) => t.includes("redirect to"))) {
                                fromI = texts.findIndex((t) => t.includes("redirect from"));
                                toI = texts.findIndex((t) => t.includes("redirect to"));
                                start = ri + 1;
                                break;
                            }
                        }
                        if (start < 0 || fromI < 0 || toI < 0) continue;
                        for (let ri = start; ri < trs.length; ri++) {
                            const tds = [...trs[ri].querySelectorAll("td")];
                            if (tds.length <= Math.max(fromI, toI)) continue;
                            const firstTxt = normHeader(tds[0].innerText).split(/\\s+/)[0];
                            if (!/^\\d+$/.test(firstTxt)) continue;
                            const fromText = (tds[fromI].innerText || "").trim().replace(/\\s+/g, " ");
                            const toText = (tds[toI].innerText || "").trim().replace(/\\s+/g, " ");
                            const hrefs = [...tds[toI].querySelectorAll("a[href]")].map((a) => a.href).filter(Boolean);
                            rows.push({ fromText, toText, hrefs });
                        }
                    }
                    return rows;
                }
                """
            )
            if not isinstance(raw, list):
                return []
            out: list[dict[str, Any]] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                out.append(
                    {
                        "fromText": item.get("fromText") or "",
                        "toText": item.get("toText") or "",
                        "hrefs": item.get("hrefs") if isinstance(item.get("hrefs"), list) else [],
                    }
                )
            return out
        except Exception as e:
            self.logger.add_log(
                "⚠️ Could not read existing redirect table (dedupe skipped)",
                "warning",
                str(e),
            )
            return []

    async def create_301_redirect(
        self, 
        from_slug: str, 
        to_url: str,
        page: Page
    ) -> dict[str, Any]:
        """
        Create a 301 redirect using the 301 Redirects plugin UI with visual feedback.
        
        Args:
            from_slug: URL slug to redirect FROM (e.g., "elementor-7/")
            to_url: Full URL to redirect TO (e.g., "http://seo-automation.local/new-page/")
            page: Playwright Page object
        
        Returns:
            Dict with status and details
        """
        try:
            self.logger.add_log(
                "🔄 Creating 301 redirect",
                "info",
                f"{from_slug} → {to_url}"
            )
            
            redirect_url = urljoin(self.admin_url, "options-general.php?page=eps_redirects")
            self.logger.add_log(
                "🌐 Navigating to redirects page",
                "info",
                redirect_url
            )
            
            await page.goto(redirect_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(500)
            
            screenshot_path = f"{SCREENSHOTS_DIR}/04_redirect_page_loaded_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=screenshot_path)
            self.logger.add_screenshot(screenshot_path)
            
            await self._close_popup_if_exists(page)
            
            self.logger.add_log(
                "✅ Redirects page loaded",
                "success"
            )

            existing_rows = await self._collect_existing_eps_redirect_rows(page)
            if _eps_redirect_is_duplicate(from_slug, to_url, existing_rows):
                self.logger.add_log(
                    "⏭️ Duplicate redirect skipped (same From → To already in table)",
                    "info",
                    f"{from_slug} → {to_url}",
                )
                return {
                    "status": "skipped",
                    "reason": "duplicate",
                    "message": "Identical redirect already exists (Playwright table check).",
                    "from_slug": from_slug,
                    "to_url": to_url,
                    "logs": self.logger.get_logs(),
                    "screenshots": self.logger.get_screenshots(),
                }

            self.logger.add_log(
                "🔍 Finding form fields",
                "info",
                "Searching for 'From' and 'To' fields"
            )
            
            max_retries = 3
            for attempt in range(max_retries):
                fields = await self._find_redirect_form_fields(page)
                from_field = fields.get("from_field")
                to_field = fields.get("to_field")
                save_button = fields.get("save_button")
                
                if from_field and to_field and save_button:
                    self.logger.add_log(
                        "✅ All form fields found",
                        "success"
                    )
                    break
                
                if attempt < max_retries - 1:
                    self.logger.add_log(
                        f"⏳ Retrying field detection ({attempt + 1}/{max_retries})",
                        "warning"
                    )
                    await page.wait_for_timeout(800)
                else:
                    screenshot_path = f"{SCREENSHOTS_DIR}/redirect_form_not_found_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    await page.screenshot(path=screenshot_path)
                    self.logger.add_screenshot(screenshot_path)
                    
                    missing = []
                    if not from_field: missing.append("From")
                    if not to_field: missing.append("To")
                    if not save_button: missing.append("Save")
                    
                    error_msg = f"Missing: {', '.join(missing)}"
                    self.logger.add_log(
                        "❌ Form fields not found",
                        "error",
                        error_msg
                    )
                    return {
                        "status": "failed",
                        "error": error_msg,
                        "logs": self.logger.get_logs(),
                        "screenshots": self.logger.get_screenshots()
                    }
            
            self.logger.add_log(
                "📝 Filling 'From' field",
                "info",
                from_slug
            )
            
            await from_field.click()
            await page.wait_for_timeout(150)
            await from_field.fill(from_slug)
            await page.wait_for_timeout(200)
            
            self.logger.add_log(
                "✅ 'From' field filled",
                "success"
            )
            
            self.logger.add_log(
                "📝 Filling 'To' field",
                "info",
                to_url
            )
            
            await to_field.click()
            await page.wait_for_timeout(150)
            await to_field.fill(to_url)
            await page.wait_for_timeout(200)
            
            self.logger.add_log(
                "✅ 'To' field filled",
                "success"
            )
            
            screenshot_path = f"{SCREENSHOTS_DIR}/05_redirect_form_filled_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=screenshot_path)
            self.logger.add_screenshot(screenshot_path)
            
            self.logger.add_log(
                "🖱️  Clicking Save button",
                "info",
                "Submitting redirect"
            )
            
            await save_button.click()
            
            self.logger.add_log(
                "⏳ Saving redirect",
                "info",
                "Waiting for confirmation"
            )
            
            await page.wait_for_timeout(800)
            
            screenshot_path = f"{SCREENSHOTS_DIR}/06_redirect_saved_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=screenshot_path)
            self.logger.add_screenshot(screenshot_path)
            
            self.logger.add_log(
                "✅ 301 redirect created",
                "success",
                f"{from_slug} → {to_url}"
            )
            
            return {
                "status": "created",
                "from_slug": from_slug,
                "to_url": to_url,
                "logs": self.logger.get_logs(),
                "screenshots": self.logger.get_screenshots()
            }
            
        except Exception as e:
            self.logger.add_log(
                "❌ Redirect creation failed",
                "error",
                str(e)
            )
            try:
                screenshot_path = f"{SCREENSHOTS_DIR}/error_redirect_creation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=screenshot_path)
                self.logger.add_screenshot(screenshot_path)
            except Exception:
                pass
            logger.error(f"Failed to create redirect: {e}")
            return {
                "status": "failed",
                "error": str(e),
                "logs": self.logger.get_logs(),
                "screenshots": self.logger.get_screenshots()
            }

    async def _safe_scroll_into_view(self, page: Page, target, *, timeout_ms: int = 2000) -> None:
        """Scroll into view with short timeout."""
        try:
            await target.scroll_into_view_if_needed(timeout=timeout_ms)
        except Exception:
            try:
                await target.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
            except Exception:
                pass
        await page.wait_for_timeout(50)

    async def _ensure_yoast_metabox_open(self, page: Page) -> None:
        """Expand classic Yoast postbox if WordPress left it closed (snippet fields stay display:none)."""
        box = page.locator("#wpseo_meta").first
        try:
            if await box.count() == 0:
                return
            cls = (await box.get_attribute("class")) or ""
            if "closed" not in cls:
                return
            hdr = box.locator(".postbox-header, .hndle").first
            if await hdr.count() > 0:
                await hdr.click(timeout=4000, force=True)
                await page.wait_for_timeout(350)
        except Exception:
            pass

    async def _open_yoast_sidebar_tab_if_present(self, page: Page) -> None:
        """If Yoast lives in the block editor right sidebar, activate its tab so preview fields are visible."""
        try:
            side = page.locator(".interface-complementary-area")
            if await side.count() == 0:
                return
            tab = side.get_by_role("tab", name=re.compile(r"Yoast", re.I)).first
            if await tab.count() == 0 or not await tab.is_visible():
                return
            if (await tab.get_attribute("aria-selected")) == "true":
                return
            await tab.click(timeout=4000)
            await page.wait_for_timeout(450)
        except Exception:
            pass

    async def _resolve_yoast_google_preview_cell(
        self, page: Page, dom_id: str
    ) -> Locator | None:
        """
        Return a locator for a Yoast snippet preview cell.

        Yoast often mounts duplicate nodes (e.g. mobile/desktop); ``.first`` can be the hidden
        copy, which makes ``click()`` wait until timeout with 'not visible'.
        """
        cells = page.locator(f"#{dom_id}")

        async def _pick_visible() -> Locator | None:
            nc = await cells.count()
            for i in range(nc):
                c = cells.nth(i)
                try:
                    if await c.is_visible():
                        return c
                except Exception:
                    continue
            return None

        hit = await _pick_visible()
        if hit:
            return hit
        await self._ensure_yoast_metabox_open(page)
        await page.wait_for_timeout(350)
        hit = await _pick_visible()
        if hit:
            return hit
        n = await cells.count()
        return cells.last if n else None

    async def _focus_yoast_contenteditable(self, page: Page, loc) -> None:
        """Focus snippet field efficiently."""
        try:
            await loc.click(force=True, timeout=2000)
        except Exception:
            try:
                await loc.evaluate("el => { el.focus(); }")
                await page.wait_for_timeout(50)
            except Exception:
                pass

    async def _set_contenteditable_text_react(
        self, loc, text: str
    ) -> bool:
        """
        Set text on a React/Draft.js controlled contenteditable when keyboard interaction misses the node.
        Yoast uses Draft.js which requires specific event handling.
        IMPORTANT: Must select all existing content, delete it, then insert new text.
        """
        try:
            # First, click to focus and ensure element is active
            await loc.click(force=True)
            await loc.evaluate("el => el.focus()")
            
            # Now use the evaluate to select all and replace
            return bool(
                await loc.evaluate(
                    """(el, txt) => {
                    const t = String(txt ?? '');
                    
                    // Ensure focus
                    el.focus();
                    
                    // Step 1: Select all content using keyboard shortcut simulation
                    // Create and dispatch Ctrl+A keydown event
                    const selectAllEvent = new KeyboardEvent('keydown', {
                      key: 'a',
                      code: 'KeyA',
                      keyCode: 65,
                      ctrlKey: true,
                      bubbles: true,
                      cancelable: true,
                    });
                    el.dispatchEvent(selectAllEvent);
                    
                    // Step 2: Use getSelection API to select all
                    const selection = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    selection.removeAllRanges();
                    selection.addRange(range);
                    
                    // Step 3: Delete selected content by dispatching beforeinput with deleteByCut
                    const deleteEvent = new InputEvent('beforeinput', {
                      bubbles: true,
                      cancelable: true,
                      inputType: 'deleteByCut',
                    });
                    el.dispatchEvent(deleteEvent);
                    
                    // Step 4: Clear the element completely
                    el.innerHTML = '';
                    el.textContent = '';
                    
                    // Step 5: Insert new text
                    el.textContent = t;
                    
                    // Step 6: Dispatch input events to notify Draft.js of the change
                    const inputEvent = new InputEvent('input', {
                      bubbles: true,
                      cancelable: true,
                      inputType: 'insertText',
                      data: t,
                    });
                    el.dispatchEvent(inputEvent);
                    
                    // Dispatch additional events for React/Draft.js
                    el.dispatchEvent(new Event('beforeinput', {
                      bubbles: true,
                      cancelable: true,
                    }));
                    
                    el.dispatchEvent(new Event('input', {
                      bubbles: true,
                    }));
                    
                    el.dispatchEvent(new Event('change', {
                      bubbles: true,
                    }));
                    
                    el.dispatchEvent(new Event('blur', {
                      bubbles: true,
                    }));
                    
                    // Verify the change
                    return (el.textContent || '').trim() === t.trim();
                }""",
                    text,
                )
            )
        except Exception as e:
            return False

    async def _scroll_to_block_editor_metaboxes(self, page: Page) -> None:
        """Gutenberg loads SEO fields under **Meta Boxes** at the bottom — scroll there first."""
        for sel in (
            ".edit-post-layout__metaboxes",
            ".edit-post-meta-boxes-area",
            ".interface-interface-skeleton__footer",
        ):
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0:
                    await self._safe_scroll_into_view(page, loc)
                    await page.wait_for_timeout(120)
                    break
            except Exception:
                continue
        try:
            await page.evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); }"
            )
        except Exception:
            pass
        await page.wait_for_timeout(180)


    async def _fill_seo_meta_description(self, page: Page, meta_description: str) -> bool:
        """
        Fill meta description with comprehensive selector fallbacks.
        """
        await page.wait_for_timeout(100)
        await self._scroll_to_block_editor_metaboxes(page)
        await self._ensure_yoast_metabox_open(page)
        await self._open_yoast_sidebar_tab_if_present(page)
        await page.wait_for_timeout(300)

        try:
            await page.locator("#yoast-google-preview-description-metabox").first.wait_for(
                state="attached", timeout=15000
            )
        except Exception:
            pass

        try:
            loc = await self._resolve_yoast_google_preview_cell(page, "yoast-google-preview-description-metabox")
            if loc:
                is_visible = await loc.is_visible()
                if is_visible:
                    await self._safe_scroll_into_view(page, loc, timeout_ms=2000)
                    await self._focus_yoast_contenteditable(page, loc)
                    await page.wait_for_timeout(100)

                    await page.keyboard.press('Control+A')
                    await page.keyboard.press('Delete')
                    await page.keyboard.type(meta_description, delay=1)
                    await page.wait_for_timeout(200)
                    final_text = await loc.inner_text()
                    if meta_description.strip() in final_text.strip() or final_text.strip() in meta_description.strip():
                        pass
                    else:
                        try:
                            await self._set_contenteditable_text_react(loc, meta_description)
                            await page.wait_for_timeout(200)
                        except Exception:
                            pass
                        final_text2 = await loc.inner_text()
                        if meta_description.strip() not in final_text2.strip() and final_text2.strip() not in meta_description.strip():
                            logger.warning("Meta description content mismatch after typing: expected %r, got %r", meta_description[:80], final_text2[:80])
                    await loc.evaluate("""(el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }""")

                    self.logger.add_log("✅ Meta description filled (preview)", "success", f"{len(meta_description)} chars")
                    return True
        except Exception:
            pass

        actual_input_selectors = [
            "textarea[name='_yoast_wpseo_metadesc']",
            "input[name='_yoast_wpseo_metadesc']",
            "textarea#yoast_wpseo_metadesc",
            "input#yoast_wpseo_metadesc",
            "textarea[name='rank_math_description']",
            "input[name='rank_math_description']",
            "#wpseo_meta textarea",
            "#wpseo_meta input",
            "textarea[placeholder*='description' i]",
            "input[placeholder*='description' i]",
        ]

        for selector in actual_input_selectors:
            try:
                loc = page.locator(selector).first
                if await loc.count() > 0:
                    is_visible = await loc.is_visible()
                    if not is_visible:
                        continue

                    try:
                        await self._safe_scroll_into_view(page, loc, timeout_ms=2000)
                    except Exception:
                        pass

                    try:
                        await loc.fill(meta_description, timeout=8000)
                        await loc.evaluate("""(el) => {
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.dispatchEvent(new Event('blur', { bubbles: true }));
                        }""")
                        self.logger.add_log("✅ Meta description filled", "success", f"{len(meta_description)} chars")
                        return True
                    except Exception:
                        continue
            except Exception:
                continue

        return False

    async def _fill_seo_meta_description_fallback(self, page: Page, meta_description: str) -> bool:
        """
        Fallback for older Yoast versions or different DOM structures.
        Tries textarea selectors with extended options.
        """
        selectors = [
            "#wpseo_meta textarea#yoast_wpseo_metadesc",
            "#wpseo_meta textarea[name='_yoast_wpseo_metadesc']",
            "#wpseo_meta textarea",
            "textarea#yoast_wpseo_metadesc",
            "textarea[name='_yoast_wpseo_metadesc']",
            "#rank_math_description",
            "textarea[name='rank_math_description']",
            ".wpseo-meta-description textarea",
            "[data-test-id*='description'] textarea",
            "[data-test-id*='description'] input",
        ]

        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                
                is_visible = await loc.is_visible()
                if not is_visible:
                    continue
                
                try:
                    await self._safe_scroll_into_view(page, loc, timeout_ms=2000)
                except Exception:
                    pass
                
                await loc.click(timeout=5000)
                await loc.fill(meta_description, timeout=15000, force=True)
                
                await loc.evaluate("""(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }""")
                
                self.logger.add_log(
                    "✅ Meta description filled (fallback)",
                    "success",
                    sel,
                )
                return True
            except Exception:
                continue
        
        return False

    async def _fill_seo_meta_title(self, page: Page, meta_title: str) -> bool:
        """Fill SEO title with comprehensive selector fallbacks."""
        await page.wait_for_timeout(100)
        await self._scroll_to_block_editor_metaboxes(page)
        await self._ensure_yoast_metabox_open(page)
        await self._open_yoast_sidebar_tab_if_present(page)
        await page.wait_for_timeout(300)

        try:
            await page.locator("#yoast-google-preview-title-metabox").first.wait_for(
                state="attached", timeout=15000
            )
        except Exception:
            pass

        try:
            loc = await self._resolve_yoast_google_preview_cell(page, "yoast-google-preview-title-metabox")
            if loc:
                is_visible = await loc.is_visible()
                if is_visible:
                    await self._safe_scroll_into_view(page, loc, timeout_ms=2000)
                    await self._focus_yoast_contenteditable(page, loc)
                    await page.wait_for_timeout(100)

                    await page.keyboard.press('Control+A')
                    await page.keyboard.press('Delete')
                    await page.keyboard.type(meta_title, delay=1)
                    await page.wait_for_timeout(200)
                    final_text = await loc.inner_text()
                    if meta_title.strip() in final_text.strip() or final_text.strip() in meta_title.strip():
                        pass
                    else:
                        try:
                            await self._set_contenteditable_text_react(loc, meta_title)
                            await page.wait_for_timeout(200)
                        except Exception:
                            pass
                        final_text2 = await loc.inner_text()
                        if meta_title.strip() not in final_text2.strip() and final_text2.strip() not in meta_title.strip():
                            logger.warning("SEO title content mismatch after typing: expected %r, got %r", meta_title[:80], final_text2[:80])
                    await loc.evaluate("""(el) => {
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                    }""")

                    self.logger.add_log("✅ SEO title filled (preview)", "success", f"{len(meta_title)} chars")
                    return True
        except Exception:
            pass

        actual_input_selectors = [
            "input[name='_yoast_wpseo_title']",
            "input#yoast_wpseo_title",
            "textarea[name='_yoast_wpseo_title']",
            "textarea#yoast_wpseo_title",
            "input[name='rank_math_title']",
            "input#rank_math_title",
            "#wpseo_meta input[type='text']",
            "#wpseo_meta textarea",
            "input[placeholder*='title' i]",
            "textarea[placeholder*='title' i]",
        ]

        for selector in actual_input_selectors:
            try:
                loc = page.locator(selector).first
                if await loc.count() > 0:
                    is_visible = await loc.is_visible()
                    if not is_visible:
                        continue

                    try:
                        await self._safe_scroll_into_view(page, loc, timeout_ms=2000)
                    except Exception:
                        pass

                    try:
                        await loc.fill(meta_title, timeout=8000)
                        await loc.evaluate("""(el) => {
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.dispatchEvent(new Event('blur', { bubbles: true }));
                        }""")
                        self.logger.add_log("✅ SEO title filled", "success", f"{len(meta_title)} chars")
                        return True
                    except Exception:
                        continue
            except Exception:
                continue

        return False

    async def _fill_seo_meta_title_fallback(self, page: Page, meta_title: str) -> bool:
        """Fallback: classic Yoast / Rank Math input fields for SEO title."""
        selectors = [
            "#wpseo_meta input#yoast_wpseo_title",
            "#wpseo_meta input[name='_yoast_wpseo_title']",
            "input#yoast_wpseo_title",
            "input[name='_yoast_wpseo_title']",
            "#rank_math_title",
            "input[name='rank_math_title']",
            ".wpseo-meta-title input",
            "[data-test-id*='title'] input",
            "#wpseo_meta textarea[name='_yoast_wpseo_title']",
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                
                is_visible = await loc.is_visible()
                if not is_visible:
                    continue
                
                try:
                    await self._safe_scroll_into_view(page, loc, timeout_ms=2000)
                except Exception:
                    pass
                
                await loc.click(timeout=5000)
                await loc.fill(meta_title, timeout=15000, force=True)
                
                await loc.evaluate("""(el) => {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                }""")
                
                self.logger.add_log("✅ SEO title filled (fallback)", "success", sel)
                return True
            except Exception:
                continue
        return False

    async def _click_save_post_editor(self, page: Page) -> bool:
        """Save / update in block editor or classic editor; wait for WP save network call."""
        candidates = [
            "button.editor-post-publish-button",
            "button.editor-post-save-button",
            'button:has-text("Update")',
            'button:has-text("Save")',
            'input[name="save"]',
            "#publish",
        ]
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                if not await loc.is_visible():
                    continue
                await self._safe_scroll_into_view(page, loc)
                try:
                    async with page.expect_response(
                        _is_wordpress_editor_save_response,
                        timeout=35000,
                    ):
                        await loc.click(timeout=15000)
                    self.logger.add_log(
                        "✅ Save completed (WP REST/classic response)",
                        "success",
                        sel,
                    )
                except Exception as resp_e:
                    self.logger.add_log(
                        "Save: no matching HTTP response (click was sent)",
                        "warning",
                        f"{sel} | {resp_e}",
                    )
                return True
            except Exception:
                continue
        try:
            pub = page.get_by_role(
                "button", name=re.compile(r"Update|Save|Publish", re.I)
            ).first
            if await pub.count() > 0 and await pub.is_visible():
                await self._safe_scroll_into_view(page, pub)
                try:
                    async with page.expect_response(
                        _is_wordpress_editor_save_response,
                        timeout=35000,
                    ):
                        await pub.click(timeout=15000)
                    self.logger.add_log(
                        "✅ Save completed (WP REST/classic response)",
                        "success",
                        "role button",
                    )
                except Exception as resp_e:
                    self.logger.add_log(
                        "Save: no matching HTTP response (click was sent)",
                        "warning",
                        str(resp_e),
                    )
                return True
        except Exception:
            pass
        return False

    async def _find_row_title_in_list(
        self, page: Page, page_slug: str, *, post_type: str
    ) -> Any:
        """Return row-title ElementHandle or None. post_type is 'page' or 'post'."""
        if post_type == "page":
            list_url = urljoin(self.admin_url, "edit.php?post_type=page")
        else:
            list_url = urljoin(self.admin_url, "edit.php")
        await page.goto(list_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(500)
        for link in await page.query_selector_all("a.row-title"):
            text = (await link.text_content() or "").strip()
            if slug_matches_admin_title(page_slug, text):
                return link
            if page_slug.lower() in text.lower().replace(" ", "-"):
                return link
        return None

    async def _prepare_seo_post_editor(
        self,
        page: Page,
        page_url: str,
        post_id: int | None,
        *,
        light_mode: bool,
        start_log_message: str,
        shot_prefix_pages_list: str,
        shot_prefix_not_found: str,
        shot_prefix_editor: str,
        editor_loaded_log: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Open editor with proper Yoast load detection."""
        nav_ms = 30000 if light_mode else 45000
        page.set_default_timeout(nav_ms)

        self.logger.add_log(start_log_message, "info", "")

        parsed = urlparse(page_url)
        page_path = parsed.path.strip("/")
        page_slug = page_path.split("/")[-1] if page_path else ""

        self.logger.add_log("🔍 Resolved", "info", f"slug={page_slug!r}")

        if post_id is not None:
            edit_url = urljoin(self.admin_url, f"post.php?post={int(post_id)}&action=edit")
            self.logger.add_log("🌐 Opening editor", "info", edit_url)
            await page.goto(edit_url, wait_until="domcontentloaded", timeout=nav_ms)
            await page.wait_for_timeout(500)
            try:
                await page.wait_for_selector(
                    "#wpseo_meta, .edit-post-layout__metaboxes, #yoast-google-preview-description-metabox",
                    state="attached",
                    timeout=15000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(500)
        else:
            if not page_slug:
                return ("", {"status": "failed", "error": "No post_id and no URL path segment"})

            page_link = await self._find_row_title_in_list(page, page_slug, post_type="page")
            if page_link is None:
                page_link = await self._find_row_title_in_list(page, page_slug, post_type="post")

            if page_link is None:
                return ("", {"status": "failed", "error": f"Content not found for slug '{page_slug}'"})

            self.logger.add_log("✅ Matched; opening editor", "success", page_slug)
            try:
                await self._safe_scroll_into_view(page, page_link, timeout_ms=2000)
            except Exception:
                pass
            await page_link.click(timeout=10000)
            await page.wait_for_timeout(800)
            try:
                await page.wait_for_selector(
                    "#wpseo_meta, .edit-post-layout__metaboxes, #yoast-google-preview-description-metabox",
                    state="attached",
                    timeout=15000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(500)

        self.logger.add_log(editor_loaded_log, "success", "")
        return (page_slug, None)

    async def update_meta_description(
        self,
        page_url: str,
        meta_description: str,
        page: Page,
        post_id: int | None = None,
        *,
        light_mode: bool = False,
    ) -> dict[str, Any]:
        """Update SEO meta description with light mode enabled for speed."""
        try:
            if self.pause_ctrl is not None:
                await self.pause_ctrl.wait_if_paused()
            
            page_slug, prep_err = await self._prepare_seo_post_editor(
                page,
                page_url,
                post_id,
                light_mode=light_mode,
                start_log_message="📝 Meta description",
                shot_prefix_pages_list="07_pages_list",
                shot_prefix_not_found="page_not_found",
                shot_prefix_editor="08_page_editor",
                editor_loaded_log="✅ Editor ready",
            )
            if prep_err:
                return prep_err

            # Pause checkpoint before filling field
            if self.pause_ctrl is not None:
                await self.pause_ctrl.wait_if_paused()

            filled = await self._fill_seo_meta_description(page, meta_description)
            if not filled:
                filled = await self._fill_seo_meta_description_fallback(page, meta_description)
            
            if not filled:
                return {"status": "failed", "error": "Meta description field not found"}

            # Pause checkpoint before saving
            if self.pause_ctrl is not None:
                await self.pause_ctrl.wait_if_paused()

            if not await self._click_save_post_editor(page):
                self.logger.add_log("⚠️ Could not click save", "warning", "")

            await page.wait_for_timeout(600)

            self.logger.add_log("✅ Meta description complete", "success", page_slug or str(post_id))

            return {
                "status": "updated",
                "page_url": page_url,
                "meta_description": meta_description,
                "post_id": post_id,
            }

        except Exception as e:
            self.logger.add_log("❌ Meta update failed", "error", str(e)[:60])
            logger.error(f"Failed to update meta description: {e}")
            return {"status": "failed", "error": str(e)}

    async def update_meta_title(
        self,
        page_url: str,
        meta_title: str,
        page: Page,
        post_id: int | None = None,
        *,
        light_mode: bool = False,
    ) -> dict[str, Any]:
        """Update SEO title with light mode enabled for speed."""
        try:
            if self.pause_ctrl is not None:
                await self.pause_ctrl.wait_if_paused()
            
            page_slug, prep_err = await self._prepare_seo_post_editor(
                page,
                page_url,
                post_id,
                light_mode=light_mode,
                start_log_message="📝 SEO title",
                shot_prefix_pages_list="07_pages_list_title",
                shot_prefix_not_found="page_not_found_title",
                shot_prefix_editor="08_page_editor_title",
                editor_loaded_log="✅ Editor ready",
            )
            if prep_err:
                return prep_err

            # Pause checkpoint before filling field
            if self.pause_ctrl is not None:
                await self.pause_ctrl.wait_if_paused()

            filled = await self._fill_seo_meta_title(page, meta_title)
            if not filled:
                filled = await self._fill_seo_meta_title_fallback(page, meta_title)
            if not filled:
                return {"status": "failed", "error": "SEO title field not found"}

            # Pause checkpoint before saving
            if self.pause_ctrl is not None:
                await self.pause_ctrl.wait_if_paused()

            if not await self._click_save_post_editor(page):
                self.logger.add_log("⚠️ Could not click save", "warning", "")

            await page.wait_for_timeout(600)

            self.logger.add_log("✅ SEO title complete", "success", page_slug or str(post_id))

            return {
                "status": "updated",
                "page_url": page_url,
                "meta_title": meta_title,
                "post_id": post_id,
            }

        except Exception as e:
            self.logger.add_log("❌ SEO title failed", "error", str(e)[:60])
            logger.error(f"Failed to update SEO title: {e}")
            return {"status": "failed", "error": str(e)}

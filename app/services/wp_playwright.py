"""
WordPress automation via Playwright for UI-based operations.
Handles 301 redirects and meta descriptions through WordPress admin panel.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urlparse, urljoin, urlunparse

from playwright.async_api import Playwright, async_playwright, Page

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = "/tmp/playwright_screenshots"
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
        
        if self.stream_logs:
            print(formatted, flush=True)

        try:
            from app.services.monitor_broadcast import MONITOR_DEFAULT_ID, emit_monitor_playwright

            emit_monitor_playwright(MONITOR_DEFAULT_ID, action, level, details)
        except Exception:
            pass

    def add_screenshot(self, screenshot_path: str):
        """Record a screenshot path."""
        self.screenshots.append(screenshot_path)
        if self.stream_logs:
            print(f"📸 Screenshot: {screenshot_path}", flush=True)
    
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
    """
    
    def __init__(self, admin_url: str, username: str, password: str):
        """
        Initialize WordPress Playwright automation.
        
        Args:
            admin_url: WordPress admin panel URL (e.g., http://example.com/wp-admin/)
            username: WordPress username
            password: WordPress password
        """
        self.admin_url = normalize_wp_admin_root(admin_url)
        self.username = username
        self.password = password
        self.logger = PlaywrightLogger()
        
        # Parse base URL from admin_url
        parsed = urlparse(self.admin_url)
        self.base_url = f"{parsed.scheme}://{parsed.netloc}"
    
    async def login(self, page: Page) -> bool:
        """
        Log into WordPress admin panel with comprehensive error handling and visual feedback.
        
        Returns:
            True if login successful, False otherwise
        """
        try:
            self.logger.add_log(
                "🔐 Initiating WordPress Login",
                "info",
                f"URL: {self.admin_url}"
            )
            
            await page.goto(self.admin_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(500)
            
            screenshot_path = f"{SCREENSHOTS_DIR}/01_login_page_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=screenshot_path)
            self.logger.add_screenshot(screenshot_path)
            self.logger.add_log(
                "📸 Login page screenshot captured",
                "info",
                screenshot_path
            )
            
            try:
                await page.wait_for_url("**/wp-admin/", timeout=2000)
                self.logger.add_log(
                    "✅ Already logged into WordPress",
                    "success"
                )
                return True
            except:
                pass
            
            self.logger.add_log(
                "📝 Filling login credentials",
                "info",
                "Username and password"
            )
            
            username_field = await page.query_selector('input[name="log"]')
            if not username_field:
                username_field = await page.query_selector('input[type="text"]')
            
            password_field = await page.query_selector('input[name="pwd"]')
            if not password_field:
                password_field = await page.query_selector('input[type="password"]')
            
            if not username_field or not password_field:
                self.logger.add_log(
                    "❌ Login form not found",
                    "error",
                    "Could not locate username/password fields"
                )
                screenshot_path = f"{SCREENSHOTS_DIR}/02_login_form_not_found_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=screenshot_path)
                self.logger.add_screenshot(screenshot_path)
                return False
            
            await username_field.click()
            await page.wait_for_timeout(100)
            await username_field.fill(self.username)
            await page.wait_for_timeout(100)
            
            await password_field.click()
            await page.wait_for_timeout(100)
            await password_field.fill(self.password)
            await page.wait_for_timeout(100)
            
            self.logger.add_log(
                "🖱️  Clicking login button",
                "info",
                "Credentials filled, submitting form"
            )
            
            login_button = await page.query_selector('button[type="submit"], input[type="submit"]')
            if login_button:
                await login_button.click()
            else:
                await page.press('input[type="password"]', 'Enter')
            
            await page.wait_for_url("**/wp-admin/", timeout=15000)
            await page.wait_for_timeout(500)
            
            screenshot_path = f"{SCREENSHOTS_DIR}/03_login_success_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            await page.screenshot(path=screenshot_path)
            self.logger.add_screenshot(screenshot_path)
            
            self.logger.add_log(
                "✅ Successfully logged into WordPress",
                "success",
                "Ready to perform tasks"
            )
            return True
            
        except Exception as e:
            self.logger.add_log(
                "❌ Login failed",
                "error",
                str(e)
            )
            try:
                screenshot_path = f"{SCREENSHOTS_DIR}/error_login_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=screenshot_path)
                self.logger.add_screenshot(screenshot_path)
            except:
                pass
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
            except:
                pass
            logger.error(f"Failed to create redirect: {e}")
            return {
                "status": "failed",
                "error": str(e),
                "logs": self.logger.get_logs(),
                "screenshots": self.logger.get_screenshots()
            }
    
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
                    await loc.scroll_into_view_if_needed()
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
        Fill meta description in Yoast SEO metabox.
        
        Targets: div#yoast-google-preview-description-metabox (contenteditable)
        """
        await page.wait_for_timeout(200)
        await self._scroll_to_block_editor_metaboxes(page)
        await page.wait_for_timeout(600)

        self.logger.add_log("📦 Locating Yoast meta field", "info", "")

        try:
            await page.locator("#yoast-google-preview-description-metabox").first.wait_for(
                state="attached", timeout=15000
            )
            self.logger.add_log("✓ Yoast meta field is present in DOM", "debug", "")
        except Exception as e:
            self.logger.add_log(
                f"Timeout waiting for Yoast field",
                "warning",
                ""
            )

        try:
            loc = page.locator("#yoast-google-preview-description-metabox").first
            
            if await loc.count() == 0:
                return False
            
            self.logger.add_log("🎯 Found meta description field", "info", "div#yoast-google-preview-description-metabox")
            
            # Scroll into view
            await loc.scroll_into_view_if_needed()
            await page.wait_for_timeout(250)
            
            # Click to focus
            try:
                await loc.click(timeout=5000)
            except Exception:
                await loc.click(force=True, timeout=2000)
            
            await page.wait_for_timeout(150)
            
            # For contenteditable DIV: Select all and delete
            await page.keyboard.press('Control+A')  # Ctrl+A to select all
            await page.wait_for_timeout(50)
            await page.keyboard.press('Delete')  # Delete selected content
            await page.wait_for_timeout(100)
            
            # Verify it's empty
            current = await loc.inner_text()
            if current.strip():
                # If still not empty, use JavaScript to clear completely
                await loc.evaluate('el => { el.textContent = ""; }')
                await page.wait_for_timeout(100)
            
            # Now type the new content - use page.keyboard.type() for better reliability
            await page.keyboard.type(meta_description, delay=1)
            await page.wait_for_timeout(300)
            
            # Verify the content
            final_text = await loc.inner_text()
            
            if meta_description.strip() == final_text.strip():
                self.logger.add_log(
                    "✅ Meta description filled",
                    "success",
                    f"{len(final_text)} chars",
                )
                return True
            elif meta_description in final_text:
                # Content is there, even if there's extra characters
                self.logger.add_log(
                    "✅ Meta description filled",
                    "success",
                    f"{len(final_text)} chars",
                )
                return True
            else:
                self.logger.add_log(
                    "⚠️ Content mismatch",
                    "warning",
                    f"Expected {len(meta_description)}, got {len(final_text)}",
                )
                return True
        
        except Exception as e:
            self.logger.add_log(
                f"Exception: {str(e)[:80]}",
                "error",
                ""
            )
            return False

    async def _fill_seo_meta_description_fallback(self, page: Page, meta_description: str) -> bool:
        """
        Fallback for older Yoast versions or different DOM structures.
        Tries textarea selectors.
        """
        selectors = [
            "#wpseo_meta textarea#yoast_wpseo_metadesc",
            "#wpseo_meta textarea[name='_yoast_wpseo_metadesc']",
            "#wpseo_meta textarea",
            "textarea#yoast_wpseo_metadesc",
            "textarea[name='_yoast_wpseo_metadesc']",
            "#rank_math_description",
            "textarea[name='rank_math_description']",
        ]

        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                
                await loc.scroll_into_view_if_needed()
                await page.wait_for_timeout(100)
                
                await loc.click(timeout=5000)
                await loc.fill(meta_description, timeout=15000, force=True)
                
                self.logger.add_log(
                    "✅ Meta description filled via textarea",
                    "success",
                    sel,
                )
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
                await loc.scroll_into_view_if_needed()
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
                await pub.scroll_into_view_if_needed()
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

    async def update_meta_description(
        self,
        page_url: str,
        meta_description: str,
        page: Page,
        post_id: int | None = None,
        *,
        light_mode: bool = False,
    ) -> dict[str, Any]:
        """
        Update SEO meta description in the post editor.

        When ``post_id`` is set (same ID as REST ``resolve_post_url`` / dry-run), opens
        ``post.php?post=ID&action=edit`` so **posts** (e.g. hello-world) and **pages**
        with marketing titles are handled without list-table slug guessing.

        ``light_mode``: fewer screenshots and shorter waits (used for batched meta runs).
        """
        try:
            nav_ms = 45000 if light_mode else 90000
            page.set_default_timeout(nav_ms)
            page.set_default_navigation_timeout(nav_ms)

            self.logger.add_log("📝 Updating meta description", "info", page_url)

            parsed = urlparse(page_url)
            page_path = parsed.path.strip("/")
            page_slug = page_path.split("/")[-1] if page_path else ""

            self.logger.add_log(
                "🔍 Resolved target",
                "info",
                f"slug={page_slug!r} post_id={post_id}",
            )

            page_link = None

            if post_id is not None:
                edit_url = urljoin(
                    self.admin_url, f"post.php?post={int(post_id)}&action=edit"
                )
                self.logger.add_log(
                    "🌐 Opening editor by post ID (REST-matched URL)",
                    "info",
                    edit_url,
                )
                await page.goto(edit_url, wait_until="domcontentloaded", timeout=nav_ms)
                post_nav_wait = 350 if light_mode else 900
                await page.wait_for_timeout(post_nav_wait)
                try:
                    await page.wait_for_selector(
                        "#wpseo_meta, .edit-post-layout__metaboxes",
                        state="attached",
                        timeout=15000 if light_mode else 20000,
                    )
                except Exception:
                    pass
            else:
                if not page_slug:
                    return {
                        "status": "failed",
                        "error": "No post_id and URL has no path segment to match.",
                        "logs": self.logger.get_logs(),
                        "screenshots": self.logger.get_screenshots(),
                    }

                screenshot_path = (
                    f"{SCREENSHOTS_DIR}/07_pages_list_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                )

                page_link = await self._find_row_title_in_list(
                    page, page_slug, post_type="page"
                )
                if page_link is None:
                    self.logger.add_log(
                        "📋 Not in pages list; trying posts",
                        "info",
                        page_slug,
                    )
                    page_link = await self._find_row_title_in_list(
                        page, page_slug, post_type="post"
                    )

                await page.screenshot(path=screenshot_path)
                self.logger.add_screenshot(screenshot_path)

                if page_link is None:
                    all_pages_text: list[str] = []
                    for link in await page.query_selector_all("a.row-title"):
                        text = (await link.text_content() or "").strip()
                        all_pages_text.append(text)
                    pages_summary = (
                        ", ".join(all_pages_text[:10]) if all_pages_text else "No items"
                    )
                    if len(all_pages_text) > 10:
                        pages_summary += f", and {len(all_pages_text) - 10} more..."
                    self.logger.add_log(
                        "❌ Content not found",
                        "error",
                        f"slug={page_slug!r} | {pages_summary}",
                    )
                    screenshot_path = (
                        f"{SCREENSHOTS_DIR}/page_not_found_"
                        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    )
                    await page.screenshot(path=screenshot_path)
                    self.logger.add_screenshot(screenshot_path)
                    return {
                        "status": "failed",
                        "error": (
                            f"Content for slug '{page_slug}' not found in WordPress "
                            f"pages or posts lists. Available (last list): {pages_summary}"
                        ),
                        "logs": self.logger.get_logs(),
                        "screenshots": self.logger.get_screenshots(),
                    }

                self.logger.add_log(
                    "✅ Matched list row; opening editor",
                    "success",
                    page_slug,
                )
                try:
                    await page_link.scroll_into_view_if_needed()
                    await page.wait_for_timeout(300)
                except Exception:
                    pass
                await page_link.click(timeout=15000)
                await page.wait_for_timeout(800 if light_mode else 1500)

            if not light_mode:
                screenshot_path = (
                    f"{SCREENSHOTS_DIR}/08_page_editor_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                )
                await page.screenshot(path=screenshot_path)
                self.logger.add_screenshot(screenshot_path)

            self.logger.add_log("✅ Editor loaded; filling SEO meta", "success", "")

            if not await self._fill_seo_meta_description(page, meta_description):
                screenshot_path = (
                    f"{SCREENSHOTS_DIR}/meta_field_not_found_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                )
                await page.screenshot(path=screenshot_path)
                self.logger.add_screenshot(screenshot_path)
                return {
                    "status": "failed",
                    "error": "Meta description field not found (Yoast/Rank Math).",
                    "logs": self.logger.get_logs(),
                    "screenshots": self.logger.get_screenshots(),
                }

            if not light_mode:
                screenshot_path = (
                    f"{SCREENSHOTS_DIR}/09_meta_filled_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                )
                await page.screenshot(path=screenshot_path)
                self.logger.add_screenshot(screenshot_path)

            if not await self._click_save_post_editor(page):
                self.logger.add_log(
                    "⚠️  Could not click save; meta may be unsaved",
                    "warning",
                    "",
                )

            await page.wait_for_timeout(900 if light_mode else 2500)

            if not light_mode:
                screenshot_path = (
                    f"{SCREENSHOTS_DIR}/10_meta_saved_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                )
                await page.screenshot(path=screenshot_path)
                self.logger.add_screenshot(screenshot_path)

            self.logger.add_log(
                "✅ Meta description flow complete",
                "success",
                page_slug or str(post_id),
            )

            return {
                "status": "updated",
                "page_url": page_url,
                "meta_description": meta_description,
                "post_id": post_id,
                "logs": self.logger.get_logs(),
                "screenshots": self.logger.get_screenshots(),
            }

        except Exception as e:
            self.logger.add_log(
                "❌ Meta update failed",
                "error",
                str(e)
            )
            try:
                screenshot_path = f"{SCREENSHOTS_DIR}/error_meta_update_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                await page.screenshot(path=screenshot_path)
                self.logger.add_screenshot(screenshot_path)
            except Exception:
                pass
            logger.error(f"Failed to update meta description: {e}")
            return {
                "status": "failed",
                "error": str(e),
                "logs": self.logger.get_logs(),
                "screenshots": self.logger.get_screenshots()
            }


async def run_playwright_automation(
    admin_url: str,
    username: str,
    password: str,
    tasks: list[dict[str, Any]],
    headless: bool | None = None
) -> dict[str, Any]:
    """
    Run Playwright automation with a list of tasks with comprehensive error handling.
    
    Intelligently selects headed or headless mode based on environment:
    - Windows/macOS: Headed mode (native GUI available)
    - Linux with X server: Headed mode (DISPLAY environment variable set)
    - Linux without X server: Headless mode (fallback for CI/Docker)
    
    Browser window displays all automation steps in real-time for user visibility.
    In headless mode, detailed logs are captured and screenshots are saved.
    
    Args:
        admin_url: WordPress admin URL
        username: WordPress username
        password: WordPress password
        tasks: List of task dicts with "type", "from_slug"/"page_url", "to_url"/"meta_description"
        headless: Run browser in headless mode (None=auto-detect based on environment)
    
    Returns:
        Results with logs and screenshots
    """
    if headless is None:
        headless = should_run_headless()
        logger.info(f"Auto-detected headless mode: {headless} (Display available: {is_display_available()})")
    
    async with async_playwright() as p:
        browser = await launch_chromium(p, headless=headless)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 1024},
            ignore_https_errors=True,
            extra_http_headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )
        page = await context.new_page()
        
        page.set_default_timeout(90000)
        page.set_default_navigation_timeout(90000)
        
        wp = WordPressPlaywright(admin_url, username, password)
        
        wp.logger.add_log(
            "━" * 80,
            "info",
            "Starting WordPress automation"
        )
        
        try:
            if not await wp.login(page):
                wp.logger.add_log(
                    "━" * 80,
                    "error",
                    "AUTOMATION FAILED: Login unsuccessful"
                )
                return {
                    "status": "failed",
                    "error": "Could not log in to WordPress",
                    "logs": wp.logger.get_logs(),
                    "screenshots": wp.logger.get_screenshots()
                }
            
            results = []
            
            for i, task in enumerate(tasks):
                task_type = task.get("type")
                
                wp.logger.add_log(
                    f"Task {i + 1}/{len(tasks)}",
                    "info",
                    f"Type: {task_type}"
                )
                
                if task_type == "redirect":
                    result = await wp.create_301_redirect(
                        task["from_slug"],
                        task["to_url"],
                        page
                    )
                elif task_type == "meta_description":
                    raw_pid = task.get("post_id")
                    post_id = int(raw_pid) if raw_pid is not None else None
                    result = await wp.update_meta_description(
                        task["page_url"],
                        task["meta_description"],
                        page,
                        post_id=post_id,
                    )
                else:
                    result = {"status": "failed", "error": f"Unknown task type: {task_type}"}
                
                results.append(result)
                
                await page.wait_for_timeout(500)
            
            successful = sum(1 for r in results if r.get("status") in ["created", "updated"])
            failed = sum(1 for r in results if r.get("status") == "failed")
            
            wp.logger.add_log(
                "━" * 80,
                "success" if failed == 0 else "warning",
                f"Automation complete: {successful} successful, {failed} failed"
            )
            
            return {
                "status": "completed",
                "total_tasks": len(tasks),
                "successful": successful,
                "failed": failed,
                "results": results,
                "logs": wp.logger.get_logs(),
                "screenshots": wp.logger.get_screenshots()
            }
        
        except Exception as e:
            wp.logger.add_log(
                "━" * 80,
                "error",
                f"EXCEPTION: {str(e)}"
            )
            logger.error(f"Playwright automation failed: {e}")
            return {
                "status": "failed",
                "error": str(e),
                "logs": wp.logger.get_logs(),
                "screenshots": wp.logger.get_screenshots()
            }
        
        finally:
            await context.close()
            await browser.close()

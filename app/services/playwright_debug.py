"""
Debugging utility for Playwright automation.
Captures detailed element information for troubleshooting.
"""

from __future__ import annotations

from playwright.async_api import Page
import logging

logger = logging.getLogger(__name__)


async def debug_pages_list(page: Page) -> dict:
    """
    Debug the WordPress pages list to see what pages actually exist.
    
    Args:
        page: Playwright page object
    
    Returns:
        Dict with detailed page information
    """
    try:
        all_links = await page.query_selector_all('a.row-title')
        
        pages_found = []
        for idx, link in enumerate(all_links):
            text = await link.text_content() or ""
            href = await link.get_attribute('href') or ""
            
            pages_found.append({
                "index": idx,
                "text": text.strip(),
                "href": href,
                "visible": await link.is_visible(),
            })
        
        logger.warning(f"DEBUG: Found {len(pages_found)} pages in list")
        for page_info in pages_found:
            logger.warning(f"  Page #{page_info['index']}: '{page_info['text']}' | Href: {page_info['href']}")
        
        return {
            "total_pages": len(pages_found),
            "pages": pages_found,
        }
    except Exception as e:
        logger.error(f"DEBUG: Error scanning pages list: {e}")
        return {"error": str(e)}


async def debug_redirect_form(page: Page) -> dict:
    """
    Debug the redirect form to see what fields are available.
    
    Args:
        page: Playwright page object
    
    Returns:
        Dict with detailed form information
    """
    try:
        form_info = {
            "text_inputs": [],
            "buttons": [],
            "all_inputs": [],
        }
        
        text_inputs = await page.query_selector_all('input[type="text"]')
        logger.warning(f"DEBUG: Found {len(text_inputs)} text inputs")
        for idx, inp in enumerate(text_inputs):
            name = await inp.get_attribute('name') or ""
            placeholder = await inp.get_attribute('placeholder') or ""
            value = await inp.get_attribute('value') or ""
            visible = await inp.is_visible()
            
            info = {
                "index": idx,
                "name": name,
                "placeholder": placeholder,
                "value": value,
                "visible": visible,
            }
            form_info["text_inputs"].append(info)
            logger.warning(f"  Input #{idx}: name='{name}' | placeholder='{placeholder}' | visible={visible}")
        
        buttons = await page.query_selector_all('button, input[type="submit"]')
        logger.warning(f"DEBUG: Found {len(buttons)} buttons/submit inputs")
        for idx, btn in enumerate(buttons):
            text = await btn.text_content() or ""
            btn_type = await btn.get_attribute('type') or "button"
            visible = await btn.is_visible()
            
            info = {
                "index": idx,
                "text": text.strip(),
                "type": btn_type,
                "visible": visible,
            }
            form_info["buttons"].append(info)
            logger.warning(f"  Button #{idx}: text='{text.strip()}' | type='{btn_type}' | visible={visible}")
        
        all_inputs = await page.query_selector_all('input')
        logger.warning(f"DEBUG: Found {len(all_inputs)} total input fields")
        
        return form_info
    except Exception as e:
        logger.error(f"DEBUG: Error scanning form: {e}")
        return {"error": str(e)}


async def debug_page_structure(page: Page) -> dict:
    """
    Debug the overall page structure.
    
    Args:
        page: Playwright page object
    
    Returns:
        Dict with page structure information
    """
    try:
        structure = {
            "url": page.url,
            "title": await page.title(),
            "forms": len(await page.query_selector_all('form')),
            "tables": len(await page.query_selector_all('table')),
            "sections": len(await page.query_selector_all('section, [role="main"]')),
        }
        
        logger.warning(f"DEBUG PAGE STRUCTURE:")
        logger.warning(f"  URL: {structure['url']}")
        logger.warning(f"  Title: {structure['title']}")
        logger.warning(f"  Forms: {structure['forms']}")
        logger.warning(f"  Tables: {structure['tables']}")
        logger.warning(f"  Sections: {structure['sections']}")
        
        return structure
    except Exception as e:
        logger.error(f"DEBUG: Error scanning page structure: {e}")
        return {"error": str(e)}

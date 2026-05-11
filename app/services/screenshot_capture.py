"""
Screenshot capture utilities for live browser monitoring.
Converts screenshots to base64 for easy transmission over HTTP.
"""

import asyncio
import base64
import os
from pathlib import Path
from typing import Optional
from PIL import Image
from io import BytesIO


def compress_screenshot(screenshot_path: str, quality: int = 70, max_width: int = 800) -> Optional[str]:
    """
    Compress a screenshot to reduce bandwidth and convert to base64.
    
    Args:
        screenshot_path: Path to the screenshot file
        quality: JPEG quality (1-95)
        max_width: Maximum width to resize to
    
    Returns:
        Base64 encoded string with data URI prefix, or None if failed
    """
    try:
        if not os.path.exists(screenshot_path):
            return None

        # Open image
        img = Image.open(screenshot_path)

        # Resize if needed
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

        # Convert to JPEG if PNG (smaller file size)
        if img.format == "PNG":
            img = img.convert("RGB")

        # Compress to bytes
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        img_bytes = buffer.getvalue()

        # Encode to base64
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        return f"data:image/jpeg;base64,{b64}"

    except Exception as e:
        return None


async def capture_viewport(page, execution_logger=None, compress: bool = True) -> Optional[str]:
    """
    Capture the current viewport and optionally send to execution logger.
    
    Args:
        page: Playwright Page object
        execution_logger: ExecutionLogger instance to broadcast screenshot
        compress: Whether to compress the screenshot
    
    Returns:
        Base64 encoded screenshot or file path
    """
    try:
        screenshot_path = f"/tmp/playwright_screenshots/viewport_{id(page)}.png"
        os.makedirs("/tmp/playwright_screenshots", exist_ok=True)

        await page.screenshot(path=screenshot_path, full_page=False)

        if compress:
            b64_data = compress_screenshot(screenshot_path)
            if b64_data and execution_logger:
                await execution_logger.log(
                    action="screenshot_captured",
                    status="success",
                    browser_screenshot=b64_data,
                )
            return b64_data

        return screenshot_path

    except Exception as e:
        return None


async def get_element_info(page, selector: str) -> Optional[dict]:
    """
    Get information about an element's visibility and position.
    
    Args:
        page: Playwright Page object
        selector: CSS selector or other locator
    
    Returns:
        Dictionary with element info or None if not found
    """
    try:
        element = page.locator(selector)
        
        # Check if element exists
        if await element.count() == 0:
            return None

        # Get bounding box
        box = await element.bounding_box()
        if not box:
            return None

        # Get visibility info
        is_visible = await element.is_visible()
        is_enabled = await element.is_enabled()

        # Get viewport size
        viewport = page.viewportSize
        in_viewport = (
            box["x"] >= 0
            and box["y"] >= 0
            and box["x"] + box["width"] <= (viewport["width"] if viewport else 0)
            and box["y"] + box["height"] <= (viewport["height"] if viewport else 0)
        )

        return {
            "selector": selector,
            "visible": is_visible,
            "enabled": is_enabled,
            "in_viewport": in_viewport,
            "x": int(box["x"]),
            "y": int(box["y"]),
            "width": int(box["width"]),
            "height": int(box["height"]),
        }

    except Exception as e:
        return None

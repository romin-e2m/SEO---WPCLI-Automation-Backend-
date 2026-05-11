#!/usr/bin/env python3
"""
Test script to verify Playwright system is working correctly.
Runs Playwright automation with visible browser to test all critical paths.
"""

import asyncio
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.services.wp_playwright import (
    run_playwright_automation,
    extract_slug_with_trailing_slash,
)


async def test_slug_extraction():
    """Test URL slug extraction."""
    print("\n=== Testing URL Slug Extraction ===")
    test_cases = [
        ("http://seo-automation.local/elementor-7/", "elementor-7/"),
        ("http://seo-automation.local/sample-page/", "sample-page/"),
        ("http://seo-automation.local/elementor-7", "elementor-7/"),
        ("http://example.com/deep/nested/page/", "page/"),
    ]
    
    for url, expected in test_cases:
        result = extract_slug_with_trailing_slash(url)
        status = "✓" if result == expected else "✗"
        print(f"{status} {url}")
        print(f"  Expected: {expected}")
        print(f"  Got: {result}")
        if result != expected:
            return False
    
    return True


async def test_playwright_setup():
    """Test that Playwright can launch and connect to browser."""
    print("\n=== Testing Playwright Browser Launch ===")
    
    from playwright.async_api import async_playwright
    from app.services.wp_playwright import launch_chromium
    
    try:
        async with async_playwright() as p:
            # Test headless=False (shows browser window)
            print("Launching browser in HEADED mode (visible window)...")
            browser = await launch_chromium(p, headless=False)
            
            context = await browser.new_context(viewport={"width": 1280, "height": 1024})
            page = await context.new_page()
            
            print("✓ Browser launched successfully")
            print(f"✓ Browser type: {browser.browser_type.name}")
            print(f"✓ Page viewport: 1280x1024")
            
            # Navigate to a test page
            print("Testing page navigation...")
            await page.goto("https://example.com", timeout=10000)
            title = await page.title()
            print(f"✓ Page title: {title}")
            
            await context.close()
            await browser.close()
            
            print("✓ Browser closed successfully")
            return True
            
    except Exception as e:
        print(f"✗ Error: {e}")
        if "Executable doesn't exist" in str(e):
            print("\n⚠ Playwright browsers not installed!")
            print("Run: python -m playwright install chromium")
        return False


async def test_wordpress_login(admin_url: str, username: str, password: str):
    """Test WordPress login flow."""
    print(f"\n=== Testing WordPress Login ===")
    print(f"URL: {admin_url}")
    print(f"Username: {username}")
    
    from playwright.async_api import async_playwright
    from app.services.wp_playwright import WordPressPlaywright, launch_chromium
    
    try:
        async with async_playwright() as p:
            print("Launching browser in HEADED mode...")
            browser = await launch_chromium(p, headless=False)
            context = await browser.new_context(viewport={"width": 1280, "height": 1024})
            page = await context.new_page()
            
            page.set_default_timeout(30000)
            
            wp = WordPressPlaywright(admin_url, username, password)
            
            print("Attempting login...")
            success = await wp.login(page)
            
            if success:
                print("✓ WordPress login successful")
                logs = wp.logger.get_logs()
                print(f"✓ Captured {len(logs)} action logs")
                for log in logs:
                    print(f"  - {log.get('action')}")
            else:
                print("✗ WordPress login failed")
                logs = wp.logger.get_logs()
                for log in logs:
                    print(f"  [{log.get('level')}] {log.get('action')}: {log.get('details')}")
            
            await context.close()
            await browser.close()
            
            return success
            
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all tests."""
    print("=" * 60)
    print("PLAYWRIGHT SYSTEM VERIFICATION")
    print("=" * 60)
    
    # Test 1: URL slug extraction
    if not await test_slug_extraction():
        print("\n✗ Slug extraction test failed")
        return 1
    
    # Test 2: Playwright setup
    if not await test_playwright_setup():
        print("\n✗ Playwright setup test failed")
        return 1
    
    # Test 3: WordPress login (optional, requires running WordPress)
    admin_url = os.getenv("WP_ADMIN_URL", "http://seo-automation.local/wp-admin/")
    username = os.getenv("WP_USERNAME", "romin")
    password = os.getenv("WP_PASSWORD", "Romin@1843")
    
    if admin_url and username and password:
        print(f"\nTesting with WordPress at {admin_url}")
        if not await test_wordpress_login(admin_url, username, password):
            print("\n⚠ WordPress login test failed (site may not be running)")
            return 0
    else:
        print("\n⊘ Skipping WordPress test (env vars not set)")
    
    print("\n" + "=" * 60)
    print("✓ ALL TESTS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

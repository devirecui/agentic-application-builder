import asyncio
import os
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout
from utils import detect_board, slugify


async def apply_to_job(url: str, profile: dict, resume_path: str, config: dict) -> dict:
    result = {"success": False, "status": "failed", "notes": ""}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.get("headless", False),
            slow_mo=config.get("slow_mo", 50)
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        
        try:
            await page.goto(url, timeout=config.get("timeout", 30000))
            await page.wait_for_load_state("networkidle", timeout=10000)
            
            if await _detect_captcha(page):
                if config.get("manual_fallback", True):
                    print("\n⚠️  CAPTCHA detected. Please complete it manually and press Enter...")
                    input()
                else:
                    result["status"] = "captcha_blocked"
                    result["notes"] = "CAPTCHA detected, manual_fallback disabled"
                    return result
            
            board = detect_board(url)
            handlers = {
                "linkedin": _fill_linkedin,
                "greenhouse": _fill_greenhouse,
                "lever": _fill_lever,
                "workday": _fill_workday,
                "microsoft": _fill_generic,
                "generic": _fill_generic,
            }
            
            handler = handlers.get(board, _fill_generic)
            success = await handler(page, profile, resume_path)
            
            if success:
                result["success"] = True
                result["status"] = "applied"
            else:
                result["status"] = "manual_required"
                result["notes"] = f"Auto-fill incomplete for {board} board"
                
        except PlaywrightTimeout:
            result["notes"] = "Page timeout"
            if config.get("screenshot_on_error", True):
                await _screenshot(page, url, config)
        except Exception as e:
            result["notes"] = str(e)
            if config.get("screenshot_on_error", True):
                await _screenshot(page, url, config)
        finally:
            await browser.close()
    
    return result


async def _detect_captcha(page: Page) -> bool:
    captcha_indicators = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        ".g-recaptcha",
        "#captcha",
        "[data-sitekey]"
    ]
    for selector in captcha_indicators:
        try:
            element = await page.query_selector(selector)
            if element:
                return True
        except:
            pass
    return False


async def _fill_generic(page: Page, profile: dict, resume_path: str) -> bool:
    filled = 0
    
    field_map = {
        'input[name*="first"][type="text"]': profile.get("name", "").split()[0],
        'input[name*="last"][type="text"]': profile.get("name", "").split()[-1],
        'input[name*="email"], input[type="email"]': profile.get("email", ""),
        'input[name*="phone"], input[type="tel"]': profile.get("phone", ""),
        'input[name*="linkedin"]': profile.get("linkedin", ""),
        'input[name*="location"], input[name*="city"]': profile.get("location", ""),
    }
    
    for selector, value in field_map.items():
        if not value:
            continue
        try:
            elements = await page.query_selector_all(selector)
            for el in elements[:1]:
                await el.click()
                await el.fill(value)
                filled += 1
        except:
            pass
    
    if resume_path and os.path.exists(resume_path):
        try:
            file_inputs = await page.query_selector_all('input[type="file"]')
            if file_inputs:
                await file_inputs[0].set_input_files(resume_path)
                filled += 1
        except:
            pass
    
    return filled > 2


async def _fill_linkedin(page: Page, profile: dict, resume_path: str) -> bool:
    try:
        easy_apply = await page.query_selector('button:has-text("Easy Apply")')
        if easy_apply:
            await easy_apply.click()
            await page.wait_for_timeout(2000)
        
        return await _fill_generic(page, profile, resume_path)
    except:
        return False


async def _fill_greenhouse(page: Page, profile: dict, resume_path: str) -> bool:
    return await _fill_generic(page, profile, resume_path)


async def _fill_lever(page: Page, profile: dict, resume_path: str) -> bool:
    return await _fill_generic(page, profile, resume_path)


async def _fill_workday(page: Page, profile: dict, resume_path: str) -> bool:
    print("⚠️  Workday detected - automation support is limited. Manual completion may be required.")
    return await _fill_generic(page, profile, resume_path)


async def _screenshot(page: Page, url: str, config: dict):
    try:
        log_dir = config.get("output_dir", "output/logs")
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        filename = f"error_{slugify(url[:30])}_{datetime.now().strftime('%H%M%S')}.png"
        await page.screenshot(path=os.path.join(log_dir, filename))
    except:
        pass

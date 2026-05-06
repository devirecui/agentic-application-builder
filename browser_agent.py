import asyncio
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout
from utils import detect_board, slugify


# Only Greenhouse and Lever get auto-fill attempts
AUTO_APPLY_BOARDS = {"greenhouse", "lever"}

# Intermediate redirect services we should follow to find the real ATS
_INTERMEDIATE_HOPS = ("click.appcast.io", "appcast.io", "jobsearch.appcast.io")

# ATS domain -> board name mapping
_ATS_PATTERNS = {
    "greenhouse":      ("greenhouse.io", "boards.greenhouse"),
    "lever":           ("lever.co",),
    "workday":         ("workday.com", "myworkdayjobs"),
    "linkedin":        ("linkedin.com",),
    "microsoft":       ("microsoft.com", "careers.microsoft"),
    "icims":           ("icims.com",),
    "taleo":           ("taleo.net",),
    "smartrecruiters": ("smartrecruiters.com",),
    "jobvite":         ("jobvite.com",),
    "amazon":          ("amazon.jobs",),
    "ziprecruiter":    ("ziprecruiter.com",),
    "dice":            ("dice.com",),
}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
    "Connection": "keep-alive",
}


def _detect_destination_board(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for board, patterns in _ATS_PATTERNS.items():
        if any(p in host for p in patterns):
            return board
    return "unknown"


async def _follow_appcast(appcast_url: str) -> tuple[str, str]:
    """
    Use httpx to follow an Appcast redirect URL to its final ATS destination.
    Returns (final_url, board_type).
    """
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=_BROWSER_HEADERS,
        timeout=15.0,
        max_redirects=15,
    ) as client:
        try:
            resp = await client.get(appcast_url)
            final = str(resp.url)
            return final, _detect_destination_board(final)
        except Exception as e:
            return appcast_url, "unknown"


async def _resolve_adzuna(page: Page, url: str, nav_timeout: int) -> tuple[str, str, list[str]]:
    """
    Full redirect chain resolver — all steps via Playwright (same browser session).

    Appcast tracking URLs use Cloudflare bot protection; httpx gets a JS-challenge
    page. Using Playwright's real browser session passes the check cleanly.

    Chain: adzuna -> [adzuna details] -> click.appcast.io -> final ATS
    Returns (final_url, board_type, chain_log).
    Logs: [resolve] adzuna.com -> click.appcast.io -> greenhouse.io/COMPANY [ok]
    """
    chain = [url]

    # ── Step 1: Navigate to Adzuna, wait for JS rendering ────────────────────
    await page.goto(url, timeout=nav_timeout)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeout:
        pass

    pw_final = page.url
    if pw_final != chain[-1]:
        chain.append(pw_final)

    if "adzuna.com" not in urlparse(pw_final).netloc:
        return pw_final, _detect_destination_board(pw_final), chain

    # ── Step 2: Scan rendered hrefs ──────────────────────────────────────────
    try:
        links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    except Exception:
        links = []

    for link in links:
        board = _detect_destination_board(link)
        if board != "unknown":
            chain.append(link)
            return link, board, chain

    appcast_link = next((l for l in links if any(d in l for d in _INTERMEDIATE_HOPS)), None)
    if not appcast_link:
        return pw_final, "unknown", chain

    chain.append(appcast_link)

    # ── Step 3: Navigate Playwright to Appcast — same session bypasses CF ────
    # Appcast uses a JS window.location redirect that causes ERR_ABORTED on the
    # original navigation. This is normal — catch it and read page.url afterward.
    try:
        await page.goto(appcast_link, timeout=nav_timeout)
    except Exception as e:
        if "ERR_ABORTED" not in str(e) and "net::ERR" not in str(e):
            return appcast_link, "unknown", chain + [f"[nav: {e}]"]
        # ERR_ABORTED is expected from Appcast JS redirect — fall through to read page.url

    try:
        await page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeout:
        pass

    final = page.url
    if final != chain[-1]:
        chain.append(final)
    board = _detect_destination_board(final)
    return final, board, chain


def _log_chain(chain: list[str], board: str) -> None:
    labels = []
    for url in chain:
        if url.startswith("["):
            labels.append(url)
            continue
        host = urlparse(url).netloc or url[:40]
        labels.append(host)
    icon = "[ok]" if board in AUTO_APPLY_BOARDS else "[x]"
    print(f"    [resolve] {' -> '.join(labels)} {icon} ({board})")


async def apply_to_job(url: str, profile: dict, resume_path: str, config: dict) -> dict:
    result = {
        "success": False,
        "status": "failed",
        "notes": "",
        "resolved_url": url,
        "board": "unknown",
    }

    nav_timeout = config.get("timeout", 30000)
    board = detect_board(url)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=config.get("headless", False),
            slow_mo=config.get("slow_mo", 50),
        )
        context = await browser.new_context(
            user_agent=_BROWSER_HEADERS["User-Agent"]
        )
        page = await context.new_page()

        try:
            if board == "adzuna":
                # ── Hybrid resolution: Playwright + httpx ─────────────────
                resolved_url, board, chain = await _resolve_adzuna(page, url, nav_timeout)
                result["resolved_url"] = resolved_url
                result["board"] = board
                _log_chain(chain, board)

                if board not in AUTO_APPLY_BOARDS:
                    result["status"] = "manual"
                    result["notes"] = f"destination board: {board}, manual apply required"
                    return result

                # Navigate Playwright to the resolved ATS page for form filling
                await page.goto(resolved_url, timeout=nav_timeout)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass

            else:
                result["board"] = board
                await page.goto(url, timeout=nav_timeout)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass

            # ── CAPTCHA check ──────────────────────────────────────────────
            if await _detect_captcha(page):
                if config.get("manual_fallback", True):
                    print("\n  CAPTCHA detected. Please complete it manually and press Enter...")
                    input()
                else:
                    result["status"] = "captcha_blocked"
                    result["notes"] = "CAPTCHA detected, manual_fallback disabled"
                    await _screenshot(page, url, config, label="captcha")
                    return result

            # ── Board-specific form handler ────────────────────────────────
            handlers = {
                "linkedin":   _fill_linkedin,
                "greenhouse": _fill_greenhouse,
                "lever":      _fill_lever,
                "workday":    _fill_workday,
                "microsoft":  _fill_generic,
                "generic":    _fill_generic,
            }
            handler = handlers.get(board, _fill_generic)
            success = await handler(page, profile, resume_path)

            if success:
                result["success"] = True
                result["status"] = "applied"
                await _screenshot(page, url, config, label="success")
            else:
                result["status"] = "manual_required"
                result["notes"] = f"auto-fill incomplete for {board} board"
                await _screenshot(page, url, config, label="failed")

        except PlaywrightTimeout:
            result["notes"] = "page timeout"
            await _screenshot(page, url, config, label="timeout")
        except Exception as e:
            result["notes"] = str(e)
            await _screenshot(page, url, config, label="error")
        finally:
            await browser.close()

    return result


async def _detect_captcha(page: Page) -> bool:
    for selector in [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        ".g-recaptcha",
        "#captcha",
        "[data-sitekey]",
    ]:
        try:
            if await page.query_selector(selector):
                return True
        except Exception:
            pass
    return False


async def _fill_generic(page: Page, profile: dict, resume_path: str) -> bool:
    filled = 0
    field_map = {
        'input[name*="first"][type="text"]':            profile.get("name", "").split()[0],
        'input[name*="last"][type="text"]':             profile.get("name", "").split()[-1],
        'input[name*="email"], input[type="email"]':    profile.get("email", ""),
        'input[name*="phone"], input[type="tel"]':      profile.get("phone", ""),
        'input[name*="linkedin"]':                      profile.get("linkedin", ""),
        'input[name*="location"], input[name*="city"]': profile.get("location", ""),
    }
    for selector, value in field_map.items():
        if not value:
            continue
        try:
            for el in (await page.query_selector_all(selector))[:1]:
                await el.click()
                await el.fill(value)
                filled += 1
        except Exception:
            pass

    if resume_path and os.path.exists(resume_path):
        try:
            file_inputs = await page.query_selector_all('input[type="file"]')
            if file_inputs:
                await file_inputs[0].set_input_files(resume_path)
                filled += 1
        except Exception:
            pass

    return filled > 2


async def _fill_linkedin(page: Page, profile: dict, resume_path: str) -> bool:
    try:
        btn = await page.query_selector('button:has-text("Easy Apply")')
        if btn:
            await btn.click()
            await page.wait_for_timeout(2000)
        return await _fill_generic(page, profile, resume_path)
    except Exception:
        return False


async def _fill_greenhouse(page: Page, profile: dict, resume_path: str) -> bool:
    return await _fill_generic(page, profile, resume_path)


async def _fill_lever(page: Page, profile: dict, resume_path: str) -> bool:
    return await _fill_generic(page, profile, resume_path)


async def _fill_workday(page: Page, profile: dict, resume_path: str) -> bool:
    print("  Workday detected -- automation support is limited.")
    return await _fill_generic(page, profile, resume_path)


async def _screenshot(page: Page, url: str, config: dict, label: str = "screenshot"):
    try:
        log_dir = config.get("output_dir", "output/logs")
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        filename = f"{label}_{slugify(url[:40])}_{datetime.now().strftime('%H%M%S')}.png"
        await page.screenshot(path=os.path.join(log_dir, filename), full_page=False)
    except Exception:
        pass

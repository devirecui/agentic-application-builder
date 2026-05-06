import asyncio
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout
from utils import detect_board, slugify


# Module-level file logger — delay=True so the file is not created until first write
_log = logging.getLogger("browser_agent")
if not _log.handlers:
    _log.setLevel(logging.INFO)
    _h = logging.FileHandler("output/logs/browser_agent.log", encoding="utf-8", delay=True)
    _h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    _log.addHandler(_h)
    _log.propagate = False


# ATS boards that get auto-fill attempts
AUTO_APPLY_BOARDS = {"greenhouse", "lever", "smartrecruiters", "icims", "jobvite"}

# Intermediate redirect services we follow to find the real ATS
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
    "clearancejobs":   ("clearancejobs.com",),
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

_SUCCESS_PHRASES = [
    "thank you",
    "application submitted",
    "application received",
    "application complete",
    "successfully applied",
    "we've received",
    "you have applied",
    "your application has been",
    "we received your application",
]


def _detect_destination_board(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for board, patterns in _ATS_PATTERNS.items():
        if any(p in host for p in patterns):
            return board
    return "unknown"


async def _resolve_adzuna(page: Page, url: str, nav_timeout: int) -> tuple[str, str, list[str]]:
    """
    Full redirect chain resolver — all steps via Playwright (same browser session).

    Appcast tracking URLs use Cloudflare bot protection; httpx gets a JS-challenge
    page. Using Playwright's real browser session passes the check cleanly.

    Chain: adzuna -> [adzuna details] -> click.appcast.io -> final ATS
    Returns (final_url, board_type, chain_log).
    """
    chain = [url]

    # Step 1: Navigate to Adzuna, wait for JS rendering
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

    # Step 2: Scan rendered hrefs for direct ATS links
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

    # Step 3: Navigate Playwright to Appcast — same session bypasses Cloudflare.
    # Appcast uses a JS window.location redirect that causes ERR_ABORTED on the
    # original navigation. This is normal — catch it and read page.url afterward.
    try:
        await page.goto(appcast_link, timeout=nav_timeout)
    except Exception as e:
        if "ERR_ABORTED" not in str(e) and "net::ERR" not in str(e):
            return appcast_link, "unknown", chain + [f"[nav: {e}]"]

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
    msg = f"    [resolve] {' -> '.join(labels)} {icon} ({board})"
    print(msg)
    _log.info(msg.strip())


async def _check_success(page: Page) -> bool:
    """Return True if the current page indicates a successful application submission."""
    try:
        content = (await page.content()).lower()
        return any(p in content for p in _SUCCESS_PHRASES)
    except Exception:
        return False


async def _try_submit(page: Page, selectors: list[str]) -> bool:
    """Click the first matching submit button and wait for navigation. Returns True if clicked."""
    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await page.wait_for_timeout(800)
                await btn.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeout:
                    pass
                return True
        except Exception:
            continue
    return False


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

    # Ensure log directory exists and point file handler there
    log_dir = config.get("output_dir", "output/logs")
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    for handler in _log.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.baseFilename = os.path.abspath(os.path.join(log_dir, "browser_agent.log"))

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
                resolved_url, board, chain = await _resolve_adzuna(page, url, nav_timeout)
                result["resolved_url"] = resolved_url
                result["board"] = board
                _log_chain(chain, board)

                if board not in AUTO_APPLY_BOARDS:
                    result["status"] = "manual"
                    result["notes"] = f"destination board: {board}, manual apply required"
                    return result

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

            # CAPTCHA check
            if await _detect_captcha(page):
                if config.get("manual_fallback", True):
                    print("\n  CAPTCHA detected. Please complete it manually and press Enter...")
                    input()
                else:
                    result["status"] = "captcha_blocked"
                    result["notes"] = "CAPTCHA detected, manual_fallback disabled"
                    await _screenshot(page, url, config, label="captcha")
                    return result

            # Screenshot before any form interaction
            await _screenshot(page, url, config, label="before_submit")

            handlers = {
                "linkedin":        _fill_linkedin,
                "greenhouse":      _fill_greenhouse,
                "lever":           _fill_lever,
                "workday":         _fill_workday,
                "smartrecruiters": _fill_smartrecruiters,
                "icims":           _fill_icims,
                "jobvite":         _fill_jobvite,
                "microsoft":       _fill_generic,
                "generic":         _fill_generic,
            }
            handler = handlers.get(board, _fill_generic)
            success, notes = await handler(page, profile, resume_path)

            if success:
                result["success"] = True
                result["status"] = "applied"
                result["notes"] = notes
                _log.info(f"[applied] board={board} url={url}")
                await _screenshot(page, url, config, label="success")
            else:
                result["status"] = "manual_required"
                result["notes"] = notes or f"auto-fill incomplete for {board} board"
                _log.info(f"[manual_required] board={board} notes={result['notes']} url={url}")
                await _screenshot(page, url, config, label="failed")

        except PlaywrightTimeout:
            result["notes"] = "page timeout"
            _log.warning(f"[timeout] url={url}")
            await _screenshot(page, url, config, label="timeout")
        except Exception as e:
            result["notes"] = str(e)
            _log.error(f"[error] {e} url={url}")
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


async def _fill_generic(page: Page, profile: dict, resume_path: str) -> tuple[bool, str]:
    filled = 0
    field_map = {
        'input[name*="first"][type="text"]':            profile.get("name", "").split()[0] if profile.get("name") else "",
        'input[name*="last"][type="text"]':             profile.get("name", "").split()[-1] if profile.get("name") else "",
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

    return filled > 2, f"filled {filled} fields"


async def _fill_linkedin(page: Page, profile: dict, resume_path: str) -> tuple[bool, str]:
    try:
        btn = await page.query_selector('button:has-text("Easy Apply")')
        if btn:
            await btn.click()
            await page.wait_for_timeout(2000)
        return await _fill_generic(page, profile, resume_path)
    except Exception as e:
        return False, str(e)


async def _fill_greenhouse(page: Page, profile: dict, resume_path: str) -> tuple[bool, str]:
    return await _fill_generic(page, profile, resume_path)


async def _fill_lever(page: Page, profile: dict, resume_path: str) -> tuple[bool, str]:
    return await _fill_generic(page, profile, resume_path)


async def _fill_workday(page: Page, profile: dict, resume_path: str) -> tuple[bool, str]:
    print("  Workday detected -- automation support is limited.")
    return await _fill_generic(page, profile, resume_path)


async def _fill_smartrecruiters(page: Page, profile: dict, resume_path: str) -> tuple[bool, str]:
    """
    SmartRecruiters (jobs.smartrecruiters.com) — uses name/email/phoneNumber fields.
    Confirms success via page content after submit.
    """
    try:
        filled = 0
        name_parts = profile.get("name", "").split()
        first = name_parts[0] if name_parts else ""
        last = name_parts[-1] if len(name_parts) > 1 else ""

        sr_fields = {
            'input[name="firstName"]':   first,
            'input[name="lastName"]':    last,
            'input[name="email"]':       profile.get("email", ""),
            'input[name="phoneNumber"]': profile.get("phone", ""),
        }
        for selector, value in sr_fields.items():
            if not value:
                continue
            try:
                el = await page.query_selector(selector)
                if el:
                    await el.click()
                    await el.fill(value)
                    filled += 1
            except Exception:
                pass

        # Fallback to generic field names if SmartRecruiters-specific ones missed
        if filled < 2:
            ok, msg = await _fill_generic(page, profile, resume_path)
            if not ok:
                return False, f"SmartRecruiters: {msg}"
        else:
            # Resume upload
            if resume_path and os.path.exists(resume_path):
                try:
                    file_inputs = await page.query_selector_all('input[type="file"]')
                    if file_inputs:
                        await file_inputs[0].set_input_files(resume_path)
                        filled += 1
                except Exception:
                    pass

        if filled < 2:
            return False, f"SmartRecruiters: only filled {filled} fields"

        submitted = await _try_submit(page, [
            'button[type="submit"]',
            'button:has-text("Apply")',
            'button:has-text("Submit Application")',
            'button:has-text("Send Application")',
            'button:has-text("Submit")',
        ])

        if submitted and await _check_success(page):
            return True, "applied via SmartRecruiters"
        if submitted:
            return False, "SmartRecruiters: submitted but no confirmation text detected"
        return False, "SmartRecruiters: could not find submit button"

    except Exception as e:
        return False, f"SmartRecruiters error: {e}"


async def _fill_icims(page: Page, profile: dict, resume_path: str) -> tuple[bool, str]:
    """
    iCIMS — handles multi-step application flow (up to 3 steps).
    Tries guest-apply path if a login/register gate is present first.
    """
    try:
        # Some iCIMS instances show a login gate — look for guest/continue path
        for label in ["Continue as Guest", "Apply as Guest", "Continue Without Creating an Account"]:
            try:
                el = await page.query_selector(
                    f'a:has-text("{label}"), button:has-text("{label}")'
                )
                if el:
                    await el.click()
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeout:
                        pass
                    break
            except Exception:
                pass

        # Multi-step loop — iCIMS can have 2-4 pages
        for step in range(4):
            if await _check_success(page):
                return True, f"applied via iCIMS (completed at step {step})"

            filled = 0
            field_map = {
                'input[name*="first"][type="text"]':         profile.get("name", "").split()[0] if profile.get("name") else "",
                'input[name*="last"][type="text"]':          profile.get("name", "").split()[-1] if profile.get("name") else "",
                'input[name*="email"], input[type="email"]': profile.get("email", ""),
                'input[name*="phone"], input[type="tel"]':   profile.get("phone", ""),
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

            advanced = await _try_submit(page, [
                'input[type="submit"]',
                'button:has-text("Next")',
                'a:has-text("Next")',
                'button:has-text("Submit")',
                'button[type="submit"]',
                'button:has-text("Continue")',
            ])

            if not advanced:
                break

            if await _check_success(page):
                return True, f"applied via iCIMS (step {step + 1})"

        return False, "iCIMS: form incomplete or multi-step flow not fully handled"

    except Exception as e:
        return False, f"iCIMS error: {e}"


async def _fill_jobvite(page: Page, profile: dict, resume_path: str) -> tuple[bool, str]:
    """
    Jobvite (jobs.jobvite.com) — standard field names, single-step submit.
    """
    try:
        filled = 0
        field_map = {
            'input[name*="first"][type="text"]':         profile.get("name", "").split()[0] if profile.get("name") else "",
            'input[name*="last"][type="text"]':          profile.get("name", "").split()[-1] if profile.get("name") else "",
            'input[name*="email"], input[type="email"]': profile.get("email", ""),
            'input[name*="phone"], input[type="tel"]':   profile.get("phone", ""),
            'input[name*="linkedin"]':                   profile.get("linkedin", ""),
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

        if filled < 2:
            return False, f"Jobvite: only filled {filled} fields"

        submitted = await _try_submit(page, [
            'button:has-text("Apply")',
            'button:has-text("Submit")',
            'button[type="submit"]',
            'input[type="submit"]',
        ])

        if submitted and await _check_success(page):
            return True, "applied via Jobvite"
        if submitted:
            return False, "Jobvite: submitted but no confirmation text detected"
        return False, "Jobvite: could not find submit button"

    except Exception as e:
        return False, f"Jobvite error: {e}"


async def _screenshot(page: Page, url: str, config: dict, label: str = "screenshot"):
    try:
        log_dir = config.get("output_dir", "output/logs")
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        filename = f"{label}_{slugify(url[:40])}_{datetime.now().strftime('%H%M%S')}.png"
        await page.screenshot(path=os.path.join(log_dir, filename), full_page=False)
    except Exception:
        pass

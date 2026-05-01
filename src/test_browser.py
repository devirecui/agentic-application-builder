"""Browser automation dry-run.

Launches Playwright Chromium against https://httpbin.org/forms/post (a public
test form), fills fields with the profile data from config.yaml, screenshots
the filled form, and exits WITHOUT submitting.
"""
import asyncio
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from playwright.async_api import async_playwright


TARGET_URL = "https://httpbin.org/forms/post"
SCREENSHOT_PATH = ROOT / "output" / "logs" / "test_browser.png"


# httpbin's pizza-order form fields:
#   custname (text), custtel (tel), custemail (email), size (radio),
#   topping (checkbox), delivery (time), comments (textarea)
FIELD_PLAN = [
    ("input[name='custname']",  "name",  "Customer name"),
    ("input[name='custtel']",   "phone", "Phone"),
    ("input[name='custemail']", "email", "Email"),
]


async def run() -> int:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    profile = config.get("personal", {})
    SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)

    filled: list[tuple[str, str, str]] = []
    skipped: list[tuple[str, str, str]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=50)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(TARGET_URL, timeout=30000)
            await page.wait_for_load_state("domcontentloaded")

            for selector, profile_key, label in FIELD_PLAN:
                value = profile.get(profile_key, "")
                if not value:
                    skipped.append((selector, label, "no value in profile"))
                    continue
                el = await page.query_selector(selector)
                if not el:
                    skipped.append((selector, label, "selector not found"))
                    continue
                await el.fill(value)
                filled.append((selector, label, value))

            # Bonus: also exercise the comments textarea with the user's location
            comments = await page.query_selector("textarea[name='comments']")
            if comments and profile.get("location"):
                note = f"Test fill from job-apply-agent: {profile['location']}"
                await comments.fill(note)
                filled.append(("textarea[name='comments']", "Comments", note))

            # Pick a size radio so the screenshot shows the form fully filled.
            size_medium = await page.query_selector("input[name='size'][value='medium']")
            if size_medium:
                await size_medium.check()
                filled.append(("input[name='size'][value='medium']", "Size", "medium"))

            await page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
            print(f"\nScreenshot saved: {SCREENSHOT_PATH}")
        finally:
            await browser.close()

    print("\n=== Browser dry-run report ===")
    print(f"Target: {TARGET_URL}")
    print(f"Submitted: NO  (dry run only)\n")

    print(f"Filled ({len(filled)}):")
    for sel, label, val in filled:
        shown = val if len(val) <= 40 else val[:37] + "..."
        print(f"  ✓ {label:<14} [{sel}] = {shown}")

    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for sel, label, reason in skipped:
            print(f"  - {label:<14} [{sel}] — {reason}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

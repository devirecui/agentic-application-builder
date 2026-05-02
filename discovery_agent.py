"""
discovery_agent.py — Pull job listings from multiple free sources.

Sources (in order of preference):
  1. RemoteOK public JSON API
  2. Remotive public JSON API
  3. Indeed RSS via Playwright (fallback — slow, but works when others miss a query)
"""
import re
import sys
import time
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── RemoteOK ────────────────────────────────────────────────────────────────

def _remoteok_jobs(query: str) -> list[dict]:
    """
    RemoteOK API: https://remoteok.com/api
    Returns all remote jobs; we filter by keyword match in title/tags/description.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0, headers=headers) as client:
            resp = client.get("https://remoteok.com/api")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return []

    # First element is a legal notice dict
    jobs = [item for item in data if isinstance(item, dict) and item.get("position")]

    keywords = [w.lower() for w in re.split(r"[\s,]+", query) if len(w) > 2]
    results = []
    for job in jobs:
        haystack = " ".join([
            job.get("position", ""),
            job.get("company", ""),
            job.get("description", ""),
            " ".join(job.get("tags", [])),
        ]).lower()
        if sum(1 for kw in keywords if kw in haystack) >= max(1, len(keywords) // 2):
            results.append({
                "title": job.get("position", ""),
                "company": job.get("company", ""),
                "url": job.get("url") or f"https://remoteok.com/remote-jobs/{job.get('id', '')}",
                "snippet": re.sub(r"<[^>]+>", " ", job.get("description", ""))[:300],
            })
    return results


# ── Remotive ────────────────────────────────────────────────────────────────

def _remotive_jobs(query: str) -> list[dict]:
    """
    Remotive API: https://remotive.com/api/remote-jobs
    Supports ?search= parameter.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    # Use first 3 significant words for search
    words = [w for w in re.split(r"[\s,]+", query) if len(w) > 3][:3]
    search_term = " ".join(words)
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0, headers=headers) as client:
            resp = client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": search_term, "limit": 20},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return []

    results = []
    for job in data.get("jobs", []):
        results.append({
            "title": job.get("title", ""),
            "company": job.get("company_name", ""),
            "url": job.get("url", ""),
            "snippet": re.sub(r"<[^>]+>", " ", job.get("description", ""))[:300],
        })
    return results


# ── Indeed via Playwright (fallback) ────────────────────────────────────────

def _indeed_playwright_jobs(query: str, location: str = "remote") -> list[dict]:
    """
    Use Playwright to load Indeed search page and extract results.
    Slow but catches enterprise roles that API-only platforms miss.
    Returns [] silently if Indeed blocks or parsing fails.
    """
    from urllib.parse import urlencode
    from bs4 import BeautifulSoup
    params = {"q": query, "l": location, "sort": "date", "fromage": "7"}
    url = f"https://www.indeed.com/jobs?{urlencode(params)}"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
    except Exception:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Try multiple card selector patterns (Indeed changes layout frequently)
    cards = (
        soup.select("div.job_seen_beacon")
        or soup.select("div[data-jk]")
        or soup.select("li.css-5lfssm")
        or soup.select("td.resultContent")
    )

    for card in cards[:25]:
        # Title
        title_el = (
            card.select_one("h2.jobTitle span[title]")
            or card.select_one("h2.jobTitle a span")
            or card.select_one("h2 a span")
            or card.select_one("[data-testid='job-title']")
        )
        # Company
        company_el = (
            card.select_one("span[data-testid='company-name']")
            or card.select_one(".companyName")
            or card.select_one(".company")
        )
        # Link — prefer the job card anchor
        link_el = (
            card.select_one("h2.jobTitle a")
            or card.select_one("a[data-jk]")
            or card.select_one("a[href*='/rc/clk']")
            or card.select_one("a[href*='/pagead/']")
        )
        snippet_el = (
            card.select_one("div[data-testid='job-snippet']")
            or card.select_one(".job-snippet")
            or card.select_one(".summary")
        )

        title = title_el.get("title") or title_el.get_text(strip=True) if title_el else ""
        company = company_el.get_text(strip=True) if company_el else ""
        href = link_el.get("href", "") if link_el else ""
        if href.startswith("/"):
            href = "https://www.indeed.com" + href
        snippet = snippet_el.get_text(separator=" ", strip=True)[:300] if snippet_el else ""

        if title and href and "indeed.com" in href:
            results.append({"title": title, "company": company, "url": href, "snippet": snippet})

    return results


# ── Public entry point ───────────────────────────────────────────────────────

def discover_jobs(searches: list[dict], tracker: dict, use_indeed: bool = True) -> list[dict]:
    """
    Pull listings from RemoteOK + Remotive for each search config.
    Also runs Playwright-scraped Indeed when use_indeed=True (slower but catches
    enterprise roles that the API-only platforms miss).
    Deduplicates against tracker.
    Returns list of new candidate job dicts with min_match_score attached.
    """
    existing_urls: set[str] = {app.get("url") for app in tracker.get("applications", [])}
    seen_urls: set[str] = set()
    candidates: list[dict] = []

    for search in searches:
        query = search.get("query", "")
        location = search.get("location", "remote")
        min_score = search.get("min_match_score", 60)

        if not query:
            continue

        print(f"  Searching: '{query}'")

        batch: list[dict] = []
        batch.extend(_remoteok_jobs(query))
        time.sleep(0.5)
        batch.extend(_remotive_jobs(query))

        if use_indeed:
            print(f"    Also scraping Indeed (Playwright)...")
            batch.extend(_indeed_playwright_jobs(query, location))
        elif not batch:
            print(f"    API sources empty -- trying Playwright Indeed scrape...")
            batch.extend(_indeed_playwright_jobs(query, location))

        added = 0
        for job in batch:
            url = job.get("url", "")
            if not url or url in existing_urls or url in seen_urls:
                continue
            seen_urls.add(url)
            job["min_match_score"] = min_score
            job["search_query"] = query
            candidates.append(job)
            added += 1

        print(f"    -> {added} new candidates from '{query}'")
        time.sleep(0.5)

    return candidates

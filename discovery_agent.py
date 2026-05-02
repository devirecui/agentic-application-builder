"""
discovery_agent.py — Pull job listings from multiple sources.

Primary:   Indeed RSS  (https://www.indeed.com/rss?q=QUERY&l=LOC&sort=date)
Secondary: RemoteOK public JSON API  (fallback if Indeed 403s)
           Remotive public JSON API  (fallback if Indeed 403s)
"""
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlencode
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_SEVEN_DAYS_AGO = datetime.now(timezone.utc) - timedelta(days=7)

_RSS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ── Indeed RSS ───────────────────────────────────────────────────────────────

def _build_indeed_rss_url(query: str, location: str) -> str:
    params = urlencode({"q": query, "l": location, "sort": "date", "limit": "25"})
    return f"https://www.indeed.com/rss?{params}"


def _parse_pubdate(text: str) -> datetime | None:
    try:
        return parsedate_to_datetime(text.strip())
    except Exception:
        return None


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_indeed_rss(xml_text: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return jobs

    channel = root.find("channel")
    if channel is None:
        return jobs

    for item in channel.findall("item"):
        raw_title = (item.findtext("title") or "").strip()
        link      = (item.findtext("link") or "").strip()
        pub_raw   = (item.findtext("pubDate") or "").strip()
        desc      = _strip_html(item.findtext("description") or "")[:400]

        if not link or not raw_title:
            continue

        # Filter to last 7 days
        pub_dt = _parse_pubdate(pub_raw)
        if pub_dt and pub_dt < _SEVEN_DAYS_AGO:
            continue

        # Indeed title format: "Job Title - Company Name"
        if " - " in raw_title:
            parts = raw_title.rsplit(" - ", 1)
            title   = parts[0].strip()
            company = parts[1].strip()
        else:
            title   = raw_title
            company = ""

        jobs.append({
            "title":   title,
            "company": company,
            "url":     link,
            "snippet": desc,
        })

    return jobs


def _indeed_rss_jobs(query: str, location: str, retries: int = 3) -> list[dict]:
    url = _build_indeed_rss_url(query, location)
    last_err = None
    for i in range(retries):
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=30.0,
                headers=_RSS_HEADERS,
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return _parse_indeed_rss(resp.text)
        except httpx.HTTPStatusError as e:
            last_err = e.response.status_code
            if e.response.status_code == 403:
                break   # Won't recover with retries — fall through to secondaries
            time.sleep(2 ** i)
        except Exception as e:
            last_err = str(e)[:60]
            time.sleep(2 ** i)
    print(f"    [warn] Indeed RSS unavailable ({last_err}); using secondary sources", flush=True)
    return []


# ── RemoteOK (secondary) ─────────────────────────────────────────────────────

def _remoteok_jobs(query: str) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0, headers=headers) as client:
            resp = client.get("https://remoteok.com/api")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    jobs = [item for item in data if isinstance(item, dict) and item.get("position")]

    # Strip punctuation/quotes from query for keyword matching
    clean = re.sub(r'["\']', "", query)
    keywords = [w.lower() for w in re.split(r"[\s,]+", clean) if len(w) > 2]
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
                "title":   job.get("position", ""),
                "company": job.get("company", ""),
                "url":     job.get("url") or f"https://remoteok.com/remote-jobs/{job.get('id', '')}",
                "snippet": re.sub(r"<[^>]+>", " ", job.get("description", ""))[:300],
            })
    return results


# ── Remotive (secondary) ─────────────────────────────────────────────────────

def _remotive_jobs(query: str) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    clean = re.sub(r'["\']', "", query)
    words = [w for w in re.split(r"[\s,]+", clean) if len(w) > 3][:3]
    search_term = " ".join(words)
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0, headers=headers) as client:
            resp = client.get(
                "https://remotive.com/api/remote-jobs",
                params={"search": search_term, "limit": 20},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    return [
        {
            "title":   job.get("title", ""),
            "company": job.get("company_name", ""),
            "url":     job.get("url", ""),
            "snippet": re.sub(r"<[^>]+>", " ", job.get("description", ""))[:300],
        }
        for job in data.get("jobs", [])
    ]


# ── Public entry point ───────────────────────────────────────────────────────

def discover_jobs(searches: list[dict], tracker: dict) -> list[dict]:
    """
    For each search:
      1. Try Indeed RSS (primary — quoted phrases, date-filtered)
      2. Fall back to RemoteOK + Remotive if Indeed returns nothing

    Deduplicates against tracker.
    Returns new candidate job dicts with min_match_score attached.
    """
    existing_urls: set[str] = {app.get("url") for app in tracker.get("applications", [])}
    seen_urls:    set[str]  = set()
    candidates:   list[dict] = []

    for search in searches:
        query    = search.get("query", "")
        location = search.get("location", "remote")
        min_score = search.get("min_match_score", 60)

        if not query:
            continue

        print(f"  Searching: {query!r}", flush=True)

        # Primary: Indeed RSS
        batch = _indeed_rss_jobs(query, location)
        indeed_count = len(batch)

        # Secondary fallback
        if not batch:
            time.sleep(0.5)
            batch.extend(_remoteok_jobs(query))
            time.sleep(0.5)
            batch.extend(_remotive_jobs(query))

        raw_count = len(batch)

        added = 0
        for job in batch:
            url = job.get("url", "")
            if not url or url in existing_urls or url in seen_urls:
                continue
            seen_urls.add(url)
            job["min_match_score"] = min_score
            job["search_query"]    = query
            candidates.append(job)
            added += 1

        source = "Indeed RSS" if indeed_count else "RemoteOK/Remotive"
        print(f"    {raw_count} raw  |  {added} new after dedup  [{source}]", flush=True)
        time.sleep(0.5)

    return candidates

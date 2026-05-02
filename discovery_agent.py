"""
discovery_agent.py — Pull job listings from Adzuna (primary) and RemoteOK (fallback).

Primary:  Adzuna Jobs Search API  https://api.adzuna.com/v1/api/jobs/us/search/1
Fallback: RemoteOK public JSON API (no auth, startup-heavy but always available)

Adzuna credentials required in .env:
  ADZUNA_APP_ID=your_app_id
  ADZUNA_APP_KEY=your_app_key
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import httpx

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs/us/search/1"
_SEVEN_DAYS_AGO = datetime.now(timezone.utc) - timedelta(days=7)


# ── Adzuna (primary) ─────────────────────────────────────────────────────────

def _adzuna_creds() -> tuple[str, str]:
    app_id  = os.getenv("ADZUNA_APP_ID", "")
    app_key = os.getenv("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        raise EnvironmentError(
            "Adzuna credentials missing. Add to .env:\n"
            "  ADZUNA_APP_ID=your_app_id\n"
            "  ADZUNA_APP_KEY=your_app_key\n"
            "Sign up free at https://developer.adzuna.com/"
        )
    return app_id, app_key


def _adzuna_jobs(query: str, max_days_old: int = 30, retries: int = 3) -> list[dict]:
    try:
        app_id, app_key = _adzuna_creds()
    except EnvironmentError as e:
        print(f"    [error] {e}", flush=True)
        return []

    params = {
        "app_id":           app_id,
        "app_key":          app_key,
        "results_per_page": 50,
        "what":             query,
        "sort_by":          "date",
        "max_days_old":     max_days_old,
    }
    headers = {
        "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":      "application/json",
        "Content-Type": "application/json",
    }
    last_err = None
    data = None
    for i in range(retries):
        try:
            with httpx.Client(follow_redirects=True, timeout=30.0, headers=headers) as client:
                resp = client.get(_ADZUNA_BASE, params=params)
                resp.raise_for_status()
                data = resp.json()
            break
        except httpx.HTTPStatusError as e:
            last_err = f"HTTP {e.response.status_code}"
            if e.response.status_code in (401, 403):
                print(f"    [error] Adzuna auth failed ({last_err}) — check APP_ID/APP_KEY", flush=True)
                return []
            time.sleep(2 ** i)
        except Exception as e:
            last_err = str(e)[:80]
            time.sleep(2 ** i)
    else:
        print(f"    [warn] Adzuna fetch failed ({last_err})", flush=True)
        return []

    total = data.get("count", 0)
    print(f"    Adzuna: {total} total listings matched", flush=True)

    results = []
    for job in data.get("results", []):
        # Filter to last 7 days
        created_raw = job.get("created", "")
        if created_raw:
            try:
                created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                if created_dt < _SEVEN_DAYS_AGO:
                    continue
            except (ValueError, TypeError):
                pass

        url     = job.get("redirect_url", "")
        title   = job.get("title", "").strip()
        company = job.get("company", {}).get("display_name", "").strip()
        desc    = re.sub(r"\s+", " ", job.get("description", "")).strip()[:400]
        sal_min = job.get("salary_min")
        sal_max = job.get("salary_max")

        if not url or not title:
            continue

        results.append({
            "title":      title,
            "company":    company,
            "url":        url,
            "snippet":    desc,
            "salary_min": sal_min,
            "salary_max": sal_max,
            "created":    created_raw,
        })

    return results


# ── RemoteOK (fallback only) ─────────────────────────────────────────────────

def _remoteok_jobs(query: str) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":     "application/json",
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0, headers=headers) as client:
            resp = client.get("https://remoteok.com/api")
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    jobs = [item for item in data if isinstance(item, dict) and item.get("position")]
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
                "title":      job.get("position", ""),
                "company":    job.get("company", ""),
                "url":        job.get("url") or f"https://remoteok.com/remote-jobs/{job.get('id', '')}",
                "snippet":    re.sub(r"<[^>]+>", " ", job.get("description", ""))[:300],
                "salary_min": None,
                "salary_max": None,
                "created":    "",
            })
    return results


# ── Public entry point ───────────────────────────────────────────────────────

def discover_jobs(searches: list[dict], tracker: dict, max_days_old: int = 30) -> list[dict]:
    """
    For each search config, query Adzuna (primary). Falls back to RemoteOK if
    Adzuna returns nothing (no creds, rate limit, etc.).
    Deduplicates against tracker. Returns new candidates with min_match_score attached.
    """
    existing_urls: set[str] = {app.get("url") for app in tracker.get("applications", [])}
    seen_urls:     set[str] = set()
    # Title+company key dedup catches same role posted across multiple locations
    seen_title_company: set[str] = set()
    candidates:    list[dict] = []

    for search in searches:
        query     = search.get("query", "")
        min_score = search.get("min_match_score", 60)

        if not query:
            continue

        print(f"  Searching: {query!r}", flush=True)

        batch  = _adzuna_jobs(query, max_days_old=max_days_old)
        source = "Adzuna"

        if not batch:
            print(f"    Adzuna empty — trying RemoteOK fallback...", flush=True)
            batch  = _remoteok_jobs(query)
            source = "RemoteOK"

        raw_count = len(batch)
        added = 0
        for job in batch:
            url     = job.get("url", "")
            title   = job.get("title", "").lower().strip()
            company = job.get("company", "").lower().strip()
            tc_key  = f"{company}|{title}"
            if not url or url in existing_urls or url in seen_urls:
                continue
            if tc_key and tc_key in seen_title_company:
                continue
            seen_urls.add(url)
            if tc_key:
                seen_title_company.add(tc_key)
            job["min_match_score"] = min_score
            job["search_query"]    = query
            candidates.append(job)
            added += 1

        print(f"    {raw_count} raw  |  {added} new after dedup  [{source}]", flush=True)
        time.sleep(0.3)

    return candidates

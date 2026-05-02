import re
import sys
import time
import httpx
from anthropic import Anthropic

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from jd_analyzer import fetch_jd, analyze_jd
from tracker import add_application, is_duplicate


LOW_PAY_THRESHOLD = 150_000

FIT_PROMPT = """Given this JD analysis, write exactly ONE sentence (under 25 words) explaining
whether this role is a strong fit and why. Be specific — mention the most relevant strength
and biggest gap if applicable.

JD ANALYSIS:
{jd_analysis}"""


def _format_salary(salary_min, salary_max) -> str:
    if salary_min is None and salary_max is None:
        return ""
    if salary_min and salary_max:
        return f"${int(salary_min/1000)}k-${int(salary_max/1000)}k"
    if salary_min:
        return f"${int(salary_min/1000)}k+"
    return f"up to ${int(salary_max/1000)}k"


def _fit_summary(jd_analysis: dict, model: str) -> str:
    client = Anthropic()
    import json
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=80,
            messages=[{"role": "user", "content": FIT_PROMPT.format(
                jd_analysis=json.dumps(jd_analysis, indent=2)[:3000]
            )}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    except Exception:
        return f"Score {jd_analysis.get('match_score', 0)}: {', '.join(jd_analysis.get('gaps', [])[:2])}"


_company_signal_cache: dict[str, str] = {}


def _company_signal(company: str) -> str:
    if not company or company in ("Unknown", ""):
        return "no data"

    if company in _company_signal_cache:
        return _company_signal_cache[company]

    signals = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    # Glassdoor search
    for query in [f"{company} Glassdoor reviews", f"{company} Microsoft partner"]:
        search_url = f"https://www.google.com/search?q={_urlencode(query)}"
        try:
            with httpx.Client(follow_redirects=True, timeout=15.0, headers=headers) as client:
                resp = client.get(search_url)
            text = resp.text[:8000]

            if "glassdoor" in query.lower():
                rating = re.search(r"(\d\.\d)\s*(?:out of 5|/5|\s*stars?)", text, re.I)
                reviews = re.search(r"([\d,]+)\s*reviews?", text, re.I)
                if rating:
                    sig = f"Glassdoor {rating.group(1)}/5"
                    if reviews:
                        sig += f" ({reviews.group(1)} reviews)"
                    signals.append(sig)
            elif "microsoft partner" in query.lower():
                if re.search(r"gold partner|solutions partner|specialization", text, re.I):
                    tier = re.search(r"(gold partner|solutions partner|[A-Z][a-z]+ specialization)", text, re.I)
                    signals.append(f"MS Partner: {tier.group(1) if tier else 'yes'}")

        except Exception:
            pass
        time.sleep(0.5)

    result = "; ".join(signals) if signals else "no data"
    _company_signal_cache[company] = result
    return result


def _urlencode(s: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(s)


def rank_jobs(candidates: list[dict], resume_data: dict, model: str, tracker: dict,
              tracker_path: str) -> list[dict]:
    """
    For each candidate: fetch JD, analyze, filter by min_score, enrich, write to tracker.
    Returns ranked list sorted by match_score desc.
    """
    from tracker import save_tracker

    results = []

    for i, job in enumerate(candidates):
        url = job["url"]
        min_score = job.get("min_match_score", 60)
        print(f"  [{i+1}/{len(candidates)}] Analyzing: {job.get('title', url)[:60]}", flush=True)

        # Fetch full JD; fall back to Adzuna snippet when redirect is bot-blocked
        try:
            jd_text = fetch_jd(url)
        except Exception as fetch_err:
            snippet = job.get("snippet", "")
            if snippet:
                title   = job.get("title", "")
                company = job.get("company", "")
                jd_text = f"Job Title: {title}\nCompany: {company}\n\n{snippet}"
                print(f"    [fallback] Using Adzuna description ({len(snippet)} chars)", flush=True)
            else:
                print(f"    [skip] JD fetch failed, no description available", flush=True)
                continue

        try:
            jd_analysis = analyze_jd(jd_text, resume_data, model)
        except Exception as e:
            print(f"    [skip] JD analysis failed: {e}", flush=True)
            continue

        score = jd_analysis.get("match_score", 0)
        if score < min_score:
            print(f"    [skip] Score {score} < threshold {min_score}", flush=True)
            continue

        # Use company from JD analysis if richer than source
        company = jd_analysis.get("company") or job.get("company", "Unknown")
        role    = jd_analysis.get("role") or job.get("title", "Unknown")
        gaps    = jd_analysis.get("gaps", [])[:3]

        # Salary signal from Adzuna
        sal_min  = job.get("salary_min")
        sal_max  = job.get("salary_max")
        sal_str  = _format_salary(sal_min, sal_max)

        # Generate fit summary; append low-pay flag when below threshold
        fit = _fit_summary(jd_analysis, model)
        if sal_min is not None and sal_min < LOW_PAY_THRESHOLD:
            fit = f"[LOW PAY: {sal_str}] {fit}"

        # Company signal enrichment
        print(f"    Fetching company signal for: {company}", flush=True)
        signal = _company_signal(company)

        entry = {
            "title":         role,
            "company":       company,
            "url":           url,
            "match_score":   score,
            "top_gaps":      gaps,
            "fit_summary":   fit,
            "company_signal": signal,
            "salary_signal": sal_str,
            "jd_analysis":   jd_analysis,
        }
        results.append(entry)

        # Write to tracker with status: discovered
        if not is_duplicate(url, tracker):
            tracker = add_application(
                tracker,
                company=company,
                role=role,
                url=url,
                match_score=score,
                keywords_added=jd_analysis.get("keywords", []),
                tailored_resume="",
                status="discovered",
                notes=fit,
            )
            # Patch in extra fields
            for app in tracker["applications"]:
                if app["url"] == url:
                    app["top_gaps"]      = gaps
                    app["fit_summary"]   = fit
                    app["company_signal"] = signal
                    app["salary_signal"] = sal_str
                    break
            save_tracker(tracker, tracker_path)

    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results

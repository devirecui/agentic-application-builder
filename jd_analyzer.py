import json
import os
import re
import time
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


JD_PROMPT = """You are analyzing a job description to help tailor a resume.

RESUME SUMMARY:
{resume_summary}

JOB DESCRIPTION:
{jd_text}

Return JSON only with these fields:
- company: string
- role: string
- required_skills: list of strings
- preferred_skills: list of strings
- keywords: list of most important terms to include
- match_score: integer 0-100
- gaps: list of skills in JD not in resume
- summary: 2 sentence description of the role

Respond with raw JSON only, no markdown fences."""


def fetch_jd(url: str, retries: int = 3) -> str:
    last_err = None
    for i in range(retries):
        try:
            with httpx.Client(follow_redirects=True, timeout=30.0,
                              headers={"User-Agent": "Mozilla/5.0"}) as client:
                resp = client.get(url)
                resp.raise_for_status()
            return _extract_text(resp.text)
        except Exception as e:
            last_err = e
            time.sleep(2 ** i)
    raise RuntimeError(f"Failed to fetch JD after {retries} attempts: {last_err}")


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


def _resume_summary(resume_data: dict) -> str:
    parts = []
    if resume_data.get("name"):
        parts.append(f"Name: {resume_data['name']}")
    if resume_data.get("summary"):
        parts.append(f"Summary: {resume_data['summary']}")
    skills = resume_data.get("skills") or []
    if skills:
        parts.append("Skills: " + ", ".join(skills[:50]))
    exp = resume_data.get("experience") or []
    if exp:
        parts.append("Experience:\n" + "\n".join(exp[:40]))
    edu = resume_data.get("education") or []
    if edu:
        parts.append("Education:\n" + "\n".join(edu[:10]))
    return "\n\n".join(parts)


def analyze_jd(jd_text: str, resume_data: dict, model: str = "claude-sonnet-4-20250514") -> dict:
    client = Anthropic()
    prompt = JD_PROMPT.format(
        resume_summary=_resume_summary(resume_data),
        jd_text=jd_text[:12000],
    )

    last_err = None
    for attempt in range(2):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return _parse_json(text)
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise RuntimeError(f"Anthropic API failed: {last_err}")


def _parse_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    return json.loads(text)

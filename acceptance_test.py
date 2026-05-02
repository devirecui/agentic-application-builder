"""
Full acceptance test: parse -> analyze JD -> tailor -> validate -> track
Run from project root: python acceptance_test.py
"""
import io
import json
import os
import sys
import pprint

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(__file__))

from resume_parser import parse_resume
from jd_analyzer import fetch_jd, analyze_jd
from resume_tailor import tailor_resume, save_tailored_resume
from tracker import load_tracker, save_tracker, add_application

JD_URL = "https://jobs.careers.microsoft.com/global/en/job/1822002"
RESUME_PATH = "data/resume_base.docx"
TRACKER_PATH = "data/applications.json"
TAILORED_DIR = "data/tailored"
MODEL = "claude-sonnet-4-6"


def banner(title: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


# ── Step 1: Parse resume ──────────────────────────────────────────────────────
banner("STEP 1: Parse data/resume_base.docx")
resume_data = parse_resume(RESUME_PATH)
print("name:", resume_data["name"])
print("contact:", resume_data["contact"])
print("\nsummary:", resume_data["summary"])
print("\nskills:")
for s in resume_data["skills"]:
    print(" ", s)
print("\nexperience bullets:")
for b in resume_data["experience"]:
    print(" ", b)
print("\neducation:")
for e in resume_data["education"]:
    print(" ", e)
print("\ncertifications:")
for c in resume_data["certifications"]:
    print(" ", c)
print(f"\n[raw_text omitted — {len(resume_data['raw_text'])} chars]")


# ── Step 2: Fetch and analyze JD ─────────────────────────────────────────────
banner("STEP 2: Fetch and analyze JD")
print(f"URL: {JD_URL}")
jd_text = fetch_jd(JD_URL)
print(f"Fetched JD text ({len(jd_text)} chars). First 500 chars:")
print(jd_text[:500])
print("...\n")

jd_analysis = analyze_jd(jd_text, resume_data, model=MODEL)
print("Full JD analysis JSON:")
print(json.dumps(jd_analysis, indent=2))


# ── Step 3: Tailor resume ─────────────────────────────────────────────────────
banner("STEP 3: Tailor resume")
tailored_md = tailor_resume(resume_data, jd_analysis, model=MODEL)
tailored_path = save_tailored_resume(
    tailored_md,
    jd_analysis.get("company", "company"),
    jd_analysis.get("role", "role"),
    TAILORED_DIR,
)
print(f"Saved DOCX: {tailored_path}")
print(f"Saved MD:   {tailored_path.replace('.docx', '.md')}")
print("\n-- FULL TAILORED MARKDOWN --\n")
print(tailored_md)


# ── Step 4: Validate ──────────────────────────────────────────────────────────
banner("STEP 4: Validation")
errors = []

# 4a. Contact info preserved
orig_contact = resume_data["contact"]
for field, value in [
    ("email", orig_contact.get("email", "")),
    ("phone", orig_contact.get("phone", "")),
    ("name", resume_data["name"]),
]:
    if value and value not in tailored_md:
        errors.append(f"FAIL: {field} '{value}' missing from tailored resume")
    else:
        print(f"PASS: {field} present — {value!r}")

# 4b. No new certifications fabricated (certs from original must cover tailored)
import re
orig_certs_lower = {c.lower() for c in resume_data["certifications"]}
# Extract cert section from tailored
cert_section_match = re.search(
    r"## Certifications\n(.*?)(?=\n## |\Z)", tailored_md, re.DOTALL | re.IGNORECASE
)
def _norm(s: str) -> str:
    """Normalize punctuation/whitespace for loose cert comparison."""
    return re.sub(r"[–—‒\-\s]+", " ", s).lower().strip()

if cert_section_match:
    cert_lines = [
        l.strip("- *").strip()
        for l in cert_section_match.group(1).strip().splitlines()
        if l.strip() and not l.strip().startswith(">")
    ]
    orig_certs_norm = [_norm(c) for c in resume_data["certifications"]]
    print(f"\nCertifications in tailored ({len(cert_lines)}):")
    for c in cert_lines:
        print(f"  {c}")
    for c in cert_lines:
        c_norm = _norm(c)
        matched = any(
            (o in c_norm or c_norm in o or
             # strip trailing year for a base-name match
             o.rstrip("0123456789 ") in c_norm or c_norm.rstrip("0123456789 ") in o)
            for o in orig_certs_norm
        )
        if not matched and c_norm and orig_certs_norm:
            errors.append(f"POSSIBLE FABRICATION: '{c}' not found in original certs")
        else:
            print(f"  PASS cert: {c!r}")
else:
    if resume_data["certifications"]:
        errors.append("FAIL: Certifications section missing from tailored resume")

# 4c. JD gap keywords appear somewhere in tailored
gaps = jd_analysis.get("gaps", [])
gaps_found = []
gaps_missing = []
for kw in gaps:
    if kw.lower() in tailored_md.lower():
        gaps_found.append(kw)
    else:
        gaps_missing.append(kw)

print(f"\nGap keywords found in tailored ({len(gaps_found)}/{len(gaps)}): {gaps_found}")
if gaps_missing:
    print(f"Gap keywords NOT found ({len(gaps_missing)}): {gaps_missing}")
    # This is informational, not a hard failure — tailoring may handle some gaps implicitly

# 4d. Report
print("\n-- Validation Summary --")
if errors:
    for e in errors:
        print(f"  ERROR: {e}")
    sys.exit(1)
else:
    print("  All validation checks passed.")


# ── Step 5: Log to tracker ────────────────────────────────────────────────────
banner("STEP 5: Log to tracker (status=test_dry_run)")
tracker = load_tracker(TRACKER_PATH)
tracker = add_application(
    tracker,
    company=jd_analysis.get("company", "Unknown"),
    role=jd_analysis.get("role", "Unknown"),
    url=JD_URL,
    match_score=jd_analysis.get("match_score", 0),
    keywords_added=jd_analysis.get("keywords", []),
    tailored_resume=tailored_path,
    status="test_dry_run",
    notes="Acceptance test run",
)
save_tracker(tracker, TRACKER_PATH)

entry = tracker["applications"][-1]
print("Tracker entry:")
print(json.dumps(entry, indent=2))

print("\n\nACCEPTANCE TEST COMPLETE — no errors.")

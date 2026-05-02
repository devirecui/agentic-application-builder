"""
Two-job acceptance test pipeline.
Usage:  python run_two_jobs.py
"""
import io, json, os, re, sys, textwrap

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(__file__))

from resume_parser import parse_resume
from jd_analyzer   import fetch_jd, analyze_jd
from resume_tailor import tailor_resume, save_tailored_resume
from tracker       import load_tracker, save_tracker, add_application

URLS = [
    "https://jobs.careers.microsoft.com/global/en/job/1704030",
    "https://jobs.careers.microsoft.com/global/en/job/1634513",
]
RESUME_PATH  = "data/resume_base.docx"
TRACKER_PATH = "data/applications.json"
TAILORED_DIR = "data/tailored"
MODEL        = "claude-sonnet-4-6"


# ── helpers ───────────────────────────────────────────────────────────────────

def sep(char="=", width=72):
    print(char * width)

def banner(title: str):
    sep()
    print(f"  {title}")
    sep()
    print()

def _norm(s: str) -> str:
    return re.sub(r"[–—‒\-\s]+", " ", s).lower().strip()

def validate_tailored(tailored_md: str, resume_data: dict, jd_analysis: dict) -> list[str]:
    errors = []

    # contact
    for field, val in [
        ("email", resume_data["contact"].get("email", "")),
        ("phone", resume_data["contact"].get("phone", "")),
        ("name",  resume_data["name"]),
    ]:
        if val and val not in tailored_md:
            errors.append(f"CONTACT FAIL: {field} '{val}' missing")
        else:
            print(f"  PASS contact.{field}: {val!r}")

    # certs
    cert_match = re.search(
        r"## Certifications\n(.*?)(?=\n## |\Z)", tailored_md,
        re.DOTALL | re.IGNORECASE
    )
    if cert_match:
        cert_lines = [
            l.strip("- *").strip()
            for l in cert_match.group(1).strip().splitlines()
            if l.strip() and not l.strip().startswith(">")
        ]
        orig_norm = [_norm(c) for c in resume_data["certifications"]]
        for c in cert_lines:
            if not c:
                continue
            cn = _norm(c)
            matched = any(
                o in cn or cn in o
                or o.rstrip("0123456789 ") in cn
                or cn.rstrip("0123456789 ") in o
                for o in orig_norm
            )
            if not matched and orig_norm:
                errors.append(f"CERT FABRICATION: '{c}'")
            else:
                print(f"  PASS cert: {c!r}")
    elif resume_data["certifications"]:
        errors.append("FAIL: Certifications section missing")

    # gaps coverage (informational — not a hard failure)
    gaps = jd_analysis.get("gaps", [])
    found  = [g for g in gaps if g.lower() in tailored_md.lower()]
    absent = [g for g in gaps if g.lower() not in tailored_md.lower()]
    print(f"\n  Gap coverage: {len(found)}/{len(gaps)} gap keywords appear in rewrite")
    if absent:
        print(f"  Not found (expected for hard-skill gaps): {absent[:5]}")

    return errors


def extract_top_bullets(tailored_md: str, n: int = 3) -> list[str]:
    """Return the first n non-empty bullet lines from the Experience section."""
    exp_match = re.search(
        r"## Experience\n(.*?)(?=\n## |\Z)", tailored_md, re.DOTALL | re.IGNORECASE
    )
    if not exp_match:
        return []
    bullets = [
        re.sub(r"^[-*]\s*", "", l).strip()
        for l in exp_match.group(1).splitlines()
        if re.match(r"^\s*[-*]\s", l)
    ]
    return bullets[:n]


# ── Step 1: Parse resume once ─────────────────────────────────────────────────

banner("STEP 1 — Parse data/resume_base.docx")
resume_data = parse_resume(RESUME_PATH)
print(f"name    : {resume_data['name']}")
print(f"contact : {resume_data['contact']}")
print(f"summary : {resume_data['summary'][:120]}...")
print(f"skills  : {len(resume_data['skills'])} items")
print(f"exp     : {len(resume_data['experience'])} bullets")
print(f"certs   : {len(resume_data['certifications'])} items")
print()

# ── Per-job pipeline ──────────────────────────────────────────────────────────

results = {}   # url -> {jd_analysis, tailored_md, tailored_path, errors}
tracker = load_tracker(TRACKER_PATH)

for idx, url in enumerate(URLS, 1):
    job_label = f"JOB {idx}: {url}"
    banner(f"STEP 2 — Fetch & analyze JD  |  {job_label}")

    # -- fetch --
    print(f"Fetching via Playwright...")
    jd_text = fetch_jd(url)
    print(f"Fetched {len(jd_text):,} chars. First 300 chars of body:")
    print(jd_text[:300])
    print("...\n")

    # -- analyze --
    print("Analyzing with Claude...")
    jd_analysis = analyze_jd(jd_text, resume_data, model=MODEL)
    print("\nFull JD analysis:")
    print(json.dumps(jd_analysis, indent=2))

    # -- tailor --
    banner(f"STEP 3 — Tailor resume  |  {job_label}")
    tailored_md = tailor_resume(resume_data, jd_analysis, model=MODEL)
    tailored_path = save_tailored_resume(
        tailored_md,
        jd_analysis.get("company", "company"),
        jd_analysis.get("role", "role"),
        TAILORED_DIR,
    )
    print(f"Saved DOCX : {tailored_path}")
    print(f"Saved MD   : {tailored_path.replace('.docx', '.md')}")
    print("\n-- FULL TAILORED MARKDOWN --\n")
    print(tailored_md)

    # -- validate --
    banner(f"STEP 4 — Validate  |  {job_label}")
    errors = validate_tailored(tailored_md, resume_data, jd_analysis)
    print("\n-- Validation Summary --")
    if errors:
        for e in errors:
            print(f"  ERROR: {e}")
    else:
        print("  All checks passed.")

    # -- track --
    banner(f"STEP 5 — Log to tracker  |  {job_label}")
    tracker = add_application(
        tracker,
        company        = jd_analysis.get("company", "Unknown"),
        role           = jd_analysis.get("role",    "Unknown"),
        url            = url,
        match_score    = jd_analysis.get("match_score", 0),
        keywords_added = jd_analysis.get("keywords", []),
        tailored_resume= tailored_path,
        status         = "ready_to_apply",
        notes          = "",
    )
    save_tracker(tracker, TRACKER_PATH)
    entry = tracker["applications"][-1]
    print("Tracker entry:")
    print(json.dumps(entry, indent=2))

    results[url] = {
        "jd_analysis":   jd_analysis,
        "tailored_md":   tailored_md,
        "tailored_path": tailored_path,
        "errors":        errors,
    }

# ── Side-by-side comparison ───────────────────────────────────────────────────

banner("SIDE-BY-SIDE COMPARISON")

a = results[URLS[0]]
b = results[URLS[1]]

ja = a["jd_analysis"]
jb = b["jd_analysis"]

COL = 36

def row(label, va, vb):
    print(f"  {label:<20}  {str(va):<{COL}}  {str(vb)}")

row("Field", f"JOB 1 — {ja.get('role','?')[:30]}", f"JOB 2 — {jb.get('role','?')[:30]}")
sep("-")
row("Company",      ja.get("company","?"),      jb.get("company","?"))
row("Match score",  f"{ja.get('match_score',0)}/100", f"{jb.get('match_score',0)}/100")
row("Validation",
    "PASS" if not a["errors"] else f"FAIL ({len(a['errors'])})",
    "PASS" if not b["errors"] else f"FAIL ({len(b['errors'])})")

print()
print("  TOP 5 GAPS")
gaps_a = ja.get("gaps", [])[:5]
gaps_b = jb.get("gaps", [])[:5]
for i in range(max(len(gaps_a), len(gaps_b))):
    ga = textwrap.shorten(gaps_a[i] if i < len(gaps_a) else "", width=COL, placeholder="...")
    gb = textwrap.shorten(gaps_b[i] if i < len(gaps_b) else "", width=36, placeholder="...")
    print(f"  {i+1}.  {ga:<{COL}}  {gb}")

print()
print("  TOP 3 STRONGEST BULLETS SURFACED")
bullets_a = extract_top_bullets(a["tailored_md"], 3)
bullets_b = extract_top_bullets(b["tailored_md"], 3)
for i in range(max(len(bullets_a), len(bullets_b))):
    ba = textwrap.fill(bullets_a[i] if i < len(bullets_a) else "", width=COL)
    bb = textwrap.fill(bullets_b[i] if i < len(bullets_b) else "", width=COL)
    ba_lines = ba.splitlines() or [""]
    bb_lines = bb.splitlines() or [""]
    max_lines = max(len(ba_lines), len(bb_lines))
    for li in range(max_lines):
        prefix = f"  {i+1}.  " if li == 0 else "       "
        la = ba_lines[li] if li < len(ba_lines) else ""
        lb = bb_lines[li] if li < len(bb_lines) else ""
        print(f"{prefix}{la:<{COL}}  {lb}")
    print()

# ── Recommendation ────────────────────────────────────────────────────────────
sep()
print("  RECOMMENDATION")
sep()
score_a = ja.get("match_score", 0)
score_b = jb.get("match_score", 0)
role_a  = ja.get("role", "Job 1")
role_b  = jb.get("role", "Job 2")

if score_a >= score_b:
    primary, secondary = (role_a, score_a, URLS[0], ja), (role_b, score_b, URLS[1], jb)
else:
    primary, secondary = (role_b, score_b, URLS[1], jb), (role_a, score_a, URLS[0], ja)

p_role, p_score, p_url, p_jd = primary
s_role, s_score, s_url, s_jd = secondary

print(f"""
  Apply to "{p_role}" first (score {p_score}/100).

  Why:
  - Higher match score ({p_score} vs {s_score}) indicates stronger keyword
    alignment between your resume and the JD requirements.
  - Fewer gaps mean the tailored resume can make an honest, direct case
    without over-stretching.
  - Key strength areas surfaced: {", ".join(p_jd.get("keywords", [])[:4])}.

  Then apply to "{s_role}" (score {s_score}/100).
  Remaining gaps to address in cover letter:
    {chr(10).join("  - " + g for g in s_jd.get("gaps", [])[:3])}

  Both tailored resumes are saved to data/tailored/ and logged in the tracker
  with status ready_to_apply.
""")

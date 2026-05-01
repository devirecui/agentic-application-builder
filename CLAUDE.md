# Job Application Automation Agent

## Project Overview

An agentic job application system that tailors your resume to job descriptions, auto-fills applications via browser automation, and tracks everything in a local JSON database.

## Architecture

```
job-apply-agent/
├── CLAUDE.md                  # This file - project instructions
├── SKILL.md                   # Skill definition for Claude Code
├── requirements.txt           # Python dependencies
├── config.yaml                # User configuration
├── src/
│   ├── main.py                # Entry point - single and batch apply
│   ├── resume_parser.py       # Parse PDF/DOCX resume into structured data
│   ├── jd_analyzer.py         # Analyze JD, find gaps, score match
│   ├── resume_tailor.py       # Use Anthropic API to tailor resume per JD
│   ├── browser_agent.py       # Playwright browser automation for form filling
│   ├── tracker.py             # JSON-based application tracker
│   └── utils.py               # Shared utilities
├── data/
│   ├── resume_base.pdf        # User's base resume (place here)
│   ├── applications.json      # Application tracker database
│   └── tailored/              # Auto-generated tailored resumes per role
└── output/
    └── logs/                  # Application logs
```

## Setup Instructions

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure your profile

Edit `config.yaml` with your personal details for form auto-fill:

```yaml
personal:
  name: "Jarrett Driscoll"
  email: "jdriscollpro@gmail.com"
  phone: "412-616-2093"
  location: "North Huntingdon, PA"
  linkedin: "linkedin.com/in/jarrettdriscoll"
  github: ""
  website: ""

resume:
  base_path: "data/resume_base.pdf"
  tailored_output_dir: "data/tailored"

anthropic:
  model: "claude-sonnet-4-20250514"
  max_tokens: 4000

apply:
  headless: false          # Set true to run browser in background
  slow_mo: 50              # Milliseconds between actions (increase if site is slow)
  screenshot_on_error: true
  manual_fallback: true    # Pause for manual completion if CAPTCHA detected
```

### 3. Add your resume

Place your resume PDF at `data/resume_base.pdf`

### 4. Run

**Single application:**
```bash
python src/main.py apply --url "https://jobs.company.com/role/12345"
```

**Batch from file:**
```bash
python src/main.py batch --file jobs.txt
```

**Analyze JD only (no apply):**
```bash
python src/main.py analyze --url "https://jobs.company.com/role/12345"
```

**View tracker:**
```bash
python src/main.py status
```

## How It Works

### Step 1: Resume Parsing
`resume_parser.py` extracts structured data from your base resume including name, contact, experience, skills, education, and certifications. Supports PDF and DOCX.

### Step 2: JD Analysis
`jd_analyzer.py` fetches the job description from the URL, extracts key requirements, identifies keyword gaps between your resume and the JD, and produces a match score.

### Step 3: Resume Tailoring
`resume_tailor.py` calls the Anthropic API with your parsed resume and the JD analysis. It rewrites bullet points to emphasize relevant experience, adds missing keywords naturally, reorders skills by relevance, and generates a tailored DOCX and PDF version saved to `data/tailored/`.

### Step 4: Browser Automation
`browser_agent.py` uses Playwright to navigate to the application URL, detect form fields, fill them using your config and tailored resume data, attach the tailored resume file, and submit. Detects CAPTCHA and pauses for manual completion if needed.

### Step 5: Tracking
`tracker.py` logs every application to `data/applications.json` with company, role, URL, date, match score, tailored resume path, and status. Deduplicates to prevent double-applying.

## Extending for New Job Boards

Each job board has different form structures. Add a new handler in `browser_agent.py`:

```python
BOARD_HANDLERS = {
    "linkedin.com": fill_linkedin,
    "greenhouse.io": fill_greenhouse,
    "lever.co": fill_lever,
    "workday.com": fill_workday,
    "icims.com": fill_icims,
    "default": fill_generic
}
```

Each handler receives the page object and your profile data and returns True on success.

## Tracker Schema

```json
{
  "applications": [
    {
      "id": "uuid",
      "company": "Microsoft",
      "role": "Senior Cloud Solution Architect",
      "url": "https://...",
      "applied_at": "2026-05-01T09:00:00",
      "match_score": 87,
      "keywords_added": ["AgentCore", "co-sell", "specialization"],
      "tailored_resume": "data/tailored/microsoft_csa_20260501.pdf",
      "status": "applied",
      "notes": ""
    }
  ]
}
```

## Important Notes

- Set `headless: false` initially so you can monitor and intervene
- Some ATS systems detect automation -- slow_mo helps avoid detection
- Always review the tailored resume before submitting to a role you care about
- LinkedIn Easy Apply and Greenhouse have the best automation support
- Workday is notoriously difficult -- expect manual fallback frequently
- Never automate applications to roles you're not genuinely interested in

## Portfolio Note

This project demonstrates production-grade Python agentic architecture including browser automation, LLM-powered document tailoring, structured data extraction, and async workflow orchestration. Directly relevant to Azure AI Foundry and AWS AgentCore agentic system patterns.

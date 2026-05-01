---
name: job-apply-agent
description: Automates job applications end-to-end. Use whenever the user wants to apply to jobs, tailor their resume to a job description, analyze keyword gaps between their resume and a JD, batch apply to multiple roles, or track their job applications. Triggers on: "apply to this job", "tailor my resume", "analyze this JD", "batch apply", "job application tracker", "auto-fill application", "find keyword gaps in my resume". This skill handles the full pipeline from JD analysis through resume tailoring through browser form-filling through application tracking.
---

# Job Application Automation Agent

## What This Skill Does

End-to-end job application automation:
1. Fetches and analyzes job description from URL
2. Parses user's base resume (PDF or DOCX)
3. Identifies keyword gaps and match score
4. Tailors resume using Anthropic API
5. Auto-fills application form via Playwright
6. Tracks everything in local JSON database

## When Claude Code Runs This Skill

Read `CLAUDE.md` first for full architecture and setup instructions.

Then scaffold the project by running:

```bash
# Install dependencies
pip install -r requirements.txt --break-system-packages
playwright install chromium

# Verify structure
ls -la src/
```

## Core Files To Generate

When scaffolding the project, generate these files in order:

### 1. requirements.txt
```
anthropic>=0.25.0
playwright>=1.40.0
pdfplumber>=0.10.0
python-docx>=1.1.0
pyyaml>=6.0
httpx>=0.27.0
beautifulsoup4>=4.12.0
rich>=13.0.0
click>=8.1.0
uuid
```

### 2. src/utils.py
Shared utilities: logging setup, file helpers, URL validation, slug generation for filenames.

### 3. src/resume_parser.py
- `parse_resume(path: str) -> dict` -- handles PDF via pdfplumber, DOCX via python-docx
- Returns structured dict: `{name, contact, summary, experience, skills, education, certifications}`
- Strips formatting noise, preserves bullet structure

### 4. src/jd_analyzer.py
- `fetch_jd(url: str) -> str` -- httpx + BeautifulSoup to extract JD text
- `analyze_jd(jd_text: str, resume_data: dict) -> dict` -- Anthropic API call
- Returns: `{company, role, required_skills, preferred_skills, keywords, match_score, gaps, summary}`

### 5. src/resume_tailor.py
- `tailor_resume(resume_data: dict, jd_analysis: dict, config: dict) -> str` -- Anthropic API
- Rewrites experience bullets to emphasize relevant skills
- Adds missing keywords naturally without fabricating experience
- Returns tailored resume as markdown
- `save_tailored_resume(content: str, company: str, role: str, output_dir: str) -> str` -- saves DOCX

### 6. src/tracker.py
- `load_tracker(path: str) -> dict` -- loads applications.json
- `save_tracker(data: dict, path: str)` -- saves with pretty print
- `is_duplicate(url: str, tracker: dict) -> bool` -- dedup check
- `add_application(tracker: dict, application: dict) -> dict` -- adds entry
- `print_status(tracker: dict)` -- rich table display of all applications

### 7. src/browser_agent.py
- `apply_to_job(url: str, profile: dict, resume_path: str, config: dict) -> bool`
- Playwright async browser automation
- Board detection: linkedin.com, greenhouse.io, lever.co, workday, icims, default generic
- CAPTCHA detection with manual fallback pause
- Screenshot on error to output/logs/
- Returns True on success, False on failure

### 8. src/main.py
Click CLI with three commands:
- `apply` -- single URL application
- `batch` -- reads URLs from text file, applies sequentially
- `analyze` -- JD analysis only, no apply
- `status` -- prints tracker table

## Anthropic API Prompts

### JD Analysis Prompt
```
You are analyzing a job description to help tailor a resume.

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
```

### Resume Tailoring Prompt
```
You are tailoring a resume for a specific job application.

ORIGINAL RESUME:
{resume_json}

JD ANALYSIS:
{jd_analysis}

Rewrite the resume to:
1. Emphasize experience most relevant to this role
2. Naturally incorporate these keywords: {keywords}
3. Reorder skills to put most relevant first
4. Strengthen bullet points with the role's language
5. Never fabricate experience or credentials

Return the tailored resume as clean markdown.
Do not add skills or experience that don't exist in the original.
```

## Error Handling

- Network errors on JD fetch: retry 3x with backoff
- CAPTCHA detected: print warning, pause for manual completion, continue
- ATS blocks automation: screenshot, log as "manual_required", skip
- Duplicate application: skip with log message
- Anthropic API error: retry once, then fail gracefully

## Output

After each application print a rich summary:
```
✅ Applied: Microsoft - Senior Cloud Solution Architect
   Match Score: 87%
   Keywords Added: AgentCore, co-sell, specialization  
   Resume: data/tailored/microsoft_csa_20260501.pdf
   Tracker: data/applications.json updated
```

## Usage Examples

Single apply:
```bash
python src/main.py apply --url "https://careers.microsoft.com/job/200035047"
```

Batch from file (one URL per line):
```bash
python src/main.py batch --file my_jobs.txt
```

Analyze only:
```bash
python src/main.py analyze --url "https://careers.microsoft.com/job/200035047"
```

View status:
```bash
python src/main.py status
```

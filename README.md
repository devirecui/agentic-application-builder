# Job Scanner Agent

An agentic job research system that discovers, scores, and helps you prepare tailored applications.
Research-and-rank first — you decide when to apply.

## Daily Workflow

```bash
# 1. Discover and score new jobs from RSS feeds
python main.py discover

# 2. Review the ranked shortlist
python main.py report

# 3. Tailor your resume for a specific role you want to pursue
python main.py prepare --url "https://..."

# 4. Apply manually using the tailored DOCX from data/tailored/
```

## Architecture

```
Job Scanner Agent/
├── main.py                  # CLI entry point
├── config.yaml              # Configuration (searches, personal info, model)
├── discovery_agent.py       # Pull Indeed RSS feeds, deduplicate
├── ranker.py                # Fetch JDs, score with Claude, enrich company signals
├── weekly_report.py         # Render Rich table + save markdown report
├── prepare.py               # Tailor resume for a specific job (explicit only)
├── jd_analyzer.py           # JD fetch + Claude analysis
├── resume_tailor.py         # Claude-powered resume rewriting
├── resume_parser.py         # PDF/DOCX → structured data
├── browser_agent.py         # Playwright automation (manual use only)
├── tracker.py               # JSON application database
├── utils.py                 # Shared helpers
├── data/
│   ├── resume_base.docx     # Your base resume
│   ├── applications.json    # Tracker database
│   └── tailored/            # Auto-generated tailored resumes
└── output/
    ├── logs/
    └── reports/             # YYYYMMDD_weekly.md reports
```

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Copy your resume to `data/resume_base.docx`, then edit `config.yaml` with your personal details.

Set `ANTHROPIC_API_KEY` in your environment or a `.env` file.

## Commands

| Command | Description |
|---------|-------------|
| `python main.py discover` | Pull RSS feeds, score all new jobs, write to tracker |
| `python main.py report` | Show ranked table of this week's discovered jobs |
| `python main.py prepare --url URL` | Tailor resume for a specific discovered job |
| `python main.py status` | Show all tracked applications |
| `python main.py analyze --url URL` | Analyze a JD without tracking |
| `python main.py apply --url URL` | Manual browser-based application |

## Configuration

Edit `config.yaml` to customize discovery searches:

```yaml
discovery:
  run_every_hours: 24
  searches:
    - query: "Cloud Solution Architect Azure AI"
      location: "remote"
      min_match_score: 65
    - query: "AI Architect Azure OpenAI agentic"
      location: "remote"
      min_match_score: 65
```

## How It Works

1. **Discover** — Pulls Indeed RSS feeds for each configured search query. Deduplicates against tracker.
2. **Rank** — Fetches full JD for each candidate (Playwright for SPAs, httpx for static). Runs Claude analysis: match score, skill gaps, keywords. Filters below threshold. Fetches company signal (Glassdoor rating, MS partner tier).
3. **Report** — Renders a Rich terminal table sorted by score. Saves markdown to `output/reports/`. Shows top 5 recommendations.
4. **Prepare** — You explicitly choose a URL. Claude tailors your resume to that specific JD. Saves DOCX + MD to `data/tailored/`. Updates tracker to `ready`.
5. **Apply** — You do this manually with the tailored DOCX. The browser agent (`apply`) is available but never called automatically.

## Tracker Statuses

| Status | Meaning |
|--------|---------|
| `discovered` | Found via RSS, scored, waiting for your decision |
| `ready` | Resume tailored, ready to apply manually |
| `applied` | You've submitted the application |
| `analyze_only` | Analyzed but not tracked for apply |

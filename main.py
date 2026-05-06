import asyncio
import logging
import os
import sys
import time
import yaml
import click
from datetime import datetime

# Ensure UTF-8 output on Windows and unbuffered for live progress
import os
os.environ.setdefault("PYTHONUNBUFFERED", "1")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

sys.path.insert(0, os.path.dirname(__file__))

from utils import setup_logging, validate_url, ensure_dirs
from resume_parser import parse_resume
from jd_analyzer import fetch_jd, analyze_jd
from resume_tailor import tailor_resume, save_tailored_resume
from browser_agent import apply_to_job
from tracker import load_tracker, save_tracker, is_duplicate, add_application, print_status
from discovery_agent import discover_jobs
from ranker import rank_jobs
from weekly_report import generate_report
from prepare import prepare_job, prepare_batch

console = Console()


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


# ── Shared discover + rank logic ─────────────────────────────────────────────

def _discover_and_rank(config: dict) -> tuple[list[dict], dict | None, str]:
    """
    Pull feeds, deduplicate, rank. Returns (ranked, resume_data, tracker_path).
    resume_data is None when there are no candidates to score.
    """
    ensure_dirs("data/tailored", "output/logs", "output/reports")

    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    tracker      = load_tracker(tracker_path)
    searches     = config.get("discovery", {}).get("searches", [])

    if not searches:
        raise ValueError("No searches configured in config.yaml under discovery.searches")

    resume_path    = config.get("resume", {}).get("base_path", "data/resume_base.docx")
    analysis_model = config.get("anthropic", {}).get("analysis_model", "claude-haiku-4-5-20251001")
    max_days_old   = config.get("discovery", {}).get("max_days_old", 30)

    console.print(f"[cyan]Pulling job feeds for {len(searches)} searches (last {max_days_old} days)...[/cyan]")
    candidates = discover_jobs(searches, tracker, max_days_old=max_days_old)
    console.print(f"[green]Found {len(candidates)} new candidates after deduplication[/green]")

    if not candidates:
        return [], None, tracker_path

    console.print(f"[cyan]Parsing resume...[/cyan]")
    resume_data = parse_resume(resume_path)

    console.print(f"[cyan]Ranking {len(candidates)} candidates (fetching JDs + scoring)...[/cyan]")
    ranked = rank_jobs(candidates, resume_data, analysis_model, tracker, tracker_path)

    return ranked, resume_data, tracker_path


def _print_ranked_top10(ranked: list[dict]) -> None:
    console.print(f"\n[bold green]Discovery complete: {len(ranked)} jobs passed threshold[/bold green]")
    for i, job in enumerate(ranked[:10], 1):
        score = job["match_score"]
        color = "green" if score >= 80 else "yellow" if score >= 65 else "red"
        console.print(
            f"  {i:>2}. [{color}]{score:>3}%[/{color}]  "
            f"[bold]{job['title']}[/bold]  @ {job['company']}"
        )


def _print_prepare_summary(tailored: list[dict]) -> None:
    table = Table(title="Auto-Prepare Summary", show_lines=True, expand=True)
    table.add_column("#",       justify="right", style="bold", no_wrap=True, min_width=2)
    table.add_column("Role",    ratio=2)
    table.add_column("Company", ratio=1)
    table.add_column("Sc",      justify="right", no_wrap=True, min_width=3)
    table.add_column("Salary",  ratio=1, no_wrap=True)
    table.add_column("Tailored Resume", ratio=3)

    for i, t in enumerate(tailored, 1):
        score = t["score"]
        color = "green" if score >= 80 else "yellow" if score >= 65 else "red"
        path  = t["tailored_path"]
        path_color = "red" if path == "[FAILED]" else "default"
        table.add_row(
            str(i),
            t["title"],
            t["company"],
            Text(str(score), style=color),
            t.get("salary", "") or "-",
            Text(path, style=path_color),
        )
    console.print(table)


# ── Single-job pipeline ───────────────────────────────────────────────────────

def run_pipeline(url: str, config: dict, do_apply: bool = True) -> dict:
    logger = setup_logging(
        config.get("logging", {}).get("level", "INFO"),
        config.get("logging", {}).get("output_dir", "output/logs")
    )

    ensure_dirs("data/tailored", "output/logs")

    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    tracker = load_tracker(tracker_path)

    if is_duplicate(url, tracker):
        console.print(f"[yellow]Already applied to {url} -- skipping[/yellow]")
        return {"skipped": True}

    console.print(f"\n[cyan]Fetching job description...[/cyan]")
    jd_text = fetch_jd(url)

    resume_path = config.get("resume", {}).get("base_path", "data/resume_base.pdf")
    console.print(f"[cyan]Parsing resume...[/cyan]")
    resume_data = parse_resume(resume_path)

    analysis_model = config.get("anthropic", {}).get("analysis_model", "claude-haiku-4-5-20251001")
    tailor_model   = config.get("anthropic", {}).get("model", "claude-sonnet-4-6")

    console.print(f"[cyan]Analyzing job description...[/cyan]")
    jd_analysis = analyze_jd(jd_text, resume_data, analysis_model)

    console.print(Panel(
        f"[bold]{jd_analysis.get('company')} - {jd_analysis.get('role')}[/bold]\n"
        f"Match Score: [green]{jd_analysis.get('match_score')}%[/green]\n"
        f"Gaps: {', '.join(jd_analysis.get('gaps', [])[:5])}\n"
        f"Keywords: {', '.join(jd_analysis.get('keywords', [])[:8])}\n\n"
        f"{jd_analysis.get('summary', '')}",
        title="JD Analysis",
        border_style="cyan"
    ))

    console.print(f"[cyan]Tailoring resume...[/cyan]")
    tailored_content = tailor_resume(resume_data, jd_analysis, tailor_model)
    tailored_path = save_tailored_resume(
        tailored_content,
        jd_analysis.get("company", "company"),
        jd_analysis.get("role", "role"),
        config.get("resume", {}).get("tailored_output_dir", "data/tailored")
    )
    console.print(f"[green]Tailored resume saved: {tailored_path}[/green]")

    result = {"success": False, "status": "analyze_only"}

    if do_apply:
        console.print(f"[cyan]Launching browser automation...[/cyan]")
        apply_config = {
            **config.get("apply", {}),
            "output_dir": config.get("logging", {}).get("output_dir", "output/logs")
        }
        result = asyncio.run(apply_to_job(
            url,
            config.get("personal", {}),
            tailored_path,
            apply_config
        ))

        status_icon = "" if result["success"] else ""
        console.print(f"\n{status_icon} [bold]Application Result: {result['status']}[/bold]")
        if result.get("notes"):
            console.print(f"   Notes: {result['notes']}")

    tracker = add_application(
        tracker,
        company=jd_analysis.get("company", "Unknown"),
        role=jd_analysis.get("role", "Unknown"),
        url=url,
        match_score=jd_analysis.get("match_score", 0),
        keywords_added=jd_analysis.get("keywords", []),
        tailored_resume=tailored_path,
        status=result.get("status", "analyze_only"),
        notes=result.get("notes", "")
    )
    save_tracker(tracker, tracker_path)

    return {**result, "jd_analysis": jd_analysis, "tailored_resume": tailored_path}


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Job Application Automation Agent"""
    pass


@cli.command()
@click.option("--url", required=True, help="Job application URL")
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def apply(url, config_path):
    """Apply to a single job"""
    if not validate_url(url):
        console.print(f"[red]Invalid URL: {url}[/red]")
        return

    config = load_config(config_path)
    run_pipeline(url, config, do_apply=True)


@cli.command()
@click.option("--file", "jobs_file", required=True, help="Text file with one URL per line")
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def batch(jobs_file, config_path):
    """Apply to multiple jobs from a file"""
    config = load_config(config_path)

    with open(jobs_file, "r") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    console.print(f"[cyan]Found {len(urls)} jobs to process[/cyan]")

    for i, url in enumerate(urls, 1):
        console.print(f"\n[bold]--- Job {i}/{len(urls)} ---[/bold]")
        if not validate_url(url):
            console.print(f"[red]Skipping invalid URL: {url}[/red]")
            continue

        try:
            run_pipeline(url, config, do_apply=True)
        except Exception as e:
            console.print(f"[red]Error processing {url}: {e}[/red]")
            continue


@cli.command()
@click.option("--url", required=True, help="Job posting URL to analyze")
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def analyze(url, config_path):
    """Analyze a job description without applying"""
    if not validate_url(url):
        console.print(f"[red]Invalid URL: {url}[/red]")
        return

    config = load_config(config_path)
    run_pipeline(url, config, do_apply=False)


@cli.command()
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def status(config_path):
    """View all tracked applications"""
    config = load_config(config_path)
    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    tracker = load_tracker(tracker_path)
    print_status(tracker)


def _run_auto_apply(config: dict, tracker_path: str) -> None:
    """Run auto-apply on all eligible tracker entries. Used by discover --auto-apply."""
    from browser_agent import AUTO_APPLY_BOARDS

    blacklist    = [b.lower() for b in config.get("apply", {}).get("blacklist", [])]
    max_applies  = config.get("apply", {}).get("max_applies", 10)
    tracker      = load_tracker(tracker_path)
    profile      = config.get("personal", {})
    apply_config = {
        **config.get("apply", {}),
        "output_dir": config.get("logging", {}).get("output_dir", "output/logs"),
    }

    eligible = [a for a in tracker["applications"] if a.get("apply_status") == "eligible"]
    eligible.sort(key=lambda x: -x.get("match_score", 0))

    console.print(f"\n[cyan]Auto-apply: {len(eligible)} eligible entries (hard stop after {max_applies} successes)[/cyan]\n")

    success_count = 0
    boards_seen: dict[str, int] = {}

    for app in eligible:
        company     = app.get("company", "")
        role        = app.get("role", "")
        score       = app.get("match_score", 0)
        url         = app.get("url", "")
        resume_path = app.get("tailored_resume", "")

        if any(b in company.lower() for b in blacklist):
            console.print(f"[yellow][skipped] {role} @ {company} -> blacklisted[/yellow]")
            app["apply_status"] = "skipped"
            app["apply_reason"] = "blacklisted company"
            save_tracker(tracker, tracker_path)
            continue

        if not resume_path or not os.path.exists(resume_path):
            console.print(f"[red][skipped] {role} @ {company} -> tailored resume not found[/red]")
            continue

        console.print(f"[bold]Processing ({score}):[/bold] {role} @ {company}")
        time.sleep(2)

        try:
            result = asyncio.run(apply_to_job(url, profile, resume_path, apply_config))
        except Exception as e:
            console.print(f"  [red][failed] {role} @ {company} -> {e}[/red]")
            app["apply_status"] = "failed"
            app["apply_reason"] = str(e)
            save_tracker(tracker, tracker_path)
            continue

        board        = result.get("board", "unknown")
        resolved_url = result.get("resolved_url", url)
        boards_seen[board] = boards_seen.get(board, 0) + 1

        if result["status"] == "manual":
            console.print(f"  [yellow][manual] {role} @ {company} -> destination: {board}[/yellow]")
            console.print(f"  URL: {resolved_url}")
            app["apply_status"] = "manual"
            app["apply_reason"] = f"destination board: {board}, manual apply required"
            app["resolved_url"] = resolved_url
            app["board"]        = board

        elif result.get("success"):
            console.print(f"  [green][applied] {role} @ {company} (score: {score})[/green]")
            app["apply_status"] = "applied"
            app["status"]       = "applied"
            app["applied_at"]   = datetime.now().isoformat(timespec="seconds")
            app["board"]        = board
            success_count += 1

        else:
            console.print(f"  [red][failed] {role} @ {company} -> {result.get('notes', '')}[/red]")
            app["apply_status"] = "failed"
            app["apply_reason"] = result.get("notes", "")
            app["board"]        = board

        save_tracker(tracker, tracker_path)

        if success_count >= max_applies:
            console.print(f"\n[bold yellow]Hard stop: reached {max_applies} successful applies.[/bold yellow]")
            break

    # Board breakdown summary
    if boards_seen:
        console.print(f"\n[bold]Board resolution breakdown:[/bold]")
        for board, count in sorted(boards_seen.items(), key=lambda x: -x[1]):
            icon  = "[green][ok][/green]" if board in AUTO_APPLY_BOARDS else "[yellow][x][/yellow]"
            console.print(f"  {icon} {board}: {count}")

    console.print(f"\n[bold green]Auto-apply complete: {success_count} applied of {len(eligible)} eligible[/bold green]")
    tracker = load_tracker(tracker_path)
    _print_apply_report(tracker)


@cli.command()
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
@click.option("--auto-prepare", "auto_prepare", is_flag=True, default=False,
              help="Automatically tailor resume for every job passing threshold")
@click.option("--auto-apply", "auto_apply_flag", is_flag=True, default=False,
              help="After prepare, auto-apply to all eligible tracker entries")
@click.option("--dry-run", "dry_run", is_flag=True, default=False,
              help="Discover + deduplicate + keyword score only; no Claude calls, no cost")
def discover(config_path, auto_prepare, auto_apply_flag, dry_run):
    """Discover and rank new jobs; optionally auto-tailor and auto-apply."""
    config = load_config(config_path)

    if dry_run:
        _discover_dry_run(config)
        return

    try:
        ranked, resume_data, tracker_path = _discover_and_rank(config)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return

    if not ranked:
        console.print("[yellow]No new jobs to process.[/yellow]")
        if auto_apply_flag:
            _run_auto_apply(config, tracker_path)
        return

    _print_ranked_top10(ranked)

    if auto_prepare:
        console.print(f"\n[cyan]Auto-preparing {len(ranked)} resumes...[/cyan]")
        tailored = prepare_batch(ranked, resume_data, config)
        n_ok = sum(1 for t in tailored if t["tailored_path"] != "[FAILED]")
        console.print(f"[bold green]Tailored {n_ok}/{len(tailored)} resumes[/bold green]\n")
        _print_prepare_summary(tailored)
    else:
        console.print(f"\n[dim]Run 'python main.py report' to see the full ranked table.[/dim]")
        if not auto_apply_flag:
            console.print(f"[dim]Add --auto-prepare to tailor all resumes automatically.[/dim]")

    if auto_apply_flag:
        _run_auto_apply(config, config.get("tracker", {}).get("path", "data/applications.json"))


@cli.command()
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def report(config_path):
    """Show weekly ranked job report"""
    config = load_config(config_path)
    ensure_dirs("output/reports")
    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    generate_report(tracker_path, output_dir="output/reports")


@cli.command()
@click.option("--url", required=True, help="URL of a discovered job to prepare")
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def prepare(url, config_path):
    """Tailor resume for a discovered job"""
    config = load_config(config_path)
    prepare_job(url, config)


@cli.command("run")
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def run_scheduler(config_path):
    """Start scheduler: discover + auto-prepare every run_every_hours (from config)"""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError:
        console.print("[red]APScheduler not installed. Run: pip install apscheduler[/red]")
        return

    config = load_config(config_path)
    hours  = config.get("discovery", {}).get("run_every_hours", 24)
    ensure_dirs("data/tailored", "output/logs", "output/reports")

    log_path = os.path.join("output", "logs", "scheduler.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    sched_log = logging.getLogger("job_scanner.scheduler")
    sched_log.setLevel(logging.INFO)
    sched_log.addHandler(file_handler)
    sched_log.propagate = False

    def _scheduled_run():
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        sched_log.info(f"Run starting")
        console.print(f"\n[bold cyan]--- Scheduled run {stamp} ---[/bold cyan]")
        try:
            ranked, resume_data, tracker_path = _discover_and_rank(config)
            sched_log.info(f"Discovered: {len(ranked)} jobs passed threshold")

            if ranked and resume_data:
                tailored = prepare_batch(ranked, resume_data, config)
                n_ok = sum(1 for t in tailored if t["tailored_path"] != "[FAILED]")
                sched_log.info(f"Tailored: {n_ok}/{len(tailored)} resumes")
            else:
                n_ok = 0
                sched_log.info("No new candidates — skipping tailoring")

            generate_report(tracker_path, output_dir="output/reports")
            sched_log.info("Report written")
            console.print(f"[green]Run complete: {n_ok} resumes tailored. Report saved.[/green]")

        except Exception as e:
            sched_log.error(f"Run failed: {e}")
            console.print(f"[red]Scheduled run failed: {e}[/red]")

    console.print(f"[cyan]Scheduler starting — runs every {hours}h[/cyan]")
    console.print(f"[dim]Log: {log_path}[/dim]")
    console.print(f"[dim]Press Ctrl+C to stop.[/dim]\n")

    _scheduled_run()  # run immediately on start

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(_scheduled_run, "interval", hours=hours)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[yellow]Scheduler stopped.[/yellow]")


def _print_apply_report(tracker: dict) -> None:
    from rich.text import Text
    apps = tracker.get("applications", [])
    relevant = [a for a in apps if a.get("apply_status") in ("applied", "manual", "failed")]
    if not relevant:
        console.print("[yellow]No auto-apply results yet.[/yellow]")
        return

    table = Table(title=f"Auto-Apply Results ({len(relevant)} entries)", show_lines=True, expand=True)
    table.add_column("Status",  style="bold", no_wrap=True)
    table.add_column("Score",   justify="right", no_wrap=True, min_width=4)
    table.add_column("Company", ratio=1)
    table.add_column("Role",    ratio=2)
    table.add_column("Board",   no_wrap=True)
    table.add_column("Notes",   ratio=2)

    status_colors = {"applied": "green", "manual": "yellow", "failed": "red"}
    priority = {"applied": 0, "manual": 1, "failed": 2}

    for app in sorted(relevant, key=lambda a: (priority.get(a.get("apply_status", ""), 3), -a.get("match_score", 0))):
        status = app.get("apply_status", "")
        color  = status_colors.get(status, "default")
        table.add_row(
            Text(status, style=color),
            str(app.get("match_score", "")),
            app.get("company", ""),
            app.get("role", ""),
            app.get("board_type", app.get("board", "-")),
            (app.get("apply_reason") or app.get("notes", ""))[:80],
        )

    console.print(table)


def _keyword_score(candidate: dict, skills: set) -> int:
    """Fast keyword-overlap score against resume skills — no Claude call."""
    text = (candidate.get("title", "") + " " + candidate.get("snippet", "")).lower()
    hits = sum(1 for s in skills if len(s) > 2 and s in text)
    return min(100, hits * 8)


def _discover_dry_run(config: dict) -> None:
    """Discovery + dedup + keyword scoring only. Zero Claude calls."""
    ensure_dirs("output/logs")
    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    tracker      = load_tracker(tracker_path)
    searches     = config.get("discovery", {}).get("searches", [])
    max_days_old = config.get("discovery", {}).get("max_days_old", 30)
    resume_path  = config.get("resume", {}).get("base_path", "data/resume_base.docx")

    if not searches:
        console.print("[red]No searches configured in config.yaml under discovery.searches[/red]")
        return

    console.print("[bold yellow][DRY RUN] Keyword-only scoring — zero Claude calls[/bold yellow]")
    console.print(f"[cyan]Pulling job feeds for {len(searches)} searches (last {max_days_old} days)...[/cyan]")
    candidates = discover_jobs(searches, tracker, max_days_old=max_days_old)
    console.print(f"[green]Found {len(candidates)} new candidates after deduplication[/green]")

    if not candidates:
        console.print("[yellow]Nothing new to score.[/yellow]")
        return

    console.print(f"[cyan]Parsing resume for keyword matching...[/cyan]")
    resume_data = parse_resume(resume_path)
    skills = {s.lower() for s in (resume_data.get("skills") or [])}

    would_pass = 0
    by_query: dict[str, tuple[int, int]] = {}  # query -> (pass, total)
    for c in candidates:
        query     = c.get("search_query", "unknown")
        threshold = c.get("min_match_score", 60)
        score     = _keyword_score(c, skills)
        passed    = score >= threshold
        if passed:
            would_pass += 1
        p, t = by_query.get(query, (0, 0))
        by_query[query] = (p + (1 if passed else 0), t + 1)

    console.print(f"\n[bold green]{would_pass}/{len(candidates)} candidates would pass threshold[/bold green]")
    console.print(f"\n[bold]By search query:[/bold]")
    for query, (p, t) in by_query.items():
        console.print(f"  {p:>3}/{t:<3}  {query}")
    console.print(f"\n[dim]Note: keyword scoring is approximate — Claude scores differently.[/dim]")
    console.print(f"[dim]Run without --dry-run to score with Claude and tailor resumes.[/dim]")


@cli.command("auto-apply")
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def auto_apply(config_path):
    """Auto-apply to all eligible tracker entries."""
    config       = load_config(config_path)
    blacklist    = [b.lower() for b in config.get("apply", {}).get("blacklist", [])]
    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")

    if not any("ust" in b for b in blacklist):
        console.print("[bold red]UST/UST Global not found in config.yaml apply.blacklist -- add it before running.[/bold red]")
        return

    console.print(f"[green]Blacklist active:[/green] {', '.join(config.get('apply', {}).get('blacklist', []))}")
    _run_auto_apply(config, tracker_path)


@cli.command("cost-report")
def cost_report():
    """Show API token usage and estimated cost breakdown."""
    log_path = Path("output/logs/token_usage.log")
    if not log_path.exists():
        console.print("[yellow]No token usage data yet. Run 'discover' or 'apply' first.[/yellow]")
        return

    entries = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 6:
                continue
            try:
                ts, source, model, in_tok, out_tok, cost = parts
                entries.append({
                    "ts": ts, "source": source, "model": model,
                    "input_tokens":  int(in_tok),
                    "output_tokens": int(out_tok),
                    "cost":          float(cost),
                })
            except (ValueError, TypeError):
                continue

    if not entries:
        console.print("[yellow]No valid usage entries found in token_usage.log.[/yellow]")
        return

    total_cost = sum(e["cost"] for e in entries)

    by_model: dict[str, float] = {}
    for e in entries:
        by_model[e["model"]] = by_model.get(e["model"], 0.0) + e["cost"]

    analysis_cost  = sum(e["cost"] for e in entries if e["source"] in ("jd_analyzer", "ranker"))
    tailoring_cost = sum(e["cost"] for e in entries if e["source"] == "resume_tailor")
    other_cost     = total_cost - analysis_cost - tailoring_cost

    analysis_count  = sum(1 for e in entries if e["source"] == "jd_analyzer")
    tailoring_count = sum(1 for e in entries if e["source"] == "resume_tailor")
    avg_analysis   = analysis_cost  / analysis_count  if analysis_count  else 0.0
    avg_tailoring  = tailoring_cost / tailoring_count if tailoring_count else 0.0

    # Projected monthly from data span
    dates = []
    for e in entries:
        try:
            dates.append(datetime.fromisoformat(e["ts"]))
        except ValueError:
            pass
    if len(dates) >= 2:
        span_days = max((max(dates) - min(dates)).total_seconds() / 86400, 0.5)
        projected_monthly = total_cost / span_days * 30
        span_label = f"Data spans {span_days:.1f} day(s)"
    else:
        projected_monthly = total_cost * 30
        span_label = "Single session — projected at 1 run/day"

    console.print(f"\n[bold cyan]API Cost Report[/bold cyan]")
    console.print(f"  Total spend to date:   [bold green]${total_cost:.4f}[/bold green]  "
                  f"({sum(e['input_tokens'] + e['output_tokens'] for e in entries):,} tokens)\n")

    console.print("[bold]By model:[/bold]")
    for m, cost in sorted(by_model.items(), key=lambda x: -x[1]):
        console.print(f"  {m:<44} ${cost:.4f}")

    console.print(f"\n[bold]By operation:[/bold]")
    console.print(f"  Analysis  (jd_analyzer + ranker)  ${analysis_cost:.4f}  ({analysis_count} jobs)")
    console.print(f"  Tailoring (resume_tailor)          ${tailoring_cost:.4f}  ({tailoring_count} resumes)")
    if other_cost > 0.000_01:
        console.print(f"  Other                              ${other_cost:.4f}")

    console.print(f"\n[bold]Averages:[/bold]")
    console.print(f"  Cost per job analyzed:    ${avg_analysis:.4f}")
    console.print(f"  Cost per resume tailored: ${avg_tailoring:.4f}")

    console.print(f"\n[bold]Projected monthly:[/bold]")
    console.print(f"  {span_label}")
    console.print(f"  [bold yellow]${projected_monthly:.2f}/month[/bold yellow]")


@cli.command("apply-report")
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def apply_report(config_path):
    """Show results table from the last auto-apply run."""
    config       = load_config(config_path)
    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    tracker      = load_tracker(tracker_path)
    _print_apply_report(tracker)


if __name__ == "__main__":
    cli()

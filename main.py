import asyncio
import logging
import os
import sys
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

    resume_path  = config.get("resume", {}).get("base_path", "data/resume_base.docx")
    model        = config.get("anthropic", {}).get("model", "claude-sonnet-4-6")
    max_days_old = config.get("discovery", {}).get("max_days_old", 30)

    console.print(f"[cyan]Pulling job feeds for {len(searches)} searches (last {max_days_old} days)...[/cyan]")
    candidates = discover_jobs(searches, tracker, max_days_old=max_days_old)
    console.print(f"[green]Found {len(candidates)} new candidates after deduplication[/green]")

    if not candidates:
        return [], None, tracker_path

    console.print(f"[cyan]Parsing resume...[/cyan]")
    resume_data = parse_resume(resume_path)

    console.print(f"[cyan]Ranking {len(candidates)} candidates (fetching JDs + scoring)...[/cyan]")
    ranked = rank_jobs(candidates, resume_data, model, tracker, tracker_path)

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

    model = config.get("anthropic", {}).get("model", "claude-sonnet-4-6")

    console.print(f"[cyan]Analyzing job description...[/cyan]")
    jd_analysis = analyze_jd(jd_text, resume_data, model)

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
    tailored_content = tailor_resume(resume_data, jd_analysis, model)
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


@cli.command()
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
@click.option("--auto-prepare", "auto_prepare", is_flag=True, default=False,
              help="Automatically tailor resume for every job passing threshold")
def discover(config_path, auto_prepare):
    """Discover and rank new jobs; optionally auto-tailor all passing resumes"""
    config = load_config(config_path)

    try:
        ranked, resume_data, tracker_path = _discover_and_rank(config)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return

    if not ranked:
        console.print("[yellow]No new jobs to process.[/yellow]")
        return

    _print_ranked_top10(ranked)

    if auto_prepare:
        console.print(f"\n[cyan]Auto-preparing {len(ranked)} resumes...[/cyan]")
        for i, job in enumerate(ranked, 1):
            console.print(
                f"  Tailoring [{i}/{len(ranked)}]: {job['title'][:50]} @ {job['company']}",
                end="  ", flush=True
            )
            # will print inline; prepare_batch handles the actual work
        console.print()  # newline after progress hints

        tailored = prepare_batch(ranked, resume_data, config)
        n_ok = sum(1 for t in tailored if t["tailored_path"] != "[FAILED]")
        console.print(f"[bold green]Tailored {n_ok}/{len(tailored)} resumes[/bold green]\n")
        _print_prepare_summary(tailored)
    else:
        console.print(f"\n[dim]Run 'python main.py report' to see the full ranked table.[/dim]")
        console.print(f"[dim]Add --auto-prepare to tailor all resumes automatically.[/dim]")


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


if __name__ == "__main__":
    cli()

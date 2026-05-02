import asyncio
import os
import sys
import yaml
import click

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
from prepare import prepare_job

console = Console()


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def run_pipeline(url: str, config: dict, do_apply: bool = True) -> dict:
    logger = setup_logging(
        config.get("logging", {}).get("level", "INFO"),
        config.get("logging", {}).get("output_dir", "output/logs")
    )
    
    ensure_dirs("data/tailored", "output/logs")
    
    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    tracker = load_tracker(tracker_path)
    
    if is_duplicate(url, tracker):
        console.print(f"[yellow]⚠️  Already applied to {url} -- skipping[/yellow]")
        return {"skipped": True}
    
    console.print(f"\n[cyan]📋 Fetching job description...[/cyan]")
    jd_text = fetch_jd(url)
    
    resume_path = config.get("resume", {}).get("base_path", "data/resume_base.pdf")
    console.print(f"[cyan]📄 Parsing resume...[/cyan]")
    resume_data = parse_resume(resume_path)
    
    model = config.get("anthropic", {}).get("model", "claude-sonnet-4-20250514")
    
    console.print(f"[cyan]🔍 Analyzing job description...[/cyan]")
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
    
    console.print(f"[cyan]✏️  Tailoring resume...[/cyan]")
    tailored_content = tailor_resume(resume_data, jd_analysis, model)
    tailored_path = save_tailored_resume(
        tailored_content,
        jd_analysis.get("company", "company"),
        jd_analysis.get("role", "role"),
        config.get("resume", {}).get("tailored_output_dir", "data/tailored")
    )
    console.print(f"[green]✅ Tailored resume saved: {tailored_path}[/green]")
    
    result = {"success": False, "status": "analyze_only"}
    
    if do_apply:
        console.print(f"[cyan]🌐 Launching browser automation...[/cyan]")
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
        
        status_icon = "✅" if result["success"] else "⚠️"
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
def discover(config_path):
    """Discover and rank new jobs from Indeed RSS (+ RemoteOK/Remotive fallback)"""
    config = load_config(config_path)
    ensure_dirs("data/tailored", "output/logs", "output/reports")

    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    tracker = load_tracker(tracker_path)

    searches = config.get("discovery", {}).get("searches", [])
    if not searches:
        console.print("[red]No searches configured in config.yaml under discovery.searches[/red]")
        return

    resume_path = config.get("resume", {}).get("base_path", "data/resume_base.docx")
    model = config.get("anthropic", {}).get("model", "claude-sonnet-4-6")

    max_days_old = config.get("discovery", {}).get("max_days_old", 30)
    console.print(f"[cyan]Pulling job feeds for {len(searches)} searches (last {max_days_old} days)...[/cyan]")
    candidates = discover_jobs(searches, tracker, max_days_old=max_days_old)
    console.print(f"[green]Found {len(candidates)} new candidates after deduplication[/green]")

    if not candidates:
        console.print("[yellow]No new jobs to process.[/yellow]")
        return

    console.print(f"[cyan]Parsing resume...[/cyan]")
    resume_data = parse_resume(resume_path)

    console.print(f"[cyan]Ranking {len(candidates)} candidates (fetching JDs + scoring)...[/cyan]")
    ranked = rank_jobs(candidates, resume_data, model, tracker, tracker_path)

    console.print(f"\n[bold green]Discovery complete: {len(ranked)} jobs passed threshold[/bold green]")
    for i, job in enumerate(ranked[:10], 1):
        score = job["match_score"]
        color = "green" if score >= 80 else "yellow" if score >= 65 else "red"
        console.print(
            f"  {i:>2}. [{color}]{score:>3}%[/{color}]  "
            f"[bold]{job['title']}[/bold]  @ {job['company']}"
        )

    console.print(f"\n[dim]Run 'python main.py report' to see the full ranked table.[/dim]")


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


if __name__ == "__main__":
    cli()

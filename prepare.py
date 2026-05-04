import os
import sys
import yaml
import click
from rich.console import Console
from rich.markdown import Markdown

sys.path.insert(0, os.path.dirname(__file__))

from resume_parser import parse_resume
from jd_analyzer import fetch_jd, analyze_jd
from resume_tailor import tailor_resume, save_tailored_resume
from tracker import load_tracker, save_tracker


console = Console()


def prepare_batch(ranked: list[dict], resume_data: dict, config: dict) -> list[dict]:
    """
    Tailor resumes for all ranked entries using their already-computed jd_analysis.
    Skips JD re-fetch. Updates tracker to status: ready. Returns summary list.
    """
    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    model        = config.get("anthropic", {}).get("model", "claude-sonnet-4-6")
    output_dir   = config.get("resume", {}).get("tailored_output_dir", "data/tailored")

    tracker = load_tracker(tracker_path)
    results = []

    for entry in ranked:
        jd_analysis = entry.get("jd_analysis", {})
        company     = entry.get("company", "company")
        role        = entry.get("title", "role")
        url         = entry.get("url", "")
        score       = entry.get("match_score", 0)
        salary      = entry.get("salary_signal", "")

        try:
            tailored_md   = tailor_resume(resume_data, jd_analysis, model)
            tailored_path = save_tailored_resume(tailored_md, company, role, output_dir)
        except Exception as e:
            print(f"    [error] Tailoring failed for {role} @ {company}: {e}", flush=True)
            results.append({
                "title": role, "company": company, "score": score,
                "salary": salary, "tailored_path": "[FAILED]",
            })
            continue

        for app in tracker["applications"]:
            if app["url"] == url:
                app["status"] = "ready"
                app["tailored_resume"] = tailored_path
                break
        save_tracker(tracker, tracker_path)

        results.append({
            "title": role, "company": company, "score": score,
            "salary": salary, "tailored_path": tailored_path,
        })

    return results


def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def prepare_job(url: str, config: dict) -> None:
    tracker_path = config.get("tracker", {}).get("path", "data/applications.json")
    tracker = load_tracker(tracker_path)

    # Find the entry in tracker
    entry = next((a for a in tracker.get("applications", []) if a.get("url") == url), None)
    if entry is None:
        console.print(f"[red]URL not found in tracker. Run 'discover' first.[/red]")
        sys.exit(1)
    if entry.get("status") not in ("discovered",):
        console.print(
            f"[yellow]Entry status is '{entry.get('status')}' (expected 'discovered'). "
            f"Proceeding anyway.[/yellow]"
        )

    resume_path = config.get("resume", {}).get("base_path", "data/resume_base.docx")
    model = config.get("anthropic", {}).get("model", "claude-sonnet-4-6")
    output_dir = config.get("resume", {}).get("tailored_output_dir", "data/tailored")

    console.print(f"[cyan]Parsing resume...[/cyan]")
    resume_data = parse_resume(resume_path)

    console.print(f"[cyan]Fetching job description...[/cyan]")
    jd_text = fetch_jd(url)

    console.print(f"[cyan]Analyzing job description...[/cyan]")
    jd_analysis = analyze_jd(jd_text, resume_data, model)

    console.print(f"[cyan]Tailoring resume...[/cyan]")
    tailored_md = tailor_resume(resume_data, jd_analysis, model)

    company = jd_analysis.get("company", entry.get("company", "company"))
    role = jd_analysis.get("role", entry.get("role", "role"))
    tailored_path = save_tailored_resume(tailored_md, company, role, output_dir)

    # Update tracker
    for app in tracker["applications"]:
        if app["url"] == url:
            app["status"] = "ready"
            app["tailored_resume"] = tailored_path
            app["match_score"] = jd_analysis.get("match_score", app.get("match_score", 0))
            break
    save_tracker(tracker, tracker_path)

    console.print(f"\n[green]Tailored resume saved:[/green] {tailored_path}")
    console.print(f"[green]Tracker updated:[/green] status → ready\n")
    console.print("[bold]--- TAILORED RESUME MARKDOWN ---[/bold]")
    console.print(Markdown(tailored_md))


@click.command()
@click.option("--url", required=True, help="URL of a discovered job to prepare")
@click.option("--config", "config_path", default="config.yaml", help="Config file path")
def main(url, config_path):
    """Tailor resume for a discovered job and mark it ready."""
    config = load_config(config_path)
    prepare_job(url, config)


if __name__ == "__main__":
    main()

import os
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from tracker import load_tracker


console = Console()


def _this_week_start() -> datetime:
    today = datetime.now()
    return today - timedelta(days=today.weekday())


def _row_color(score: int) -> str:
    if score >= 80:
        return "green"
    if score >= 65:
        return "yellow"
    return "red"


def generate_report(tracker_path: str, output_dir: str = "output/reports") -> None:
    tracker = load_tracker(tracker_path)
    week_start = _this_week_start().date()

    discovered = []
    for app in tracker.get("applications", []):
        if app.get("status") != "discovered":
            continue
        applied_at = app.get("applied_at", "")[:10]
        try:
            entry_date = datetime.fromisoformat(applied_at).date()
        except (ValueError, TypeError):
            continue
        if entry_date >= week_start:
            discovered.append(app)

    if not discovered:
        console.print("[yellow]No discovered jobs this week.[/yellow]")
        return

    discovered.sort(key=lambda x: x.get("match_score", 0), reverse=True)

    # ── Rich terminal table ──────────────────────────────────────────────────
    table = Table(
        title=f"Weekly Job Report  ({week_start} - {datetime.now().date()})",
        show_lines=True,
        expand=True,
        min_width=80,
    )
    table.add_column("#",          justify="right", style="bold", no_wrap=True, min_width=2)
    table.add_column("Sc",         justify="right", no_wrap=True, min_width=3)
    table.add_column("Title",      ratio=2, no_wrap=False)
    table.add_column("Company",    ratio=1, no_wrap=False)
    table.add_column("Salary",     ratio=1, no_wrap=True)
    table.add_column("Top Gap",    ratio=1, no_wrap=False)
    table.add_column("Fit Summary", ratio=3, no_wrap=False)
    table.add_column("Signal",     ratio=1, no_wrap=False)

    md_rows = []

    for rank, app in enumerate(discovered, 1):
        score   = app.get("match_score", 0)
        color   = _row_color(score)
        gaps    = app.get("top_gaps") or app.get("gaps", [])
        top_gap = gaps[0] if gaps else "-"
        fit     = app.get("fit_summary") or app.get("notes", "-")
        signal  = app.get("company_signal", "no data")
        salary  = app.get("salary_signal", "")
        url     = app.get("url", "")
        role    = app.get("role", app.get("title", "-"))
        company = app.get("company", "-")

        sal_color = "red" if salary and "LOW PAY" in fit else "default"

        table.add_row(
            str(rank),
            Text(str(score), style=color),
            role,
            company,
            Text(salary or "-", style=sal_color),
            top_gap,
            fit,
            signal[:40],
        )

        md_rows.append(
            f"| {rank} | {score} | {role} | {company} | {salary or '-'} | {top_gap} | {fit} | {signal} | {url} |"
        )

    console.print(table)

    # ── Recommended top 5 ───────────────────────────────────────────────────
    console.print()
    console.print(Panel("[bold green]Recommended This Week[/bold green]", expand=False))
    for app in discovered[:5]:
        score = app.get("match_score", 0)
        color = _row_color(score)
        role = app.get("role", app.get("title", "-"))
        company = app.get("company", "-")
        console.print(
            f"  [{color}]{score:>3}%[/{color}]  "
            f"[bold]{role}[/bold]  "
            f"@ {company}  --  {app.get('url', '')}"
        )

    # ── Save markdown report ─────────────────────────────────────────────────
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    md_path = os.path.join(output_dir, f"{stamp}_weekly.md")

    header = "| Rank | Score | Title | Company | Salary | Top Gap | Fit Summary | Company Signal | URL |\n"
    sep    = "|------|-------|-------|---------|--------|---------|-------------|----------------|-----|\n"
    rec_section = "\n## Recommended This Week\n\n"
    for i, app in enumerate(discovered[:5], 1):
        rec_section += (
            f"{i}. **{app.get('role', app.get('title','—'))}** @ {app.get('company','—')}  "
            f"(score {app.get('match_score',0)})  \n"
            f"   {app.get('url','')}\n\n"
        )

    md_content = (
        f"# Weekly Job Report — {week_start} to {datetime.now().date()}\n\n"
        + header + sep
        + "\n".join(md_rows)
        + "\n"
        + rec_section
    )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    console.print(f"\n[dim]Report saved → {md_path}[/dim]")

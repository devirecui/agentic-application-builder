import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.table import Table


console = Console()


def load_tracker(path: str) -> dict:
    if not os.path.exists(path):
        return {"applications": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "applications" not in data:
            data["applications"] = []
        return data
    except (json.JSONDecodeError, OSError):
        return {"applications": []}


def save_tracker(data: dict, path: str) -> None:
    Path(os.path.dirname(path) or ".").mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def is_duplicate(url: str, tracker: dict) -> bool:
    return any(app.get("url") == url for app in tracker.get("applications", []))


def add_application(tracker: dict, *, company: str, role: str, url: str,
                     match_score: int, keywords_added: list, tailored_resume: str,
                     status: str, notes: str = "") -> dict:
    entry = {
        "id": str(uuid.uuid4()),
        "company": company,
        "role": role,
        "url": url,
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "match_score": match_score,
        "keywords_added": keywords_added,
        "tailored_resume": tailored_resume,
        "status": status,
        "notes": notes,
    }
    tracker.setdefault("applications", []).append(entry)
    return tracker


def print_status(tracker: dict) -> None:
    apps = tracker.get("applications", [])
    if not apps:
        console.print("[yellow]No applications tracked yet.[/yellow]")
        return

    table = Table(title=f"Job Applications ({len(apps)})", show_lines=False)
    table.add_column("Date", style="cyan")
    table.add_column("Company", style="bold")
    table.add_column("Role")
    table.add_column("Score", justify="right", style="green")
    table.add_column("Status")

    for app in sorted(apps, key=lambda a: a.get("applied_at", ""), reverse=True):
        table.add_row(
            (app.get("applied_at") or "")[:10],
            app.get("company", ""),
            app.get("role", ""),
            str(app.get("match_score", "")),
            app.get("status", ""),
        )
    console.print(table)

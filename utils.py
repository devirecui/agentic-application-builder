import logging
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# Pricing per million tokens (USD) — source: docs.anthropic.com/en/docs/about-claude/pricing
_TOKEN_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5":  (1.00,  5.00),   # Haiku 4.5 — verified May 2026
    "claude-haiku-3-5":  (0.80,  4.00),   # Haiku 3.5
    "claude-haiku":      (0.25,  1.25),   # Haiku 3
    "claude-sonnet-4":   (3.00, 15.00),   # Sonnet 4.x
    "claude-sonnet-3-7": (3.00, 15.00),   # Sonnet 3.7
    "claude-sonnet":     (3.00, 15.00),   # Sonnet 3.x fallback
    "claude-opus-4":    (15.00, 75.00),   # Opus 4.x
    "claude-opus":      (15.00, 75.00),   # Opus 3.x fallback
}


def log_token_usage(source_file: str, model: str, input_tokens: int, output_tokens: int) -> None:
    cost_in, cost_out = 3.00, 15.00  # default sonnet pricing
    for prefix, pricing in _TOKEN_PRICING.items():
        if prefix in model:
            cost_in, cost_out = pricing
            break
    est_cost = (input_tokens / 1_000_000) * cost_in + (output_tokens / 1_000_000) * cost_out
    try:
        log_dir = Path("output/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        ts   = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        line = f"{ts} | {source_file} | {model} | {input_tokens} | {output_tokens} | {est_cost:.6f}\n"
        with open(log_dir / "token_usage.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # never crash on logging failure


def setup_logging(level: str = "INFO", output_dir: str = "output/logs") -> logging.Logger:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    log_path = os.path.join(output_dir, "agent.log")

    logger = logging.getLogger("job_apply_agent")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


def validate_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def ensure_dirs(*paths: str) -> None:
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "untitled"


def detect_board(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "adzuna.com" in host:
        return "adzuna"
    if "linkedin.com" in host:
        return "linkedin"
    if "greenhouse.io" in host or "boards.greenhouse" in host:
        return "greenhouse"
    if "lever.co" in host or "jobs.lever.co" in host:
        return "lever"
    if "workday" in host or "myworkdayjobs" in host:
        return "workday"
    if "icims.com" in host:
        return "icims"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "jobvite.com" in host:
        return "jobvite"
    if "microsoft.com" in host or "careers.microsoft" in host:
        return "microsoft"
    if "clearancejobs.com" in host:
        return "clearancejobs"
    if "dice.com" in host:
        return "dice"
    if "ziprecruiter.com" in host:
        return "ziprecruiter"
    return "generic"

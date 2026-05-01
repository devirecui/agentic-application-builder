import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse


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
    if "microsoft.com" in host or "careers.microsoft" in host:
        return "microsoft"
    return "generic"

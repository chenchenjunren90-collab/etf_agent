"""Prevent duplicate daily prediction runs for the same calendar date."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"


def daily_full_path(date_str: str) -> Path:
    return OUTPUT_DIR / f"{date_str}_full.json"


def daily_submit_path(date_str: str) -> Path:
    return OUTPUT_DIR / f"{date_str}_submit.json"


def has_daily_run(date_str: str) -> bool:
    """True if this date already has a completed daily_job output."""
    return daily_full_path(date_str).is_file()


def load_submit(date_str: str) -> list[dict[str, Any]]:
    path = daily_submit_path(date_str)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []
